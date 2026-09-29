"""The per-file workspace (docs/PLAN-designer.md 3.6 and 9.5): data/cache/design/<file-id>/ holding the DesignPlan,
cropped images, rendered diagrams, measured text, thumbnails, the report, an event log and a manifest.

Layout on disk (fixed by contract):
    plan.json  report.json  manifest.json  events.jsonl
    images/<name>.png  diagrams/<name>.png  art/<name>.png  thumbs/<page>.png (1-based, zero-padded to 3)
    text/measure.json  plan.prev.json (the plan before the last restyle/polish)
Refs are 'ws:<kind>/<name>' where kind is images|diagrams|art|thumbs|text.

Owner: builder F.

Every write is atomic (a temp file, then a rename). The manifest's `touched` time (and the manifest file's mtime, bumped
on reads) is what the LRU cap and the 14-day TTL in prune() go by. Sandbox files keep their workspaces under a
per-sandbox temp dir that drop_sandbox() removes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from .plan import DesignPlan

ROOT = Path(__file__).resolve().parent.parent.parent
DESIGN_DIR = ROOT / 'data' / 'cache' / 'design'
CAP_BYTES = 1024 * 1024 * 1024
TTL_DAYS = 14
KINDS = ('images', 'diagrams', 'art', 'thumbs', 'text')
FILE_ID = r'^[0-9a-f]{12}$'
# the directory each sandbox's workspaces live under: <tmp>/tracegraph-design/<sandbox id>/<file id>/
SANDBOX_DIR = Path(tempfile.gettempdir()) / 'tracegraph-design'
# file ids are uuid4().hex[:12] (FILE_ID); other safe ids are accepted so tests and sandboxes can name their own
SAFE_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
NAME = re.compile(r'^[a-z0-9][a-z0-9._-]{0,79}$')
REF = re.compile(r'^ws:([a-z]+)/(.+)$')
TOP_FILES = ('plan.json', 'plan.prev.json', 'report.json', 'events.jsonl')
COPIED = ('images', 'diagrams', 'art', 'text')   # what copy_to seeds a new file's workspace with


def _safe_id(value: str, what: str = 'file id') -> str:
    s = str(value or '')
    if not SAFE_ID.match(s):
        raise ValueError(f'not a valid {what}: {s[:40]!r}')
    return s


def _atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp')
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text('utf-8'))
    except (OSError, ValueError):
        return None


def _tree_bytes(d: Path) -> int:
    total = 0
    for p in d.rglob('*'):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


class Workspace:
    def __init__(self, file_id: str, root: Path | None = None):
        """A workspace for one file under `root` (default DESIGN_DIR). Nothing is created until the first write."""
        self.file_id = _safe_id(file_id)
        self.root = Path(root) if root is not None else DESIGN_DIR
        self.path = self.root / file_id

    def __repr__(self) -> str:
        return f'Workspace({self.file_id!r}, root={str(self.root)!r})'

    def exists(self) -> bool:
        return self.path.is_dir()

    # ---------- the manifest ----------

    def _manifest_path(self) -> Path:
        return self.path / 'manifest.json'

    def _load_manifest(self) -> dict:
        m = _read_json(self._manifest_path())
        if not isinstance(m, dict) or not isinstance(m.get('files'), dict):
            now = time.time()
            m = {'file_id': self.file_id, 'created': now, 'touched': now, 'bytes': 0, 'files': {}}
        return m

    def _record(self, name: str, data: bytes | None) -> None:
        """Update the manifest for one file (`name` is a ref or a top-level file name; data None removes it)."""
        m = self._load_manifest()
        if data is None:
            m['files'].pop(name, None)
        else:
            m['files'][name] = {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        m['touched'] = time.time()
        m['bytes'] = sum(int(f.get('bytes') or 0) for f in m['files'].values())
        _atomic(self._manifest_path(), json.dumps(m, indent=1, sort_keys=True).encode('utf-8'))

    def _touch(self) -> None:
        """A read counts as use for the LRU cap: bump the manifest's mtime (cheaper than rewriting it)."""
        try:
            os.utime(self._manifest_path())
        except OSError:
            pass

    def _write_top(self, name: str, data: bytes) -> None:
        _atomic(self.path / name, data)
        self._record(name, data)

    # ---------- plan and report ----------

    def save_plan(self, plan: DesignPlan) -> None:
        """Write plan.json (the previous one becomes plan.prev.json)."""
        data = json.dumps(plan.to_dict(), ensure_ascii=False).encode('utf-8')
        cur = self.path / 'plan.json'
        if cur.is_file():
            prev = cur.read_bytes()
            _atomic(self.path / 'plan.prev.json', prev)
            self._record('plan.prev.json', prev)
        self._write_top('plan.json', data)

    def load_plan(self, previous: bool = False) -> DesignPlan | None:
        d = _read_json(self.path / ('plan.prev.json' if previous else 'plan.json'))
        if not isinstance(d, dict):
            return None
        try:
            plan = DesignPlan.from_dict(d)
        except (TypeError, ValueError, KeyError):
            return None
        self._touch()
        return plan

    def save_report(self, report: dict) -> None:
        self._write_top('report.json', json.dumps(report, ensure_ascii=False, default=str).encode('utf-8'))

    def load_report(self) -> dict | None:
        d = _read_json(self.path / 'report.json')
        return d if isinstance(d, dict) else None

    # ---------- assets ----------

    def put(self, kind: str, name: str, data: bytes) -> str:
        """Store bytes as <kind>/<name> atomically; returns the 'ws:<kind>/<name>' ref. name is [a-z0-9._-]{1,80}."""
        ref = f'ws:{kind}/{name}'
        path = self.path_of(ref)
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError('workspace data must be bytes')
        data = bytes(data)
        _atomic(path, data)
        self._record(ref, data)
        return ref

    def get(self, ref: str) -> bytes | None:
        try:
            path = self.path_of(ref)
        except ValueError:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        self._touch()
        return data

    def path_of(self, ref: str) -> Path:
        """The file a 'ws:' ref names; raises ValueError for a ref outside this workspace."""
        m = REF.match(str(ref or ''))
        if not m:
            raise ValueError(f'not a workspace ref: {str(ref)[:60]!r}')
        kind, name = m.groups()
        if kind not in KINDS:
            raise ValueError(f'unknown workspace kind {kind!r}')
        if not NAME.match(name) or '..' in name:
            raise ValueError(f'not a valid workspace file name: {name[:60]!r}')
        return self.path / kind / name

    def save_thumb(self, page: int, png: bytes) -> str:
        """thumbs/<page+1:03d>.png for a 0-based page; returns the ref."""
        page = int(page)
        if page < 0 or page > 998:
            raise ValueError(f'page {page} is out of range')
        return self.put('thumbs', f'{page + 1:03d}.png', png)

    def thumbs(self) -> list[Path]:
        """Thumbnail paths in page order ([] when none)."""
        d = self.path / 'thumbs'
        if not d.is_dir():
            return []
        return sorted(p for p in d.iterdir() if p.is_file() and re.fullmatch(r'\d{3}\.png', p.name))

    def clear_thumbs(self) -> None:
        """Remove every thumbnail (before a re-render with fewer pages)."""
        for p in self.thumbs():
            p.unlink(missing_ok=True)
            self._record(f'ws:thumbs/{p.name}', None)

    # ---------- the event log ----------

    def log(self, event: dict) -> None:
        """Append {t, phase, ...} to events.jsonl (direction, assets, layout decisions, QA, critic edits, fallbacks)."""
        ev = {'t': round(time.time(), 3), 'phase': None}
        ev.update(event if isinstance(event, dict) else {'note': str(event)})
        line = json.dumps(ev, ensure_ascii=False, default=str) + '\n'
        self.path.mkdir(parents=True, exist_ok=True)
        with open(self.path / 'events.jsonl', 'a', encoding='utf-8') as f:
            f.write(line)

    def events(self) -> list[dict]:
        try:
            lines = (self.path / 'events.jsonl').read_text('utf-8').splitlines()
        except OSError:
            return []
        out = []
        for ln in lines:
            try:
                ev = json.loads(ln)
            except ValueError:
                continue
            if isinstance(ev, dict):
                out.append(ev)
        return out

    def manifest(self) -> dict:
        """{file_id, created, touched, bytes, files: {ref: {bytes, sha256}}}."""
        m = self._load_manifest()
        return {'file_id': self.file_id, 'created': m.get('created'), 'touched': m.get('touched'),
                'bytes': m.get('bytes', 0), 'files': dict(m.get('files') or {})}

    def copy_to(self, file_id: str) -> 'Workspace':
        """A new file's workspace seeded from this one (restyle/convert reuse assets and measurements)."""
        new = Workspace(file_id, root=self.root)
        if new.path == self.path:
            return self
        if not self.exists():
            return new
        m = self._load_manifest()
        for kind in COPIED:
            src = self.path / kind
            if not src.is_dir():
                continue
            for p in src.iterdir():
                if not p.is_file() or not NAME.match(p.name):
                    continue
                data = p.read_bytes()
                _atomic(new.path / kind / p.name, data)
                ref = f'ws:{kind}/{p.name}'
                new._record(ref, data)
        new.log({'phase': 'copy', 'from': self.file_id, 'files': len(new._load_manifest()['files']),
                 'source_created': m.get('created')})
        return new

    def delete(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def size_bytes(self) -> int:
        return _tree_bytes(self.path) if self.exists() else 0

    def last_used(self) -> float:
        """When the workspace was last written or read (the manifest's touched time or mtime)."""
        m = _read_json(self._manifest_path())
        t = float(m.get('touched') or 0) if isinstance(m, dict) else 0.0
        for p in (self._manifest_path(), self.path):
            try:
                t = max(t, p.stat().st_mtime)
                break
            except OSError:
                continue
        return t


def open_workspace(file_id: str, *, sandbox: str | None = None) -> Workspace:
    """The workspace of a stored file, or, for a sandbox file, one under a per-sandbox temp dir that drop_sandbox()
    removes."""
    if sandbox is not None:
        return Workspace(file_id, root=Path(SANDBOX_DIR) / _safe_id(sandbox, 'sandbox id'))
    return Workspace(file_id, root=DESIGN_DIR)


def drop(file_id: str) -> None:
    """Delete a file's workspace (called when the user deletes the file)."""
    try:
        Workspace(file_id, root=DESIGN_DIR).delete()
    except ValueError:
        return


def drop_sandbox(sandbox_id: str) -> None:
    """Delete every workspace of a sandbox (called when the sandbox is cleared)."""
    try:
        shutil.rmtree(Path(SANDBOX_DIR) / _safe_id(sandbox_id, 'sandbox id'), ignore_errors=True)
    except ValueError:
        return


def prune(cap_bytes: int = CAP_BYTES, ttl_days: int = TTL_DAYS) -> int:
    """Remove workspaces older than ttl_days, then LRU above cap_bytes; returns bytes freed."""
    freed = 0
    now = time.time()
    ttl = ttl_days * 86400
    root = Path(DESIGN_DIR)
    spaces = []
    if root.is_dir():
        for d in root.iterdir():
            if not d.is_dir() or not SAFE_ID.match(d.name):
                continue
            ws = Workspace(d.name, root=root)
            spaces.append((ws.last_used(), ws, ws.size_bytes()))
    alive = []
    for used, ws, size in spaces:
        if now - used > ttl:
            ws.delete()
            freed += size
        else:
            alive.append((used, ws, size))
    total = sum(s for _, _, s in alive)
    for used, ws, size in sorted(alive, key=lambda x: x[0]):
        if total <= cap_bytes:
            break
        ws.delete()
        total -= size
        freed += size
    # sandbox workspaces left behind by a sandbox that was never cleared expire on the same TTL
    sroot = Path(SANDBOX_DIR)
    if sroot.is_dir():
        for d in sroot.iterdir():
            try:
                if d.is_dir() and now - max((p.stat().st_mtime for p in d.rglob('*')), default=d.stat().st_mtime) > ttl:
                    size = _tree_bytes(d)
                    shutil.rmtree(d, ignore_errors=True)
                    freed += size
            except OSError:
                continue
    return freed

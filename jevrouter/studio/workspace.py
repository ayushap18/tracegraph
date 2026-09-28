"""The per-file workspace (docs/PLAN-designer.md 3.6 and 9.5): data/cache/design/<file-id>/ holding the DesignPlan,
cropped images, rendered diagrams, measured text, thumbnails, the report, an event log and a manifest.

Layout on disk (fixed by contract):
    plan.json  report.json  manifest.json  events.jsonl
    images/<name>.png  diagrams/<name>.png  art/<name>.png  thumbs/<page>.png (1-based, zero-padded to 3)
    text/measure.json  plan.prev.json (the plan before the last restyle/polish)
Refs are 'ws:<kind>/<name>' where kind is images|diagrams|art|thumbs|text.

Owner: builder F.
"""
from __future__ import annotations

from pathlib import Path

from .plan import DesignPlan

ROOT = Path(__file__).resolve().parent.parent.parent
DESIGN_DIR = ROOT / 'data' / 'cache' / 'design'
CAP_BYTES = 1024 * 1024 * 1024
TTL_DAYS = 14
KINDS = ('images', 'diagrams', 'art', 'thumbs', 'text')
FILE_ID = r'^[0-9a-f]{12}$'


class Workspace:
    def __init__(self, file_id: str, root: Path | None = None):
        """A workspace for one file under `root` (default DESIGN_DIR). Nothing is created until the first write."""
        self.file_id = file_id
        self.root = Path(root) if root is not None else DESIGN_DIR
        self.path = self.root / file_id

    def exists(self) -> bool:
        return self.path.is_dir()

    def save_plan(self, plan: DesignPlan) -> None:
        """Write plan.json (the previous one becomes plan.prev.json)."""
        raise NotImplementedError('studio.workspace.Workspace.save_plan: builder F')

    def load_plan(self, previous: bool = False) -> DesignPlan | None:
        raise NotImplementedError('studio.workspace.Workspace.load_plan: builder F')

    def save_report(self, report: dict) -> None:
        raise NotImplementedError('studio.workspace.Workspace.save_report: builder F')

    def load_report(self) -> dict | None:
        raise NotImplementedError('studio.workspace.Workspace.load_report: builder F')

    def put(self, kind: str, name: str, data: bytes) -> str:
        """Store bytes as <kind>/<name> atomically; returns the 'ws:<kind>/<name>' ref. name is [a-z0-9._-]{1,80}."""
        raise NotImplementedError('studio.workspace.Workspace.put: builder F')

    def get(self, ref: str) -> bytes | None:
        raise NotImplementedError('studio.workspace.Workspace.get: builder F')

    def path_of(self, ref: str) -> Path:
        """The file a 'ws:' ref names; raises ValueError for a ref outside this workspace."""
        raise NotImplementedError('studio.workspace.Workspace.path_of: builder F')

    def save_thumb(self, page: int, png: bytes) -> str:
        """thumbs/<page+1:03d>.png for a 0-based page; returns the ref."""
        raise NotImplementedError('studio.workspace.Workspace.save_thumb: builder F')

    def thumbs(self) -> list[Path]:
        """Thumbnail paths in page order ([] when none)."""
        raise NotImplementedError('studio.workspace.Workspace.thumbs: builder F')

    def log(self, event: dict) -> None:
        """Append {t, phase, ...} to events.jsonl (direction, assets, layout decisions, QA, critic edits, fallbacks)."""
        raise NotImplementedError('studio.workspace.Workspace.log: builder F')

    def events(self) -> list[dict]:
        raise NotImplementedError('studio.workspace.Workspace.events: builder F')

    def manifest(self) -> dict:
        """{file_id, created, touched, bytes, files: {ref: {bytes, sha256}}}."""
        raise NotImplementedError('studio.workspace.Workspace.manifest: builder F')

    def copy_to(self, file_id: str) -> 'Workspace':
        """A new file's workspace seeded from this one (restyle/convert reuse assets and measurements)."""
        raise NotImplementedError('studio.workspace.Workspace.copy_to: builder F')

    def delete(self) -> None:
        raise NotImplementedError('studio.workspace.Workspace.delete: builder F')


def open_workspace(file_id: str, *, sandbox: str | None = None) -> Workspace:
    """The workspace of a stored file, or, for a sandbox file, one under a per-sandbox temp dir that drop_sandbox()
    removes."""
    raise NotImplementedError('studio.workspace.open_workspace: builder F')


def drop(file_id: str) -> None:
    """Delete a file's workspace (called when the user deletes the file)."""
    raise NotImplementedError('studio.workspace.drop: builder F')


def drop_sandbox(sandbox_id: str) -> None:
    """Delete every workspace of a sandbox (called when the sandbox is cleared)."""
    raise NotImplementedError('studio.workspace.drop_sandbox: builder F')


def prune(cap_bytes: int = CAP_BYTES, ttl_days: int = TTL_DAYS) -> int:
    """Remove workspaces older than ttl_days, then LRU above cap_bytes; returns bytes freed."""
    raise NotImplementedError('studio.workspace.prune: builder F')

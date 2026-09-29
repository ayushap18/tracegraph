"""Per-sandbox memory: finished turns (for follow-up context and "keep"), temporary files, the files its runs created
(docs/PLAN-files.md), and when it was last used.

Everything here lives in process memory only. Nothing is written to the store or to disk, and a sandbox that sits idle
past its TTL (or falls off the LRU end) is simply forgotten.
"""
import time
from collections import OrderedDict

from .config import CREATE_SANDBOX_BYTES, CREATE_SANDBOX_FILES

MAX_SANDBOXES = 200          # most sandboxes kept in memory; the least recently used is dropped first
MAX_THREAD = 50              # finished turns kept per sandbox
TURNS = 3                    # turns of context a follow-up sees (same as a saved chat)
MAX_FILES = 5                # files per sandbox
MAX_TOTAL_BYTES = 20 * 1024 * 1024  # all of a sandbox's files together (each file is also capped at files.MAX_BYTES)
DEFAULT_TTL = 1800.0         # seconds idle before the sweeper forgets a sandbox (TG_SANDBOX_TTL)


class SandboxError(ValueError):
    """A request the sandbox memory refuses; `status` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def turn_of(rec: dict) -> dict:
    return {'qid': rec['qid'], 'query': rec['text'],
            'answer': (rec.get('merged') or {}).get('answer') or rec.get('error') or '', 'record': rec}


class SandboxMemory:
    def __init__(self, now: float | None = None):
        self.thread: list[dict] = []                     # [{qid, query, answer, record}], oldest first
        self.files: dict[str, tuple[dict, str]] = {}     # id -> (metadata, extracted text)
        self.created: OrderedDict[str, tuple[dict, dict, bytes]] = OrderedDict()  # id -> (CreatedFile, spec, bytes)
        self.last_used = time.time() if now is None else now

    def touch(self, now: float | None = None):
        self.last_used = time.time() if now is None else now

    def qids(self) -> list[int]:
        return [t['qid'] for t in self.thread]

    def context(self, replaces: int | None = None) -> list[dict]:
        """The follow-up context: the last TURNS turns, or the TURNS turns before `replaces` (an edited turn)."""
        turns = self.thread
        if replaces is not None:
            i = next((n for n, t in enumerate(turns) if t['qid'] == replaces), len(turns))
            turns = turns[:i]
        return [{'query': t['query'], 'answer': t['answer']} for t in turns[-TURNS:]]

    def record(self, rec: dict, replaces: int | None = None):
        """A finished run joins the thread; an edited run takes the place of the turn it replaces (if still there)."""
        turn = turn_of(rec)
        i = next((n for n, t in enumerate(self.thread) if t['qid'] == replaces), None) if replaces is not None else None
        if i is None:
            self.thread.append(turn)
        else:
            self.thread[i] = turn
        del self.thread[:-MAX_THREAD]

    def add_file(self, meta: dict, text: str):
        if len(self.files) >= MAX_FILES:
            raise SandboxError(f'at most {MAX_FILES} files per sandbox')
        if self.file_bytes() + meta['size'] > MAX_TOTAL_BYTES:
            raise SandboxError(f'a sandbox holds at most {MAX_TOTAL_BYTES // (1024 * 1024)} MB of files', 413)
        self.files[meta['id']] = (meta, text)

    def file_bytes(self) -> int:
        return sum(m['size'] for m, _ in self.files.values())

    def add_created(self, meta: dict, spec: dict, data: bytes):
        """A file a run made, kept in memory only. Past CREATE_SANDBOX_FILES files or CREATE_SANDBOX_BYTES the oldest
        are forgotten (a run has already answered, so refusing would leave its file card pointing at nothing)."""
        self.created[meta['id']] = (meta, spec, data)
        while len(self.created) > 1 and (len(self.created) > CREATE_SANDBOX_FILES or
                                         sum(len(d) for _, _, d in self.created.values()) > CREATE_SANDBOX_BYTES):
            self.created.popitem(last=False)


def forget_design(sid: str):
    """A sandbox dropped off the LRU end takes its Studio design workspaces with it. Never raises."""
    try:
        from .studio import workspace
        workspace.drop_sandbox(sid)
    except Exception:
        pass


class Sandboxes:
    """Sandbox id -> SandboxMemory, least recently used first."""

    def __init__(self, cap: int = MAX_SANDBOXES):
        self.cap = cap
        self.mem: OrderedDict[str, SandboxMemory] = OrderedDict()

    def __contains__(self, sid) -> bool:
        return sid in self.mem

    def get(self, sid: str, create: bool = False, now: float | None = None) -> SandboxMemory | None:
        """The sandbox's memory (marked as used), created on demand when `create`; None if it doesn't exist."""
        m = self.mem.pop(sid, None)
        if m is None:
            if not create:
                return None
            m = SandboxMemory(now)
        m.touch(now)
        self.mem[sid] = m  # most recently used last
        while len(self.mem) > self.cap:
            old, _ = self.mem.popitem(last=False)
            forget_design(old)
        return m

    def peek(self, sid: str) -> SandboxMemory | None:
        """The memory without marking it used (a finished run still counts via its own touch)."""
        return self.mem.get(sid)

    def drop(self, sid: str) -> bool:
        return self.mem.pop(sid, None) is not None

    def sweep(self, now: float, ttl: float, busy=()) -> list[str]:
        """Forgets sandboxes idle for more than `ttl` seconds, except those in `busy` (queries still running)."""
        gone = [sid for sid, m in self.mem.items() if now - m.last_used > ttl and sid not in busy]
        for sid in gone:
            del self.mem[sid]
        return gone

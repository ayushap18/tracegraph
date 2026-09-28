"""Small in-memory caches (docs/PLAN-speed-evals-chat.md A4): bounded, least recently used entries go first, and every
entry expires after its TTL. Nothing here is written to disk, and LLM answers are never cached.

- Route decisions: the Router keeps a RouteCache keyed by the subtask text as Jev sees it plus a fingerprint of the
  route criteria (agents, their descriptions and any label examples), so a new agent or label can never reuse an old
  decision; the Router also clears it outright when agents, labels or the examples switch change. Jev's multi score
  (does the query hold several requests?) depends on the text alone and is cached by it.
- Live data for the keyless agents (jevrouter/agents/tools.py): currency rates and weather for 10 minutes, knowledge
  lookups for 24 hours, in the module-wide LIVE cache.

A run counts its hits with track(): cache lookups made inside it (in the same asyncio task) add to its counter.
"""
import copy
import hashlib
import json
import time
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar

MISSING = object()

ROUTE_TTL = 3600.0  # route decisions only go stale through labels or agents, which clear the cache anyway
ROUTE_MAX = 2000
RATES_TTL = 600.0
WEATHER_TTL = 600.0
LOOKUP_TTL = 86400.0
LIVE_MAX = 1000


class TTLCache:
    def __init__(self, maxsize: int, ttl: float, clock=time.monotonic):
        self.maxsize, self.ttl, self.clock = maxsize, ttl, clock
        self.data: OrderedDict = OrderedDict()  # key -> (expires, value), least recently used first

    def get(self, key, default=None):
        hit = self.data.get(key)
        if hit is None:
            return default
        if hit[0] <= self.clock():
            del self.data[key]
            return default
        self.data.move_to_end(key)
        return hit[1]

    def put(self, key, value, ttl: float | None = None):
        self.data[key] = (self.clock() + (self.ttl if ttl is None else ttl), value)
        self.data.move_to_end(key)
        while len(self.data) > self.maxsize:
            self.data.popitem(last=False)

    def clear(self):
        self.data.clear()

    def __len__(self):
        return len(self.data)


# ---------- per-run hit counting ----------

HITS: ContextVar[list | None] = ContextVar('cache_hits', default=None)


@contextmanager
def track():
    """Counts cache hits made inside the block (in this task): `with track() as hits: ...; hits[0]`."""
    hits = [0]
    token = HITS.set(hits)
    try:
        yield hits
    finally:
        HITS.reset(token)


def count_hit():
    hits = HITS.get()
    if hits is not None:
        hits[0] += 1


# ---------- live data ----------

LIVE = TTLCache(LIVE_MAX, LOOKUP_TTL)


async def fetch(key, ttl: float, get):
    """The cached value for key, or `await get()` stored for ttl seconds. Failures and empty results aren't stored,
    so a throttled API is asked again next time. Callers get a copy, so they can't change what is cached."""
    value = LIVE.get(key, MISSING)
    if value is not MISSING:
        count_hit()
        return copy.deepcopy(value)
    value = await get()
    if value:
        LIVE.put(key, copy.deepcopy(value), ttl)
    return value


def live_key(url: str, params: dict) -> tuple:
    return url, tuple(sorted((k, str(v)) for k, v in params.items()))


# ---------- route decisions ----------

def fingerprint(criteria: dict) -> str:
    """A short hash of the route criteria: any change to the agents offered, their descriptions or examples changes it."""
    return hashlib.sha1(json.dumps(criteria, sort_keys=True, default=str).encode()).hexdigest()[:16]


def normalize(text: str) -> str:
    return ' '.join(str(text or '').split()).casefold()


class RouteCache:
    def __init__(self, maxsize: int = ROUTE_MAX, ttl: float = ROUTE_TTL, clock=time.monotonic):
        self.cache = TTLCache(maxsize, ttl, clock)

    def key(self, text: str, criteria: dict, extra: str | None = None) -> tuple:
        """extra: anything else Jev saw besides the text (a hash of the previous turn for a keyless follow-up, A4), so
        "make it 500" after one question never reuses its decision after another."""
        return (normalize(text), fingerprint(criteria)) if extra is None else (normalize(text), fingerprint(criteria), extra)

    def get(self, key) -> dict | None:
        d = self.cache.get(key)
        return copy.deepcopy(d) if d is not None else None

    def put(self, key, decision: dict):
        self.cache.put(key, copy.deepcopy(decision))

    def clear(self):
        self.cache.clear()

    def __len__(self):
        return len(self.cache)

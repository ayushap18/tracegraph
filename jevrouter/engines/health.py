"""Per-engine health since server start: calls, successes, Auto fallbacks, latency and the last error. Memory only.

instrument() wraps an engine's stream() so every call is counted, whether it came through Auto or went to the engine
directly. Auto reports its own fallbacks (from the engine that failed, to the one it tried next) and cooldowns.
"""
import math
import time
from collections import deque

WINDOW = 200  # latencies kept per engine for p50 / p95
ERROR_CHARS = 160


class Health:
    def __init__(self, window: int = WINDOW):
        self.window = window
        self.stats: dict[str, dict] = {}

    def of(self, name: str) -> dict:
        if name not in self.stats:
            self.stats[name] = {'calls': 0, 'ok': 0, 'fallbacks_from': 0, 'fallbacks_to': 0, 'last_error': None,
                                'ms': deque(maxlen=self.window)}
        return self.stats[name]

    def record(self, name: str, ok: bool, ms: float, error: str | None = None):
        s = self.of(name)
        s['calls'] += 1
        if ok:
            s['ok'] += 1
            s['ms'].append(ms)  # latency of answers, so instant "not installed" failures don't flatter p50
        else:
            s['last_error'] = short(error)

    def fallback(self, frm: str, to: str):
        self.of(frm)['fallbacks_from'] += 1
        self.of(to)['fallbacks_to'] += 1

    def snapshot(self, engines: dict) -> list[dict]:
        """One EngineHealth row per engine, in the engines' order. cooling_until comes from any Auto engine's
        cooldowns, as epoch seconds."""
        now_mono, now = time.monotonic(), time.time()
        cooling = {}
        for e in engines.values():
            for name, (until, _) in (getattr(e, 'cooling', None) or {}).items():
                if until > now_mono:
                    cooling[name] = max(cooling.get(name, 0), round(now + until - now_mono, 1))
        out = []
        for name, e in engines.items():
            s = self.stats.get(name) or self.of(name)
            ms = sorted(s['ms'])
            out.append({'name': name, 'label': getattr(e, 'label', name), 'calls': s['calls'], 'ok': s['ok'],
                        'fallbacks_from': s['fallbacks_from'], 'fallbacks_to': s['fallbacks_to'],
                        'p50_ms': pct(ms, 0.5), 'p95_ms': pct(ms, 0.95), 'last_error': s['last_error'],
                        'cooling_until': cooling.get(name)})
        return out


def pct(values: list[float], p: float) -> int | None:
    """Nearest-rank percentile of sorted values."""
    return round(values[max(0, math.ceil(p * len(values)) - 1)]) if values else None


def short(error) -> str:
    t = ' '.join(str(error or 'failed').split())
    return t if len(t) <= ERROR_CHARS else t[:ERROR_CHARS - 3] + '...'


def instrument(engine, health: Health):
    """Counts every stream() call of this engine in `health`. Safe to call again: the wrapper is installed once and
    reports to the latest Health it was given. A cancelled call isn't counted either way."""
    engine._health = health
    if getattr(engine, '_health_wrapped', False):
        return engine
    inner = engine.stream

    async def stream(**kw):
        t0 = time.perf_counter()
        try:
            reply = await inner(**kw)
        except Exception as e:
            from .base import EngineError, EngineRefusal
            ok = isinstance(e, EngineRefusal)  # a refusal is an answer: the engine itself worked
            why = e.why if isinstance(e, EngineError) else f'{type(e).__name__}: {e}'
            engine._health.record(engine.name, ok, (time.perf_counter() - t0) * 1000, None if ok else why)
            raise
        engine._health.record(engine.name, True, (time.perf_counter() - t0) * 1000)
        return reply

    engine.stream = stream
    engine._health_wrapped = True
    return engine


def instrument_all(engines: dict, health: Health):
    """Every engine, and every engine inside an Auto, so calls Auto makes are counted per backend too."""
    for e in engines.values():
        instrument(e, health)
        for inner in (getattr(e, 'engines', None) or {}).values():
            instrument(inner, health)

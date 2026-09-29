"""Auto engine: the user's own order of backends, falling through to the next one when a call fails.

The default order puts the subscription CLIs first (Claude Code, Codex, Antigravity, OpenCode) and the pay-per-token
API keys last, so a plan that is out of quota, logged out or timing out never fails a run while another backend works.
TG_ENGINE_ORDER (comma separated) or the UI sets the order. An engine that just failed is skipped for a while, so a
used-up plan costs one failed call, not one per request. Each move to the next engine is counted in engines/health.py.

Health-aware order (docs/PLAN-speed-evals-chat.md A5): once an engine has a few calls behind it, its live success rate
and then its p50 (from engines/health.py) decide its place; the user's order breaks ties, and an engine that is cooling
down or blocked is never tried first. Hedged requests (TG_HEDGE=1, off by default because they spend plan quota): when
the first engine has shown no text by its p90, the next one starts too, the first to answer is kept and the other is
cancelled, at most once per call.
"""
import asyncio
import math
import os
import time

from ..config import HEALTH_MIN_CALLS, HEDGE_MIN, HEDGE_UNKNOWN, SPEED_BAND, env_flag
from .base import COOLDOWN, LASTING, Engine, EngineError, EngineRefusal

SHORT_COOLDOWN = 30.0  # timeouts, crashes and one-off API errors may


def default_order(names) -> list[str]:
    raw = os.environ.get('TG_ENGINE_ORDER', '')
    wanted = [n.strip().lower() for n in raw.split(',') if n.strip()]
    order = [n for n in dict.fromkeys(wanted) if n in names]
    return order + [n for n in names if n not in order]


def percentile(values, p: float) -> float | None:
    values = sorted(values)
    return values[max(0, math.ceil(p * len(values)) - 1)] if values else None


class AutoEngine(Engine):
    name = 'auto'
    label = 'Auto'

    def __init__(self, engines: dict[str, Engine], order=None):
        self.engines = engines  # insertion order is the default preference
        self.order = default_order(list(engines)) if order is None else [n for n in order if n in engines]
        self.cooling: dict[str, tuple[float, str]] = {}  # name -> (until, why)
        self.last: str | None = None
        self.warm_calls: list[dict] = []  # what prewarm was asked for, replayed on whichever engine takes over the lead
        self.tasks: set[asyncio.Task] = set()

    def set_order(self, names):
        names = [str(n) for n in names]
        unknown = [n for n in names if n not in self.engines]
        if unknown:
            raise ValueError(f'unknown engine {unknown[0]!r}')
        self.order = list(dict.fromkeys(names)) + [n for n in self.engines if n not in names]
        self.cooling.clear()  # a new order is a fresh start

    def usable(self) -> list[Engine]:
        return [self.engines[n] for n in self.order if self.engines[n].available()[0]]

    # ---------- health ----------

    def stats(self, name: str) -> dict | None:
        """This backend's live counters from engines/health.py (set on Auto by health.instrument), or None."""
        health = getattr(self, '_health', None)
        return health.stats.get(name) if health is not None else None

    def ready(self, e: Engine, now: float | None = None) -> bool:
        """Not cooling down after a failure here, and not blocked (out of quota, logged out) by its health wrapper."""
        now = time.monotonic() if now is None else now
        return self.cooling.get(e.name, (0, ''))[0] <= now and (getattr(e, '_blocked', None) or (0, ''))[0] <= now

    def rate(self, name: str) -> float | None:
        """Share of recent calls that answered, once there are HEALTH_MIN_CALLS of them."""
        s = self.stats(name)
        return s['ok'] / s['calls'] if s and s['calls'] >= HEALTH_MIN_CALLS else None

    def p(self, name: str, q: float) -> float | None:
        """A latency percentile in ms of this backend's answers, once it has HEALTH_MIN_CALLS of them."""
        s = self.stats(name)
        return percentile(s['ms'], q) if s and len(s['ms']) >= HEALTH_MIN_CALLS else None

    def healthy(self, e: Engine, min_ok: float = 0.5, now: float | None = None) -> bool:
        rate = self.rate(e.name)
        return e.available()[0] and self.ready(e, now) and (rate is None or rate >= min_ok)

    def ranked(self, engines: list[Engine]) -> list[Engine]:
        """Health-aware order: ready engines first, then by success rate (bands: 90% and up, 50% and up, below), then by
        p50 (engines within SPEED_BAND times of the fastest are equally fast), then the user's order. An engine without
        enough calls yet keeps the user's place in the top bands, so a fresh start is exactly the user's order."""
        now = time.monotonic()
        p50 = {e.name: self.p(e.name, 0.5) for e in engines}
        known = [v for v in p50.values() if v]
        best = min(known) if known else None

        def key(ie):
            i, e = ie
            rate = self.rate(e.name)
            rate_band = 0 if rate is None or rate >= 0.9 else 1 if rate >= 0.5 else 2
            ms = p50[e.name]
            speed = int(math.log(ms / best, SPEED_BAND)) if ms and best else 0
            return (not self.ready(e, now), rate_band, speed, i)
        return [e for _, e in sorted(enumerate(engines), key=key)]

    def fit(self, web=False, exec=False, vision=False) -> list[Engine]:
        if vision:  # a call with images goes only to engines that read them (Studio's design critic); no fallback
            return [e for e in self.usable() if getattr(e, 'supports_vision', False) and (not web or e.supports_web)
                    and (not exec or e.supports_exec)]
        fit = [e for e in self.usable() if (not web or e.supports_web) and (not exec or e.supports_exec)]
        return fit or (self.usable() if web else [])  # nothing can search: answering without the web beats failing

    def chain(self, web=False, exec=False, first: str | None = None, vision=False) -> list[Engine]:
        """Engines to try for one call, in health-aware order; cooling or blocked ones last. `first` (a backend's name)
        goes to the front when it can take the call and is ready (difficulty routing, A5). vision: only engines that
        can read images (a call with images=...)."""
        out = self.ranked(self.fit(web, exec, vision))
        lead = next((e for e in out if e.name == first), None)
        if lead is not None and self.ready(lead):
            out = [lead] + [e for e in out if e is not lead]
        return out

    def fastest(self, web=False, exec=False, min_ok: float = 0.5) -> Engine | None:
        """The healthy backend with the lowest p50; without latencies yet, the first in health-aware order."""
        now = time.monotonic()
        ok = [e for e in self.ranked(self.fit(web, exec)) if self.healthy(e, min_ok, now)]
        timed = [e for e in ok if self.p(e.name, 0.5) is not None]
        return min(timed, key=lambda e: self.p(e.name, 0.5)) if timed else (ok[0] if ok else None)

    def strongest(self, prefer, web=False, exec=False, min_ok: float = 0.5) -> Engine | None:
        """The first healthy backend in `prefer` (strongest first) that can take the call, else the health-aware lead."""
        now = time.monotonic()
        ok = [e for e in self.ranked(self.fit(web, exec)) if self.healthy(e, min_ok, now)]
        return next((e for n in prefer for e in ok if e.name == n), ok[0] if ok else None)

    def lead(self) -> Engine | None:
        c = self.chain()
        return c[0] if c else None

    @property
    def billing(self):
        e = self.lead()
        return e.billing if e else 'api'

    @property
    def supports_web(self):
        return any(e.supports_web for e in self.usable())

    @property
    def supports_exec(self):
        return any(e.supports_exec for e in self.usable())

    def seer(self) -> Engine | None:
        """The healthy backend, in health-aware order, that can read images (Studio's critic and Polish), or None."""
        now = time.monotonic()
        return next((e for e in self.ranked(self.fit(vision=True)) if self.healthy(e, now=now)), None)

    @property
    def supports_vision(self):
        """True only while the chain has a healthy engine that can read images; a call with images goes to it."""
        return self.seer() is not None

    def available(self):
        if self.usable():
            return True, ''
        return False, 'no engine is installed or configured'

    def info(self) -> dict:
        lead = self.lead()
        now = time.monotonic()
        return {**super().info(), 'order': list(self.order), 'lead': lead.name if lead else None,
                'cooling': {n: why for n, (until, why) in self.cooling.items() if until > now}}

    # ---------- calls ----------

    def failed(self, e: Engine, err: EngineError):
        why = err.why or 'failed'
        self.cooling[e.name] = (time.monotonic() + (COOLDOWN if LASTING.search(why) else SHORT_COOLDOWN), why)
        self.warm_new_lead()
        return why

    def hedge_after(self, e: Engine) -> float:
        """Seconds to wait for this engine's first text before a hedge starts: its p90, never under HEDGE_MIN, and
        HEDGE_UNKNOWN until it has a p90 (a cold CLI start must not look like a stall)."""
        p90 = self.p(e.name, 0.9)
        return max(HEDGE_MIN, p90 / 1000 if p90 is not None else HEDGE_UNKNOWN)

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False, schema=None,
                     exec=False, first: str | None = None, images=None):
        vision = bool(images)
        queue = self.chain(web, exec, first, vision)
        if vision:  # healthy vision engines first; one that is cooling down is still tried, last
            now = time.monotonic()
            queue = [e for e in queue if self.healthy(e, now=now)] + [e for e in queue if not self.healthy(e, now=now)]
        if not queue:
            raise EngineError('no engine that can read images is set up' if vision else self.available()[1])
        call = dict(system=system, prompt=prompt, effort=effort, max_tokens=max_tokens, web=web, schema=schema, exec=exec)
        if vision:
            call['images'] = list(images)
        hedge = env_flag('TG_HEDGE')
        failures = []

        def record(e: Engine, err: EngineError):
            why = self.failed(e, err)
            failures.append(f'{e.label}: {why}')
            health = getattr(self, '_health', None)  # set by health.instrument
            if health is not None and queue:
                health.fallback(e.name, queue[0].name)
            if err.partial and emit_delta and queue:
                emit_delta(f'\n[{e.label} failed ({why}); switching to {queue[0].label}]\n')

        while queue:
            e = queue.pop(0)
            backup = next((b for b in queue if self.ready(b)), None) if hedge else None
            if backup is not None:
                hedge = False  # at most one hedge per call
                reply, winner, failed = await self.race(e, backup, call, emit_delta)
                for engine, _ in failed:
                    if engine in queue:
                        queue.remove(engine)
                for engine, err in failed:
                    record(engine, err)
                if reply is None:
                    continue  # the engine that lost the race was cancelled, not failed, so it is still in the queue
                e = winner
            else:
                try:
                    reply = await e.stream(**call, emit_delta=emit_delta)
                except EngineRefusal:
                    raise  # another backend shouldn't be asked to do what this one refused
                except EngineError as err:
                    record(e, err)
                    continue
            self.cooling.pop(e.name, None)
            self.last = e.name
            reply.engine = reply.engine or e.name
            return reply
        raise EngineError('; '.join(failures))

    async def race(self, a: Engine, b: Engine, call: dict, emit_delta):
        """One hedged call: `a` starts; if it has shown no text and not finished by hedge_after(a), `b` starts too. The
        first engine to stream text (or to answer, for calls that don't stream) owns the reply, and the other is cancelled
        at that moment. Returns (reply or None, the engine that answered, [(engine, EngineError)] of those that failed).
        A refusal is raised as it is: no other engine should be asked what one refused."""
        owner: list[Engine] = []
        tasks: dict[asyncio.Task, Engine] = {}

        def claim(e: Engine) -> bool:
            if not owner:
                owner.append(e)
                for t, other in tasks.items():
                    if other is not e and not t.done():
                        t.cancel()
            return owner[0] is e

        def gate(e: Engine):
            if emit_delta is None:
                return None

            def emit(chunk):
                if chunk and claim(e):
                    emit_delta(chunk)
            return emit

        def start(e: Engine) -> asyncio.Task:
            t = asyncio.ensure_future(e.stream(**call, emit_delta=gate(e)))
            tasks[t] = e
            return t

        failed = []
        try:
            ta = start(a)
            await asyncio.wait({ta}, timeout=self.hedge_after(a))
            pending = {ta} if ta.done() or owner else {ta, start(b)}  # answered, failed or streaming in time: no hedge
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    e = tasks[t]
                    if t.cancelled():
                        continue
                    err = t.exception()
                    if err is None:
                        if claim(e):
                            return t.result(), e, failed
                        continue  # answered after the other engine had started streaming its reply
                    if not isinstance(err, EngineError) or isinstance(err, EngineRefusal):
                        raise err
                    failed.append((e, err))
                    if owner and owner[0] is e:  # it had started streaming, so the other was already cancelled
                        return None, None, failed
            return None, None, failed
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()

    async def prewarm(self, **call):
        if call not in self.warm_calls:
            self.warm_calls.append(call)
        lead = self.lead()
        if lead is not None:
            await lead.prewarm(**call)

    def warm_new_lead(self):
        """After a failure moves the lead to another engine, start its processes for the calls the next runs will make."""
        lead = self.lead()
        if lead is None or not self.warm_calls:
            return

        async def warm():
            for call in self.warm_calls:
                try:
                    await lead.prewarm(**call)
                except Exception:
                    pass
        task = asyncio.create_task(warm())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def aclose(self):
        for task in list(self.tasks):
            task.cancel()  # the engines it wraps are closed by their owner


class Steered:
    """Auto with one backend tried first for every call (difficulty routing, A5): the fastest for an easy step, the
    strongest for a hard one. Everything else is Auto itself, so a failure still falls through to the next engine."""

    def __init__(self, auto: AutoEngine, first: str):
        self._auto, self.first = auto, first

    def __getattr__(self, name):
        return getattr(self._auto, name)

    async def stream(self, **kw):
        return await self._auto.stream(**kw, first=self.first)

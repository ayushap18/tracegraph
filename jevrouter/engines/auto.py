"""Auto engine: the user's own order of backends, falling through to the next one when a call fails.

The default order puts the subscription CLIs first (Claude Code, Codex, Antigravity) and the pay-per-token Anthropic
API last, so a plan that is out of quota, logged out or timing out never fails a run while another backend works.
TG_ENGINE_ORDER (comma separated) or the UI sets the order. An engine that just failed is skipped for a while, so a
used-up plan costs one failed call, not one per request. Each move to the next engine is counted in engines/health.py.
"""
import asyncio
import os
import time

from .base import COOLDOWN, LASTING, Engine, EngineError, EngineRefusal

SHORT_COOLDOWN = 30.0  # timeouts, crashes and one-off API errors may


def default_order(names) -> list[str]:
    raw = os.environ.get('TG_ENGINE_ORDER', '')
    wanted = [n.strip().lower() for n in raw.split(',') if n.strip()]
    order = [n for n in dict.fromkeys(wanted) if n in names]
    return order + [n for n in names if n not in order]


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

    def chain(self, web=False, exec=False) -> list[Engine]:
        """Engines to try for one call: usable ones that can do what it needs, those cooling down after a failure last."""
        now = time.monotonic()
        fit = [e for e in self.usable() if (not web or e.supports_web) and (not exec or e.supports_exec)]
        if web and not fit:  # nothing can search: answering without the web beats failing
            fit = self.usable()
        ready = [e for e in fit if self.cooling.get(e.name, (0, ''))[0] <= now]
        return ready + [e for e in fit if e not in ready]

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

    def available(self):
        if self.usable():
            return True, ''
        return False, 'no engine is installed or configured'

    def info(self) -> dict:
        lead = self.lead()
        now = time.monotonic()
        return {**super().info(), 'order': list(self.order), 'lead': lead.name if lead else None,
                'cooling': {n: why for n, (until, why) in self.cooling.items() if until > now}}

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False, schema=None,
                     exec=False):
        chain = self.chain(web, exec)
        if not chain:
            raise EngineError(self.available()[1])
        failures = []
        for i, e in enumerate(chain):
            try:
                reply = await e.stream(system=system, prompt=prompt, effort=effort, emit_delta=emit_delta,
                                       max_tokens=max_tokens, web=web, schema=schema, exec=exec)
            except EngineRefusal:
                raise  # another backend shouldn't be asked to do what this one refused
            except EngineError as err:
                why = err.why or 'failed'
                self.cooling[e.name] = (time.monotonic() + (COOLDOWN if LASTING.search(why) else SHORT_COOLDOWN), why)
                self.warm_new_lead()
                failures.append(f'{e.label}: {why}')
                health = getattr(self, '_health', None)  # set by health.instrument
                if health is not None and i + 1 < len(chain):
                    health.fallback(e.name, chain[i + 1].name)
                if err.partial and emit_delta and i + 1 < len(chain):
                    emit_delta(f'\n[{e.label} failed ({why}); switching to {chain[i + 1].label}]\n')
                continue
            self.cooling.pop(e.name, None)
            self.last = e.name
            reply.engine = reply.engine or e.name
            return reply
        raise EngineError('; '.join(failures))

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

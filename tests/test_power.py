"""Backend power (docs/PLAN-speed-evals-chat.md A5 and A6): Jev's hard score, health-aware Auto, engine per step, hedged
requests, and the verify step with its warning in the merged answer."""
import asyncio
import json
import time

import pytest

from jevrouter import verify as verify_mod
from jevrouter.agents import llm, tools
from jevrouter.engines import EngineError, Reply
from jevrouter.engines import auto as auto_mod
from jevrouter.engines.auto import AutoEngine, Steered
from jevrouter.engines.health import Health, instrument_all
from jevrouter.jev import route_one
from jevrouter.pipeline import Router
from tests.fakes import FakeJev, ScriptEngine


def backends():
    return {'claude-code': ScriptEngine(), 'codex': ScriptEngine(name='codex', label='Codex', web=False),
            'agy': ScriptEngine(name='agy', label='Antigravity')}


def with_auto(es=None):
    es = es or backends()
    return {'auto': AutoEngine(dict(es)), **es}


def timed(health: Health, name: str, ms: float, n: int = 3, ok: bool = True):
    for _ in range(n):
        health.record(name, ok, ms, None if ok else 'boom')


async def events_of(router, query, **kw):
    events = []
    router.bus.taps.append(events.append)
    await router.handle(query, 'you', **kw)
    return events


def agent_calls(engine):
    return [c for c in engine.calls if c['schema'] is None and not c['system'].startswith('Do not use tools.')]


# ---------- Jev's hard score ----------

async def test_route_one_asks_for_a_hard_score():
    jev = FakeJev(hard=0.82)
    d = await route_one(jev, 'write a report on bees', {'chat': 'x', 'report': 'y'})
    assert d['hard'] == 0.82 and 'hard' in jev.calls[-1][1]


# ---------- health-aware Auto ----------

def test_auto_keeps_the_users_order_without_health_data():
    auto = AutoEngine(backends())
    instrument_all({'auto': auto}, Health())
    assert [e.name for e in auto.chain()] == ['claude-code', 'codex', 'agy']


def test_auto_orders_by_success_rate_then_p50_with_the_users_order_as_tiebreak():
    es = backends()
    auto = AutoEngine(es)
    health = Health()
    instrument_all({'auto': auto}, health)
    timed(health, 'claude-code', 700)
    timed(health, 'codex', 500)
    timed(health, 'agy', 400)
    # within 2x of the fastest counts as equally fast: the user's order stands
    assert [e.name for e in auto.chain()] == ['claude-code', 'codex', 'agy']
    timed(health, 'claude-code', 5000, n=6)  # now over 2x slower than agy
    assert [e.name for e in auto.chain()] == ['codex', 'agy', 'claude-code']
    timed(health, 'codex', 0, n=12, ok=False)  # codex fails most calls: success rate beats speed
    assert [e.name for e in auto.chain()] == ['agy', 'claude-code', 'codex']


def test_auto_never_tries_a_cooling_or_blocked_engine_first():
    es = backends()
    auto = AutoEngine(es)
    health = Health()
    instrument_all({'auto': auto}, health)
    timed(health, 'agy', 100)
    timed(health, 'claude-code', 5000)
    timed(health, 'codex', 5000)
    assert auto.chain()[0].name == 'agy'
    es['agy']._blocked = (time.monotonic() + 60, 'out of quota')
    assert auto.chain()[0].name != 'agy' and auto.chain()[-1].name == 'agy'
    es['agy']._blocked = None
    auto.cooling['agy'] = (time.monotonic() + 60, 'timed out')
    assert auto.chain()[-1].name == 'agy'
    assert auto.chain(first='agy')[0].name != 'agy'  # a steered call doesn't start on a cooling engine either
    assert auto.chain(first='codex')[0].name == 'codex'


def test_fastest_and_strongest():
    es = backends()
    auto = AutoEngine(es)
    health = Health()
    instrument_all({'auto': auto}, health)
    assert auto.fastest().name == 'claude-code'  # no latencies yet: the first in order
    timed(health, 'codex', 300)
    timed(health, 'agy', 200)
    assert auto.fastest().name == 'agy' and auto.fastest(web=True).name == 'agy'
    assert auto.strongest(('claude-code', 'codex')).name == 'claude-code'
    timed(health, 'claude-code', 0, n=6, ok=False)
    assert auto.strongest(('claude-code', 'codex')).name == 'codex'  # unhealthy: passed over


async def test_steered_goes_first_and_still_falls_through():
    es = backends()
    auto = AutoEngine(es)
    r = await Steered(auto, 'agy').stream(system='s', prompt='p')
    assert r.engine == 'agy' and es['agy'].calls and not es['claude-code'].calls

    async def fail(**kw):
        raise EngineError('timed out')
    es['agy'].stream = fail
    r = await Steered(auto, 'agy').stream(system='s', prompt='p')
    assert r.engine == 'claude-code' and 'agy' in auto.cooling


# ---------- engine per step ----------

def power_router(hard, active='auto', es=None):
    es = with_auto(es)
    router = Router(FakeJev(route_for=lambda t: ('chat', 0.9), hard=hard), None, es[active], engines=es)
    timed(router.health, 'agy', 200)  # agy is the fastest; claude-code is the strongest
    timed(router.health, 'claude-code', 3000)
    timed(router.health, 'codex', 2500)
    return router, es


async def test_easy_step_on_auto_goes_to_the_fastest_engine_at_low_effort():
    router, es = power_router(hard=0.1)
    events = await events_of(router, 'hey there friend')
    a = next(e for e in events if e['type'] == 'answered')
    assert a['engine'] == 'agy' and a['checks']['effort'] == 'low'
    assert [c['effort'] for c in agent_calls(es['agy'])] == ['low'] and not agent_calls(es['claude-code'])


async def test_hard_step_on_auto_goes_to_the_strongest_engine_at_high_effort():
    router, es = power_router(hard=0.9)
    events = await events_of(router, 'hey there friend')
    a = next(e for e in events if e['type'] == 'answered')
    assert a['engine'] == 'claude-code' and a['checks']['effort'] == 'high'
    assert not agent_calls(es['agy'])


async def test_moderate_step_keeps_autos_own_order_and_effort():
    router, es = power_router(hard=0.5)
    events = await events_of(router, 'hey there friend')
    a = next(e for e in events if e['type'] == 'answered')
    assert a['checks']['effort'] == 'low'  # the chat agent's own effort; nothing was changed
    assert a['engine'] == router.engines['auto'].chain()[0].name


async def test_pinned_engine_stays_pinned():
    router, es = power_router(hard=0.1, active='codex')
    events = await events_of(router, 'hey there friend')  # the active engine is codex, not Auto
    assert next(e for e in events if e['type'] == 'answered')['engine'] == 'codex' and not agent_calls(es['agy'])
    router, es = power_router(hard=0.9)
    events = await events_of(router, 'hey there friend', engine=es['agy'])  # named for this run
    assert next(e for e in events if e['type'] == 'answered')['engine'] == 'agy'


async def test_deep_mode_runs_on_the_selected_engine_at_high_effort():
    """A selected engine is used for every step, even an easy one; deep's high effort still applies."""
    router, es = power_router(hard=0.1, active='codex')
    events = await events_of(router, 'hey there friend', mode='deep')
    a = next(e for e in events if e['type'] == 'answered')
    assert a['engine'] == 'codex' and a['checks']['effort'] == 'high'
    assert next(e for e in events if e['type'] == 'query')['engine'] == 'codex'
    assert not agent_calls(es['agy']) and not agent_calls(es['claude-code'])
    # with Auto selected, deep mode picks the strongest healthy engine and stays on it
    router, es = power_router(hard=0.1, active='auto')
    events = await events_of(router, 'hey there friend', mode='deep')
    assert next(e for e in events if e['type'] == 'query')['engine'] == 'claude-code'
    assert next(e for e in events if e['type'] == 'answered')['engine'] == 'claude-code'


async def test_deep_mode_keeps_a_named_engine_pinned():
    # a compare tab or "Try another engine" names an engine: it is pinned whatever is selected
    router, es = power_router(hard=0.1, active='codex')
    assert router.deep_engine() is es['codex']
    events = await events_of(router, 'hey there friend', engine=es['claude-code'], mode='deep')
    assert next(e for e in events if e['type'] == 'query')['engine'] == 'claude-code'
    assert next(e for e in events if e['type'] == 'answered')['engine'] == 'claude-code'
    assert not agent_calls(es['agy']) and not agent_calls(es['codex'])


# ---------- hedged requests ----------

class Slow:
    """An engine that answers after `delay` seconds (or fails), streaming its text in one delta."""
    supports_web = supports_exec = False
    billing = 'subscription'

    def __init__(self, name, delay=0.0, fail=False, first_delta_at=None):
        self.name, self.label, self.delay, self.fail = name, name.title(), delay, fail
        self.first_delta_at = first_delta_at
        self.started = self.cancelled = 0

    def available(self):
        return True, ''

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, **kw):
        self.started += 1
        try:
            if self.first_delta_at is not None:
                await asyncio.sleep(self.first_delta_at)
                if emit_delta:
                    emit_delta(f'{self.name} starts ')
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.fail:
            raise EngineError('boom')
        if emit_delta:
            emit_delta(f'{self.name} answer')
        return Reply(f'{self.name} answer')


@pytest.fixture
def quick_hedge(monkeypatch):
    monkeypatch.setenv('TG_HEDGE', '1')
    monkeypatch.setattr(auto_mod, 'HEDGE_MIN', 0.05)
    monkeypatch.setattr(auto_mod, 'HEDGE_UNKNOWN', 0.05)


async def test_hedge_starts_the_next_engine_and_keeps_the_first_answer(quick_hedge):
    a, b, c = Slow('a', delay=2), Slow('b', delay=0.05), Slow('c')
    auto = AutoEngine({'a': a, 'b': b, 'c': c})
    shown = []
    r = await auto.stream(system='s', prompt='p', emit_delta=shown.append)
    assert r.engine == 'b' and shown == ['b answer'] and a.cancelled == 1 and c.started == 0


async def test_no_hedge_when_off_or_when_the_first_engine_streams_in_time(monkeypatch, quick_hedge):
    a, b = Slow('a', delay=0.2, first_delta_at=0.01), Slow('b')
    r = await AutoEngine({'a': a, 'b': b}).stream(system='s', prompt='p', emit_delta=lambda t: None)
    assert r.engine == 'a' and b.started == 0  # text arrived before the hedge time
    monkeypatch.delenv('TG_HEDGE')
    a, b = Slow('a', delay=0.2), Slow('b')
    r = await AutoEngine({'a': a, 'b': b}).stream(system='s', prompt='p')
    assert r.engine == 'a' and b.started == 0


async def test_the_engine_that_streams_first_owns_the_reply(quick_hedge):
    a, b = Slow('a', delay=0.3, first_delta_at=0.1), Slow('b', delay=1)
    shown = []
    r = await AutoEngine({'a': a, 'b': b}).stream(system='s', prompt='p', emit_delta=shown.append)
    # the hedge started at 0.05 s, then a streamed first: b was cancelled and never shown
    assert r.engine == 'a' and b.started == 1 and b.cancelled == 1 and shown == ['a starts ', 'a answer']


async def test_hedge_survives_a_failure_and_is_used_once(quick_hedge):
    a, b, c = Slow('a', delay=0.2, fail=True), Slow('b', delay=0.4), Slow('c', delay=0.2)
    auto = AutoEngine({'a': a, 'b': b, 'c': c})
    r = await auto.stream(system='s', prompt='p')
    assert r.engine == 'b' and 'a' in auto.cooling and c.started == 0
    a, b, c = Slow('a', delay=0.2, fail=True), Slow('b', delay=0.1, fail=True), Slow('c', delay=0.2)
    auto = AutoEngine({'a': a, 'b': b, 'c': c})
    r = await auto.stream(system='s', prompt='p')
    assert r.engine == 'c' and c.started == 1 and {'a', 'b'} <= set(auto.cooling)


def test_hedge_waits_for_the_engines_p90(monkeypatch):
    auto = AutoEngine({'a': Slow('a')})
    health = Health()
    instrument_all({'auto': auto}, health)
    assert auto.hedge_after(auto.engines['a']) == 10.0  # no latencies yet: a cold start is not a stall
    timed(health, 'a', 1000, n=9)
    timed(health, 'a', 7000, n=1)
    assert auto.hedge_after(auto.engines['a']) == 4.0  # p90 is 1 s, but never under 4 s
    timed(health, 'a', 9000, n=5)
    assert auto.hedge_after(auto.engines['a']) == 9.0


# ---------- verify ----------

def test_math_check_accepts_rounding_and_flags_a_wrong_value():
    assert verify_mod.check_math('what is 15% of 380', 'It is 57.')['verified'] == 'ok'
    assert verify_mod.check_math('10 / 3', 'About 3.33')['verified'] == 'ok'
    assert verify_mod.check_math('what is 12 * 12', 'one hundred and forty-four')['verified'] == 'ok'
    bad = verify_mod.check_math('what is 15% of 380', 'It is 75.')
    assert bad['verified'] == 'mismatch' and '57' in bad['verify_note']
    assert verify_mod.check_math('who was Ada Lovelace', 'A mathematician') is None


async def test_currency_check_uses_todays_rate(monkeypatch):
    async def rate(http, ttl, url, **params):
        assert params == {'amount': 100.0, 'from': 'EUR', 'to': 'INR'}
        return {'rates': {'INR': 9000.0}}
    monkeypatch.setattr(verify_mod, 'cached_json', rate)
    ok = await verify_mod.check_currency('convert 100 EUR to INR', 'About 8,850 rupees', http=object())
    assert ok['verified'] == 'ok'  # within 5% of the reference rate
    bad = await verify_mod.check_currency('convert 100 EUR to INR', '100 EUR is 5,000 INR', http=object())
    assert bad['verified'] == 'mismatch' and '9,000.00 INR' in bad['verify_note']
    assert await verify_mod.check_currency('weather in Paris', 'sunny', http=object()) is None

    async def down(*a, **k):
        raise OSError('offline')
    monkeypatch.setattr(verify_mod, 'cached_json', down)
    assert (await verify_mod.check_currency('100 EUR to INR', '9000', http=object()))['verified'] == 'skipped'


class Checker(ScriptEngine):
    """A second engine for the grounding check: says whether the answer matches the sources."""

    def __init__(self, supported, **kw):
        super().__init__(**kw)
        self.supported = supported

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False, schema=None,
                     exec=False):
        self.calls.append({'system': system, 'prompt': prompt, 'effort': effort, 'schema': schema, 'web': web})
        if system.startswith('Do not use tools. You check whether'):
            return Reply(json.dumps({'supported': self.supported, 'note': '' if self.supported else 'The sources give 1815.'}), 9, 2)
        return await super().stream(system=system, prompt=prompt, effort=effort, emit_delta=emit_delta,
                                    max_tokens=max_tokens, web=web, schema=schema, exec=exec)


@pytest.fixture
def abstract(monkeypatch):
    async def ddg(http, q):
        return 'Ada', 'Ada Lovelace (born 1815) was an English mathematician.', 'https://en.wikipedia.org/wiki/Ada_Lovelace'
    monkeypatch.setattr(verify_mod, 'ddg_abstract', ddg)


async def test_grounding_check_only_in_deep_mode(abstract):
    checker = Checker(False, name='codex', label='Codex')
    v, tin, tout = await verify_mod.verify('Who was Ada Lovelace?', 'Born in 1915.', agent='knowledge', http=object(),
                                           deep=True, checker=checker)
    assert v['verified'] == 'mismatch' and 'Codex' in v['verify_note'] and '1815' in v['verify_note'] and tin == 9
    assert 'Ada Lovelace (born 1815)' in checker.calls[-1]['prompt']
    v, *_ = await verify_mod.verify('Who was Ada Lovelace?', 'Born in 1815.', agent='knowledge', http=object(),
                                    deep=True, checker=Checker(True))
    assert v == {'verified': 'ok', 'verify_note': None}
    v, *_ = await verify_mod.verify('Who was Ada Lovelace?', 'x', agent='knowledge', http=object(), deep=False,
                                    checker=checker)
    assert v == {}  # outside deep mode only the free numeric checks run
    v, *_ = await verify_mod.verify('Who was Ada Lovelace?', 'x', agent='knowledge', http=object(), deep=True)
    assert v['verified'] == 'skipped' and 'second engine' in v['verify_note']


async def test_deep_knowledge_answer_is_checked_by_a_second_engine(abstract, monkeypatch):
    es = {'claude-code': ScriptEngine(), 'codex': Checker(False, name='codex', label='Codex', web=False)}
    router = Router(FakeJev(route_for=lambda t: ('knowledge', 0.9)), object(), es['claude-code'], engines=es)
    monkeypatch.setattr(llm, 'ddg_abstract', verify_mod.ddg_abstract)
    events = await events_of(router, 'Who was Ada Lovelace?', mode='deep')
    a = next(e for e in events if e['type'] == 'answered')
    assert a['engine'] == 'claude-code' and a['checks']['verified'] == 'mismatch'
    merged = next(e for e in events if e['type'] == 'merged')['answer']
    assert merged.endswith('**Check this answer:** Codex checked it against the sources: The sources give 1815.')
    assert es['codex'].calls and events[-1]['tokens']['llm_in'] >= 9


async def test_wrong_number_warns_in_the_merged_answer_in_any_mode():
    engine = ScriptEngine()  # echoes the prompt, so it never says 57
    router = Router(FakeJev(route_for=lambda t: ('chat', 0.9)), None, engine)
    events = await events_of(router, 'what is 15% of 380')
    a = next(e for e in events if e['type'] == 'answered')
    assert a['checks']['verified'] == 'mismatch'
    merged = next(e for e in events if e['type'] == 'merged')['answer']
    assert '**Check this answer:** A keyless recompute gives 57' in merged
    assert router.history[-1]['merged']['answer'] == merged


async def test_warning_names_the_step_when_there_are_several():
    engine = ScriptEngine(plan={'subtasks': [{'text': 'what is 15% of 380', 'depends_on': []},
                                             {'text': 'tell me a joke', 'depends_on': []}]})
    router = Router(FakeJev(route_for=lambda t: ('chat', 0.9), multi=0.5), None, engine)
    events = await events_of(router, 'what is 15% of 380 and tell me a joke')
    merged = next(e for e in events if e['type'] == 'merged')['answer']
    assert '**Check these answers:**\n- what is 15% of 380: A keyless recompute gives 57' in merged
    assert '**Check these answers:**' in ''.join(e['text'] for e in events if e['type'] == 'delta' and e['tid'] == 'merge')


async def test_keyless_answers_are_never_verified():
    from tests.test_pipeline import by_keyword, fake_registry
    router = Router(FakeJev(route_for=by_keyword), registry=fake_registry())
    events = await events_of(router, 'weather in Paris')
    assert 'checks' not in next(e for e in events if e['type'] == 'answered')

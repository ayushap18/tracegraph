"""Speed plan (docs/PLAN-speed-evals-chat.md A1-A4): stage timings, fewer serial LLM calls, sooner streaming, caches."""
import asyncio

import pytest

from jevrouter import cache
from jevrouter.agents import AgentResult, tools
from jevrouter.cache import RouteCache, TTLCache, track
from jevrouter.merger import merge, template
from jevrouter.pipeline import Router, timings, union_ms
from jevrouter.planner import needs_llm_plan, plan
from jevrouter.store import Store
from tests.fakes import FakeJev, ScriptEngine
from tests.test_api import json_of
from tests.test_pipeline import by_keyword, fake_registry, run

TIMING_KEYS = {'plan_ms', 'route_ms', 'agents_ms', 'merge_ms', 'first_token_ms', 'planner', 'merger', 'cache_hits'}


def llm_calls(engine, kind):
    """The engine calls of one kind: 'plan' (structured), 'merge' or 'agent'."""
    def of(c):
        if c['schema'] is not None:
            return 'plan'
        return 'merge' if c['system'].startswith('Do not use tools. You combine') else 'agent'
    return [c for c in engine.calls if of(c) == kind]


# ---------- A1: timings ----------

async def test_every_run_stores_timings_and_sends_them_on_done():
    router = Router(FakeJev(route_for=by_keyword, multi=0.9), registry=fake_registry())
    events = await run(router, 'weather in Paris and convert 100 EUR to INR')
    done, rec = events[-1], router.history[-1]
    t = done['timings']
    assert set(t) == TIMING_KEYS and rec['timings'] == t == router.store.get_run(rec['qid'])['timings']
    # keyless: the heuristic asked Jev's multi score, the answers were joined by the template
    assert t['planner'] == 'heuristic' and t['merger'] == 'template' and t['merge_ms'] is None
    assert t['plan_ms'] is not None and t['route_ms'] is not None and t['agents_ms'] >= 10  # the fake weather sleeps 10 ms
    assert 0 <= t['first_token_ms'] <= done['total_ms'] and t['cache_hits'] == 0


async def test_single_request_has_no_planner_call():
    router = Router(FakeJev(route_for=by_keyword), registry=fake_registry())
    events = await run(router, 'weather in Paris')
    t = events[-1]['timings']
    assert t['planner'] == 'single' and t['plan_ms'] is None and t['merger'] == 'single' and t['merge_ms'] is None


async def test_failed_run_still_has_timings():
    router = Router(FakeJev(route_for=by_keyword, fail_on=('weather',)), registry=fake_registry())
    events = await run(router, 'weather in Paris')
    assert events[-1]['status'] == 'error' and set(events[-1]['timings']) == TIMING_KEYS
    assert events[-1]['timings']['agents_ms'] is None


def test_union_of_spans():
    assert union_ms([(0, 1), (0.5, 2), (3, 4)]) == 3000  # overlapping steps count once
    assert union_ms([(0, 1), (2, 3)], start=2.5) == 500  # routing counts only after planning ends
    assert union_ms([(0, 1)], start=5) == 0  # a speculative route that finished while planning cost nothing after it
    clock = {'plan': None, 'route': [], 'agents': [], 'merge': None, 'first': None, 'planner': 'single', 'merger': None,
             'hits': 2}
    assert timings(clock, 0) == {'plan_ms': None, 'route_ms': None, 'agents_ms': None, 'merge_ms': None,
                                 'first_token_ms': None, 'planner': 'single', 'merger': 'single', 'cache_hits': 2}


async def test_timings_endpoint(client_with_runs):
    client = client_with_runs
    r = await json_of(await client.get('/api/timings'))
    assert r['engine'] is None and r['runs'] == 3  # the running run and the run without timings are skipped
    stages = {s['stage']: s for s in r['stages']}
    assert [s['stage'] for s in r['stages']] == ['plan', 'route', 'agents', 'merge', 'first_token', 'total']
    assert stages['total'] == {'stage': 'total', 'p50': 200, 'p90': 900, 'n': 3}
    assert stages['plan'] == {'stage': 'plan', 'p50': 50, 'p90': 800, 'n': 2}  # null stages are left out
    assert stages['merge']['n'] == 0 and stages['merge']['p50'] is None
    keyless = await json_of(await client.get('/api/timings?engine=none'))
    assert keyless['engine'] == 'none' and keyless['runs'] == 2
    codex = await json_of(await client.get('/api/timings?engine=codex&limit=1'))
    assert codex['runs'] == 1 and {s['stage']: s for s in codex['stages']}['total']['p50'] == 900
    assert (await client.get('/api/timings?limit=x')).status == 400


@pytest.fixture
async def client_with_runs():
    from aiohttp.test_utils import TestClient, TestServer
    from jevrouter import app as appmod
    store = Store()

    def rec(qid, engine, total, plan_ms, status='done', timed=True):
        r = {'qid': qid, 'text': 'q', 'source': 'you', 'at': 1.0, 'status': status, 'engine': engine, 'total_ms': total,
             'plan': None, 'tasks': [], 'merged': None}
        if timed:
            r['timings'] = {'plan_ms': plan_ms, 'route_ms': 10, 'agents_ms': 20, 'merge_ms': None, 'first_token_ms': 30,
                            'planner': 'llm' if plan_ms else 'single', 'merger': 'template', 'cache_hits': 0}
        return r
    for r in (rec(1, None, 100, None), rec(2, None, 200, 50), rec(3, 'codex', 900, 800), rec(4, None, 5000, 10, 'running'),
              rec(5, None, 7, 1, timed=False)):
        store.save_run(r)
    async with TestClient(TestServer(appmod.create_app(
            lambda http: Router(FakeJev(), http, store=store, registry=fake_registry())))) as c:
        yield c


# ---------- A2: fewer serial LLM calls ----------

@pytest.mark.parametrize('query, llm', [
    ('time in Tokyo and 15% of 380', False), ('weather in Paris and convert 100 EUR to INR', False),
    ('Convert 50 EUR to INR and then what time is it there', True),  # dependent step
    ('weather in Paris and convert 100 EUR to INR, then which is better value', True),
    ('convert 100 USD to INR and double it', True),
    # one question needs no LLM plan (docs/PLAN-accuracy-v2.md A7), even when "and" is part of a name
    ('Who was Marks and Spencer\'s founder?', False),
    ('Paris weather, Tokyo time', False),  # a comma list of requests: the text splitter splits it
    ('Paris weather, the height of the Eiffel Tower', True),  # a comma between clauses the text splitter left joined
    ('What time is it in New York?', False), ('Will it rain in Mumbai tomorrow?', False),
    ('Is it safe to eat raw eggs?', False), ('Is Pluto a planet? Why not?', True),
    ('Research the history of the Eiffel Tower in depth, 5 pages as a PDF', True),  # a file request with other content
    (' and '.join(['weather in Paris'] * 7), True),  # long
])
def test_needs_llm_plan(query, llm):
    assert needs_llm_plan(query) is llm


@pytest.mark.parametrize('query', [
    'Who is the CEO of Tesla and how old is he', 'Who wrote Hamlet and when was the author born',
    'Who founded Microsoft and what is his net worth', 'Find the population of France and divide by 2',
    'What is 15% of 380 and multiply by 3', 'What is the weather and the time in Tokyo',
    'convert 100 and 200 USD to EUR', 'which country won the 2018 world cup and the time in the winning country',
])
def test_parts_that_lean_on_each_other_need_the_llm_plan(query):
    assert needs_llm_plan(query)


@pytest.mark.parametrize('query', [
    'what is 15% of 380 and the time in Tokyo', 'weather in Paris and time in Tokyo',
    'convert 100 USD to EUR and weather in London', 'What is the capital of France and the population of Japan',
])
def test_independent_parts_still_skip_the_llm_plan(query):
    assert not needs_llm_plan(query)


async def test_sure_multi_score_does_not_plan_a_dependent_step_as_independent():
    engine = ScriptEngine(plan={'subtasks': [{'text': 'Who is the CEO of Tesla', 'depends_on': []},
                                             {'text': 'How old is the CEO of Tesla', 'depends_on': [0]}]})
    p = await plan('Who is the CEO of Tesla and how old is he', FakeJev(multi=0.9), engine)
    assert p['planner'] == 'claude-code' and p['deps'] == [[], [0]]


def test_needs_llm_plan_for_follow_ups_and_files():
    q = 'time in Tokyo and 15% of 380'
    assert needs_llm_plan(q, context=[{'query': 'a', 'answer': 'b'}]) and needs_llm_plan(q, files=['a.csv'])


async def test_sure_split_skips_the_llm_planner():
    engine = ScriptEngine()
    jev = FakeJev(multi=0.9)
    p = await plan('time in Tokyo and 15% of 380', jev, engine)
    assert p['planner'] == 'heuristic' and p['subtasks'] == ['time in Tokyo', '15% of 380'] and not engine.calls
    assert sorted(c[1] for c in jev.calls) == [['multi'], ['unsafe']]  # the whole query's safety check runs alongside


@pytest.mark.parametrize('multi, llm', [(0.9, False), (0.1, False), (0.6, True)])
async def test_unsure_split_asks_the_llm(multi, llm):
    engine = ScriptEngine(plan={'subtasks': [{'text': 'time in Tokyo', 'depends_on': []},
                                             {'text': '15% of 380', 'depends_on': []}]})
    p = await plan('time in Tokyo and 15% of 380', FakeJev(multi=multi), engine)
    assert (p['planner'] == 'claude-code') is llm and bool(engine.calls) is llm
    if multi == 0.1:
        assert p['subtasks'] == ['time in Tokyo and 15% of 380']  # Jev is sure it's one request


async def test_unsafe_query_never_takes_the_heuristic_shortcut():
    """A query that is unsafe as a whole is planned from its literal text (tests/test_gate.py covers the parts)."""
    engine = ScriptEngine(plan={'subtasks': [{'text': 'weather in Paris', 'depends_on': []}]})
    p = await plan('weather in Paris and how to make a bomb at home', FakeJev(multi=0.9, unsafe=0.95), engine)
    assert p.get('literal') and p['subtasks'] == ['weather in Paris', 'how to make a bomb at home'] and not engine.calls[1:]


async def test_quick_mode_never_calls_the_llm_planner_and_deep_always_does():
    engine = ScriptEngine(plan={'subtasks': [{'text': 'x', 'depends_on': []}]})
    q = 'Convert 50 EUR to INR and then what time is it there'
    assert (await plan(q, FakeJev(), engine, mode='quick'))['planner'] == 'heuristic' and not engine.calls
    assert (await plan('time in Tokyo and 15% of 380', FakeJev(), engine, mode='deep'))['planner'] == 'claude-code'


async def test_exact_answers_are_merged_by_template_on_an_engine():
    """Two keyless answers on an engine: no LLM plan, no LLM merge (the run makes no LLM call at all)."""
    engine = ScriptEngine()
    router = Router(FakeJev(route_for=by_keyword, multi=0.9), engine=engine, registry=fake_registry())
    events = await run(router, 'weather in Paris and convert 100 EUR to INR')
    assert not engine.calls and events[-2]['engine'] == 'concat'
    t = events[-1]['timings']
    assert t['planner'] == 'heuristic' and t['merger'] == 'template' and t['merge_ms'] is None


async def test_prose_answers_still_get_the_llm_merger():
    engine = ScriptEngine()
    router = Router(FakeJev(route_for=lambda t: ('chat', 0.9) if 'joke' in t else by_keyword(t), multi=0.9),
                    engine=engine)
    router.registry['weather'] = fake_registry()['weather']
    events = await run(router, 'tell me a joke and weather in Paris')
    assert len(llm_calls(engine, 'merge')) == 1 and events[-1]['timings']['merger'] == 'llm'
    assert events[-1]['timings']['merge_ms'] is not None and events[-2]['engine'] == 'claude-code'


def test_template_styles():
    results = [('weather', 'Paris: 18°C\nChance of rain today: 10%'), ('currency', '100.00 EUR = 9,000.00 INR')]
    assert template(results) == '**weather**: Paris: 18°C\nChance of rain today: 10%\n\n**currency**: 100.00 EUR = 9,000.00 INR'
    assert template(results, 'concise') == '**weather**: Paris: 18°C\n\n**currency**: 100.00 EUR = 9,000.00 INR'
    assert template(results, 'bullets') == ('- **weather**: Paris: 18°C\n  Chance of rain today: 10%\n'
                                            '- **currency**: 100.00 EUR = 9,000.00 INR')
    table = template(results, 'table', ['weather in Paris', 'convert 100 EUR to INR'])
    assert table.splitlines() == ['| Question | Answer |', '| --- | --- |',
                                  '| weather in Paris | Paris: 18°C Chance of rain today: 10% |',
                                  '| convert 100 EUR to INR | 100.00 EUR = 9,000.00 INR |']
    assert template(results, 'steps') == template(results)  # a template can't write steps: the default join


async def test_single_exact_answer_takes_the_style_the_template_can_apply():
    out = await merge('q', [('weather', 'Paris: 18°C\nRain: 10%')], lambda t: None, style='bullets', exact=True)
    assert out['answer'] == '- Paris: 18°C\n- Rain: 10%' and out['kind'] == 'single'
    out = await merge('q', [('chat', 'Line one.\nLine two.')], lambda t: None, style='concise', exact=False)
    assert out['answer'] == 'Line one.\nLine two.'  # prose from an LLM agent already had the style in its instructions


async def test_speculative_route_is_reused_for_an_unchanged_single_step():
    engine = ScriptEngine(plan={'subtasks': [{'text': 'the time in Tokyo, please', 'depends_on': []}]})
    jev = FakeJev(route_for=lambda t: ('time', 0.9))
    router = Router(jev, engine=engine, registry={'time': fake_agent('Tokyo: 9:00')})
    events = await run(router, 'the time in Tokyo, please')  # the comma: an LLM plan, and the route alongside it
    routes = [c for c in jev.calls if 'route' in c[1]]
    assert len(routes) == 1 and len(llm_calls(engine, 'plan')) == 1  # routed once, alongside the planner
    assert [e['type'] for e in events].count('routed') == 1 and events[-2]['answer'] == 'Tokyo: 9:00'
    assert events[-1]['tokens']['jev_in'] == 200  # the planner's safety check + the one route


async def test_speculative_route_is_dropped_when_the_plan_splits():
    engine = ScriptEngine(plan={'subtasks': [{'text': 'hey', 'depends_on': []}, {'text': 'hello again', 'depends_on': []}]})
    jev = FakeJev(route_for=lambda t: ('chat', 0.9))
    router = Router(jev, engine=engine, registry={'chat': fake_agent('hi')})
    await run(router, 'good morning, and hello again')
    routed_texts = [c[0] for c in jev.calls if 'route' in c[1]]
    assert routed_texts == ['good morning, and hello again', 'hey', 'hello again']  # the speculative route, then one per step


async def test_speculative_route_is_not_reused_for_a_rewritten_step():
    engine = ScriptEngine(plan={'subtasks': [{'text': 'convert 100 USD to GBP', 'depends_on': []}]})
    jev = FakeJev(route_for=lambda t: ('currency', 0.9))
    router = Router(jev, engine=engine, registry={'currency': fake_agent('100 USD = 79 GBP')})
    await run(router, 'and 100 dollars, in pounds too?')
    assert [c[0] for c in jev.calls if 'route' in c[1]] == ['and 100 dollars, in pounds too?', 'convert 100 USD to GBP']


def fake_agent(answer):
    async def agent(text, emit):
        emit(answer)
        return AgentResult(answer, True)
    return agent


# ---------- A3: stream sooner ----------

async def test_fast_step_answers_while_a_slow_one_still_runs():
    async def slow_weather(text, emit):
        await asyncio.sleep(0.2)
        emit('Paris: 18°C')
        return AgentResult('Paris: 18°C', True)
    router = Router(FakeJev(route_for=by_keyword, multi=0.9),
                    registry={**fake_registry(), 'weather': slow_weather})
    events = []
    router.bus.taps.append(events.append)
    task = asyncio.create_task(router.handle('weather in Paris and convert 100 EUR to INR', 'you'))
    await asyncio.sleep(0.1)
    assert [e['tid'] for e in events if e['type'] == 'answered'] == ['1.2']  # currency answered, weather still running
    await task
    t = events[-1]['timings']
    assert t['first_token_ms'] < 100 <= events[-1]['total_ms']


async def test_a_step_waits_only_for_its_own_dependencies():
    """Step 3 depends on step 1 only: it runs while step 2 (slow) is still going."""
    order = []

    def agent(name, delay=0.0):
        async def run_(text, emit):
            await asyncio.sleep(delay)
            order.append(name)
            return AgentResult(name, True)
        return run_
    three = {'subtasks': [{'text': 'weather in Paris', 'depends_on': []}, {'text': 'convert 100 EUR to INR', 'depends_on': []},
                          {'text': 'and the time there', 'depends_on': [0]}]}
    router = Router(FakeJev(route_for=lambda t: ('time', 0.9) if 'time' in t else by_keyword(t)),
                    engine=ScriptEngine(plan=three), registry={'weather': agent('weather'), 'currency': agent('currency', 0.2),
                                                               'time': agent('time')})
    await run(router, 'weather in Paris, and convert 100 EUR to INR, and then the time there', )
    assert order == ['weather', 'time', 'currency']


# ---------- A4: caches ----------

def test_ttl_cache_expires_and_evicts_least_recently_used():
    now = [0.0]
    c = TTLCache(2, 10, clock=lambda: now[0])
    c.put('a', 1)
    c.put('b', 2)
    assert c.get('a') == 1  # a is now the most recently used
    c.put('c', 3)
    assert c.get('b') is None and c.get('a') == 1 and c.get('c') == 3 and len(c) == 2
    now[0] = 10.5
    assert c.get('a') is None and len(c) == 1
    c.put('d', 4, ttl=1)
    now[0] = 11.6
    assert c.get('d') is None


def test_route_cache_key_follows_text_and_criteria():
    rc = RouteCache()
    agents = {'weather': 'Weather', 'chat': 'Chat'}
    assert rc.key('  Weather in   PARIS ', agents) == rc.key('weather in paris', agents)
    assert rc.key('weather in paris', agents) != rc.key('weather in paris', {**agents, 'poet': 'Poems'})
    with_examples = {'weather': {'description': 'Weather', 'examples': ['is it hot in Rome'], 'not': []}, 'chat': 'Chat'}
    assert rc.key('weather in paris', agents) != rc.key('weather in paris', with_examples)


async def test_route_cache_hits_and_marks_the_decision():
    jev = FakeJev(route_for=by_keyword)
    router = Router(jev, registry=fake_registry())
    await run(router, 'weather in Paris')
    events = await run(router, 'Weather in  Paris')
    routed = next(e for e in events if e['type'] == 'routed')
    assert routed['cached'] is True and routed['jev_ms'] == 0 and routed['agent'] == 'weather'
    assert len([c for c in jev.calls if 'route' in c[1]]) == 1
    assert events[-1]['timings']['cache_hits'] == 1 and events[-1]['tokens']['jev_in'] == 0
    first = next(e for e in (await run(Router(FakeJev(route_for=by_keyword), registry=fake_registry()), 'weather in Paris'))
                 if e['type'] == 'routed')
    assert 'cached' not in first  # only a cached decision says so


async def test_gates_still_run_on_a_cached_decision():
    """The cache holds Jev's raw decision; the checks after it run on every route."""
    router = Router(FakeJev(route_for=lambda t: ('weather', 0.9)), registry=fake_registry())
    await run(router, 'weather please')
    events = await run(router, 'weather please')
    routed = next(e for e in events if e['type'] == 'routed')
    assert routed['cached'] and routed['agent'] == 'clarify'  # "missing detail for weather", as the first time


@pytest.mark.parametrize('change', ['labels', 'agents', 'examples', 'engine'])
async def test_route_cache_is_cleared_when_routing_inputs_change(change):
    router = Router(FakeJev(route_for=by_keyword), registry=fake_registry(), store=Store())
    await run(router, 'weather in Paris')
    assert len(router.route_cache) == 1
    if change == 'labels':
        router.refresh_labels()
    elif change == 'agents':
        router.store.add_agent({'name': 'poet', 'description': 'Writes poems on request', 'prompt': 'Write poems.', 'web': False})
        router.reload_customs()
    elif change == 'examples':
        router.set_route_examples(True)
    else:
        router.use_engine(None)
    assert len(router.route_cache) == 0


async def test_eval_runs_bypass_the_route_cache():
    jev = FakeJev(route_for=by_keyword)
    router = Router(jev, registry=fake_registry())
    for _ in range(2):
        await router.handle('weather in Paris', 'eval')
    assert len([c for c in jev.calls if 'route' in c[1]]) == 2 and len(router.route_cache) == 0


async def test_live_data_is_cached_with_ttl(monkeypatch):
    calls = []

    async def fake_get_json(http, url, **params):
        calls.append(url)
        if 'currencies' in url:
            return {'EUR': 'Euro', 'INR': 'Indian Rupee'}
        return {'rates': {'INR': 9000.0}, 'date': '2026-09-27'}
    monkeypatch.setattr(tools, 'get_json', fake_get_json)
    with track() as hits:
        a = await tools.agent_currency('convert 100 EUR to INR', None)
    assert hits == [0] and len(calls) == 2
    with track() as hits:
        b = await tools.agent_currency('convert 100 EUR to INR', None)
    assert a.answer == b.answer and len(calls) == 2 and hits == [2]  # the currency list and the rate
    key = cache.live_key('https://api.frankfurter.app/latest', {'amount': 100.0, 'from': 'EUR', 'to': 'INR'})
    expires, _ = cache.LIVE.data[key]
    assert expires - cache.LIVE.clock() == pytest.approx(cache.RATES_TTL, abs=5)
    cache.LIVE.data[key] = (0, cache.LIVE.data[key][1])  # the rate expires; the currency list (24 h) doesn't
    await tools.agent_currency('convert 100 EUR to INR', None)
    assert calls[2:] == ['https://api.frankfurter.app/latest']


async def test_failed_or_empty_lookups_are_not_cached(monkeypatch):
    replies = [{}, {'AbstractText': 'A mathematician.', 'AbstractURL': 'https://en.wikipedia.org/wiki/Ada_Lovelace'}]

    async def fake_get_json(http, url, **params):
        if 'wikipedia' in url:
            raise OSError('Wikipedia unreachable too')
        return replies.pop(0)
    monkeypatch.setattr(tools, 'get_json', fake_get_json)
    assert not (await tools.agent_knowledge('Who was Ada Lovelace?', None)).ok  # throttled: an empty answer
    assert (await tools.agent_knowledge('Who was Ada Lovelace?', None)).ok
    assert (await tools.agent_knowledge('Who was Ada Lovelace?', None)).ok and not replies  # from the cache


async def test_cached_lookup_is_marked_on_the_answer_and_counted(monkeypatch):
    async def fake_get_json(http, url, **params):
        return {'AbstractText': 'A mathematician.', 'AbstractURL': 'https://en.wikipedia.org/wiki/Ada_Lovelace'}
    monkeypatch.setattr(tools, 'get_json', fake_get_json)
    router = Router(FakeJev(route_for=lambda t: ('knowledge', 0.9)), registry={'knowledge': tools_agent(tools.agent_knowledge)})
    first = await run(router, 'Who was Ada Lovelace?')
    assert 'checks' not in next(e for e in first if e['type'] == 'answered')
    second = await run(router, 'Who was Ada Lovelace?')
    answered = next(e for e in second if e['type'] == 'answered')
    assert answered['checks'] == {'cached': True} and second[-1]['timings']['cache_hits'] == 2  # route + lookup


def tools_agent(fn):
    async def agent(text, emit):
        out = await fn(text, None)
        emit(out.answer)
        return out
    return agent


async def test_llm_answers_are_never_cached():
    """The route is cached, the LLM agent's answer is not: the engine answers the same question every time."""
    engine = ScriptEngine()
    router = Router(FakeJev(route_for=lambda t: ('chat', 0.9)), engine=engine)
    for _ in range(2):
        events = await run(router, 'tell me a joke')
    assert len(llm_calls(engine, 'agent')) == 2 and next(e for e in events if e['type'] == 'routed')['cached']
    assert next(e for e in events if e['type'] == 'answered')['checks'] == {'effort': 'low'}  # no cached flag


async def test_multi_score_is_cached_by_query_text():
    jev = FakeJev(route_for=by_keyword, multi=0.9)
    router = Router(jev, registry=fake_registry())
    await run(router, 'weather in Paris and convert 100 EUR to INR')
    events = await run(router, 'Weather in Paris and convert 100 EUR to INR')
    assert [c[1] for c in jev.calls].count(['multi']) == 1
    assert events[1]['multi'] == 0.9 and events[-1]['timings']['cache_hits'] == 3  # the multi score and both routes
    assert events[-1]['tokens']['jev_in'] == 0
    for _ in range(2):
        await router.handle('weather in Paris and convert 100 EUR to INR', 'eval')
    assert [c[1] for c in jev.calls].count(['multi']) == 3  # evals always ask Jev


async def test_knowledge_falls_back_to_wikipedia_when_duckduckgo_fails(monkeypatch):
    seen = []

    async def fake_get_json(http, url, **params):
        seen.append(url)
        if 'duckduckgo' in url:
            raise TimeoutError  # throttled network
        if 'api.php' in url:
            assert params['srsearch'] == 'Ada Lovelace'
            return {'query': {'search': [{'title': 'Ada Lovelace'}]}}
        assert url.endswith('/page/summary/Ada_Lovelace')
        return {'type': 'standard', 'extract': 'Ada Lovelace was a mathematician. She wrote the first program.',
                'content_urls': {'desktop': {'page': 'https://en.wikipedia.org/wiki/Ada_Lovelace'}}}
    monkeypatch.setattr(tools, 'get_json', fake_get_json)
    r = await tools.agent_knowledge('Who was Ada Lovelace?', None)
    assert r.ok and r.answer.startswith('Ada Lovelace was a mathematician') and r.source.endswith('/Ada_Lovelace')
    assert len(seen) == 3


async def test_knowledge_wikipedia_fallback_skips_disambiguation(monkeypatch):
    async def fake_get_json(http, url, **params):
        if 'duckduckgo' in url:
            return {}
        if 'api.php' in url:
            return {'query': {'search': [{'title': 'Mercury'}]}}
        return {'type': 'disambiguation', 'extract': 'Mercury may refer to:'}
    monkeypatch.setattr(tools, 'get_json', fake_get_json)
    assert not (await tools.agent_knowledge('Mercury', None)).ok


def test_a_long_document_run_gets_the_long_deadline():
    from jevrouter.pipeline import Router
    r = Router(FakeJev())
    engine = object()
    assert r.deadline('Make a 12-13 page PDF on the history of AI with diagrams', engine) == r.long_run_timeout
    assert r.deadline('Make a 12-13 page PDF on the history of AI with diagrams', None) == r.run_timeout  # keyless
    assert r.deadline('What time is it in Tokyo?', engine) == r.run_timeout
    assert r.long_run_timeout >= r.run_timeout

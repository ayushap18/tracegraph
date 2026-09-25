import asyncio

from jevrouter.agents import AgentResult
from jevrouter.pipeline import Router
from tests.fakes import FakeAnthropic, FakeJev

FIELDS = {
    'query': {'type', 'qid', 'text', 'source'},
    'plan': {'type', 'qid', 'planner', 'subtasks', 'multi', 'ms'},
    'routed': {'type', 'qid', 'tid', 'agent', 'pick', 'reason', 'probabilities', 'confidence', 'urgency', 'unsafe', 'clear',
               'jev_ms', 'model'},
    'delta': {'type', 'qid', 'tid', 'text'},
    'answered': {'type', 'qid', 'tid', 'agent', 'agent_ms', 'answer', 'ok', 'source', 'engine'},
    'merged': {'type', 'qid', 'answer', 'engine', 'ms'},
    'done': {'type', 'qid', 'total_ms', 'stats'},
    'error': {'type', 'qid', 'tid', 'message'},
}
STATS = {'queries', 'subtasks', 'errors', 'jev_input_tokens', 'claude_input_tokens', 'claude_output_tokens', 'by_agent'}


def by_keyword(text):
    for word, agent in (('weather', 'weather'), ('EUR', 'currency'), ('%', 'math'), ('Ada', 'knowledge'), ('news', 'research')):
        if word in text:
            return agent, 0.9
    return 'chat', 0.3  # below MIN_CONFIDENCE -> clarify


def fake_registry(**extra):
    async def weather(text, emit):
        await asyncio.sleep(0.01)
        emit('Paris: now 18°C, ')
        emit('clear sky')
        return AgentResult('Paris: now 18°C, clear sky', True, 'open-meteo.com')

    async def currency(text, emit):
        emit('100.00 EUR = 9,000.00 INR')
        return AgentResult('100.00 EUR = 9,000.00 INR', True, 'frankfurter.app')

    async def boom(text, emit):
        raise RuntimeError('api exploded')
    return {'weather': weather, 'currency': currency, 'math': boom, **extra}




async def run(router, query):
    events = []
    router.bus.taps.append(events.append)
    await router.handle(query, 'you')
    return events


def check_fields(events):
    for e in events:
        assert set(e) == FIELDS[e['type']], e


async def test_two_subtask_event_sequence():
    router = Router(FakeJev(route_for=by_keyword, multi=0.9), registry=fake_registry())
    events = await run(router, 'weather in Paris and convert 100 EUR to INR')
    check_fields(events)
    types = [e['type'] for e in events]
    assert types[0] == 'query' and types[1] == 'plan' and types[-1] == 'done' and types[-2] == 'merged'
    assert types.count('routed') == 2 and types.count('answered') == 2
    # route all, then run all
    assert max(i for i, t in enumerate(types) if t == 'routed') < min(i for i, t in enumerate(types) if t in ('answered', 'delta'))

    q, p = events[0], events[1]
    assert q == {'type': 'query', 'qid': 1, 'text': 'weather in Paris and convert 100 EUR to INR', 'source': 'you'}
    assert p['planner'] == 'heuristic' and p['multi'] == 0.9
    assert p['subtasks'] == [{'tid': '1.1', 'text': 'weather in Paris'}, {'tid': '1.2', 'text': 'convert 100 EUR to INR'}]

    routed = {e['tid']: e for e in events if e['type'] == 'routed'}
    answered = {e['tid']: e for e in events if e['type'] == 'answered'}
    assert routed['1.1']['agent'] == 'weather' and routed['1.2']['agent'] == 'currency'
    assert list(routed['1.1']['probabilities'].values()) == sorted(routed['1.1']['probabilities'].values(), reverse=True)
    assert 'research' not in routed['1.1']['probabilities']
    assert answered['1.1']['ok'] and answered['1.1']['engine'] == 'keyless' and answered['1.1']['source'] == 'open-meteo.com'
    deltas = ''.join(e['text'] for e in events if e['type'] == 'delta' and e['tid'] == '1.1')
    assert deltas == answered['1.1']['answer']
    for tid in routed:
        assert events.index(routed[tid]) < events.index(answered[tid])

    merged = events[-2]
    assert merged['engine'] == 'concat'
    assert merged['answer'] == '**weather**: Paris: now 18°C, clear sky\n\n**currency**: 100.00 EUR = 9,000.00 INR'
    done = events[-1]
    assert set(done['stats']) == STATS
    assert done['stats']['queries'] == 1 and done['stats']['subtasks'] == 2
    assert done['stats']['by_agent']['weather'] == 1 and done['stats']['by_agent']['currency'] == 1
    assert done['stats']['jev_input_tokens'] == 300  # multi + 2 routes

    rec = router.history[-1]
    assert set(rec) == {'qid', 'text', 'source', 'at', 'plan', 'tasks', 'merged', 'total_ms', 'error'}
    assert rec['error'] is None and not router.inflight
    assert rec['plan']['planner'] == 'heuristic' and len(rec['tasks']) == 2
    assert rec['tasks'][0]['agent'] == 'weather' and rec['tasks'][0]['answer'] and rec['tasks'][0]['probabilities']
    assert rec['merged'] == {'answer': merged['answer'], 'engine': 'concat'}


async def test_single_subtask_passes_through():
    events = await run(Router(FakeJev(route_for=by_keyword), registry=fake_registry()), 'weather in Paris')
    check_fields(events)
    assert events[1]['multi'] is None and len(events[1]['subtasks']) == 1
    assert events[-2]['engine'] == 'single' and events[-2]['answer'] == 'Paris: now 18°C, clear sky'
    assert not [e for e in events if e['type'] == 'delta' and e['tid'] == 'merge']


async def test_guards_and_agent_failure():
    router = Router(FakeJev(route_for=by_keyword), registry=fake_registry())
    events = await run(router, 'hmm')
    check_fields(events)
    a = next(e for e in events if e['type'] == 'answered')
    assert a['agent'] == 'clarify' and not a['ok']
    assert router.stats['by_agent']['clarify'] == 1

    events = await run(Router(FakeJev(route_for=by_keyword, unsafe=0.95), registry=fake_registry()), 'weather in Paris')
    assert next(e for e in events if e['type'] == 'answered')['agent'] == 'blocked'

    events = await run(Router(FakeJev(route_for=by_keyword), registry=fake_registry()), '18% of 2450')
    a = next(e for e in events if e['type'] == 'answered')
    assert a['agent'] == 'math' and not a['ok'] and 'api exploded' in a['answer'] and events[-1]['type'] == 'done'


async def test_route_failure_for_one_subtask():
    router = Router(FakeJev(route_for=by_keyword, fail_on=('EUR to',)), registry=fake_registry())
    events = await run(router, 'weather in Paris and convert 100 EUR to INR')
    check_fields(events)
    err = next(e for e in events if e['type'] == 'error')
    assert err['tid'] == '1.2' and err['qid'] == 1 and 'Jev' in err['message']
    assert [e['tid'] for e in events if e['type'] == 'answered'] == ['1.1']
    assert events[-2]['engine'] == 'single' and events[-1]['type'] == 'done' and router.stats['errors'] == 1
    # the replayed record carries the per-task error, like the live stream did
    tasks = {t['tid']: t for t in router.history[-1]['tasks']}
    assert tasks['1.2']['error'] == err['message'] and 'agent' not in tasks['1.2'] and 'error' not in tasks['1.1']


async def test_everything_fails_still_ends_with_done():
    router = Router(FakeJev(fail_on=('',)), registry=fake_registry())
    events = await run(router, 'hmm')
    check_fields(events)
    assert [e['type'] for e in events] == ['query', 'plan', 'error', 'error', 'done']
    rec = router.history[-1]
    assert rec['merged'] is None and rec['error'] == events[-2]['message']  # no `merged` was sent live, so none is replayed


async def test_hello_history_is_qid_ordered_and_includes_inflight():
    router = Router(FakeJev(route_for=by_keyword, delay=0.05), registry=fake_registry())
    slow = asyncio.create_task(router.handle('weather in Paris and convert 100 EUR to INR', 'you'))  # qid 1
    await asyncio.sleep(0.02)
    hist = router.hello()['history']
    assert [r['qid'] for r in hist] == [1] and hist[0]['total_ms'] is None and hist[0]['text'].startswith('weather')
    router.jev.delay = 0
    await router.handle('hmm', 'you')  # qid 2 finishes first
    await slow
    assert [r['qid'] for r in router.history] == [2, 1]
    hist = router.hello()['history']
    assert [r['qid'] for r in hist] == [1, 2] and all(r['total_ms'] is not None for r in hist) and not router.inflight


def test_clarify_reason_names_the_condition_that_fired():
    from types import SimpleNamespace as NS
    from jevrouter.jev import decide
    assert decide(NS(choice='chat', confidence=1.0), 0.0, 0.12) == ('clarify', 'unclear (12%)')
    assert decide(NS(choice='chat', confidence=0.2), 0.0, 0.9) == ('clarify', 'low confidence (20%)')


async def test_pipeline_with_claude():
    # planner, research agent (streamed), merger all from Claude; weather stays keyless
    claude = FakeAnthropic(['{"subtasks": ["weather in Paris", "latest news on Mars rovers"]}'],
                           ['Perseverance ', 'found ', 'rocks.'], ['Paris is mild; ', 'Perseverance found rocks.'])
    from jevrouter import agents
    registry = agents.build(None, claude)
    registry['weather'] = fake_registry()['weather']
    router = Router(FakeJev(route_for=by_keyword), claude=claude, registry=registry)
    assert 'research' in router.agents and 'research' in router.stats['by_agent']
    events = await run(router, 'weather in Paris and latest news on Mars rovers')
    check_fields(events)
    assert events[1]['planner'] == 'claude' and events[1]['multi'] is None
    routed = {e['tid']: e for e in events if e['type'] == 'routed'}
    assert routed['1.2']['agent'] == 'research' and 'research' in routed['1.2']['probabilities']
    ans = {e['tid']: e for e in events if e['type'] == 'answered'}
    assert ans['1.2']['engine'] == 'claude' and ans['1.2']['answer'] == 'Perseverance found rocks.'
    assert [e['text'] for e in events if e['type'] == 'delta' and e['tid'] == '1.2'] == ['Perseverance ', 'found ', 'rocks.']
    assert [e['text'] for e in events if e['type'] == 'delta' and e['tid'] == 'merge'] == ['Paris is mild; ', 'Perseverance found rocks.']
    merged = events[-2]
    assert merged['engine'] == 'claude' and merged['answer'] == 'Paris is mild; Perseverance found rocks.'
    s = events[-1]['stats']
    assert s['claude_input_tokens'] == 30 and s['claude_output_tokens'] == 1 + 3 + 2
    assert claude.calls[1]['tools'][0]['type'] == 'web_search_20260209' and claude.calls[1]['output_config']['effort'] == 'medium'
    assert router.config()['claude'] is True

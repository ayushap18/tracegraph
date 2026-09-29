import asyncio

from jevrouter.agents import AgentResult
from jevrouter.pipeline import Router
from tests.fakes import FakeLLM, FakeJev, eng

FIELDS = {
    'query': {'type', 'qid', 'text', 'source', 'session_id', 'compare_id', 'engine', 'files'},  # v4 adds the last four
    'plan': {'type', 'qid', 'planner', 'subtasks', 'multi', 'ms'},
    'routed': {'type', 'qid', 'tid', 'agent', 'pick', 'reason', 'probabilities', 'confidence', 'urgency', 'unsafe', 'clear',
               'jev_ms', 'model', 'examples'},  # the learning plan adds examples
    'delta': {'type', 'qid', 'tid', 'text'},
    'answered': {'type', 'qid', 'tid', 'agent', 'agent_ms', 'answer', 'ok', 'source', 'engine'},
    'merged': {'type', 'qid', 'answer', 'engine', 'ms'},
    'done': {'type', 'qid', 'total_ms', 'stats', 'status', 'tokens', 'timings'},  # v4 adds status and tokens, speed plan timings
    'error': {'type', 'qid', 'tid', 'message'},
}
# Fields an event may carry on top of FIELDS, only when they have something to say (older clients ignore them):
# docs/PLAN-accuracy-v2.md adds the policy trace, Jev's signals, the @agent binding, a stated assumption and the frame
# a keyless follow-up was completed from to `routed`; caveats and the frame a follow-up can build on to `answered`; and
# what the run couldn't do and its primary file to `merged`. docs/PLAN-files-robust.md adds a create step's phases,
# tokens and checkpoint summary to `answered` (H3), and estimated vs used and a failed file step to `done` (H4, 5.4).
OPTIONAL = {
    'routed': {'input', 'cached', 'forced', 'bound', 'trace', 'signals', 'assumption', 'frame_used'},
    'answered': {'created_files', 'checks', 'caveats', 'frame', 'phases', 'llm_in', 'llm_out', 'checkpoint'},
    'merged': {'caveats', 'primary_file'},
    'done': {'cost', 'file_failed', 'checkpoints'},  # checkpoints: Resume for a timed-out or cancelled file step
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
        assert FIELDS[e['type']] <= set(e) <= FIELDS[e['type']] | OPTIONAL.get(e['type'], set()), e
        if e['type'] == 'routed':  # the decision policy's trace ends on the step's agent (docs/PLAN-accuracy-v2.md A2)
            assert e['trace'] and e['trace'][-1]['agent'] == e['agent'], e


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
    assert q == {'type': 'query', 'qid': 1, 'text': 'weather in Paris and convert 100 EUR to INR', 'source': 'you',
                 'session_id': None, 'compare_id': None, 'engine': None, 'files': []}
    assert p['planner'] == 'heuristic' and p['multi'] == 0.9
    assert p['subtasks'] == [{'tid': '1.1', 'text': 'weather in Paris', 'depends_on': []},
                             {'tid': '1.2', 'text': 'convert 100 EUR to INR', 'depends_on': []}]

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
    assert 'Paris: now 18°C, clear sky' in merged['answer'] and '100.00 EUR = 9,000.00 INR' in merged['answer']
    assert merged['answer'].index('Paris') < merged['answer'].index('9,000.00 INR')
    done = events[-1]
    assert set(done['stats']) == STATS
    assert done['stats']['queries'] == 1 and done['stats']['subtasks'] == 2
    assert done['stats']['by_agent']['weather'] == 1 and done['stats']['by_agent']['currency'] == 1
    assert done['stats']['jev_input_tokens'] == 300  # multi + 2 routes

    rec = router.history[-1]
    assert set(rec) == {'qid', 'text', 'source', 'at', 'plan', 'tasks', 'merged', 'total_ms', 'error',
                        'status', 'engine', 'session_id', 'compare_id', 'files', 'tokens',  # v4 fields
                        'mode', 'style', 'agent', 'group_id', 'chosen', 'timings',  # speed plan and chat variety
                        'suspects'}  # accuracy v2 (D6)
    assert rec['suspects'] == []
    assert rec['status'] == 'done' and done['status'] == 'done' and done['tokens'] == rec['tokens'] == {'jev_in': 300, 'llm_in': 0, 'llm_out': 0}
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
    assert decide(NS(choice='knowledge', confidence=1.0), 0.0, 0.12) == ('clarify', 'unclear (12%)')
    assert decide(NS(choice='chat', confidence=0.2), 0.0, 0.9) == ('clarify', 'low confidence (20%)')
    # small talk needs no more detail: a chat pick is exempt from the clarity veto (docs/PLAN-accuracy-v2.md A6)
    assert decide(NS(choice='chat', confidence=1.0), 0.0, 0.12) == ('chat', 'chat at 100%')


async def test_pipeline_with_llm_engine():
    # planner, research agent (streamed), merger all from Claude; weather stays keyless
    claude = FakeLLM(['{"subtasks": ["weather in Paris", "latest news on Mars rovers"]}'],
                           ['Perseverance ', 'found ', 'rocks.'], ['Paris is mild; ', 'Perseverance found rocks.'])
    from jevrouter import agents
    engine = eng(claude)
    registry = agents.build(None, engine)
    registry['weather'] = fake_registry()['weather']
    # Jev unsure the query holds two requests: the LLM plans it (a sure split would skip the LLM planner, test_speed.py)
    router = Router(FakeJev(route_for=by_keyword, multi=0.6), engine=engine, registry=registry)
    assert 'research' in router.agents and 'research' in router.stats['by_agent']
    events = await run(router, 'weather in Paris and latest news on Mars rovers')
    check_fields(events)
    assert events[1]['planner'] == 'api' and events[1]['multi'] is None
    routed = {e['tid']: e for e in events if e['type'] == 'routed'}
    assert routed['1.2']['agent'] == 'research' and 'research' in routed['1.2']['probabilities']
    ans = {e['tid']: e for e in events if e['type'] == 'answered'}
    assert ans['1.2']['engine'] == 'api' and ans['1.2']['answer'] == 'Perseverance found rocks.'
    assert [e['text'] for e in events if e['type'] == 'delta' and e['tid'] == '1.2'] == ['Perseverance ', 'found ', 'rocks.']
    assert [e['text'] for e in events if e['type'] == 'delta' and e['tid'] == 'merge'] == ['Paris is mild; ', 'Perseverance found rocks.']
    merged = events[-2]
    assert merged['engine'] == 'api' and merged['answer'] == 'Paris is mild; Perseverance found rocks.'
    s = events[-1]['stats']
    assert s['claude_input_tokens'] == 30 and s['claude_output_tokens'] == 1 + 3 + 2
    assert claude.calls[1]['plugins'] == [{'id': 'web'}] and claude.calls[1]['reasoning_effort'] == 'medium'
    assert router.config()['claude'] is True


# ---------- docs/PLAN-files-robust.md: pipeline hooks H2 to H8 (builder E) ----------

STATE = {'v': 1, 'kind': 'longdoc', 'format': 'pptx', 'request': 'a 12 slide deck on phones', 'ctx': 'notes',
         'parts': [{'heading': f'Part {i}', 'level': 1, 'words': 45, 'hints': [], 'diagrams': [], 'figures': 0}
                   for i in range(3)],
         'written': {'0': {'heading': 'Part 0', 'level': 1, 'blocks': [{'type': 'paragraph', 'text': 'x'}], 'notes': ''}},
         'failed': [], 'phase': 'sections', 'engine': 'claude-code', 'tokens_in': 900, 'tokens_out': 300, 'calls': 2,
         'at': 1.0, 'file_id': None}
PHASES = [{'phase': 'outline', 'calls': 1, 'llm_in': 400, 'llm_out': 100, 'ms': 5},
          {'phase': 'sections', 'calls': 1, 'llm_in': 500, 'llm_out': 200, 'ms': 7}]


def file_route(text):
    if 'deck' in text or 'slides' in text:
        return 'create', 0.9
    return by_keyword(text)


def failing_make(seen: list, *, state=STATE, wait: float = 0.0):
    """A create agent that spends tokens, saves a checkpoint, then (optionally after `wait` seconds) makes no file."""
    from jevrouter.agents import create as create_agent

    async def make(job, engine, jev, mode='balanced'):
        seen.append(job)
        job.checkpoint(dict(state))
        if wait:
            await asyncio.sleep(wait)
        return create_agent.Made('No file was made: the engine failed (stream was interrupted).', False, 'claude-code',
                                 900, 300, phases=list(PHASES))
    return make


async def test_a_failed_create_keeps_its_phases_and_tokens_marks_file_failed_and_merges_by_template(monkeypatch):
    from jevrouter import pipeline
    from jevrouter.agents import create as create_agent
    from tests.fakes import ScriptEngine
    seen = []
    monkeypatch.setattr(create_agent, 'make', failing_make(seen))
    engine = ScriptEngine(plan={'subtasks': [{'text': 'Who was Ada Lovelace', 'depends_on': []},
                                             {'text': 'make a 12 slide deck about her', 'depends_on': [0]}]})
    registry = fake_registry(knowledge=knowledge_agent)
    router = Router(FakeJev(route_for=file_route, multi=0.6), engine=engine, registry=registry, engines={'claude-code': engine})
    composed = []
    real = pipeline.compose

    async def compose(query, steps, emit, merge_engine, **kw):
        composed.append(merge_engine)
        return await real(query, steps, emit, merge_engine, **kw)
    monkeypatch.setattr(pipeline, 'compose', compose)
    events = await run(router, 'Who was Ada Lovelace and make a 12 slide deck about her')
    check_fields(events)
    create = next(e for e in events if e['type'] == 'answered' and e['agent'] == 'create')
    assert not create['ok'] and 'created_files' not in create
    assert create['phases'] == PHASES and (create['llm_in'], create['llm_out']) == (900, 300)
    assert create['checkpoint']['resumable'] and create['checkpoint']['missing'] == ['Part 1', 'Part 2']
    done = events[-1]
    assert done['file_failed'] is True and done['tokens']['llm_in'] >= 900
    assert composed == [None]  # the template joined the answers: no LLM call to wrap a failure
    rec = router.store.get_run(done['qid'])
    assert rec['file_failed'] and rec['timings']['merger'] == 'template'
    assert rec['checkpoints_state'][create['tid']]['parts'] == STATE['parts']
    task = next(t for t in rec['tasks'] if t['agent'] == 'create')
    assert task['phases'] == PHASES and task['llm_in'] == 900
    job = seen[0]
    assert job.deadline is not None and job.deadline > 0 and job.tally is not None


async def knowledge_agent(text, emit):
    emit('Ada Lovelace was a mathematician.')
    return AgentResult('Ada Lovelace was a mathematician.', True, 'wikipedia.org', 'claude-code', 10, 5)


async def test_checkpoints_are_saved_on_timeout_and_never_leave_the_server(monkeypatch):
    from jevrouter.agents import create as create_agent
    from jevrouter.pipeline import public_run
    from tests.fakes import ScriptEngine
    seen = []
    monkeypatch.setattr(create_agent, 'make', failing_make(seen, wait=5))
    engine = ScriptEngine()
    router = Router(FakeJev(route_for=file_route), engine=engine, registry=fake_registry(), engines={'claude-code': engine})
    router.run_timeout = router.long_run_timeout = 0.3
    events = await run(router, 'make slides about tea')
    assert events[-1]['status'] == 'timeout' and 'cost' not in events[-1]
    qid = events[-1]['qid']
    stored = router.store.get_run(qid)
    assert stored['status'] == 'timeout' and stored['checkpoints_state']['%d.1' % qid]['written']
    shown = public_run(stored)
    assert 'checkpoints_state' not in shown and shown['checkpoints'][0]['tid'] == '%d.1' % qid
    assert shown['checkpoints'][0]['resumable'] and shown['checkpoints'][0]['planned'] == 3
    hello = router.hello()
    assert all('checkpoints_state' not in r for r in hello['history'])
    assert any(r.get('checkpoints') for r in hello['history'])
    assert 'checkpoints_state' in stored  # the full state stays in the store for Resume


async def test_the_run_estimate_is_kept_and_actual_filled_in():
    router = Router(FakeJev(route_for=by_keyword, multi=0.9), registry=fake_registry())
    events = []
    router.bus.taps.append(events.append)
    estimate = {'version': 1, 'calls': 0, 'tokens_in': 0}
    await router.handle('weather in Paris', 'you', extras={'estimate': estimate})
    check_fields(events)
    done = events[-1]
    assert done['cost']['estimate'] == estimate
    assert done['cost']['actual'] == {'calls': 0, 'tokens_in': 0, 'tokens_out': 0, 'seconds': round(done['total_ms'] / 1000, 1)}
    assert router.store.get_run(done['qid'])['cost'] == done['cost']


async def test_a_resume_run_skips_the_planner_and_routing(monkeypatch):
    from jevrouter.agents import create as create_agent
    from tests.fakes import ScriptEngine
    seen = []

    async def make(job, engine, jev, mode='balanced'):
        seen.append(job)
        return create_agent.Made('Created **x.pptx**, 4 slides', True, 'claude-code', 50, 20)
    monkeypatch.setattr(create_agent, 'make', make)
    engine = ScriptEngine()
    jev = FakeJev(route_for=file_route)
    router = Router(jev, engine=engine, registry=fake_registry(), engines={'claude-code': engine})
    events = []
    router.bus.taps.append(events.append)
    await router.handle('the original question', 'chat', agent='create',
                        extras={'resume': {'qid': 7, 'tid': '7.2', 'state': STATE}})
    check_fields(events)
    assert engine.calls == [] and jev.calls == []  # no planner call, no route question
    routed = next(e for e in events if e['type'] == 'routed')
    assert routed['agent'] == 'create' and routed['reason'] == 'resume a partial file'
    assert seen[0].resume == {'qid': 7, 'tid': '7.2', 'state': STATE} and seen[0].request == 'the original question'
    plan = next(e for e in events if e['type'] == 'plan')
    assert [s['text'] for s in plan['subtasks']] == [STATE['request']]


async def test_partial_files_get_their_resume_reference_and_cost(monkeypatch):
    from jevrouter.agents import create as create_agent
    from tests.fakes import ScriptEngine
    from tests.test_create_api import SPEC

    async def make(job, engine, jev, mode='balanced'):
        job.checkpoint(dict(STATE))
        meta, spec, data = create_agent.build(SPEC, 'pdf', source='llm')
        meta['partial'] = {'planned': 3, 'written': 1, 'missing': ['Part 1', 'Part 2'], 'resume': None}
        return create_agent.Made('Created', True, 'claude-code', 900, 300, file=meta, spec=spec, data=data,
                                 phases=list(PHASES))
    monkeypatch.setattr(create_agent, 'make', make)
    engine = ScriptEngine()
    router = Router(FakeJev(route_for=file_route), engine=engine, registry=fake_registry(), engines={'claude-code': engine})
    events = []
    router.bus.taps.append(events.append)
    await router.handle('make slides about tea', 'chat', extras={'estimate': {'version': 1, 'calls': 3}})
    done = events[-1]
    f = next(e for e in events if e['type'] == 'answered')['created_files'][0]
    assert f['partial']['resume'] == {'qid': done['qid'], 'tid': f'{done["qid"]}.1', 'sandbox': None}
    assert f['cost']['estimate'] == {'version': 1, 'calls': 3}
    assert f['cost']['actual']['calls'] == 2 and f['cost']['actual']['tokens_in'] == 900
    assert router.store.get_created(f['id'])['partial']['resume']['tid'] == f'{done["qid"]}.1'
    state = router.store.get_run(done['qid'])['checkpoints_state'][f'{done["qid"]}.1']
    assert state['file_id'] == f['id']
    assert not done.get('file_failed')


async def test_tuned_prefer_steers_only_auto(monkeypatch):
    from jevrouter import agents
    from jevrouter.engines.auto import AutoEngine
    from tests.fakes import ScriptEngine
    a, b = ScriptEngine(name='agy', label='Antigravity'), ScriptEngine(name='claude-code', label='Claude Code')
    auto = AutoEngine({'agy': a, 'claude-code': b}, order=['agy', 'claude-code'])
    tuned = agents.Tuned(auto, prefer='claude-code')
    await tuned.stream(system='s', prompt='p')
    assert b.calls and not a.calls and tuned.billing == 'subscription'
    plain = agents.Tuned(a, prefer='claude-code')
    assert plain.prefer is None
    await plain.stream(system='s', prompt='p')
    assert len(a.calls) == 1


async def test_lean_long_files_prefer_the_cheaper_engine_on_auto(monkeypatch):
    from jevrouter.agents import create as create_agent
    from jevrouter.engines.auto import AutoEngine
    from tests.fakes import ScriptEngine
    a, b = ScriptEngine(name='agy', label='Antigravity'), ScriptEngine(name='claude-code', label='Claude Code')
    auto = AutoEngine({'agy': a, 'claude-code': b}, order=['agy', 'claude-code'])
    seen = []

    async def make(job, engine, jev, mode='balanced'):
        seen.append(engine)
        return create_agent.Made('No file was made: test.', False)
    monkeypatch.setattr(create_agent, 'make', make)
    router = Router(FakeJev(route_for=file_route), engine=auto, registry=fake_registry(),
                    engines={'auto': auto, 'agy': a, 'claude-code': b})
    await router.handle('make a 12 slide deck about tea', 'you')
    assert seen[-1].prefer == 'claude-code'
    await router.handle('make slides about tea', 'you')  # not a long file: Auto keeps its own order
    assert seen[-1].prefer is None
    monkeypatch.setenv('TG_LEAN_LONG_FILES', '0')
    await router.handle('make a 12 slide deck about tea', 'you')
    assert seen[-1].prefer is None
    monkeypatch.delenv('TG_LEAN_LONG_FILES')
    await router.handle('make a 12 slide deck about tea', 'you', engine=a)  # a pinned engine is never changed
    assert getattr(seen[-1], 'prefer', None) is None

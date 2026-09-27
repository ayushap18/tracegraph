"""v4 pipeline: dependency waves, run control (cancel, deadline), per-run engines, sessions, tokens, persistence."""
import asyncio
import json
import os

import pytest

from jevrouter import agents as agent_registry
from jevrouter.agents import AgentResult
from jevrouter.pipeline import Router
from jevrouter.planner import parse_steps, plan, prompt_for, worth_llm_plan
from jevrouter.store import Store
from tests.fakes import FakeJev, ScriptEngine
from tests.test_pipeline import by_keyword, check_fields, fake_registry, run


def recorder(answers: dict):
    """Agents that record the exact text they were given and answer from `answers`."""
    seen = {}

    def make(name):
        async def agent(text, emit):
            seen.setdefault(name, []).append(text)
            emit(answers[name])
            return AgentResult(answers[name], True)
        return agent
    return seen, {name: make(name) for name in answers}


def types_of(events):
    return [(e['type'], e.get('tid')) for e in events]


WORLD_CUP = {'subtasks': [{'text': 'Which country won the 2022 FIFA World Cup, and what is its capital?', 'depends_on': []},
                          {'text': 'What is the current time in that capital?', 'depends_on': [0]}]}


def cup_route(text):
    return ('time', 0.9) if 'current time' in text else ('knowledge', 0.9)


async def test_dependent_step_waits_and_gets_context():
    engine = ScriptEngine(plan=WORLD_CUP, rewrites={'that capital': 'What is the current time in Buenos Aires?'})
    seen, reg = recorder({'knowledge': 'Argentina won; its capital is Buenos Aires.', 'time': 'Buenos Aires: 9:00 PM'})
    jev = FakeJev(route_for=cup_route)
    router = Router(jev, engine=engine, registry=reg)
    events = await run(router, 'Find the country that won the 2022 FIFA World Cup, then tell me the current time in its capital')
    plan_ev = next(e for e in events if e['type'] == 'plan')
    assert [s['depends_on'] for s in plan_ev['subtasks']] == [[], ['1.1']]
    t = types_of(events)
    assert t.index(('answered', '1.1')) < t.index(('routed', '1.2'))  # wave 2 starts only after its dependency answered
    # Jev sees the rewritten text plus the context; the keyless time agent gets the self-contained text alone
    routed_text = jev.calls[-1][0]
    assert routed_text.startswith('What is the current time in Buenos Aires?\n\nContext from earlier steps:\n- Which country')
    assert 'Argentina won; its capital is Buenos Aires.' in routed_text
    assert seen['time'] == ['What is the current time in Buenos Aires?']
    routed2 = next(e for e in events if e['type'] == 'routed' and e['tid'] == '1.2')
    assert routed2['input'] == 'What is the current time in Buenos Aires?'
    assert router.history[-1]['tasks'][1]['depends_on'] == ['1.1'] and events[-1]['status'] == 'done'


async def test_independent_steps_share_a_wave_and_failed_dependency_is_noted():
    three = {'subtasks': [{'text': 'weather in Paris', 'depends_on': []}, {'text': 'convert 100 EUR to INR', 'depends_on': []},
                          {'text': 'which is better value', 'depends_on': [0, 1]}]}
    seen, reg = recorder({'chat': 'Paris wins.'})
    def route(text):  # Jev fails only for the currency step itself (its text also appears in step 3's context)
        if text.startswith('convert'):
            raise RuntimeError('jev down')
        return ('chat', 0.9) if 'better' in text else by_keyword(text)
    router = Router(FakeJev(route_for=route), engine=ScriptEngine(plan=three), registry={**fake_registry(), **reg})
    events = await run(router, 'weather in Paris and convert 100 EUR to INR, then which is better value')
    check_fields([e for e in events if e['type'] in ('query', 'plan', 'delta', 'answered', 'done', 'error')])
    t = types_of(events)
    # 1.1 and 1.2 route in the same first wave, before anything answers; 1.3 waits for both
    assert t.index(('error', '1.2')) < t.index(('answered', '1.1')) and t.index(('answered', '1.1')) < t.index(('routed', '1.3'))
    # the rewrite returned nothing, so the dependent step keeps its own text plus the context, including the failure
    text = seen['chat'][0]
    assert text.startswith('which is better value\n\nContext from earlier steps:\n- weather in Paris: Paris: now 18°C')
    assert '- convert 100 EUR to INR: (this step failed: Jev: jev down)' in text
    assert [e['tid'] for e in events if e['type'] == 'answered'] == ['1.1', '1.3'] and events[-1]['status'] == 'done'


def test_parse_steps_keeps_a_dag():
    assert parse_steps(['a', 'b']) == (['a', 'b'], [[], []])  # the old shape still works
    raw = [{'text': 'a', 'depends_on': [1]}, {'text': '  ', 'depends_on': []}, {'text': 'c', 'depends_on': [0, 1, 2, True, 'x']},
           {'text': 'd', 'depends_on': [2]}, {'text': 'e', 'depends_on': []}, {'text': 'f', 'depends_on': [0]}]
    # forward and self references go, the blank is skipped and indices remapped, at most four subtasks
    assert parse_steps(raw) == (['a', 'c', 'd', 'e'], [[], [0], [1], []])


async def test_keyless_plan_has_no_dependencies():
    p = await plan('weather in Paris and convert 100 EUR to INR', FakeJev(multi=0.9))
    assert p['deps'] == [[], []]


# ---------- run control ----------

async def slow(text, emit):
    await asyncio.sleep(10)
    return AgentResult('never', True)


async def wait_for(pred, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if pred():
            return
        await asyncio.sleep(0.01)
    raise AssertionError('condition never became true')


async def test_cancel_emits_cancelled_then_done():
    router = Router(FakeJev(route_for=by_keyword), registry=fake_registry(weather=slow))
    events = []
    router.bus.taps.append(events.append)
    qid = router.submit('weather in Paris', 'you')
    task = router.running[qid]
    await wait_for(lambda: any(e['type'] == 'routed' for e in events))
    assert router.cancel(qid) == 'ok'
    await task
    assert [e['type'] for e in events][-3:] == ['error', 'cancelled', 'done']
    assert events[-3]['tid'] == f'{qid}.1' and events[-2] == {'type': 'cancelled', 'qid': qid}
    assert events[-1]['status'] == 'cancelled' and router.get_run(qid)['status'] == 'cancelled'
    assert router.store.get_run(qid)['status'] == 'cancelled' and not router.inflight and not router.running
    assert router.cancel(qid) == 'finished' and router.cancel(999) == 'unknown'


async def test_cancel_kills_the_cli_child(tmp_path):
    from jevrouter.engines.codex import CodexEngine
    from tests.test_engines import call, fake
    path = fake(tmp_path, 'codex', 'time.sleep(60)\n')
    router = Router(FakeJev(route_for=lambda t: ('code', 0.9)), engine=CodexEngine(path))
    qid = router.submit('reverse a list', 'you')
    task = router.running[qid]
    await wait_for(lambda: os.path.exists(path + '.call.json'))
    await asyncio.sleep(0.2)
    assert router.cancel(qid) == 'ok'
    await asyncio.wait_for(task, 5)
    assert router.get_run(qid)['status'] == 'cancelled'
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(call(path)['pid'], 0)


async def test_run_deadline():
    router = Router(FakeJev(route_for=by_keyword), registry=fake_registry(weather=slow))
    router.run_timeout = 0.2
    events = await run(router, 'weather in Paris')
    assert [e['type'] for e in events][-3:] == ['error', 'error', 'done']
    assert events[-3]['tid'] == '1.1' and events[-2]['tid'] is None and 'timed out' in events[-2]['message']
    assert events[-1]['status'] == 'timeout' and router.history[-1]['status'] == 'timeout'


async def test_error_status_when_nothing_routes():
    router = Router(FakeJev(fail_on=('',)), registry=fake_registry())
    events = await run(router, 'hmm')
    assert events[-1]['status'] == 'error' and router.history[-1]['status'] == 'error'


# ---------- engines per run, tokens ----------

async def test_engine_override_is_per_run():
    a, b = ScriptEngine(name='claude-code'), ScriptEngine(name='codex', label='Codex')
    router = Router(FakeJev(route_for=lambda t: ('chat', 0.9)), engine=a, engines={'claude-code': a, 'codex': b})
    await router.handle('hey friend', 'you', engine=b)
    await router.handle('hey friend', 'you', engine=None)
    await router.handle('hey friend', 'you')
    recs = list(router.history)
    assert [r['engine'] for r in recs] == ['codex', None, 'claude-code']
    assert [t['engine'] for r in recs for t in r['tasks']] == ['codex', 'keyless', 'claude-code']
    assert len(b.calls) == 1 and len(a.calls) == 1 and router.engine is a  # the override never switched the active engine
    assert recs[0]['tokens'] == {'jev_in': 100, 'llm_in': 5, 'llm_out': 3} and recs[1]['tokens']['llm_in'] == 0


async def test_tokens_sum_planner_agents_and_merger():
    two = {'subtasks': [{'text': 'hey', 'depends_on': []}, {'text': 'hello again', 'depends_on': []}]}
    router = Router(FakeJev(route_for=lambda t: ('chat', 0.9)), engine=ScriptEngine(plan=two))
    events = await run(router, 'hey and hello again')
    # planner 7/4, two chat agents 5/3 each, merger 5/3; Jev: the plan's safety check, the speculative route of the
    # whole query (made alongside the LLM planner, not needed for a two-step plan) and two routes, at 100 each
    assert events[-1]['tokens'] == {'jev_in': 400, 'llm_in': 7 + 5 + 5 + 5, 'llm_out': 4 + 3 + 3 + 3}
    assert router.history[-1]['tokens'] == events[-1]['tokens']


# ---------- sessions ----------

async def test_session_follow_up_reaches_the_planner():
    engine = ScriptEngine(plan={'subtasks': [{'text': 'convert 100 USD to GBP', 'depends_on': []}]})
    router = Router(FakeJev(route_for=by_keyword), engine=engine, registry=fake_registry())
    await router.handle('convert 100 USD to EUR', 'chat', session_id='s1')
    assert not engine.calls  # a single clause with no earlier turn: the keyless plan is enough
    events = await run_session(router, 'what about GBP', 's1')
    planner_call = engine.calls[0]
    assert planner_call['schema'] is not None
    assert 'Q: convert 100 USD to EUR\nA: 100.00 EUR = 9,000.00 INR' in planner_call['prompt']
    assert planner_call['prompt'].endswith('Current query: what about GBP')
    assert events[1]['subtasks'][0]['text'] == 'convert 100 USD to GBP' and events[0]['session_id'] == 's1'
    s = router.store.list_sessions()
    assert s[0]['id'] == 's1' and s[0]['turns'] == 2 and s[0]['title'] == 'convert 100 USD to EUR'
    assert [r['qid'] for r in router.runs_where('session_id', 's1')] == [1, 2]


async def run_session(router, query, sid):
    events = []
    router.bus.taps.append(events.append)
    await router.handle(query, 'chat', session_id=sid)
    return events


def test_follow_up_prompt_and_worth():
    turns = [{'query': 'q' * 700, 'answer': 'a' * 700}]
    p = prompt_for('and in GBP?', turns, ['sales.csv'])
    assert 'Q: ' + 'q' * 600 + '\n' in p and 'A: ' + 'a' * 600 + '\n' in p and 'Attached files: sales.csv' in p
    assert worth_llm_plan('what about GBP', turns) and not worth_llm_plan('what about GBP', [])
    assert prompt_for('hi') == 'hi'


# ---------- persistence ----------

async def test_runs_survive_a_restart(tmp_path):
    db = tmp_path / 'tg.db'
    r1 = Router(FakeJev(route_for=by_keyword), registry=fake_registry(), store=Store(db))
    await r1.handle('weather in Paris', 'you', session_id='s9')
    await r1.handle('convert 100 EUR to INR', 'you')
    r1.store.close()
    r2 = Router(FakeJev(route_for=by_keyword), registry=fake_registry(), store=Store(db))
    hist = r2.hello()['history']
    assert [h['qid'] for h in hist] == [1, 2] and hist[0]['merged']['answer'] == 'Paris: now 18°C, clear sky'
    assert hist[0]['session_id'] == 's9' and hist[0]['status'] == 'done'
    assert r2.submit('hmm', 'you') == 3
    await r2.running[3]
    assert r2.get_run(3)['status'] == 'done' and r2.store.list_sessions()[0]['turns'] == 1


def test_interrupted_runs_are_marked_on_startup(tmp_path):
    s = Store(tmp_path / 'tg.db')
    s.save_run({'qid': 5, 'text': 'x', 'source': 'you', 'at': 1.0, 'status': 'running', 'tasks': []})
    s.close()
    rec = Store(tmp_path / 'tg.db').get_run(5)
    assert rec['status'] == 'error' and 'restart' in rec['error']


# ---------- custom and heavy agents ----------

async def test_custom_agent_is_offered_only_with_an_engine():
    store = Store()
    store.add_agent({'name': 'poet', 'description': 'Writes short poems about anything', 'prompt': 'Answer only in rhyming couplets.',
                     'web': True})
    engine = ScriptEngine(web=False)
    jev = FakeJev(route_for=lambda t: ('poet', 0.9))
    router = Router(jev, engine=engine, store=store)
    events = await run(router, 'a poem about cats')
    assert jev.criteria[-1]['poet'] == 'Writes short poems about anything'
    a = next(e for e in events if e['type'] == 'answered')
    assert a['agent'] == 'poet' and a['ok'] and a['engine'] == 'claude-code'
    call = engine.calls[-1]
    assert 'You are the poet agent. Answer only in rhyming couplets.' in call['system'] and call['web'] is False  # no web on this engine
    router.use_engine(None)
    assert 'poet' not in router.agents and 'poet' not in router.registry
    router.use_engine(ScriptEngine())  # web-capable engine honours web=True
    await router.registry['poet']('x', lambda t: None)
    assert router.engine.calls[-1]['web'] is True


def test_heavy_agents_follow_engine_capabilities():
    plain, codex = ScriptEngine(), ScriptEngine(name='codex', exec_ok=True)
    router = Router(FakeJev())
    assert 'report' not in router.offered(None) and 'run' not in router.offered(None)
    assert 'report' in router.offered(plain) and 'run' not in router.offered(plain) and 'run' in router.offered(codex)
    assert 'document' in router.offered(None, with_files=True) and 'document' not in router.offered(None)
    assert 'run' in agent_registry.build(None, codex) and 'run' not in agent_registry.build(None, plain)


async def test_report_and_run_agents_call_the_engine_right():
    codex = ScriptEngine(name='codex', exec_ok=True)
    reg = agent_registry.build(None, codex)
    await reg['report']('pros and cons of solid-state batteries', lambda t: None)
    await reg['run']('print the first 10 Fibonacci numbers', lambda t: None)
    rep, run_call = codex.calls
    assert rep['effort'] == 'high' and rep['web'] is True and '400-900 words' in rep['system'] and 'Sources' in rep['system']
    assert run_call['exec'] is True and 'execute it' in run_call['system']


async def test_codex_exec_uses_workspace_write_in_a_fresh_dir(tmp_path):
    from jevrouter.engines.codex import CodexEngine
    from tests.test_engines import call, fake
    path = fake(tmp_path, 'codex', '''
        out({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '34'}})
        out({'type': 'turn.completed', 'usage': {'input_tokens': 9, 'output_tokens': 1}})
    ''')
    e = CodexEngine(path)
    r = await e.stream(system='s', prompt='p', exec=True)
    c = call(path)
    a = c['argv']
    assert r.text == '34' and a[a.index('-s') + 1] == 'workspace-write'
    run_dir = a[a.index('-C') + 1]
    assert os.path.realpath(c['cwd']) == os.path.realpath(run_dir) and run_dir != e.cwd() and not os.path.exists(run_dir)
    await e.stream(system='s', prompt='p')  # without exec it stays read-only in the shared scratch dir
    a = call(path)['argv']
    assert a[a.index('-s') + 1] == 'read-only' and a[a.index('-C') + 1] == e.cwd()
    await e.aclose()


def test_run_filters_find_ids_and_keyless_history():
    store = Store()
    for qid, text, engine in [(1, 'weather in Paris', None), (2, 'explain routing', 'codex'), (3, 'weather tomorrow', None)]:
        store.save_run({'qid': qid, 'text': text, 'source': 'you', 'at': float(qid), 'status': 'done', 'engine': engine})
    assert [r['qid'] for r in store.list_runs(q='#1')] == [1]
    assert [r['qid'] for r in store.list_runs(q='2')] == [2]
    assert [r['qid'] for r in store.list_runs(engine='none', limit=1)] == [3]
    assert [r['qid'] for r in store.list_runs(engine='none', before=3)] == [1]
    assert [r['qid'] for r in store.list_runs(q='weather', engine='codex')] == []
    store.close()

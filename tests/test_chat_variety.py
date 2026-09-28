"""Chat variety, server side (docs/PLAN-speed-evals-chat.md track C): modes, answer styles, @agent, several answers
(engine groups, retry, choose) and follow-up context, plus the runs table migration."""
import asyncio
import json
import sqlite3
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jevrouter import app as appmod
from jevrouter.agents import AgentResult, tools
from jevrouter.pipeline import Router
from jevrouter.store import Store
from tests.fakes import FakeEngine, FakeJev, ScriptEngine
from tests.test_api import json_of
from tests.test_pipeline import by_keyword, fake_registry
from tests.test_speed import llm_calls


def route(text):
    if 'joke' in text or 'another' in text:
        return 'chat', 0.9
    if 'Ada' in text or 'Mars' in text:
        return 'knowledge', 0.9
    if 'report' in text:
        return 'report', 0.9
    return by_keyword(text)


def engines():
    return {'claude-code': ScriptEngine(), 'codex': ScriptEngine(name='codex', label='Codex', web=False),
            'agy': ScriptEngine(name='agy', label='Antigravity'), 'off': FakeEngine('off', 'Off', ok=False)}


def make_client(active='claude-code', jev=None, registry=None):
    es = engines()
    box = {}

    def factory(http):
        box['r'] = Router(jev or FakeJev(route_for=route, multi=0.9), http, es.get(active), engines=es, store=Store(),
                          registry=registry)
        return box['r']

    class Ctx:
        async def __aenter__(self):
            self.c = TestClient(TestServer(appmod.create_app(factory)))
            await self.c.__aenter__()
            self.c.router, self.c.engines = box['r'], es
            return self.c

        async def __aexit__(self, *exc):
            await self.c.__aexit__(*exc)
    return Ctx()


@pytest.fixture
async def client():
    async with make_client() as c:
        yield c


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The real registry's knowledge agents look up DuckDuckGo; here it finds nothing, without the network."""
    async def fake_get_json(http, url, **params):
        return {'AbstractText': '', 'AbstractURL': None}
    monkeypatch.setattr(tools, 'get_json', fake_get_json)


async def ask(client, status=200, **body):
    return await json_of(await client.post('/ask', json=body), status)


async def settle(client):
    while client.router.tasks:
        await asyncio.gather(*list(client.router.tasks))


# ---------- validation ----------

@pytest.mark.parametrize('bad, status', [
    ({'mode': 'turbo'}, 400), ({'style': 'haiku'}, 400), ({'agent': 'plumber'}, 400), ({'agent': 'clarify'}, 400),
    ({'agent': 5}, 400), ({'engines': ['claude-code']}, 400), ({'engines': ['claude-code', 'claude-code']}, 400),
    ({'engines': ['claude-code', 'codex', 'agy', 'none']}, 400), ({'engines': ['claude-code', 'gpt-9']}, 400),
    ({'engines': ['claude-code', 'off']}, 409), ({'engines': ['claude-code', 'codex'], 'engine': 'codex'}, 400),
    ({'retry_of': 99}, 404), ({'retry_of': 'x'}, 400), ({'agent': 'run'}, 400),  # run needs an engine that executes code
    ({'engines': ['claude-code', 'codex'], 'source': 'sandbox', 'sandbox_id': 'abcdefgh1'}, 400),
])
async def test_ask_validation(client, bad, status):
    r = await client.post('/ask', json={'query': 'weather in Paris', **bad})
    assert r.status == status and 'error' in await r.json(), bad


async def test_mode_style_and_agent_are_stored_on_the_run(client):
    body = await ask(client, query='weather in Paris', mode='quick', style='bullets', agent='@weather')
    await settle(client)
    rec = client.router.get_run(body['qid'])
    assert (rec['mode'], rec['style'], rec['agent'], rec['group_id'], rec['chosen']) == ('quick', 'bullets', 'weather',
                                                                                          None, True)
    body = await ask(client, query='weather in Paris')
    await settle(client)
    rec = client.router.get_run(body['qid'])
    assert (rec['mode'], rec['style'], rec['agent']) == ('balanced', 'default', None)


# ---------- modes ----------

async def test_quick_mode_prefers_keyless_agents_and_low_effort(client):
    es = client.engines
    body = await ask(client, query='tell me a joke', mode='quick')
    await settle(client)
    rec = client.router.get_run(body['qid'])
    assert rec['tasks'][0]['engine'] == 'keyless' and not es['claude-code'].calls  # the keyless chat agent answered
    body = await ask(client, query='write a report on bees', mode='quick')
    await settle(client)
    task = client.router.get_run(body['qid'])['tasks'][0]
    assert task['agent'] == 'report' and task['checks'] == {'effort': 'low'}  # no keyless report: the LLM, at low effort
    assert [c['effort'] for c in es['claude-code'].calls] == ['low']


async def test_quick_mode_joins_prose_by_template():
    engine = ScriptEngine()
    router = Router(FakeJev(route_for=route, multi=0.9), engine=engine)
    events = []
    router.bus.taps.append(events.append)
    await router.handle('write a report on bees and tell me a joke', 'you', mode='quick')
    assert not llm_calls(engine, 'merge') and events[-1]['timings']['merger'] == 'template'


async def test_deep_mode_plans_merges_and_answers_at_high_effort():
    engine = ScriptEngine(plan={'subtasks': [{'text': 'weather in Paris', 'depends_on': []},
                                             {'text': 'tell me a joke', 'depends_on': []}]})
    router = Router(FakeJev(route_for=route, multi=0.9), engine=engine)
    router.registry['weather'] = fake_registry()['weather']
    events = []
    router.bus.taps.append(events.append)
    await router.handle('weather in Paris and tell me a joke', 'you', mode='deep')
    assert len(llm_calls(engine, 'plan')) == 1 and len(llm_calls(engine, 'merge')) == 1
    assert [c['effort'] for c in llm_calls(engine, 'agent')] == ['high']
    joke = next(e for e in events if e['type'] == 'answered' and e['agent'] == 'chat')
    assert joke['checks'] == {'effort': 'high', 'verified': 'skipped', 'verify_note': None}


async def test_deep_mode_verifies_arithmetic_an_llm_agent_answered():
    engine = ScriptEngine()
    router = Router(FakeJev(route_for=route), engine=engine)
    events = []
    router.bus.taps.append(events.append)
    await router.handle('what is 15% of 380', 'you', mode='deep', agent='chat')  # the echo engine never says 57
    checks = next(e for e in events if e['type'] == 'answered')['checks']
    assert checks['verified'] == 'mismatch' and '57' in checks['verify_note']


def test_deep_engine_is_the_selected_engine_unless_auto():
    """A selected engine is used strictly; only with Auto selected does deep mode pick the strongest healthy one."""
    es = engines()
    assert Router(FakeJev(), None, es['agy'], engines=es, registry=fake_registry()).deep_engine() is es['agy']
    es['auto'] = ScriptEngine(name='auto', label='Auto')
    router = Router(FakeJev(), None, es['auto'], engines=es, registry=fake_registry())
    assert router.deep_engine() is es['claude-code']
    for _ in range(4):
        router.health.record('claude-code', False, 10, 'boom')
    assert router.deep_engine() is es['codex']  # claude-code fails most calls; anthropic isn't in this catalog
    es['codex']._blocked = (time.monotonic() + 60, 'out of quota')
    assert router.deep_engine() is es['agy']


async def test_deep_mode_uses_the_selected_engine():
    async with make_client(active='agy') as c:
        body = await ask(c, query='tell me a joke', mode='deep')
        await settle(c)
        assert c.router.get_run(body['qid'])['engine'] == 'agy'


async def test_deep_mode_keeps_named_engines_pinned_in_compare_and_retry():
    # claude-code is deep mode's own pick and agy the fastest: an easy step would be steered to agy if the named
    # claude-code were treated as unnamed.
    async with make_client(active='agy', jev=FakeJev(route_for=route, multi=0.9, hard=0.1)) as c:
        for name, ms in (('agy', 100), ('claude-code', 3000), ('codex', 2500)):
            for _ in range(3):
                c.router.health.record(name, True, ms, None)
        body = await ask(c, query='tell me a joke', mode='deep', engines=['claude-code', 'codex'])
        await settle(c)
        runs = [c.router.get_run(q) for q in body['qids']]
        assert [r['engine'] for r in runs] == ['claude-code', 'codex']
        assert [r['tasks'][0]['engine'] for r in runs] == ['claude-code', 'codex']
        retry = await ask(c, query='tell me a joke', retry_of=body['qids'][1], engine='claude-code', mode='deep')
        await settle(c)
        assert c.router.get_run(retry['qid'])['tasks'][0]['engine'] == 'claude-code'
        assert not [x for x in c.engines['agy'].calls if x['schema'] is None and 'joke' in x['prompt']]


async def test_research_mode_sends_knowledge_to_the_research_agent(client):
    body = await ask(client, query='Who was Ada Lovelace?', mode='research')
    await settle(client)
    task = client.router.get_run(body['qid'])['tasks'][0]
    assert task['agent'] == 'research' and 'research mode' in task['reason']
    assert llm_calls(client.engines['claude-code'], 'agent')[0]['web'] is True


async def test_research_mode_keeps_the_selected_engine():
    """Research never switches engines behind the user's back: an engine without web search answers from its own
    knowledge and the answer says so; only keyless is refused."""
    async with make_client(active='codex') as c:  # codex can't search the web
        body = await ask(c, query='Who was Ada Lovelace?', mode='research')
        await settle(c)
        run = c.router.get_run(body['qid'])
        assert run['engine'] == 'codex' and "can't search the web" in run['merged']['answer']
        assert any("can't search the web" in x for x in run['merged'].get('caveats', []))
        r = await c.post('/ask', json={'query': 'Who was Ada Lovelace?', 'mode': 'research', 'engine': 'codex'})
        assert r.status == 200
    async with make_client(active='claude-code') as c:  # it can: no note
        body = await ask(c, query='Who was Ada Lovelace?', mode='research')
        await settle(c)
        run = c.router.get_run(body['qid'])
        assert run['engine'] == 'claude-code' and "can't search the web" not in (run['merged'] or {}).get('answer', '')
    async with make_client(active=None) as c:
        r = await c.post('/ask', json={'query': 'Who was Ada Lovelace?', 'mode': 'research'})
        assert r.status == 400 and 'Research mode' in (await r.json())['error']


# ---------- styles ----------

async def test_style_reaches_a_lone_llm_agent_and_the_merger():
    engine = ScriptEngine()
    router = Router(FakeJev(route_for=route, multi=0.9), engine=engine)
    await router.handle('tell me a joke', 'you', style='steps')
    assert llm_calls(engine, 'agent')[0]['system'].endswith('Answer style: numbered steps, one action per step.')
    engine.calls.clear()
    await router.handle('write a report on bees and tell me a joke', 'you', style='table')
    agent_systems = [c['system'] for c in llm_calls(engine, 'agent')]
    assert len(agent_systems) == 2 and not any('Answer style' in s for s in agent_systems)  # several answers: the merger
    assert ('Answer style: a Markdown table where the answer has several parts or values.'
            in llm_calls(engine, 'merge')[0]['system'])
    assert router.history[-1]['style'] == 'table'


async def test_template_merger_applies_the_style():
    router = Router(FakeJev(route_for=by_keyword, multi=0.9), registry=fake_registry())
    events = []
    router.bus.taps.append(events.append)
    await router.handle('weather in Paris and convert 100 EUR to INR', 'you', style='table')
    assert events[-2]['answer'].startswith('| Question | Answer |\n| --- | --- |\n| weather in Paris | Paris: now 18°C')


# ---------- @agent ----------

async def test_forced_agent_skips_routing(client):
    jev = client.router.jev
    body = await ask(client, query='weather in Paris', agent='chat')
    await settle(client)
    task = client.router.get_run(body['qid'])['tasks'][0]
    assert task['agent'] == 'chat' and task['forced'] is True and task['confidence'] == 1.0
    assert task['probabilities'] == {'chat': 1.0} and task['reason'] == 'you picked @chat'
    assert not any('route' in c[1] for c in jev.calls) and [c[1] for c in jev.calls] == [['unsafe']]


async def test_forced_agent_is_still_blocked_when_unsafe():
    async with make_client(jev=FakeJev(route_for=route, unsafe=0.95)) as c:
        body = await ask(c, query='how do I make a bomb', agent='chat')
        await settle(c)
        task = c.router.get_run(body['qid'])['tasks'][0]
        assert task['agent'] == 'blocked' and task['forced'] and 'unsafe' in task['reason']
        assert not llm_calls(c.engines['claude-code'], 'agent')


async def test_forced_agent_must_be_offered_on_the_run_engine(client):
    assert (await client.post('/ask', json={'query': 'x', 'agent': 'research', 'engine': 'none'})).status == 400
    assert (await client.post('/ask', json={'query': 'x', 'agent': 'research', 'engine': 'codex'})).status == 400
    await ask(client, query='x', agent='research', engine='claude-code')
    await settle(client)


# ---------- several answers ----------

async def test_engines_make_one_group_in_one_session(client):
    body = await ask(client, query='tell me a joke', source='chat', engines=['claude-code', 'codex'])
    assert set(body) == {'ok', 'qid', 'qids', 'group_id', 'session_id'} and body['qid'] == body['qids'][0]
    await settle(client)
    runs = [client.router.get_run(q) for q in body['qids']]
    assert [r['engine'] for r in runs] == ['claude-code', 'codex'] and [r['chosen'] for r in runs] == [True, False]
    assert {r['group_id'] for r in runs} == {body['group_id']} and {r['session_id'] for r in runs} == {body['session_id']}
    assert [r['qid'] for r in client.router.store.group_runs(body['group_id'])] == body['qids']


async def test_follow_up_context_uses_only_the_chosen_answer(client):
    es = client.engines
    body = await ask(client, query='tell me a joke', source='chat', engines=['claude-code', 'codex'])
    sid, (a, b) = body['session_id'], body['qids']
    await settle(client)

    async def context_of_follow_up():
        es['claude-code'].calls.clear()
        await ask(client, query='and another one', source='chat', session_id=sid)
        await settle(client)
        return llm_calls(es['claude-code'], 'plan')[0]['prompt']
    prompt = await context_of_follow_up()
    assert 'A: claude-code: tell me a joke' in prompt and 'codex:' not in prompt
    assert await json_of(await client.post(f'/api/runs/{b}/choose')) == {'ok': True, 'group_id': body['group_id'], 'chosen': b}
    assert [client.router.get_run(q)['chosen'] for q in (a, b)] == [False, True]
    prompt = await context_of_follow_up()
    assert 'A: codex: tell me a joke' in prompt and 'A: claude-code: tell me a joke' not in prompt


async def test_choose_errors(client):
    assert (await client.post('/api/runs/99/choose')).status == 404
    assert (await client.post('/api/runs/x/choose')).status == 404
    body = await ask(client, query='weather in Paris')
    await settle(client)
    assert (await client.post(f"/api/runs/{body['qid']}/choose")).status == 409


async def test_choose_updates_a_run_still_in_flight():
    async def slow_chat(text, emit):
        await asyncio.sleep(0.2)
        return AgentResult('late', True)
    async with make_client(registry={'chat': slow_chat}) as c:
        body = await ask(c, query='tell me a joke', engines=['claude-code', 'codex'])
        await asyncio.sleep(0.05)
        await json_of(await c.post(f"/api/runs/{body['qids'][1]}/choose"))
        await settle(c)
        assert [c.router.store.get_run(q)['chosen'] for q in body['qids']] == [False, True]  # kept by the final save
        assert [h['chosen'] for h in c.router.history] == [False, True]


async def test_chosen_moves_off_a_failed_first_answer():
    async def slow_chat(text, emit):
        await asyncio.sleep(0.2)
        return AgentResult('late', True)
    async with make_client(registry={'chat': slow_chat}) as c:
        body = await ask(c, query='tell me a joke', engines=['claude-code', 'codex'])
        await asyncio.sleep(0.05)
        assert c.router.cancel(body['qids'][0]) == 'ok'  # the default choice ends cancelled while its sibling runs on
        await settle(c)
        runs = [c.router.get_run(q) for q in body['qids']]
        assert [r['status'] for r in runs] == ['cancelled', 'done']
        assert [r['chosen'] for r in runs] == [False, True]  # follow-ups build on the answer that worked
        assert [c.router.store.get_run(q)['chosen'] for q in body['qids']] == [False, True]


async def test_chosen_never_overrides_the_users_pick():
    async def slow_chat(text, emit):
        await asyncio.sleep(0.2)
        return AgentResult('late', True)
    async with make_client(registry={'chat': slow_chat}) as c:
        body = await ask(c, query='tell me a joke', engines=['claude-code', 'codex'])
        await asyncio.sleep(0.05)
        await json_of(await c.post(f"/api/runs/{body['qids'][0]}/choose"))
        c.router.cancel(body['qids'][0])
        await settle(c)
        assert [c.router.get_run(q)['chosen'] for q in body['qids']] == [True, False]


async def test_sql_agent_can_be_picked_with_a_table_attached(client):
    from jevrouter.files import extract
    from tests.test_files import CSV, NOTES
    store = client.router.store
    meta, text = extract('sales.csv', CSV.encode())
    store.add_file(meta, CSV.encode(), text)
    notes, ntext = extract('notes.md', NOTES.encode())
    store.add_file(notes, NOTES.encode(), ntext)
    assert (await client.post('/ask', json={'query': 'total units?', 'agent': 'sql'})).status == 400  # nothing attached
    assert (await client.post('/ask', json={'query': 'x', 'agent': 'sql', 'files': [notes['id']]})).status == 400  # no table
    await ask(client, query='total units?', agent='sql', files=[meta['id']])
    await settle(client)
    # keyless, @sql needs a SELECT the user wrote (the run itself only offers sql then)
    assert (await client.post('/ask', json={'query': 'total units?', 'agent': 'sql', 'files': [meta['id']],
                                            'engine': 'none'})).status == 400
    await ask(client, query='`SELECT sum(units) FROM sales`', agent='sql', files=[meta['id']], engine='none')
    await settle(client)
    first = await ask(client, query='total units?', files=[meta['id']])
    await settle(client)
    await ask(client, query='x', retry_of=first['qid'], agent='sql', engine='codex')  # a retry keeps the run's files
    await settle(client)


async def test_retry_adds_another_answer_to_the_group(client):
    es = client.engines
    first = await ask(client, query='tell me a joke', source='chat')
    sid = first['session_id']
    await settle(client)
    later = await ask(client, query='weather in Paris', source='chat', session_id=sid)
    await settle(client)
    es['codex'].calls.clear()
    body = await ask(client, query='ignored', retry_of=first['qid'], engine='codex', style='concise')
    assert body['session_id'] == sid and body['qids'] == [body['qid']]
    await settle(client)
    orig, new = client.router.get_run(first['qid']), client.router.get_run(body['qid'])
    assert orig['group_id'] == new['group_id'] == body['group_id'] and orig['chosen'] and not new['chosen']
    assert new['text'] == 'tell me a joke' and new['engine'] == 'codex' and new['style'] == 'concise'
    assert new['session_id'] == sid and new['source'] == 'chat'
    # the retry's context is the turns before the question it answers again: not the later weather turn
    assert later['qid'] < body['qid'] and not llm_calls(es['codex'], 'plan')  # no earlier turns, one clause: no LLM plan
    again = await ask(client, query='x', retry_of=body['qid'], engine='agy')
    assert again['group_id'] == body['group_id']
    await settle(client)
    assert [r['qid'] for r in client.router.store.group_runs(body['group_id'])] == [first['qid'], body['qid'], again['qid']]


def test_turns_skip_unchosen_runs_and_the_own_group(tmp_path):
    store = Store(tmp_path / 't.db')

    def save(qid, answer, group=None, chosen=True):
        store.save_run({'qid': qid, 'text': f'q{qid}', 'source': 'chat', 'at': qid, 'status': 'done', 'session_id': 's',
                        'merged': {'answer': answer, 'engine': 'single'}, 'group_id': group, 'chosen': chosen})
    save(1, 'one')
    save(2, 'two a', 'g', True)
    save(3, 'two b', 'g', False)
    save(4, 'three')
    assert [t['answer'] for t in store.turns('s', 5)] == ['one', 'two a', 'three']
    assert [t['answer'] for t in store.turns('s', 2, group_id='g')] == ['one']
    assert [t['answer'] for t in store.turns('s', 5, group_id='g')] == ['one', 'three']


# ---------- migration ----------

OLD_RUNS = """CREATE TABLE runs(qid INTEGER PRIMARY KEY, session_id TEXT, compare_id TEXT, source TEXT, text TEXT, at REAL,
                                status TEXT, engine TEXT, total_ms INTEGER, record TEXT)"""


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / 'old.db'
    db = sqlite3.connect(path)
    db.execute(OLD_RUNS)
    old = {'qid': 7, 'text': 'weather in Paris', 'source': 'chat', 'at': 1.0, 'status': 'done', 'engine': None,
           'session_id': 's', 'merged': {'answer': 'Paris: 18°C', 'engine': 'single'}, 'total_ms': 5}
    db.execute('INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)', (7, 's', None, 'chat', old['text'], 1.0, 'done', None, 5,
                                                                 json.dumps(old)))
    db.commit()
    db.close()
    store = Store(path)
    cols = {r['name'] for r in store.q('PRAGMA table_info(runs)')}
    assert {'group_id', 'chosen', 'timings'} <= cols
    assert store.get_run(7) == old and [t['answer'] for t in store.turns('s', 8)] == ['Paris: 18°C']
    assert store.timed_runs() == []  # an old run has no timings
    store.save_run({**old, 'qid': 8, 'group_id': 'g', 'chosen': False, 'timings': {'plan_ms': None}})
    assert store.group_runs('g')[0]['qid'] == 8 and len(store.timed_runs()) == 1
    assert [t['answer'] for t in store.turns('s', 9)] == ['Paris: 18°C']  # the unchosen answer isn't context
    store.close()
    Store(path).close()  # opening again is a no-op

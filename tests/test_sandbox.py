"""Sandbox runs: nothing is stored, nothing counts, and their events only reach their own stream."""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jevrouter import app as appmod
from jevrouter.pipeline import SANDBOX_QID0, SANDBOX_TURNS, Router
from tests.fakes import FakeJev, ScriptEngine
from tests.test_http import read_events
from tests.test_pipeline import by_keyword, fake_registry

SID = 'sandbox-test-1'


_open: list[Router] = []


def router(engine=None):
    r = Router(FakeJev(route_for=by_keyword), engine=engine, registry=fake_registry())
    _open.append(r)
    return r


@pytest.fixture(autouse=True)
def close_stores():
    yield
    while _open:
        _open.pop().store.close()


async def sandbox_run(r: Router, query: str, sid: str = SID) -> int:
    qid = r.submit(query, 'sandbox', sandbox=sid)
    await r.running[qid]
    return qid


async def test_sandbox_run_is_not_saved_or_counted():
    r = router()
    qid = await sandbox_run(r, 'weather in Paris')
    assert qid >= SANDBOX_QID0
    assert r.store.counts()['runs'] == 0 and r.store.list_sessions() == []
    assert r.get_run(qid) is None                      # gone once finished: not in memory, not in the DB
    assert r.hello()['history'] == [] and not r.history
    assert r.stats['queries'] == 0 and r.stats['subtasks'] == 0 and sum(r.stats['by_agent'].values()) == 0
    assert r.sandbox == {} and r.sandbox_stats == {}  # per-run bookkeeping is dropped
    # saved runs keep their own numbering, with no gap left by the sandbox
    await r.handle('weather in Paris', 'you')
    assert [h['qid'] for h in r.hello()['history']] == [1] and r.stats['queries'] == 1


async def test_sandbox_events_still_stream_with_the_run():
    r = router()
    events = []
    r.bus.taps.append(events.append)
    qid = await sandbox_run(r, 'weather in Paris')
    assert [e['type'] for e in events][0] == 'query' and events[-1]['type'] == 'done'
    assert all(e.get('qid') == qid for e in events) and events[-1]['status'] == 'done'


async def test_sandbox_follow_up_uses_memory_only():
    engine = ScriptEngine(plan={'subtasks': [{'text': 'convert 100 USD to GBP', 'depends_on': []}]})
    r = router(engine)
    await sandbox_run(r, 'convert 100 USD to EUR')
    await sandbox_run(r, 'what about GBP')
    prompt = engine.calls[0]['prompt']
    assert 'Q: convert 100 USD to EUR' in prompt and prompt.endswith('Current query: what about GBP')
    assert r.store.counts()['runs'] == 0
    # context is bounded, per sandbox, and forgotten on clear
    for i in range(SANDBOX_TURNS + 2):
        await sandbox_run(r, f'weather in Paris {i}')
    mem = r.sandboxes.peek(SID)
    assert len(mem.thread) == SANDBOX_TURNS + 4 and len(mem.context()) == SANDBOX_TURNS
    assert r.clear_sandbox(SID) == 0 and SID not in r.sandboxes


async def test_clear_sandbox_cancels_its_running_queries():
    r = router()

    async def slow(text, emit):
        await asyncio.sleep(10)

    r.registry = {**r.registry, 'weather': slow}
    qid = r.submit('weather in Paris', 'sandbox', sandbox=SID)
    await asyncio.sleep(0.05)
    assert r.clear_sandbox(SID) == 1
    await asyncio.wait_for(asyncio.shield(r.running.get(qid) or asyncio.sleep(0)), 5)
    assert r.store.counts()['runs'] == 0


@pytest.fixture
async def client():
    app = appmod.create_app(lambda http: router())
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_ask_validates_sandbox_requests(client):
    assert (await client.post('/ask', json={'query': 'hi', 'source': 'sandbox'})).status == 400
    assert (await client.post('/ask', json={'query': 'hi', 'source': 'sandbox', 'sandbox_id': 'x'})).status == 400
    assert (await client.post('/ask', json={'query': 'hi', 'source': 'sandbox', 'sandbox_id': SID,
                                            'session_id': 's1'})).status == 400
    r = await client.post('/ask', json={'query': 'hi', 'source': 'sandbox', 'sandbox_id': SID})
    body = await r.json()
    assert r.status == 200 and body['sandbox_id'] == SID and body['qid'] >= SANDBOX_QID0
    assert (await client.get('/events?sandbox=../bad')).status == 400
    assert (await client.delete('/api/sandbox/' + SID)).status == 200


async def test_streams_are_isolated(client):
    main = await client.get('/events')
    mine = await client.get(f'/events?sandbox={SID}')
    other = await client.get('/events?sandbox=sandbox-someone-else')
    hellos = [(await read_events(s, 'hello'))[0] for s in (main, mine, other)]
    assert hellos[1]['history'] == [] and 'engines' in hellos[1]
    await client.post('/ask', json={'query': 'weather in Paris', 'source': 'sandbox', 'sandbox_id': SID})
    got = await read_events(mine, 'done')
    assert got[0]['type'] == 'query' and got[-1]['type'] == 'done'
    # then a saved run: the main stream's first run event must be that one, not the sandbox's
    r = await client.post('/ask', json={'query': 'weather in Paris'})
    saved = (await r.json())['qid']
    first = (await read_events(main, 'query'))[-1]
    assert first['qid'] == saved
    # the other sandbox saw neither run
    await client.post('/control', json={'interval': 5})  # a run-independent event every stream receives
    assert [e['type'] for e in await read_events(other, 'state')] == ['state']
    runs = await (await client.get('/api/runs')).json()
    assert [x['qid'] for x in runs['runs']] == [saved]


# ---------- v2: draft agent, replaces, remember, keep, sweeper, files ----------

POET = {'name': 'poet', 'description': 'Writes short poems about anything', 'prompt': 'Answer only in rhyming couplets.'}


def one_step(text):
    return {'subtasks': [{'text': text, 'depends_on': []}]}


def poem_route(text):
    return ('poet', 0.9) if 'poem' in text else by_keyword(text)


async def test_draft_agent_is_offered_and_answers_for_that_run_only():
    engine = ScriptEngine(plan=one_step('write a poem about Paris'))
    r = router(engine)
    r.jev.route_for = poem_route
    config_agents = dict(r.config()['agents'])
    qid = r.submit('write a poem about Paris', 'sandbox', sandbox=SID, extras={'draft': POET})
    events = []
    r.bus.taps.append(events.append)
    await r.running[qid]
    assert 'poet' in r.jev.criteria[-1]                  # Jev was offered the draft
    routed = next(e for e in events if e['type'] == 'routed')
    answered = next(e for e in events if e['type'] == 'answered')
    assert routed['agent'] == answered['agent'] == 'poet' and answered['ok']
    assert any('You are the poet agent. Answer only in rhyming couplets.' in c['system'] for c in engine.calls)
    # never stored, never in the config, and gone for the next run
    assert r.store.agents() == [] and r.customs == [] and r.config()['agents'] == config_agents
    assert 'poet' not in r.registry and 'poet' not in r.agents
    await sandbox_run(r, 'write a poem about Rome')
    assert 'poet' not in r.jev.criteria[-1]


async def test_replaces_uses_earlier_context_and_swaps_the_turn():
    engine = ScriptEngine(plan=one_step('weather in Paris'))
    r = router(engine)
    a, b, c = [await sandbox_run(r, q) for q in ('weather in Paris', 'weather in Rome', 'weather in Oslo')]
    engine.calls.clear()
    qid = r.submit('weather in Madrid', 'sandbox', sandbox=SID, extras={'replaces': b})
    await r.running[qid]
    prompt = next(x['prompt'] for x in engine.calls if x['schema'] is not None)
    assert 'Q: weather in Paris' in prompt and 'Rome' not in prompt and 'Oslo' not in prompt
    mem = r.sandboxes.peek(SID)
    assert mem.qids() == [a, qid, c] and mem.thread[1]['query'] == 'weather in Madrid'
    # a replaced turn that has since left the thread: the edit is appended instead
    q2 = r.submit('weather in Lima', 'sandbox', sandbox=SID, extras={'replaces': b})
    await r.running[q2]
    assert mem.qids() == [a, qid, c, q2]


async def test_remember_false_neither_reads_nor_writes_memory():
    engine = ScriptEngine(plan=one_step('weather in Paris'))
    r = router(engine)
    first = await sandbox_run(r, 'weather in Paris')
    engine.calls.clear()
    qid = r.submit('weather in Rome and Oslo', 'sandbox', sandbox=SID, extras={'remember': False})
    await r.running[qid]
    assert not any('Q: weather in Paris' in x['prompt'] for x in engine.calls)
    assert r.sandboxes.peek(SID).qids() == [first]


async def test_sweeper_forgets_idle_sandboxes_but_not_busy_ones():
    r = router()
    r.sandbox_ttl = 100
    r.sandboxes.get('idle-sandbox', create=True, now=1000)
    r.sandboxes.get('busy-sandbox', create=True, now=1000)
    r.sandboxes.get('fresh-sandbox', create=True, now=1090)
    r.sandbox[SANDBOX_QID0 + 99] = 'busy-sandbox'  # a query still running there
    assert r.sweep_sandboxes(1150) == ['idle-sandbox']
    assert 'busy-sandbox' in r.sandboxes and 'fresh-sandbox' in r.sandboxes
    r.sandbox.clear()
    assert sorted(r.sweep_sandboxes(1300)) == ['busy-sandbox', 'fresh-sandbox']


async def test_sweeper_task_runs_with_the_app():
    app = appmod.create_app(lambda http: router())
    async with TestClient(TestServer(app)) as c:
        task = c.app[appmod.SWEEPER]
        assert not task.done()
    assert task.cancelled() or task.done()


# ---------- HTTP ----------

@pytest.fixture
async def eclient():
    """A client whose router has an LLM engine (draft agents need one)."""
    box = {}

    def factory(http):
        box['r'] = router(ScriptEngine(plan=one_step('write a poem about Paris')))
        box['r'].jev.route_for = poem_route
        return box['r']
    async with TestClient(TestServer(appmod.create_app(factory))) as c:
        c.router = box['r']
        yield c


def ask_body(query, **kw):
    return {'query': query, 'source': 'sandbox', 'sandbox_id': SID, **kw}


async def finish(r, qid):
    if (t := r.running.get(qid)) is not None:
        await asyncio.wait_for(t, 5)


async def test_draft_agent_over_http(eclient, monkeypatch):
    r = eclient.router
    monkeypatch.setattr(appmod, 'MAX_CUSTOM', 0)  # drafts aren't counted against the saved-agent limit
    resp = await eclient.post('/ask', json=ask_body('write a poem about Paris', draft_agent=POET))
    assert resp.status == 200, await resp.text()
    qid = (await resp.json())['qid']
    await finish(r, qid)
    turn = r.sandboxes.peek(SID).thread[-1]
    assert turn['qid'] == qid and turn['record']['tasks'][0]['agent'] == 'poet'
    assert r.store.agents() == [] and 'poet' not in (await (await eclient.get('/api/config')).json())['agents']
    names = [a['name'] for a in (await (await eclient.get('/api/agents')).json())['agents']]
    assert 'poet' not in names
    # validation: like a custom agent
    for bad in ({**POET, 'name': 'Bad Name'}, {**POET, 'name': 'math'}, {**POET, 'name': 'clarify'},
                {**POET, 'description': 'short'}, {**POET, 'prompt': 'x'}, 'poet'):
        assert (await eclient.post('/ask', json=ask_body('hi', draft_agent=bad))).status == 400, bad
    monkeypatch.setattr(appmod, 'MAX_CUSTOM', 12)
    assert (await eclient.post('/api/agents', json=POET)).status == 201
    assert (await eclient.post('/ask', json=ask_body('hi', draft_agent=POET))).status == 400  # clashes with a saved one
    # keyless run (per-message engine or active engine) -> 409
    resp = await eclient.post('/ask', json=ask_body('hi', engine='none', draft_agent={**POET, 'name': 'bard'}))
    assert resp.status == 409 and (await resp.json())['error'] == 'the draft agent needs an LLM engine'


async def test_keyless_draft_is_409(client):
    resp = await client.post('/ask', json=ask_body('hi', draft_agent=POET))
    assert resp.status == 409


async def test_ask_validates_v2_fields(client):
    r = client.app[appmod.ROUTER]
    assert (await client.post('/ask', json=ask_body('hi', replaces=5))).status == 400
    assert (await client.post('/ask', json=ask_body('hi', replaces='5'))).status == 400
    assert (await client.post('/ask', json=ask_body('hi', remember='no'))).status == 400
    assert (await client.post('/ask', json=ask_body('hi', files=['nope']))).status == 400
    qid = (await (await client.post('/ask', json=ask_body('weather in Paris'))).json())['qid']
    await finish(r, qid)
    resp = await client.post('/ask', json=ask_body('weather in Rome', replaces=qid, remember=True))
    assert resp.status == 200
    await finish(r, (await resp.json())['qid'])
    assert len(r.sandboxes.peek(SID).thread) == 1  # the edit took the original's place
    assert (await client.post('/ask', json=ask_body('hi', replaces=qid))).status == 400  # no longer in the thread
    # non-sandbox runs ignore the sandbox fields and still reject unknown stored files
    assert (await client.post('/ask', json={'query': 'hi', 'files': ['nope']})).status == 400


async def test_keep_saves_a_normal_chat(client):
    r = client.app[appmod.ROUTER]
    for q in ('weather in Paris', 'weather in Rome'):  # saved runs 1 and 2
        await finish(r, (await (await client.post('/ask', json={'query': q})).json())['qid'])
    assert (await client.post(f'/api/sandbox/{SID}/keep')).status == 400  # empty thread
    sq = []
    for q in ('weather in Oslo', 'weather in Lima and convert 100 EUR to INR', 'weather in Cairo'):
        sq.append((await (await client.post('/ask', json=ask_body(q))).json())['qid'])
        await finish(r, sq[-1])
    assert (await client.post(f'/api/sandbox/{SID}/keep', json={'qids': [sq[0], 123]})).status == 400
    assert (await client.post(f'/api/sandbox/{SID}/keep', json={'qids': 'all'})).status == 400
    assert (await client.post('/api/sandbox/bad/keep')).status == 400

    resp = await client.post(f'/api/sandbox/{SID}/keep', json={'qids': [sq[1], sq[0]]})
    assert resp.status == 200, await resp.text()
    kept = await resp.json()
    assert kept['qids'] == [3, 4]  # after the max saved qid, in thread order
    sid = kept['session_id']
    assert isinstance(sid, str) and len(sid) == 12
    runs = [r.store.get_run(q) for q in kept['qids']]
    assert [x['text'] for x in runs] == ['weather in Oslo', 'weather in Lima and convert 100 EUR to INR']
    for rec in runs:
        assert rec['source'] == 'chat' and rec['session_id'] == sid and rec['files'] == [] and rec['status'] == 'done'
        tids = [t['tid'] for t in rec['tasks']] + [t['tid'] for t in rec['plan']['subtasks']]
        assert tids and all(t.startswith(f"{rec['qid']}.") for t in tids)
        assert all(d.startswith(f"{rec['qid']}.") for t in rec['tasks'] for d in t['depends_on'])
    assert len(runs[1]['tasks']) == 2
    sessions = await (await client.get('/api/sessions')).json()
    assert [(s['id'], s['turns'], s['title']) for s in sessions['sessions']] == [(sid, 2, 'weather in Oslo')]
    session = await (await client.get(f'/api/sessions/{sid}')).json()
    assert [x['qid'] for x in session['runs']] == [3, 4]
    listed = await (await client.get('/api/runs')).json()
    assert [x['qid'] for x in listed['runs']] == [4, 3, 2, 1]
    assert [h['qid'] for h in r.hello()['history']][-2:] == [3, 4]
    # the sandbox is unchanged, and the next saved run continues the numbering
    assert r.sandboxes.peek(SID).qids() == sq
    assert r.submit('weather in Paris', 'you') == 5
    whole = await (await client.post(f'/api/sandbox/{SID}/keep')).json()
    assert len(whole['qids']) == 3 and whole['session_id'] != sid


def test_renumber_moves_every_tid():
    from jevrouter.pipeline import renumber
    rec = {'qid': 1000000007, 'tasks': [{'tid': '1000000007.1', 'depends_on': []},
                                        {'tid': '1000000007.2', 'depends_on': ['1000000007.1']}],
           'plan': {'planner': 'x', 'subtasks': [{'tid': '1000000007.1', 'text': 'a', 'depends_on': []},
                                                 {'tid': '1000000007.2', 'text': 'b', 'depends_on': ['1000000007.1']}]}}
    out = renumber(rec, 12)
    assert out['qid'] == 12 and [t['tid'] for t in out['tasks']] == ['12.1', '12.2']
    assert out['tasks'][1]['depends_on'] == ['12.1'] and out['plan']['subtasks'][1] == {'tid': '12.2', 'text': 'b', 'depends_on': ['12.1']}


def upload(name, data):
    import aiohttp
    f = aiohttp.FormData()
    f.add_field('file', data, filename=name)
    return f


async def test_sandbox_files_live_in_memory_only(client, monkeypatch):
    from jevrouter import sandbox as sandbox_mod
    from tests.test_files import CSV
    r = client.app[appmod.ROUTER]
    resp = await client.post(f'/api/sandbox/{SID}/files', data=upload('sales.csv', CSV.encode()))
    assert resp.status == 201, await resp.text()
    meta = await resp.json()
    assert meta['kind'] == 'csv' and meta['rows'] == 4 and set(meta) >= {'id', 'name', 'size', 'kind', 'chars', 'created'}
    # nothing on disk or in the store; stored-file ids and sandbox ids don't mix
    assert list(r.store.files_dir.iterdir()) == [] and r.store.list_files() == []
    assert (await client.post('/ask', json={'query': 'total revenue', 'files': [meta['id']]})).status == 400
    assert (await client.post('/ask', json=ask_body('total revenue', files=[meta['id']], sandbox_id='other-sandbox'))).status == 400

    r.jev.route_for = lambda text: ('data', 0.9) if 'total' in text else by_keyword(text)
    resp = await client.post('/ask', json=ask_body('total revenue', files=[meta['id']]))
    assert resp.status == 200
    await finish(r, (await resp.json())['qid'])
    rec = r.sandboxes.peek(SID).thread[-1]['record']
    assert rec['files'] == [meta['id']] and rec['tasks'][0]['agent'] == 'data' and 'sum 8,000.50' in rec['merged']['answer']
    assert set(FILE_AGENT_NAMES) <= set(r.jev.criteria[-1])
    assert any('(Attached files: sales.csv)' in state for state, qs in r.jev.calls if 'route' in qs)
    assert list(r.store.files_dir.iterdir()) == [] and r.store.counts()['runs'] == 0

    # limits: 10 MB per file (413), 20 MB per sandbox (413), 5 files (400)
    monkeypatch.setattr(appmod, 'MAX_BYTES', 100)
    assert (await client.post(f'/api/sandbox/{SID}/files', data=upload('big.txt', b'x' * 200))).status == 413
    monkeypatch.setattr(appmod, 'MAX_BYTES', 10 * 1024 * 1024)
    monkeypatch.setattr(sandbox_mod, 'MAX_TOTAL_BYTES', len(CSV) + 50)
    assert (await client.post(f'/api/sandbox/{SID}/files', data=upload('b.txt', b'y' * 100))).status == 413
    monkeypatch.setattr(sandbox_mod, 'MAX_TOTAL_BYTES', 20 * 1024 * 1024)
    assert (await client.post(f'/api/sandbox/{SID}/files', data=upload('x.exe', b'MZ'))).status == 400
    for i in range(4):
        assert (await client.post(f'/api/sandbox/{SID}/files', data=upload(f'n{i}.txt', b'note'))).status == 201
    assert (await client.post(f'/api/sandbox/{SID}/files', data=upload('n5.txt', b'note'))).status == 400
    assert (await client.post('/api/sandbox/bad/files', data=upload('n.txt', b'note'))).status == 400

    # delete one, then forgetting the sandbox drops the rest (and the thread)
    assert await (await client.delete(f"/api/sandbox/{SID}/files/{meta['id']}")).json() == {'ok': True}
    assert (await client.delete(f"/api/sandbox/{SID}/files/{meta['id']}")).status == 404
    assert (await client.post('/ask', json=ask_body('total revenue', files=[meta['id']]))).status == 400
    assert len(r.sandboxes.peek(SID).files) == 4
    assert (await client.delete(f'/api/sandbox/{SID}')).status == 200
    assert SID not in r.sandboxes
    assert list(r.store.files_dir.iterdir()) == []


FILE_AGENT_NAMES = ('document', 'data')

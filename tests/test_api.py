"""v4 HTTP API (docs/PLAN-v4.md §1): /ask additions, run control, runs, sessions, agents, files, compare, evals, engine test."""
import asyncio

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from jevrouter import app as appmod
from jevrouter import evals as evals_mod
from jevrouter import files as files_mod
from jevrouter.agents import AgentResult
from jevrouter.engines import EngineError
from jevrouter.pipeline import Router
from jevrouter.store import Store
from tests.fakes import FakeEngine, FakeJev, ScriptEngine
from tests.test_files import CSV, NOTES, tiny_pdf
from tests.test_http import read_events
from tests.test_pipeline import by_keyword, fake_registry


async def slow_weather(text, emit):
    await asyncio.sleep(10)
    return AgentResult('never', True)


def engines():
    return {'claude-code': ScriptEngine(), 'codex': ScriptEngine(name='codex', label='Codex', exec_ok=True),
            'agy': FakeEngine('agy', 'Antigravity', ok=False)}


@pytest.fixture
async def client():
    """Keyless by default (so no LLM planner), with two fake engines to override to and one unavailable."""
    es = engines()
    router_box = {}

    def factory(http):
        router_box['r'] = Router(FakeJev(route_for=by_keyword), http, None, engines=es, store=Store(),
                                 registry=fake_registry())
        return router_box['r']
    async with TestClient(TestServer(appmod.create_app(factory))) as c:
        c.router = router_box['r']
        yield c


async def json_of(resp, status=200):
    assert resp.status == status, await resp.text()
    return await resp.json()


async def finish(client, qid):
    task = client.router.running.get(qid)
    if task:
        await asyncio.wait_for(task, 5)


# ---------- /ask ----------

async def test_ask_additions(client):
    body = await json_of(await client.post('/ask', json={'query': 'weather in Paris', 'source': 'chat'}))
    sid = body['session_id']
    assert body['ok'] and body['qid'] == 1 and isinstance(sid, str) and len(sid) == 12
    await finish(client, 1)
    body = await json_of(await client.post('/ask', json={'query': 'hmm', 'source': 'chat', 'session_id': sid}))
    assert body['session_id'] == sid
    body = await json_of(await client.post('/ask', json={'query': 'hmm', 'engine': 'codex'}))
    await finish(client, body['qid'])
    assert body['session_id'] is None and client.router.get_run(body['qid'])['engine'] == 'codex'
    assert client.router.engine is None  # the override is for that run only
    for bad, status in (({'engine': 'gpt-9'}, 400), ({'engine': 'agy'}, 409), ({'files': ['nope']}, 400),
                        ({'files': 'x'}, 400), ({'source': 'robot'}, 400), ({'session_id': 5}, 400)):
        r = await client.post('/ask', json={'query': 'hi', **bad})
        assert r.status == status and 'error' in await r.json(), bad


async def test_query_event_carries_run_fields(client):
    resp = await client.get('/events')
    await read_events(resp, 'hello')
    await client.post('/ask', json={'query': 'weather in Paris', 'engine': 'none', 'session_id': 'abc'})
    events = await read_events(resp, 'done')
    q = events[0]
    assert q['type'] == 'query' and q['session_id'] == 'abc' and q['engine'] is None and q['files'] == [] and q['compare_id'] is None
    assert events[-1]['status'] == 'done' and set(events[-1]['tokens']) == {'jev_in', 'llm_in', 'llm_out'}
    resp.close()


# ---------- runs ----------

async def test_cancel_endpoint(client):
    resp = await client.get('/events')
    await read_events(resp, 'hello')
    client.router.registry['weather'] = slow_weather
    qid = (await json_of(await client.post('/ask', json={'query': 'weather in Paris'})))['qid']
    await read_events(resp, 'routed')
    assert (await json_of(await client.post(f'/api/runs/{qid}/cancel'))) == {'ok': True}
    events = await read_events(resp, 'done')
    assert [e['type'] for e in events][-2:] == ['cancelled', 'done'] and events[-1]['status'] == 'cancelled'
    assert (await client.post(f'/api/runs/{qid}/cancel')).status == 409
    assert (await client.post('/api/runs/999/cancel')).status == 404
    assert (await client.post('/api/runs/abc/cancel')).status == 404
    resp.close()


async def test_runs_list_and_get(client):
    for q in ('weather in Paris', 'convert 100 EUR to INR', 'hmm'):
        await finish(client, (await json_of(await client.post('/ask', json={'query': q})))['qid'])
    runs = (await json_of(await client.get('/api/runs?limit=2')))['runs']
    assert [r['qid'] for r in runs] == [3, 2]
    runs = (await json_of(await client.get('/api/runs?before=3&q=Paris')))['runs']
    assert [r['qid'] for r in runs] == [1] and runs[0]['status'] == 'done'
    assert (await json_of(await client.get('/api/runs/2')))['text'] == 'convert 100 EUR to INR'
    assert (await client.get('/api/runs/99')).status == 404
    assert (await client.get('/api/runs?limit=x')).status == 400


# ---------- sessions ----------

async def test_sessions(client):
    a = await json_of(await client.post('/ask', json={'query': 'weather in Paris ' + 'x' * 80, 'source': 'chat'}))
    await finish(client, a['qid'])
    b = await json_of(await client.post('/ask', json={'query': 'hmm', 'source': 'chat', 'session_id': a['session_id']}))
    await finish(client, b['qid'])
    c = await json_of(await client.post('/ask', json={'query': 'hmm', 'source': 'chat'}))
    await finish(client, c['qid'])
    sessions = (await json_of(await client.get('/api/sessions')))['sessions']
    assert [s['id'] for s in sessions] == [c['session_id'], a['session_id']]
    assert set(sessions[1]) == {'id', 'title', 'created', 'updated', 'turns'} and sessions[1]['turns'] == 2
    assert sessions[1]['title'] == ('weather in Paris ' + 'x' * 80)[:60]
    assert len((await json_of(await client.get('/api/sessions?limit=1')))['sessions']) == 1
    s = await json_of(await client.get(f"/api/sessions/{a['session_id']}"))
    assert set(s) == {'id', 'title', 'runs'} and [r['qid'] for r in s['runs']] == [a['qid'], b['qid']]
    assert await json_of(await client.delete(f"/api/sessions/{a['session_id']}")) == {'ok': True}
    assert (await client.get(f"/api/sessions/{a['session_id']}")).status == 404
    assert [x['id'] for x in (await json_of(await client.get('/api/sessions')))['sessions']] == [c['session_id']]


# ---------- agents ----------

GOOD = {'name': 'poet', 'description': 'Writes short poems about anything', 'prompt': 'Answer only in rhyming couplets.'}


async def test_agents_list_create_delete(client):
    agents = (await json_of(await client.get('/api/agents')))['agents']
    by = {a['name']: a for a in agents}
    assert by['math'] == {'name': 'math', 'description': by['math']['description'], 'kind': 'builtin', 'engine_required': False,
                          'available': True}
    assert by['research']['engine_required'] and not by['research']['available']  # keyless now
    assert by['clarify']['kind'] == 'guard' and by['blocked']['kind'] == 'guard'
    assert {'report', 'run', 'document', 'data'} <= set(by) and not by['document']['engine_required']

    resp = await client.get('/events')
    await read_events(resp, 'hello')
    a = await json_of(await client.post('/api/agents', json=GOOD), 201)
    assert a == {**GOOD, 'kind': 'custom', 'engine_required': True, 'available': False, 'web': False}
    await read_events(resp, 'config')
    await client.post('/control', json={'engine': 'codex'})
    cfg = (await read_events(resp, 'config'))[-1]
    assert cfg['agents']['poet'] == GOOD['description'] and 'run' in cfg['agents'] and cfg['features']['exec'] is True
    by = {x['name']: x for x in (await json_of(await client.get('/api/agents')))['agents']}
    assert by['poet']['available'] and by['run']['available'] and by['poet']['prompt'] == GOOD['prompt']

    for bad in ({'name': 'P'}, {'name': 'math'}, {'name': 'clarify'}, {'name': 'poet'}, {'name': '9lives'},
                {'name': 'x' * 25}, {'description': 'short'}, {'description': 'd' * 201}, {'prompt': 'tiny'},
                {'prompt': 'p' * 4001}):
        r = await client.post('/api/agents', json={**GOOD, 'name': 'other', **bad})
        assert r.status == 400 and 'error' in await r.json(), bad
    for i in range(11):
        await json_of(await client.post('/api/agents', json={**GOOD, 'name': f'agent{i}'}), 201)
    r = await client.post('/api/agents', json={**GOOD, 'name': 'onemore'})
    assert r.status == 400 and 'at most 12' in (await r.json())['error']

    assert await json_of(await client.delete('/api/agents/poet')) == {'ok': True}
    assert (await read_events(resp, 'config'))[-1]['type'] == 'config'
    assert 'poet' not in client.router.agents
    assert (await client.delete('/api/agents/math')).status == 400
    assert (await client.delete('/api/agents/poet')).status == 400
    resp.close()


# ---------- files ----------

def form(name, data):
    f = aiohttp.FormData()
    f.add_field('file', data, filename=name)
    return f


async def test_files_upload_list_delete(client, monkeypatch):
    t = await json_of(await client.post('/api/files', data=form('notes.md', NOTES.encode())), 201)
    assert set(t) >= {'id', 'name', 'size', 'kind', 'chars'} and t['kind'] == 'text' and 'rows' not in t
    c = await json_of(await client.post('/api/files', data=form('sales.csv', CSV.encode())), 201)
    assert c['kind'] == 'csv' and c['rows'] == 4 and c['columns'][0] == 'region'
    p = await json_of(await client.post('/api/files', data=form('r.pdf', tiny_pdf('Hello from a PDF'))), 201)
    assert p['kind'] == 'pdf' and p['chars'] >= len('Hello from a PDF')
    assert (client.router.store.files_dir / p['id']).exists()

    assert (await client.post('/api/files', data=form('x.exe', b'MZ'))).status == 400
    assert (await client.post('/api/files', json={'x': 1})).status == 400
    monkeypatch.setattr(appmod, 'MAX_BYTES', 100)
    assert (await client.post('/api/files', data=form('big.txt', b'x' * 200))).status == 413

    files = (await json_of(await client.get('/api/files')))['files']
    assert [f['id'] for f in files] == [p['id'], c['id'], t['id']]

    # a run with files routes to the data agent (by keyword in this fake) and reads the CSV
    client.router.jev.route_for = lambda text: ('data', 0.9) if 'total' in text else by_keyword(text)
    q = await json_of(await client.post('/ask', json={'query': 'total revenue', 'files': [c['id']]}))
    await finish(client, q['qid'])
    rec = client.router.get_run(q['qid'])
    assert rec['files'] == [c['id']] and rec['tasks'][0]['agent'] == 'data' and 'sum 8,000.50' in rec['merged']['answer']

    assert await json_of(await client.delete(f"/api/files/{c['id']}")) == {'ok': True}
    assert (await client.delete(f"/api/files/{c['id']}")).status == 404
    assert not (client.router.store.files_dir / c['id']).exists()
    assert len((await json_of(await client.get('/api/files')))['files']) == 2


def test_max_upload_is_ten_megabytes():
    assert files_mod.MAX_BYTES == appmod.MAX_BYTES == 10 * 1024 * 1024


# ---------- compare ----------

async def test_compare_fans_out(client):
    await json_of(await client.post('/api/agents', json=GOOD), 201)  # a custom agent runs on whichever engine the run uses
    client.router.jev.route_for = lambda text: ('poet', 0.9)
    body = await json_of(await client.post('/api/compare', json={'query': 'hey friend', 'engines': ['claude-code', 'codex']}))
    cid = body['compare_id']
    assert [r['engine'] for r in body['runs']] == ['claude-code', 'codex']
    for r in body['runs']:
        await finish(client, r['qid'])
    c = await json_of(await client.get(f'/api/compare/{cid}'))
    assert c['compare_id'] == cid and c['query'] == 'hey friend' and len(c['runs']) == 2
    assert [(r['engine'], r['source'], r['compare_id']) for r in c['runs']] == [('claude-code', 'compare', cid), ('codex', 'compare', cid)]
    assert c['runs'][0]['merged']['answer'].startswith('claude-code:') and c['runs'][1]['merged']['answer'].startswith('codex:')
    assert (await client.get('/api/compare/nope')).status == 404
    for bad, status in ((['codex'], 400), (['codex', 'codex'], 400), (['codex', 'gpt-9'], 400), (['codex', 'agy'], 409),
                        (['a', 'b', 'c', 'd', 'e'], 400), ('codex', 400)):
        assert (await client.post('/api/compare', json={'query': 'hi', 'engines': bad})).status == status, bad
    assert (await client.post('/api/compare', json={'query': ' ', 'engines': ['codex', 'none']})).status == 400


# ---------- evals ----------

async def test_eval_run_progress_and_results(client, tmp_path, monkeypatch):
    cases = tmp_path / 'cases.jsonl'
    cases.write_text('{"id": "w", "query": "weather in Paris", "expect_agents": ["weather"], "must_match": "Paris", "tags": ["c"]}\n'
                     '{"id": "h", "query": "hmm", "expect_outcome": "answer", "tags": ["s"]}\n'
                     '{"id": "e", "query": "convert 100 EUR to INR", "must_match": "USD", "tags": ["s"]}\n')
    monkeypatch.setattr(evals_mod, 'CASES', cases)
    resp = await client.get('/events')
    await read_events(resp, 'hello')
    eid = (await json_of(await client.post('/api/evals/run', json={'engine': 'none'})))['eval_id']
    events = await read_events(resp, 'eval_done', limit=500)
    progress = [e for e in events if e['type'] == 'eval_progress']
    assert [p['done'] for p in progress] == [1, 2, 3] and all(p['eval_id'] == eid and p['total'] == 3 for p in progress)
    assert events[-1] == {'type': 'eval_done', 'eval_id': eid, 'passed': 1, 'total': 3, 'accuracy': 0.3333, 'status': 'done'}
    assert {e['source'] for e in events if e['type'] == 'query'} == {'eval'}

    listed = (await json_of(await client.get('/api/evals')))['evals']
    assert listed == [{'eval_id': eid, 'at': listed[0]['at'], 'engine': 'none', 'status': 'done', 'done': 3, 'passed': 1,
                       'total': 3, 'accuracy': 0.3333, 'silent_wrong': 1}]  # the currency case answered ok but wrong
    d = await json_of(await client.get(f'/api/evals/{eid}'))
    assert d['done'] == 3 and [c['id'] for c in d['cases']] == ['w', 'h', 'e']
    w, h, e = d['cases']
    assert w['pass'] and w['agents'] == ['weather'] and w['expect_agents'] == ['weather'] and w['expect_outcome'] is None
    assert not h['pass'] and h['reasons'] == ['expected outcome answer, got clarify'] and h['expect_outcome'] == 'answer'
    assert set(w) >= {'id', 'query', 'tags', 'pass', 'reasons', 'agents', 'answer', 'ms', 'qid'}
    assert client.router.get_run(w['qid'])['source'] == 'eval'
    assert (await client.get('/api/evals/nope')).status == 404
    assert (await client.post(f'/api/evals/{eid}/cancel')).status == 409
    assert (await client.post('/api/evals/nope/cancel')).status == 404
    assert (await client.post('/api/evals/run', json={'engine': 'agy'})).status == 409
    resp.close()


async def test_eval_cancel(client, tmp_path, monkeypatch):
    cases = tmp_path / 'cases.jsonl'
    cases.write_text(''.join(f'{{"id": "s{i}", "query": "weather in Paris {i}", "tags": []}}\n' for i in range(4)))
    monkeypatch.setattr(evals_mod, 'CASES', cases)
    client.router.registry['weather'] = slow_weather
    resp = await client.get('/events')
    await read_events(resp, 'hello')
    eid = (await json_of(await client.post('/api/evals/run', json={})))['eval_id']
    await read_events(resp, 'routed')
    assert len(client.router.running) == 2  # concurrency 2
    assert await json_of(await client.post(f'/api/evals/{eid}/cancel')) == {'ok': True}
    events = await read_events(resp, 'eval_done', limit=500)
    assert events[-1]['total'] == 4 and events[-1]['passed'] == 0
    d = await json_of(await client.get(f'/api/evals/{eid}'))
    assert d['status'] == 'cancelled' and d['engine'] == 'none' and d['done'] <= 2
    await asyncio.sleep(0.05)
    assert not client.router.running
    resp.close()


# ---------- engine test ----------

class Broken(FakeEngine):
    def __init__(self, error=None, delay=0.0):
        super().__init__('broken', 'Broken')
        self.error, self.delay = error, delay

    async def stream(self, **kw):
        await asyncio.sleep(self.delay)
        raise self.error


async def test_engine_test_endpoint(client, monkeypatch):
    r = await json_of(await client.post('/api/engines/codex/test'))
    assert r['ok'] and r['text'] == 'codex: Reply with exactly: ok' and isinstance(r['ms'], int)
    assert client.router.engines['codex'].calls[-1]['effort'] == 'low'
    r = await json_of(await client.post('/api/engines/agy/test'))
    assert r == {'ok': False, 'ms': 0, 'error': 'not installed'}
    assert (await client.post('/api/engines/gpt-9/test')).status == 404
    client.router.engines['broken'] = Broken(EngineError('not logged in'))
    r = await json_of(await client.post('/api/engines/broken/test'))
    assert r['ok'] is False and r['error'] == 'not logged in'
    client.router.engines['broken'] = Broken(RuntimeError('x'), delay=1)
    monkeypatch.setattr(appmod, 'TEST_TIMEOUT', 0.1)
    r = await json_of(await client.post('/api/engines/broken/test'))
    assert r['ok'] is False and 'timed out' in r['error']


async def test_features_in_config(client):
    f = (await json_of(await client.get('/api/config')))['features']
    assert f == {'files': True, 'compare': True, 'evals': True, 'custom_agents': True, 'exec': False}
    await client.post('/control', json={'engine': 'codex'})
    assert (await json_of(await client.get('/api/config')))['features']['exec'] is True
    await client.post('/control', json={'engine': 'claude-code'})
    assert (await json_of(await client.get('/api/config')))['features']['exec'] is False

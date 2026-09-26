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
    assert len(r.sandbox_turns[SID]) == SANDBOX_TURNS
    assert r.clear_sandbox(SID) == 0 and SID not in r.sandbox_turns


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

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jevrouter import app as appmod
from jevrouter.pipeline import Router
from tests.fakes import FakeJev
from tests.test_pipeline import by_keyword, fake_registry


@pytest.fixture
async def client():
    app = appmod.create_app(lambda http: Router(FakeJev(route_for=by_keyword, multi=0.9), http, registry=fake_registry()))
    async with TestClient(TestServer(app)) as c:
        yield c


async def read_events(resp, until_type=None, limit=100):
    out = []
    while len(out) < limit:
        line = await asyncio.wait_for(resp.content.readline(), 5)
        if not line:
            break
        if line.startswith(b'data: '):
            out.append(json.loads(line[6:]))
            if out[-1]['type'] == until_type:
                break
    return out


async def test_ask_validation(client):
    assert (await client.post('/ask', json={'query': '  '})).status == 400
    assert (await client.post('/ask', data='not json')).status == 400
    assert (await client.post('/ask', json=['x'])).status == 400
    r = await client.post('/ask', json={'query': 'weather in Paris'})
    assert r.status == 200 and await r.json() == {'ok': True, 'qid': 1, 'session_id': None}


async def test_control_clamps_and_broadcasts(client):
    r = await client.post('/control', json={'autopilot': True, 'interval': 99})
    assert await r.json() == {'autopilot': True, 'interval': 15.0, 'engine': None}
    r = await client.post('/control', json={'interval': 0})
    assert (await r.json())['interval'] == 1.0
    assert (await client.post('/control', json={'interval': 'fast'})).status == 400


async def test_config(client):
    body = await (await client.get('/api/config')).json()
    assert set(body) == {'agents', 'guards', 'claude', 'state', 'stats', 'samples', 'prices', 'engine', 'engines', 'features',
                         'route_examples', 'limits', 'fonts'}
    assert body['limits'] == {'query_chars': 4000} and set(body['fonts']) == {'body'}
    assert body['engine'] is None and body['route_examples'] is False and body['engines'] == []
    assert body['claude'] is False and 'research' not in body['agents'] and body['guards'] == ['clarify', 'blocked', 'unsupported']
    assert set(body['prices']) == {'jev_in', 'claude_in', 'claude_out'}


async def test_events_stream(client):
    resp = await client.get('/events')
    assert resp.headers['Content-Type'] == 'text/event-stream'
    hello = (await read_events(resp, 'hello'))[0]
    assert set(hello) == {'type', 'agents', 'guards', 'claude', 'state', 'stats', 'samples', 'prices', 'engine', 'engines', 'history',
                          'features', 'route_examples', 'limits', 'fonts'}
    qid = (await (await client.post('/ask', json={'query': 'weather in Paris and convert 100 EUR to INR'})).json())['qid']
    events = await read_events(resp, 'done')
    types = [e['type'] for e in events]
    assert types[:2] == ['query', 'plan'] and types[-2:] == ['merged', 'done'] and all(e['qid'] == qid for e in events)
    assert types.count('routed') == 2 and types.count('answered') == 2

    await client.post('/control', json={'autopilot': False})
    assert (await read_events(resp, 'state'))[-1]['state'] == {'autopilot': False, 'interval': 3.0}

    # a fresh subscriber gets the finished query in history
    resp2 = await client.get('/events')
    hist = (await read_events(resp2, 'hello'))[0]['history']
    assert hist[-1]['qid'] == qid and len(hist[-1]['tasks']) == 2 and hist[-1]['merged']['engine'] == 'concat'
    resp.close()
    resp2.close()


async def test_serves_legacy_then_dist(client, tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, 'DIST', tmp_path / 'missing')
    r = await client.get('/')
    assert r.status == 200 and 'Jev' in await r.text()
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'index.html').write_text('<div id="root">v2</div>')
    (tmp_path / 'assets' / 'app.js').write_text('console.log(1)')
    monkeypatch.setattr(appmod, 'DIST', tmp_path)
    assert await (await client.get('/')).text() == '<div id="root">v2</div>'
    r = await client.get('/assets/app.js')
    assert r.status == 200 and 'javascript' in r.headers['Content-Type']
    assert (await client.get('/assets/nope.js')).status == 404
    assert (await client.get('/assets/../../PLAN.md')).status == 404
    assert (await client.get('/%2e%2e/PLAN.md')).status == 404


async def test_slow_subscriber_is_cut_off_not_silently_dropped():
    from jevrouter.events import Broadcaster
    bus = Broadcaster(limit=3)
    q = bus.subscribe()
    for i in range(5):
        bus.emit('delta', qid=1, tid='1.1', text=str(i))
    assert q not in bus.subscribers and q.qsize() == 4 and [q.get_nowait() for _ in range(4)][-1] is None


async def test_engine_switching():
    from tests.fakes import FakeEngine
    engines = {'claude-code': FakeEngine(), 'codex': FakeEngine('codex', 'Codex', web=False),
               'agy': FakeEngine('agy', 'Antigravity', ok=False)}
    app = appmod.create_app(lambda http: Router(FakeJev(route_for=by_keyword), http, engines['claude-code'], engines=engines))
    async with TestClient(TestServer(app)) as client:
        await check_switching(client)


async def check_switching(client):
    body = await (await client.get('/api/config')).json()
    assert body['engine']['name'] == 'claude-code' and body['claude'] is True and 'research' in body['agents']
    assert body['prices']['claude_in'] == 0 and [e['name'] for e in body['engines']] == ['claude-code', 'codex', 'agy']

    resp = await client.get('/events')
    await read_events(resp, 'hello')
    r = await client.post('/control', json={'engine': 'codex'})
    assert (await r.json())['engine'] == 'codex'
    cfg = (await read_events(resp, 'config'))[-1]
    assert cfg['engine']['name'] == 'codex' and 'research' not in cfg['agents']  # codex fake has no web search

    assert (await client.post('/control', json={'engine': 'agy'})).status == 409
    assert (await client.post('/control', json={'engine': 'gpt-9'})).status == 400
    r = await client.post('/control', json={'engine': 'none'})
    assert (await r.json())['engine'] is None
    cfg = (await read_events(resp, 'config'))[-1]
    assert cfg['engine'] is None and cfg['claude'] is False
    resp.close()


async def test_index_revalidates_and_assets_are_immutable(client, tmp_path, monkeypatch):
    # A new build must reach browsers: index.html is no-cache, hashed assets are cached for a year.
    dist = tmp_path / 'dist'
    (dist / 'assets').mkdir(parents=True)
    (dist / 'index.html').write_text('<html></html>')
    (dist / 'assets' / 'index-abc.js').write_text('x')
    monkeypatch.setattr(appmod, 'DIST', dist)
    r = await client.get('/')
    assert r.status == 200 and r.headers['Cache-Control'] == 'no-cache'
    r = await client.get('/assets/index-abc.js')
    assert r.status == 200 and 'immutable' in r.headers['Cache-Control']

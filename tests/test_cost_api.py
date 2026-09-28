"""The cost guard over HTTP (docs/PLAN-files-robust.md 5.4): a costly /ask comes back 409 with the estimate and starts
nothing until it is confirmed; /api/estimate prices a draft and starts nothing; keyless runs and evals are never
asked; several engines add up; the run keeps estimated vs used."""
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jevrouter import app as appmod
from jevrouter.files import extract
from jevrouter.pipeline import Router
from jevrouter.store import Store
from tests.fakes import FakeJev, ScriptEngine

Q2750 = ('create the ppt on the how mobile phone is being evolved history past present everything a ppt of 12 slides '
         'using the multiple pictured diagrams and also use the design.md for the design')
DESIGN_TEXT = ('# Design System: Lovable style\n\n## 2. Color Palette & Roles\n- **Cream** (`#f7f4ed`): Page background.\n'
               '- **Charcoal** (`#1c1c1c`): Primary text and headings.\n\n## 3. Typography Rules\n'
               '- Font family: `Camera Plain Variable`\n')


def route(text):
    t = text.lower()
    return ('create', 0.9) if any(w in t for w in ('ppt', 'slides', 'deck', 'pdf')) else ('knowledge', 0.9)


def engines():
    return {'agy': ScriptEngine(name='agy', label='Antigravity', web=True),
            'claude-code': ScriptEngine(name='claude-code', label='Claude Code', web=True)}


async def make_client(engine_name: str | None = 'agy'):
    box = {}

    def factory(http):
        es = engines()
        # no aiohttp session for the router: a created file never looks for web images in a test
        box['r'] = Router(FakeJev(route_for=route), None, es[engine_name] if engine_name else None, store=Store(),
                          engines=es)
        box['e'] = es
        return box['r']
    c = TestClient(TestServer(appmod.create_app(factory)))
    await c.start_server()
    c.router, c.engines = box['r'], box['e']
    return c


@pytest.fixture
async def client(monkeypatch):
    monkeypatch.setenv('TG_COST_CONFIRM', '1')
    c = await make_client()
    yield c
    await c.close()


@pytest.fixture
async def keyless(monkeypatch):
    monkeypatch.setenv('TG_COST_CONFIRM', '1')
    c = await make_client(None)
    yield c
    await c.close()


def add_design(store: Store) -> str:
    raw = DESIGN_TEXT.encode()
    meta, text = extract('DESIGN-lovable.md', raw)
    store.add_file(meta, raw, text)
    return meta['id']


def engine_calls(c) -> int:
    return sum(len(e.calls) for e in c.engines.values())


async def finish(c, qid):
    task = c.router.running.get(qid)
    if task:
        await asyncio.wait_for(task, 20)


async def test_a_costly_run_needs_confirming_and_starts_nothing_until_then(client):
    fid = add_design(client.router.store)
    body = {'query': Q2750, 'source': 'chat', 'mode': 'research', 'agent': 'create', 'files': [fid]}
    before = client.router.store.max_qid()
    resp = await client.post('/ask', json=body)
    assert resp.status == 409
    got = await resp.json()
    assert got['needs_confirmation'] is True and got['error'].startswith('This run needs about ')
    assert got['error'].endswith('model calls. Confirm to go ahead.')
    est = got['estimate']
    assert est['needs_confirmation'] and est['long_file'] and est['engine'] == 'agy' and est['calls'] >= 5
    assert est['summary'].startswith('This deck needs about')
    assert client.router.store.max_qid() == before and not client.router.running and not client.router.inflight
    assert engine_calls(client) == 0 and client.router.stats['queries'] == 0
    # the same draft priced on its own starts nothing either, and gives the same estimate
    priced = await client.post('/api/estimate', json={k: v for k, v in body.items() if k != 'source'} | {'source': 'chat'})
    assert priced.status == 200 and await priced.json() == est
    assert engine_calls(client) == 0 and client.router.store.max_qid() == before
    # confirmed: the run starts and keeps estimated vs used
    resp = await client.post('/ask', json={**body, 'confirm_cost': True})
    assert resp.status == 200
    ok = await resp.json()
    assert ok['ok'] and isinstance(ok['qid'], int)
    await finish(client, ok['qid'])
    run = await (await client.get(f'/api/runs/{ok["qid"]}')).json()
    assert run['cost']['estimate'] == est
    assert run['cost']['actual'] is not None and set(run['cost']['actual']) == {'calls', 'tokens_in', 'tokens_out',
                                                                               'seconds'}
    assert 'checkpoints_state' not in run


async def test_confirm_cost_must_be_a_boolean(client):
    resp = await client.post('/ask', json={'query': 'hi', 'source': 'chat', 'confirm_cost': 'yes'})
    assert resp.status == 400 and 'confirm_cost' in (await resp.json())['error']


async def test_a_cheap_run_is_never_asked(client):
    resp = await client.post('/ask', json={'query': 'make a 1 slide deck about tea', 'source': 'chat',
                                           'engine': 'claude-code'})
    assert resp.status == 200
    await finish(client, (await resp.json())['qid'])


async def test_keyless_runs_are_never_asked(keyless):
    resp = await keyless.post('/ask', json={'query': Q2750, 'source': 'chat'})
    assert resp.status == 200
    qid = (await resp.json())['qid']
    await finish(keyless, qid)
    est = await (await keyless.post('/api/estimate', json={'query': Q2750})).json()
    assert est['keyless'] and est['calls'] == 0 and not est['needs_confirmation']


async def test_evals_are_never_asked(client):
    resp = await client.post('/ask', json={'query': Q2750, 'source': 'eval', 'mode': 'research', 'agent': 'create'})
    assert resp.status == 200
    await finish(client, (await resp.json())['qid'])


async def test_the_guard_is_off_with_tg_cost_confirm_0(client, monkeypatch):
    monkeypatch.setenv('TG_COST_CONFIRM', '0')
    resp = await client.post('/ask', json={'query': Q2750, 'source': 'chat', 'mode': 'research', 'agent': 'create'})
    assert resp.status == 200
    qid = (await resp.json())['qid']
    await finish(client, qid)
    run = await (await client.get(f'/api/runs/{qid}')).json()
    assert run['cost']['estimate']['needs_confirmation'] is True  # still priced, only not asked


async def test_two_engines_add_up(client):
    body = {'query': 'make a 12 slide deck about the history of tea', 'source': 'chat'}
    one = [await (await client.post('/api/estimate', json={**body, 'engine': n})).json() for n in ('agy', 'claude-code')]
    both = await (await client.post('/api/estimate', json={**body, 'engines': ['agy', 'claude-code']})).json()
    assert both['calls'] == one[0]['calls'] + one[1]['calls']
    assert both['tokens_in'] == one[0]['tokens_in'] + one[1]['tokens_in'] and both['engine'] is None
    assert any(r['code'] == 'several_engines' for r in both['reasons'])
    resp = await client.post('/ask', json={**body, 'engines': ['agy', 'claude-code']})
    assert resp.status == 409 and (await resp.json())['estimate']['calls'] == both['calls']
    assert engine_calls(client) == 0


async def test_estimate_validates_like_ask_and_offers_a_cheaper_engine(client):
    assert (await client.post('/api/estimate', json={'query': ''})).status == 400
    assert (await client.post('/api/estimate', json={'query': 'x', 'engine': 'nope'})).status == 400
    assert (await client.post('/api/estimate', json={'query': 'x', 'files': ['missing']})).status == 400
    assert (await client.post('/api/estimate', json={'query': 'x', 'retry_of': 1})).status == 400
    assert (await client.post('/api/estimate', json={'query': 'x' * 5000})).status == 400
    est = await (await client.post('/api/estimate', json={'query': Q2750, 'mode': 'research', 'agent': 'create'})).json()
    assert est['cheaper'] and est['cheaper'][0]['engine'] == 'claude-code' and est['cheaper'][0]['label'] == 'Claude Code'
    assert engine_calls(client) == 0 and client.router.store.max_qid() == 0


async def test_retry_is_guarded_too(client, monkeypatch):
    monkeypatch.setenv('TG_COST_CONFIRM', '0')
    resp = await client.post('/ask', json={'query': 'make a 12 slide deck about the history of tea', 'source': 'chat'})
    first = await resp.json()
    await finish(client, first['qid'])
    monkeypatch.setenv('TG_COST_CONFIRM', '1')
    before = client.router.store.max_qid()
    resp = await client.post('/ask', json={'query': 'x', 'source': 'chat', 'retry_of': first['qid'], 'engine': 'agy'})
    assert resp.status == 409 and client.router.store.max_qid() == before
    resp = await client.post('/ask', json={'query': 'x', 'source': 'chat', 'retry_of': first['qid'], 'engine': 'agy',
                                           'confirm_cost': True})
    assert resp.status == 200
    await finish(client, (await resp.json())['qid'])


async def test_sandbox_runs_are_guarded(client):
    body = {'query': 'make a 12 slide deck about the history of tea', 'source': 'sandbox', 'sandbox_id': 'sandbox-cost-1'}
    resp = await client.post('/ask', json=body)
    assert resp.status == 409 and not client.router.sandbox and engine_calls(client) == 0
    resp = await client.post('/ask', json={**body, 'confirm_cost': True})
    assert resp.status == 200
    await finish(client, (await resp.json())['qid'])

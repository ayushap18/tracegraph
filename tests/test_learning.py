"""Routing that learns, M4-M5 (docs/PLAN-learning.md): route examples in Jev's criteria, the switch and per-eval
override, eval compare, the evals table migration and engine health."""
import asyncio
import json
import sqlite3
import time

import pytest

from jevrouter import evals as evals_mod
from jevrouter.config import AGENTS
from jevrouter.engines import AutoEngine, EngineError, EngineRefusal, Reply
from jevrouter.engines.health import Health, instrument, instrument_all, pct, unblock
from jevrouter.jev import clean, criteria, examples_for, route_one
from jevrouter.pipeline import Router
from jevrouter.store import Store, read_labels
from tests.fakes import FakeJev
from tests.test_api import json_of
from tests.test_labels import client, run, save, task  # noqa: F401 (client is a fixture)
from tests.test_pipeline import by_keyword, fake_registry


def L(text, picked, correct, at, verdict=None):
    return {'text': text, 'picked': picked, 'correct': correct, 'verdict': verdict or ('right' if picked == correct else 'wrong'),
            'at': at}


# ---------- criteria() ----------

def test_criteria_without_labels_is_the_plain_request():
    assert criteria(AGENTS, []) == AGENTS
    assert examples_for(AGENTS, []) == {}


def test_criteria_with_one_label():
    c = criteria(AGENTS, [L('10 km in miles', 'knowledge', 'math', 1)])
    assert c['math'] == {'description': AGENTS['math'], 'examples': ['10 km in miles'], 'not': []}
    assert c['knowledge'] == {'description': AGENTS['knowledge'], 'examples': [], 'not': ['10 km in miles']}
    assert all(c[a] == AGENTS[a] for a in AGENTS if a not in ('math', 'knowledge'))  # the rest stay plain strings
    json.dumps(c)  # JSON content, as Jev's Choice criteria require


def test_criteria_with_many_labels_keeps_the_newest_few():
    labels = [L(f'sum {i}', 'knowledge', 'math', i) for i in range(6)]
    labels += [L(f'what is {i}', 'math', 'knowledge', 10 + i) for i in range(4)]
    labels += [L('2 + 2', 'math', 'math', 20)]  # a confirmed right route is an example too
    ex = examples_for(AGENTS, labels)
    assert ex['math'] == {'examples': ['2 + 2', 'sum 5', 'sum 4'], 'not': ['what is 3', 'what is 2']}
    assert ex['knowledge'] == {'examples': ['what is 3', 'what is 2', 'what is 1'], 'not': ['sum 5', 'sum 4']}
    assert set(ex) == {'math', 'knowledge'}


def test_criteria_normalises_dedupes_and_truncates():
    long = 'convert ' + 'very ' * 80 + 'long amounts'
    ex = examples_for({'math': 'm'}, [L('  10 km\n in   miles ', 'knowledge', 'math', 3), L('10 KM in miles', 'chat', 'math', 2),
                                      L(long, 'chat', 'math', 1), L('   ', 'chat', 'math', 4)])
    assert ex['math']['examples'][:2] == ['10 km in miles', clean(long)]
    assert len(ex['math']['examples']) == 2 and len(clean(long)) == 200 and clean(long).endswith('...')


def test_poisoning_guard():
    """A text the user also said was wrong for an agent is never an example for it, so one bad label can't pull the
    agent both ways; once the bad label is gone the good one counts again."""
    good = L('10 km in miles', 'knowledge', 'math', 1)
    bad = L('10 KM in miles ', 'math', 'currency', 2)
    ex = examples_for(AGENTS, [good, bad])
    assert ex['math'] == {'examples': [], 'not': ['10 KM in miles']}
    assert ex['currency'] == {'examples': ['10 KM in miles'], 'not': []}
    assert examples_for(AGENTS, [good])['math'] == {'examples': ['10 km in miles'], 'not': []}


def test_criteria_ignore_agents_not_offered():
    assert criteria({'math': 'm'}, [L('news today', 'chat', 'research', 1)]) == {'math': 'm'}


async def test_route_one_sends_examples_to_jev():
    jev = FakeJev(route_for=lambda t: ('math', 0.9))
    d = await route_one(jev, '10 km in miles', criteria(AGENTS, [L('5 km in miles', 'knowledge', 'math', 1)]))
    assert d['examples'] is True and jev.criteria[-1]['math']['examples'] == ['5 km in miles'] and d['agent'] == 'math'
    d = await route_one(jev, '10 km in miles', criteria(AGENTS, []))
    assert d['examples'] is False and jev.criteria[-1] == AGENTS


# ---------- the switch ----------

async def test_switch_steers_new_runs_and_labels_refresh(client):
    router, jev = client.router, client.router.jev
    save(client, run(1, [task(1, agent='knowledge', text='10 km in miles')]))
    await json_of(await client.post('/api/labels', json={'qid': 1, 'tid': '1.1', 'verdict': 'wrong', 'correct': 'math'}))
    assert router.labels[0]['correct'] == 'math'  # the cache refreshes on every label change

    from tests.test_labels import ask
    qid = await ask(client, 'weather in Paris')
    assert jev.criteria[-1] == router.agents and router.get_run(qid)['tasks'][0]['examples'] is False

    events = []
    router.bus.taps.append(events.append)
    r = await json_of(await client.post('/control', json={'route_examples': True}))
    assert r['engine'] is None and router.route_examples is True
    assert [e['route_examples'] for e in events if e['type'] == 'config'] == [True]
    assert (await json_of(await client.get('/api/config')))['route_examples'] is True
    qid = await ask(client, 'weather in Paris')
    assert jev.criteria[-1]['math'] == {'description': AGENTS['math'], 'examples': ['10 km in miles'], 'not': []}
    assert jev.criteria[-1]['knowledge']['not'] == ['10 km in miles'] and jev.criteria[-1]['weather'] == AGENTS['weather']
    routed = [e for e in events if e['type'] == 'routed']
    assert routed[-1]['examples'] is True and router.get_run(qid)['tasks'][0]['examples'] is True

    lid = router.labels[0]['id']
    await client.delete(f'/api/labels/{lid}')
    assert router.labels == []
    qid = await ask(client, 'weather in Paris')
    assert jev.criteria[-1] == router.agents and router.get_run(qid)['tasks'][0]['examples'] is False

    assert (await client.post('/control', json={'route_examples': 'yes'})).status == 400
    await client.post('/control', json={'route_examples': False})
    assert router.route_examples is False


def test_switch_default_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv('TG_ROUTE_EXAMPLES', '1')
    assert Router(FakeJev()).route_examples is True and Router(FakeJev()).config()['route_examples'] is True
    monkeypatch.setenv('TG_ROUTE_EXAMPLES', 'off')
    assert Router(FakeJev()).route_examples is False


async def test_hello_carries_route_examples():
    r = Router(FakeJev())
    r.route_examples = True
    assert r.hello()['route_examples'] is True


async def test_agent_examples_endpoint(client):
    body = await json_of(await client.get('/api/agents/examples'))
    assert body == {'enabled': False, 'agents': []}
    save(client, run(1, [task(1, agent='knowledge', text='10 km in miles')]), run(2, [task(2, agent='chat', text='news today')]),
         run(3, [task(3, agent='chat', text='sum these rows')]))
    for qid, correct in ((1, 'math'), (2, 'research'), (3, 'data')):
        await json_of(await client.post('/api/labels', json={'qid': qid, 'tid': f'{qid}.1', 'verdict': 'wrong', 'correct': correct}))
    await client.post('/control', json={'route_examples': True})
    body = await json_of(await client.get('/api/agents/examples'))
    # keyless: research isn't offered, so its example isn't shown; file agents are offered whenever a file is attached
    assert body == {'enabled': True, 'agents': [
        {'agent': 'math', 'examples': ['10 km in miles'], 'not': []},
        {'agent': 'knowledge', 'examples': [], 'not': ['10 km in miles']},
        {'agent': 'chat', 'examples': [], 'not': ['sum these rows', 'news today']},
        {'agent': 'data', 'examples': ['sum these rows'], 'not': []}]}


# ---------- per-eval override ----------

def one_case(tmp_path, monkeypatch):
    cases = tmp_path / 'cases.jsonl'
    cases.write_text('{"id": "w", "query": "weather in Paris", "expect_agents": ["weather"], "tags": []}\n')
    monkeypatch.setattr(evals_mod, 'CASES', cases)


async def run_eval(client, **body):
    eid = (await json_of(await client.post('/api/evals/run', json={'engine': 'none', **body})))['eval_id']
    await client.router.evals[eid]
    return eid


async def test_eval_override_does_not_touch_the_global_switch(client, tmp_path, monkeypatch):
    router, jev = client.router, client.router.jev
    one_case(tmp_path, monkeypatch)
    save(client, run(1, [task(1, agent='knowledge', text='10 km in miles')]))
    await client.post('/api/labels', json={'qid': 1, 'tid': '1.1', 'verdict': 'wrong', 'correct': 'math'})

    on = await run_eval(client, examples=True)
    assert router.route_examples is False and isinstance(jev.criteria[-1]['math'], dict)
    e = await json_of(await client.get(f'/api/evals/{on}'))
    assert e['examples'] is True and router.get_run(e['cases'][0]['qid'])['tasks'][0]['examples'] is True
    from tests.test_labels import ask
    await ask(client, 'weather in Paris')  # a normal run afterwards still follows the switch (off)
    assert jev.criteria[-1] == router.agents

    await client.post('/control', json={'route_examples': True})
    off = await run_eval(client, examples=False)
    assert router.route_examples is True and jev.criteria[-1] == router.agents
    assert (await json_of(await client.get(f'/api/evals/{off}')))['examples'] is False
    default = await run_eval(client)  # omitted: the switch as it is when the eval starts
    assert (await json_of(await client.get(f'/api/evals/{default}')))['examples'] is True
    listed = {x['eval_id']: x['examples'] for x in (await json_of(await client.get('/api/evals')))['evals']}
    assert listed == {on: True, off: False, default: True}
    assert (await client.post('/api/evals/run', json={'examples': 'on'})).status == 400


async def test_eval_with_examples_holds_out_its_own_case(client, tmp_path, monkeypatch):
    """With examples on, an eval never shows Jev a label whose text is the case under test (promoted cases always
    are), so the A/B measures whether routing generalises rather than recall of the answer."""
    router, jev = client.router, client.router.jev
    cases = tmp_path / 'cases.jsonl'
    cases.write_text('{"id": "km", "query": "10  KM in miles", "expect_agents": ["math"], "tags": []}\n')
    monkeypatch.setattr(evals_mod, 'CASES', cases)
    save(client, run(1, [task(1, agent='knowledge', text='10 km in miles')], source='eval'),
         run(2, [task(2, agent='knowledge', text='5 feet in cm')]))
    for qid in (1, 2):
        await client.post('/api/labels', json={'qid': qid, 'tid': f'{qid}.1', 'verdict': 'wrong', 'correct': 'math'})

    eid = await run_eval(client, examples=True)
    e = await json_of(await client.get(f'/api/evals/{eid}'))
    assert e['examples'] is True and router.get_run(e['cases'][0]['qid'])['tasks'][0]['examples'] is True
    sent = jev.criteria[-1]
    assert sent['math']['examples'] == ['5 feet in cm'] and sent['knowledge']['not'] == ['5 feet in cm']

    # a normal run with the switch on still learns from every label, the case's text included
    await client.post('/control', json={'route_examples': True})
    from tests.test_labels import ask
    await ask(client, 'weather in Paris')
    assert jev.criteria[-1]['math']['examples'] == ['5 feet in cm', '10 km in miles']
    # and with examples off, the eval sends the plain descriptions as before
    await run_eval(client, examples=False)
    assert jev.criteria[-1] == router.agents


# ---------- compare ----------

def ev(eid, cases, examples=False, engine='none'):
    passed = sum(c['pass'] for c in cases)
    return {'eval_id': eid, 'at': time.time(), 'engine': engine, 'status': 'done', 'done': len(cases), 'passed': passed,
            'total': len(cases), 'accuracy': round(passed / len(cases), 4), 'silent_wrong': 0, 'examples': examples,
            'cases': cases}


def case(cid, ok, tokens=None):
    c = {'id': cid, 'query': f'q {cid}', 'tags': [], 'pass': ok, 'reasons': [], 'agents': [], 'answer': '', 'ms': 1, 'qid': 1,
         'all_ok': True}
    return {**c, 'jev_tokens': tokens} if tokens is not None else c


async def test_compare_evals(client):
    store = client.router.store
    store.save_eval(ev('ea', [case('x', False, 400), case('y', True, 500), case('z', True, 600)]))
    store.save_eval(ev('eb', [case('x', True, 700), case('y', True, 800), case('n', False, 900)], examples=True, engine='codex'))
    store.save_eval(ev('old', [case('x', True)]))  # scored before jev_tokens existed
    body = await json_of(await client.get('/api/evals/compare?a=ea&b=eb'))
    assert body['a'] == {'eval_id': 'ea', 'engine': 'none', 'examples': False, 'accuracy': 0.6667, 'passed': 2, 'total': 3,
                         'silent_wrong': 0, 'mean_jev_tokens': 500.0}
    assert body['b'] == {'eval_id': 'eb', 'engine': 'codex', 'examples': True, 'accuracy': 0.6667, 'passed': 2, 'total': 3,
                         'silent_wrong': 0, 'mean_jev_tokens': 800.0}
    assert body['cases'] == [{'id': 'x', 'query': 'q x', 'a_pass': False, 'b_pass': True},
                             {'id': 'y', 'query': 'q y', 'a_pass': True, 'b_pass': True},
                             {'id': 'z', 'query': 'q z', 'a_pass': True, 'b_pass': None},
                             {'id': 'n', 'query': 'q n', 'a_pass': None, 'b_pass': False}]
    assert (await json_of(await client.get('/api/evals/compare?a=old&b=ea')))['a']['mean_jev_tokens'] == 0.0
    for q, status in (('a=ea&b=nope', 404), ('a=nope&b=ea', 404), ('a=ea', 400), ('', 400)):
        assert (await client.get(f'/api/evals/compare?{q}')).status == status, q


async def test_eval_cases_record_jev_tokens(client, tmp_path, monkeypatch):
    one_case(tmp_path, monkeypatch)
    eid = await run_eval(client)
    c = (await json_of(await client.get(f'/api/evals/{eid}')))['cases'][0]
    assert c['jev_tokens'] == client.router.get_run(c['qid'])['tokens']['jev_in'] > 0
    body = await json_of(await client.get(f'/api/evals/compare?a={eid}&b={eid}'))
    assert body['a']['mean_jev_tokens'] == c['jev_tokens'] and body['cases'][0]['a_pass'] is True


# ---------- evals table migration ----------

def test_old_evals_table_gains_the_examples_column(tmp_path):
    path = tmp_path / 'old.db'
    db = sqlite3.connect(path)
    db.execute('CREATE TABLE evals(id TEXT PRIMARY KEY, at REAL, engine TEXT, status TEXT, done INTEGER, passed INTEGER, '
               'total INTEGER, accuracy REAL, silent_wrong INTEGER, cases TEXT)')
    db.execute("INSERT INTO evals VALUES ('e1', 1.0, 'none', 'done', 1, 1, 1, 1.0, 0, '[]')")
    db.commit()
    db.close()
    s = Store(path)
    assert s.get_eval('e1')['examples'] is False and s.list_evals()[0]['examples'] is False
    s.save_eval(ev('e2', [case('x', True, 10)], examples=True))
    assert s.get_eval('e2')['examples'] is True and s.counts()['labels'] == 0
    s.close()
    s = Store(path)  # opening again is a no-op
    assert [e['eval_id'] for e in s.list_evals()] == ['e2', 'e1']
    s.close()


def test_read_labels_is_read_only(tmp_path):
    missing = tmp_path / 'none.db'
    assert read_labels(missing) == [] and not missing.exists()
    legacy = tmp_path / 'legacy.db'
    sqlite3.connect(legacy).execute('CREATE TABLE runs(qid INTEGER)').connection.close()
    assert read_labels(legacy) == []
    s = Store(tmp_path / 'app.db')
    s.save_label({'id': 'l1', 'qid': 1, 'tid': '1.1', 'text': 't', 'picked': 'chat', 'correct': 'math', 'verdict': 'wrong',
                  'confidence': 0.5, 'margin': 0.1, 'note': None, 'at': 1.0, 'promoted': None})
    assert [x['id'] for x in read_labels(tmp_path / 'app.db')] == ['l1']
    s.close()


async def test_cli_examples_flag(tmp_path, monkeypatch):
    """--examples on reads the app's labels (read-only) and routes the eval with them; off leaves them out."""
    import typesafe_sdk
    import jevrouter.pipeline as pipeline
    cases = tmp_path / 'cases.jsonl'
    cases.write_text('{"id": "w", "query": "weather in Paris", "must_match": "Paris", "tags": []}\n')
    monkeypatch.setattr(evals_mod, 'CASES', cases)
    monkeypatch.setattr(evals_mod, 'BASELINE', tmp_path / 'baseline.json')
    db = tmp_path / 'app.db'
    s = Store(db)
    s.save_label({'id': 'l1', 'qid': 1, 'tid': '1.1', 'text': 'rain in Oslo?', 'picked': 'chat', 'correct': 'weather',
                  'verdict': 'wrong', 'confidence': 0.5, 'margin': 0.1, 'note': None, 'at': 1.0, 'promoted': None})
    s.close()
    monkeypatch.setenv('TG_DB', str(db))
    jevs = []
    real = pipeline.Router

    def make(jev, http, engine, **kw):
        jevs.append(FakeJev(route_for=by_keyword))
        return real(jevs[-1], http, engine, registry=fake_registry(), **kw)
    monkeypatch.setattr(pipeline, 'Router', make)
    monkeypatch.setattr(typesafe_sdk, 'AsyncTypeSafeClient', lambda: None)
    assert await evals_mod.cli('none', save=False, examples=True) == 0
    assert jevs[-1].criteria[-1]['weather']['examples'] == ['rain in Oslo?']
    assert await evals_mod.cli('none', save=False, examples=False) == 0
    assert all(isinstance(v, str) for v in jevs[-1].criteria[-1].values())


# ---------- engine health ----------

class Stub:
    def __init__(self, name, fail=None, delay=0.0, refuse=False):
        self.name, self.label, self.billing, self.fail, self.delay, self.refuse = name, name.title(), 'subscription', fail, delay, refuse
        self.supports_web = self.supports_exec = False
        self.calls = 0

    def available(self):
        return True, ''

    def info(self):
        return {'name': self.name, 'label': self.label}

    async def stream(self, **kw):
        self.calls += 1
        await asyncio.sleep(self.delay)
        if self.refuse:
            raise EngineRefusal(Reply('no'))
        if self.fail:
            raise EngineError(self.fail)
        return Reply(f'{self.name} ok')

    async def prewarm(self, **kw):
        pass


def rows(router):
    return {h['name']: h for h in router.health.snapshot(router.engines)}


async def test_health_counts_calls_across_an_auto_fallback():
    codex, claude, api = Stub('codex', fail="You've hit your usage limit " + 'x' * 300), Stub('claude-code', delay=0.01), Stub('anthropic')
    auto = AutoEngine({'codex': codex, 'claude-code': claude, 'anthropic': api}, order=['codex', 'claude-code', 'anthropic'])
    router = Router(FakeJev(), engine=auto, engines={'auto': auto, 'codex': codex, 'claude-code': claude, 'anthropic': api})
    t0 = time.time()
    await auto.stream(system='s', prompt='p')  # codex fails, claude answers
    await auto.stream(system='s', prompt='p')  # codex is cooling down: claude first, no fallback
    await claude.stream(system='s', prompt='p')  # a direct call (an engine override) counts too
    h = rows(router)
    assert set(h) == {'auto', 'codex', 'claude-code', 'anthropic'}
    c = h['codex']
    assert (c['calls'], c['ok'], c['fallbacks_from'], c['fallbacks_to']) == (1, 0, 1, 0)
    assert c['last_error'].startswith("You've hit your usage limit") and len(c['last_error']) <= 160
    assert c['p50_ms'] is None and t0 + 590 < c['cooling_until'] < time.time() + 610
    cl = h['claude-code']
    assert (cl['calls'], cl['ok'], cl['fallbacks_from'], cl['fallbacks_to'], cl['last_error']) == (3, 3, 0, 1, None)
    assert cl['p50_ms'] >= 10 and cl['p95_ms'] >= cl['p50_ms'] and cl['cooling_until'] is None
    assert (h['auto']['calls'], h['auto']['ok']) == (2, 2) and h['anthropic']['calls'] == 0 and h['anthropic']['p95_ms'] is None
    assert h['claude-code']['label'] == 'Claude-Code'


async def test_health_when_every_engine_fails_and_refusals():
    a, b = Stub('a', fail='timed out after 180s'), Stub('b', fail='not logged in')
    auto = AutoEngine({'a': a, 'b': b})
    health = Health()
    instrument_all({'auto': auto}, health)  # inner engines are counted even when only Auto is listed
    with pytest.raises(EngineError):
        await auto.stream(system='s', prompt='p')
    snap = {x['name']: x for x in health.snapshot({'auto': auto, 'a': a, 'b': b})}
    assert (snap['a']['fallbacks_from'], snap['b']['fallbacks_to'], snap['b']['fallbacks_from']) == (1, 1, 0)
    assert snap['auto']['calls'] == 1 and snap['auto']['ok'] == 0 and 'A: timed out' in snap['auto']['last_error']
    refuser = instrument(Stub('r', refuse=True), health)
    with pytest.raises(EngineRefusal):
        await refuser.stream(system='s', prompt='p')
    assert health.stats['r']['ok'] == 1  # the engine worked; it declined


async def test_health_instrumenting_twice_counts_once_and_ignores_cancelled():
    e = Stub('x', delay=0.2)
    first, second = Health(), Health()
    instrument(e, first)
    instrument(e, second)
    task = asyncio.create_task(e.stream(system='s', prompt='p'))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    e.delay = 0
    await e.stream(system='s', prompt='p')
    assert 'x' not in first.stats and second.stats['x']['calls'] == 1


def test_percentiles():
    assert pct([], 0.5) is None and pct([7], 0.95) == 7
    vals = sorted(range(1, 101))
    assert pct(vals, 0.5) == 50 and pct(vals, 0.95) == 95
    h = Health(window=3)
    for ms in (1000, 1, 2, 3):
        h.record('e', True, ms)
    assert list(h.stats['e']['ms']) == [1, 2, 3]  # a bounded window of the latest calls


async def test_health_endpoint(client):
    r = await json_of(await client.get('/api/engines/health'))
    names = [h['name'] for h in r['engines']]
    assert names == list(client.router.engines)
    assert set(r['engines'][0]) == {'name', 'label', 'calls', 'ok', 'fallbacks_from', 'fallbacks_to', 'p50_ms', 'p95_ms',
                                    'last_error', 'cooling_until'}
    await json_of(await client.post('/api/engines/codex/test'))
    # deep mode: the LLM plans and merges (balanced would answer this one without an LLM call)
    await client.post('/ask', json={'query': 'weather in Paris and convert 100 EUR to INR', 'engine': 'claude-code',
                                    'mode': 'deep'})
    await asyncio.gather(*client.router.tasks)
    h = {x['name']: x for x in (await json_of(await client.get('/api/engines/health')))['engines']}
    assert h['codex']['calls'] == 1 and h['codex']['ok'] == 1 and h['codex']['p50_ms'] is not None
    assert h['claude-code']['calls'] >= 1 and h['agy']['calls'] == 0 and h['agy']['label'] == 'Antigravity'


async def test_an_engine_out_of_quota_fails_at_once_until_unblocked():
    agy = Stub('agy', fail='Individual quota reached. Resets in 156h', delay=0.05)
    health = Health()
    instrument(agy, health)
    with pytest.raises(EngineError):
        await agy.stream(system='s', prompt='p')  # the slow, real failure
    t0 = time.perf_counter()
    with pytest.raises(EngineError, match='quota'):
        await agy.stream(system='s', prompt='p')  # pinned engine, no Auto: skipped without asking the CLI
    assert time.perf_counter() - t0 < 0.02 and agy.calls == 1
    assert health.snapshot({'agy': agy})[0]['cooling_until'] is not None
    agy.fail = None
    unblock(agy)  # Test or picking the engine again after the quota was refilled
    assert (await agy.stream(system='s', prompt='p')).text == 'agy ok' and agy.calls == 2


async def test_a_timeout_does_not_block_the_engine():
    e = Stub('e', fail='timed out after 180s')
    instrument(e, Health())
    for _ in range(2):
        with pytest.raises(EngineError):
            await e.stream(system='s', prompt='p')
    assert e.calls == 2  # timeouts may be one-offs: keep asking

"""Routing that learns, M1-M3 (docs/PLAN-learning.md): labels, the review queue and promoting labels to eval cases."""
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jevrouter import app as appmod
from jevrouter import evals as evals_mod
from jevrouter import labels as labels_mod
from jevrouter.pipeline import Router
from jevrouter.store import Store
from tests.fakes import FakeJev
from tests.test_api import engines, finish, json_of
from tests.test_pipeline import by_keyword, fake_registry


@pytest.fixture
async def client():
    box = {}

    def factory(http):
        box['r'] = Router(FakeJev(route_for=by_keyword), http, None, engines=engines(), store=Store(), registry=fake_registry())
        return box['r']
    async with TestClient(TestServer(appmod.create_app(factory))) as c:
        c.router = box['r']
        yield c


def task(qid, n=1, agent='weather', probs=None, conf=None, ok=True, text=None, **kw):
    probs = probs or {agent: 0.9, 'knowledge': 0.05, 'chat': 0.05}
    t = {'tid': f'{qid}.{n}', 'text': text or f'question {qid}.{n}', 'depends_on': [], 'agent': agent,
         'pick': agent if agent in probs else max(probs, key=probs.get), 'probabilities': probs,
         'confidence': conf if conf is not None else max(probs.values()), 'ok': ok, 'answer': 'a'}
    return {**t, **kw}


def run(qid, tasks, source='you', session_id=None, at=None, total_ms=1000, status='done'):
    return {'qid': qid, 'text': f'query {qid}', 'source': source, 'at': at if at is not None else 1000.0 + qid * 1000,
            'plan': None, 'tasks': tasks, 'merged': None, 'total_ms': total_ms, 'error': None, 'status': status,
            'engine': None, 'session_id': session_id, 'compare_id': None, 'files': [], 'tokens': {'jev_in': 0, 'llm_in': 0, 'llm_out': 0}}


def save(client, *recs):
    for r in recs:
        client.router.store.save_run(r)


async def ask(client, query, **body):
    qid = (await json_of(await client.post('/ask', json={'query': query, **body})))['qid']
    await finish(client, qid)
    return qid


# ---------- M1: labels ----------

async def test_label_right_copies_the_decision_from_the_run(client):
    qid = await ask(client, 'weather in Paris')
    t = client.router.get_run(qid)['tasks'][0]
    label = await json_of(await client.post('/api/labels', json={'qid': qid, 'tid': t['tid'], 'verdict': 'right',
                                                                 'correct': 'math', 'note': '  looks good '}))
    assert set(label) == {'id', 'qid', 'tid', 'text', 'picked', 'correct', 'verdict', 'confidence', 'margin', 'note', 'at', 'promoted'}
    assert label['text'] == 'weather in Paris' and label['picked'] == 'weather' and label['correct'] == 'weather'  # right: correct = picked
    assert label['confidence'] == pytest.approx(0.9) and label['margin'] == pytest.approx(0.9 - 0.1 / (len(client.router.agents) - 1), abs=1e-4)
    assert label['note'] == 'looks good' and label['promoted'] is None and label['verdict'] == 'right'


async def test_label_wrong_validation(client):
    qid = await ask(client, 'weather in Paris')
    tid = f'{qid}.1'
    body = {'qid': qid, 'tid': tid, 'verdict': 'wrong'}
    for extra, status, msg in (({}, 400, 'correct is required'), ({'correct': ''}, 400, 'correct is required'),
                               ({'correct': 'gpt'}, 400, 'gpt is not an agent'), ({'correct': 'weather'}, 400, 'mark it right'),
                               ({'correct': 5}, 400, 'correct is required')):
        r = await client.post('/api/labels', json={**body, **extra})
        assert r.status == status and msg in (await r.json())['error'], extra
    # built-ins (engine-only ones too), file agents, custom agents and the guards are all valid answers
    client.router.customs = [{'name': 'poet', 'description': 'Writes short poems', 'prompt': 'p', 'web': False}]
    for correct in ('math', 'research', 'run', 'document', 'data', 'poet', 'clarify', 'blocked'):
        label = await json_of(await client.post('/api/labels', json={**body, 'correct': correct}))
        assert label['correct'] == correct and label['picked'] == 'weather' and label['verdict'] == 'wrong'


async def test_label_accepts_an_agent_the_run_offered(client):
    """A custom agent deleted since the run is still a valid answer: Jev was offered it for that decision."""
    save(client, run(5, [task(5, agent='weather', probs={'weather': 0.6, 'oldbot': 0.4})]))
    label = await json_of(await client.post('/api/labels', json={'qid': 5, 'tid': '5.1', 'verdict': 'wrong', 'correct': 'oldbot'}))
    assert label['correct'] == 'oldbot' and label['margin'] == pytest.approx(0.2)


async def test_label_request_shape_errors(client):
    qid = await ask(client, 'weather in Paris')
    tid = f'{qid}.1'
    for body in ({'qid': str(qid), 'tid': tid, 'verdict': 'right'}, {'qid': True, 'tid': tid, 'verdict': 'right'},
                 {'qid': qid, 'tid': 5, 'verdict': 'right'}, {'qid': qid, 'tid': '', 'verdict': 'right'},
                 {'qid': qid, 'tid': tid, 'verdict': 'maybe'}, {'qid': qid, 'tid': tid}, {'qid': qid, 'tid': tid, 'verdict': 'right', 'note': 3}):
        r = await client.post('/api/labels', json=body)
        assert r.status == 400 and 'error' in await r.json(), body
    assert (await client.post('/api/labels', data='nope')).status == 400


async def test_label_404s_and_unrouted(client):
    qid = await ask(client, 'weather in Paris')
    for body in ({'qid': 999, 'tid': '999.1'}, {'qid': qid, 'tid': f'{qid}.7'}, {'qid': qid, 'tid': 'merge'}):
        r = await client.post('/api/labels', json={**body, 'verdict': 'right'})
        assert r.status == 404, body
    save(client, run(50, [{'tid': '50.1', 'text': 'x', 'error': 'Jev: down'}]))
    r = await client.post('/api/labels', json={'qid': 50, 'tid': '50.1', 'verdict': 'right'})
    assert r.status == 409 and 'never routed' in (await r.json())['error']


async def test_sandbox_runs_cannot_be_labelled(client):
    sid = 'sandbox-test-1'
    body = await json_of(await client.post('/ask', json={'query': 'weather in Paris', 'source': 'sandbox', 'sandbox_id': sid}))
    qid = body['qid']
    r = await client.post('/api/labels', json={'qid': qid, 'tid': f'{qid}.1', 'verdict': 'right'})
    assert r.status == 404  # in flight: known only to its sandbox
    await finish(client, qid)
    r = await client.post('/api/labels', json={'qid': qid, 'tid': f'{qid}.1', 'verdict': 'right'})
    assert r.status == 404 and 'no saved run' in (await r.json())['error']


async def test_one_label_per_subtask(client):
    save(client, run(3, [task(3, text='10 km in miles', agent='knowledge')]))
    first = await json_of(await client.post('/api/labels', json={'qid': 3, 'tid': '3.1', 'verdict': 'wrong', 'correct': 'math'}))
    client.router.store.set_promoted(first['id'], 'u-' + first['id'])
    same = await json_of(await client.post('/api/labels', json={'qid': 3, 'tid': '3.1', 'verdict': 'wrong', 'correct': 'math',
                                                                'note': 'again'}))
    assert same['id'] == first['id'] and same['promoted'] == 'u-' + first['id'] and same['note'] == 'again'
    changed = await json_of(await client.post('/api/labels', json={'qid': 3, 'tid': '3.1', 'verdict': 'right'}))
    # a promoted label keeps its case when the answer changes: the case is rewritten to the new answer
    assert changed['id'] == first['id'] and changed['correct'] == 'knowledge' and changed['promoted'] == 'u-' + first['id']
    labels = (await json_of(await client.get('/api/labels')))['labels']
    assert len(labels) == 1 and labels[0] == changed


async def test_list_and_delete_labels(client):
    save(client, run(1, [task(1, agent='knowledge'), task(1, 2, agent='weather')]), run(2, [task(2, agent='math')]))
    a = await json_of(await client.post('/api/labels', json={'qid': 1, 'tid': '1.1', 'verdict': 'wrong', 'correct': 'math'}))
    b = await json_of(await client.post('/api/labels', json={'qid': 1, 'tid': '1.2', 'verdict': 'right'}))
    c = await json_of(await client.post('/api/labels', json={'qid': 2, 'tid': '2.1', 'verdict': 'right'}))
    ids = lambda r: [x['id'] for x in r['labels']]
    assert ids(await json_of(await client.get('/api/labels'))) == [c['id'], b['id'], a['id']]  # newest first
    assert ids(await json_of(await client.get('/api/labels?agent=math'))) == [c['id'], a['id']]  # agent matches correct
    assert ids(await json_of(await client.get('/api/labels?verdict=wrong'))) == [a['id']]
    assert ids(await json_of(await client.get('/api/labels?qid=1&verdict=right'))) == [b['id']]
    assert ids(await json_of(await client.get('/api/labels?limit=1'))) == [c['id']]
    assert len((await json_of(await client.get('/api/labels?limit=99999')))['labels']) == 3  # capped, not an error
    for bad in ('limit=x', 'qid=x', 'verdict=maybe'):
        assert (await client.get(f'/api/labels?{bad}')).status == 400, bad
    assert await json_of(await client.delete(f"/api/labels/{a['id']}")) == {'ok': True}
    assert (await client.delete(f"/api/labels/{a['id']}")).status == 404
    assert ids(await json_of(await client.get('/api/labels'))) == [c['id'], b['id']]


async def test_label_an_in_flight_run(client):
    """A subtask that has routed can be marked before the run finishes (Chat shows the trace as it streams)."""
    import asyncio
    from tests.test_api import slow_weather
    client.router.registry['weather'] = slow_weather
    qid = (await json_of(await client.post('/ask', json={'query': 'weather in Paris'})))['qid']
    for _ in range(100):
        if (client.router.inflight.get(qid) or {}).get('tasks') and 'agent' in client.router.inflight[qid]['tasks'][0]:
            break
        await asyncio.sleep(0.01)
    label = await json_of(await client.post('/api/labels', json={'qid': qid, 'tid': f'{qid}.1', 'verdict': 'right'}))
    assert label['picked'] == 'weather'
    client.router.cancel(qid)


# ---------- M2: review queue ----------

def reasons_of(body):
    return {(i['qid'], i['tid']): i['reasons'] for i in body['items']}


async def test_review_reasons(client):
    save(client,
         run(1, [task(1, conf=0.5, probs={'weather': 0.5, 'knowledge': 0.2, 'chat': 0.3})]),           # low confidence
         run(2, [task(2, probs={'math': 0.62, 'knowledge': 0.5}, agent='math', conf=0.62)]),            # low margin only
         run(3, [task(3, agent='clarify', probs={'chat': 0.3, 'knowledge': 0.25, 'math': 0.45}, conf=0.45, ok=False)]),
         run(4, [task(4, ok=False), task(4, 2, agent='blocked', ok=False, probs={'chat': 0.95, 'math': 0.05})]),
         run(5, [task(5)]),                                                                            # healthy
         run(6, [task(6)], session_id='s', at=10_000.0, total_ms=2000),
         run(7, [task(7)], session_id='s', at=10_040.0, total_ms=1000),  # 38 s after 6 finished: 6 was re-asked
         run(8, [task(8)], session_id='s', at=10_200.0))                 # 159 s after 7: not a re-ask
    body = await json_of(await client.get('/api/review'))
    assert body['scanned'] == 8
    assert reasons_of(body) == {
        (6, '6.1'): ['re-asked'],
        (4, '4.1'): ['agent failed'],  # the blocked guard's ok=false is not an agent failure
        (3, '3.1'): ['low confidence', 'clarify'],
        (2, '2.1'): ['low margin'],
        (1, '1.1'): ['low confidence'],
    }
    items = {i['qid']: i for i in body['items']}
    assert [i['qid'] for i in body['items']] == [6, 4, 3, 2, 1]  # newest first
    one = items[1]
    assert set(one) == {'qid', 'tid', 'text', 'picked', 'confidence', 'margin', 'runner_up', 'probabilities', 'reasons', 'at'}
    assert one['picked'] == 'weather' and one['runner_up'] == 'chat' and one['margin'] == pytest.approx(0.2)
    assert list(one['probabilities']) == ['weather', 'chat', 'knowledge'] and one['text'] == 'question 1.1'
    assert items[3]['picked'] == 'clarify' and items[3]['runner_up'] == 'math'  # a guard's runner-up is Jev's top choice
    assert items[2]['margin'] == pytest.approx(0.12)


async def test_review_skips_eval_compare_labelled_and_unrouted(client):
    weak = dict(conf=0.3, probs={'weather': 0.3, 'chat': 0.35, 'math': 0.35})
    save(client, run(1, [task(1, **weak)], source='eval'), run(2, [task(2, **weak)], source='compare'),
         run(3, [task(3, **weak), task(3, 2, **weak), {'tid': '3.3', 'text': 'x', 'error': 'Jev: down'},
                 {'tid': 'merge', 'agent': 'weather', 'confidence': 0.1}]),
         run(4, [task(4, **weak)], status='running'))
    assert set(reasons_of(await json_of(await client.get('/api/review')))) == {(3, '3.1'), (3, '3.2')}
    await json_of(await client.post('/api/labels', json={'qid': 3, 'tid': '3.1', 'verdict': 'right'}))
    assert set(reasons_of(await json_of(await client.get('/api/review')))) == {(3, '3.2')}


async def test_review_filter_limit_and_window(client, monkeypatch):
    save(client, *(run(q, [task(q, conf=0.5, probs={'weather': 0.5, 'chat': 0.45, 'math': 0.05})]) for q in range(1, 6)),
         run(6, [task(6, agent='clarify', probs={'chat': 0.9, 'math': 0.1}, conf=0.9)]))
    assert [i['qid'] for i in (await json_of(await client.get('/api/review?reason=clarify')))['items']] == [6]
    assert [i['qid'] for i in (await json_of(await client.get('/api/review?reason=low%20margin')))['items']] == [5, 4, 3, 2, 1]
    assert [i['qid'] for i in (await json_of(await client.get('/api/review?limit=2')))['items']] == [6, 5]
    assert (await client.get('/api/review?reason=boring')).status == 400
    assert (await client.get('/api/review?limit=x')).status == 400
    monkeypatch.setattr(labels_mod, 'REVIEW_SCAN', 3)
    body = await json_of(await client.get('/api/review'))
    assert body['scanned'] == 3 and [i['qid'] for i in body['items']] == [6, 5, 4]


async def test_review_window_skips_eval_and_compare_runs(client, monkeypatch):
    """The window counts only runs people asked: a burst of evals newer than the last chat can't empty the queue."""
    monkeypatch.setattr(labels_mod, 'REVIEW_SCAN', 3)
    weak = dict(conf=0.3, probs={'weather': 0.3, 'chat': 0.35, 'math': 0.35})
    save(client, run(1, [task(1, **weak)], session_id='s'), run(2, [task(2, **weak)], session_id='s', at=2000.0 + 5),
         *(run(q, [task(q, **weak)], source='eval') for q in range(3, 9)),
         *(run(q, [task(q, **weak)], source='compare') for q in range(9, 12)))
    body = await json_of(await client.get('/api/review'))
    assert body['scanned'] == 2 and [i['qid'] for i in body['items']] == [2, 1]
    assert 're-asked' in reasons_of(body)[(1, '1.1')]  # the next turn is still in the window
    assert [r['qid'] for r in client.router.store.recent_runs(20)] == list(range(11, 0, -1))  # no filter: every source


async def test_review_backfills_from_real_runs(client):
    """Runs made before labels existed still feed the queue: `hmm` routes to clarify at low confidence."""
    qid = await ask(client, 'hmm')
    body = await json_of(await client.get('/api/review'))
    assert reasons_of(body)[(qid, f'{qid}.1')] == ['low confidence', 'clarify']


# ---------- M3: promote ----------

async def label(client, qid, correct, text='10 km in miles', picked='knowledge'):
    save(client, run(qid, [task(qid, agent=picked, text=text)]))
    return await json_of(await client.post('/api/labels', json={'qid': qid, 'tid': f'{qid}.1', 'verdict': 'wrong', 'correct': correct}))


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


async def test_promote_is_idempotent(client, local_cases):
    lab = await label(client, 1, 'math')
    r = await json_of(await client.post(f"/api/labels/{lab['id']}/promote"))
    assert r == {'case_id': f"u-{lab['id']}", 'created': True}
    assert lines(local_cases) == [{'id': f"u-{lab['id']}", 'query': '10 km in miles', 'expect_agents': ['math'], 'tags': ['user']}]
    assert await json_of(await client.post(f"/api/labels/{lab['id']}/promote")) == {'case_id': f"u-{lab['id']}", 'created': False}
    assert len(lines(local_cases)) == 1
    assert (await json_of(await client.get('/api/labels')))['labels'][0]['promoted'] == f"u-{lab['id']}"
    guard = await label(client, 2, 'clarify', text='the thing from before')
    await json_of(await client.post(f"/api/labels/{guard['id']}/promote"))
    assert lines(local_cases)[1] == {'id': f"u-{guard['id']}", 'query': 'the thing from before', 'expect_outcome': 'clarify',
                                     'tags': ['user']}
    # deleting a label leaves its case
    await client.delete(f"/api/labels/{lab['id']}")
    assert len(lines(local_cases)) == 2


async def test_promote_after_relabel_rewrites_the_case(client, local_cases):
    lab = await label(client, 1, 'math')
    await client.post(f"/api/labels/{lab['id']}/promote")
    relabel = await json_of(await client.post('/api/labels', json={'qid': 1, 'tid': '1.1', 'verdict': 'wrong', 'correct': 'currency'}))
    assert relabel['id'] == lab['id'] and relabel['promoted'] == f"u-{lab['id']}"
    assert lines(local_cases) == [{'id': f"u-{lab['id']}", 'query': '10 km in miles', 'expect_agents': ['currency'], 'tags': ['user']}]
    assert await json_of(await client.post(f"/api/labels/{lab['id']}/promote")) == {'case_id': f"u-{lab['id']}", 'created': False}
    assert lines(local_cases) == [{'id': f"u-{lab['id']}", 'query': '10 km in miles', 'expect_agents': ['currency'], 'tags': ['user']}]


async def test_relabelling_a_promoted_label_keeps_its_case_in_step(client, local_cases):
    """A promoted label whose answer changes rewrites its case in place (the eval must not keep the retracted answer)."""
    local_cases.write_text('{"id": "mine", "query": "q", "expect_outcome": "answer", "tags": []}\n')
    lab = await label(client, 1, 'math')
    await client.post(f"/api/labels/{lab['id']}/promote")
    cid = f"u-{lab['id']}"
    # marked right instead: the case now expects what Jev picked, and the label stays promoted
    right = await json_of(await client.post('/api/labels', json={'qid': 1, 'tid': '1.1', 'verdict': 'right'}))
    assert right['promoted'] == cid and client.router.store.get_label(lab['id'])['promoted'] == cid
    assert lines(local_cases)[1] == {'id': cid, 'query': '10 km in miles', 'expect_agents': ['knowledge'], 'tags': ['user']}
    assert [c for c in evals_mod.load_cases() if c['id'] == cid][0]['expect_agents'] == ['knowledge']
    # a guard answer becomes expect_outcome
    await client.post('/api/labels', json={'qid': 1, 'tid': '1.1', 'verdict': 'wrong', 'correct': 'clarify'})
    assert lines(local_cases)[1] == {'id': cid, 'query': '10 km in miles', 'expect_outcome': 'clarify', 'tags': ['user']}
    # the same answer again (a new note) leaves the file alone
    before = local_cases.read_text()
    same = await json_of(await client.post('/api/labels', json={'qid': 1, 'tid': '1.1', 'verdict': 'wrong', 'correct': 'clarify',
                                                                'note': 'still unclear'}))
    assert same['promoted'] == cid and local_cases.read_text() == before
    # a file agent can't be an eval case: the stale case goes and the label is no longer promoted
    doc = await json_of(await client.post('/api/labels', json={'qid': 1, 'tid': '1.1', 'verdict': 'wrong', 'correct': 'document'}))
    assert doc['promoted'] is None and client.router.store.get_label(lab['id'])['promoted'] is None
    assert [c['id'] for c in lines(local_cases)] == ['mine']
    assert cid not in {c['id'] for c in evals_mod.load_cases()}
    assert not labels_mod.tmp_path_for(local_cases).exists()


async def test_promote_temp_file_is_git_ignored(client, local_cases, monkeypatch):
    """A rewrite killed before os.replace leaves cases.local.jsonl.tmp, which holds personal questions: it must be
    covered by .gitignore like cases.local.jsonl itself."""
    import shutil
    import subprocess
    from pathlib import Path
    lab = await label(client, 1, 'math')
    await client.post(f"/api/labels/{lab['id']}/promote")
    await client.post('/api/labels', json={'qid': 1, 'tid': '1.1', 'verdict': 'right'})  # sync rewrites the case
    assert not labels_mod.tmp_path_for(local_cases).exists()

    def crash(*a):
        raise KeyboardInterrupt
    monkeypatch.setattr(labels_mod.os, 'replace', crash)
    with pytest.raises(KeyboardInterrupt):
        labels_mod.rewrite(local_cases, ['{"id": "x", "query": "private"}'])
    tmp = labels_mod.tmp_path_for(local_cases)
    assert tmp.exists() and tmp.name == 'cases.local.jsonl.tmp'
    repo = Path(__file__).resolve().parent.parent
    if not shutil.which('git') or not (repo / '.git').exists():
        pytest.skip('not a git checkout')
    for name in ('evals/cases.local.jsonl', f'evals/{tmp.name}'):
        assert subprocess.run(['git', 'check-ignore', '-q', name], cwd=repo).returncode == 0, name


async def test_promote_errors(client, local_cases):
    assert (await client.post('/api/labels/nope/promote')).status == 404
    doc = await label(client, 1, 'document', text='summarise the attached notes')
    r = await client.post(f"/api/labels/{doc['id']}/promote")
    assert r.status == 400 and 'file agent' in (await r.json())['error'] and not local_cases.exists()


async def test_promote_appends_to_a_file_without_a_trailing_newline(client, local_cases):
    local_cases.write_text('{"id": "mine", "query": "q", "expect_outcome": "answer", "tags": []}')
    lab = await label(client, 1, 'math')
    await client.post(f"/api/labels/{lab['id']}/promote")
    assert [c['id'] for c in lines(local_cases)] == ['mine', f"u-{lab['id']}"]


def test_load_cases_merges_the_local_file(tmp_path, monkeypatch, local_cases):
    committed = tmp_path / 'cases.jsonl'
    committed.write_text('{"id": "a", "query": "one", "tags": ["x"]}\n{"id": "b", "query": "two", "tags": []}\n')
    monkeypatch.setattr(evals_mod, 'CASES', committed)
    assert [c['id'] for c in evals_mod.load_cases()] == ['a', 'b']  # no local file is fine
    local_cases.write_text('{"id": "u-1", "query": "10 km in miles", "expect_agents": ["math"], "tags": ["user"]}\n'
                           'not json\n{"id": "a", "query": "a local copy", "tags": ["user"]}\n[1, 2]\n{"id": "u-2"}\n'
                           '{"id": "u-3", "query": "no tags"}\n{"id": "u-1", "query": "second copy", "tags": ["user"]}\n\n')
    cases = evals_mod.load_cases()
    assert [c['id'] for c in cases] == ['a', 'b', 'u-1', 'u-3']
    assert cases[0]['query'] == 'one' and cases[2]['query'] == '10 km in miles' and cases[3]['tags'] == []
    assert [c['id'] for c in evals_mod.load_cases(committed)] == ['a', 'b']  # an explicit path reads that file only


async def test_promoted_case_runs_in_evals(client, local_cases, tmp_path, monkeypatch):
    committed = tmp_path / 'cases.jsonl'
    committed.write_text('{"id": "w", "query": "weather in Paris", "expect_agents": ["weather"], "tags": []}\n')
    monkeypatch.setattr(evals_mod, 'CASES', committed)
    lab = await label(client, 1, 'currency', text='convert 100 EUR to INR', picked='math')
    await client.post(f"/api/labels/{lab['id']}/promote")
    eid = (await json_of(await client.post('/api/evals/run', json={'engine': 'none'})))['eval_id']
    await client.router.evals[eid]
    d = await json_of(await client.get(f'/api/evals/{eid}'))
    assert [(c['id'], c['pass'], c['tags']) for c in d['cases']] == [('w', True, []), (f"u-{lab['id']}", True, ['user'])]

"""The eval harness (docs/PLAN-speed-evals-chat.md, Track B): case schema, multi-turn sessions, fixture files, repeats
and flakiness, paraphrases, latency budgets, per-tag scores, percentiles, stored extras and the CLI options.
The judge itself is tested in tests/test_evals_judge.py."""
import asyncio
import json
import sqlite3

import pytest

from jevrouter import evals
from jevrouter.agents import AgentResult
from jevrouter.pipeline import Router
from jevrouter.store import Store
from tests.fakes import FakeEngine, FakeJev, ScriptEngine


def route(text):
    for word, agent in (('weather', 'weather'), ('orders', 'data'), ('EUR', 'currency'), ('double', 'math'), ('%', 'math')):
        if word in text:
            return agent, 0.9
    return 'chat', 0.3


def registry(**extra):
    async def weather(text, emit):
        return AgentResult('Paris: now 18°C, clear sky', True, 'open-meteo.com')

    async def currency(text, emit):
        return AgentResult('100.00 EUR = 9,000.00 INR', True, 'frankfurter.app')

    async def math(text, emit):
        return AgentResult('15% of 240 = 36' if '%' in text else 'double = 72', True)
    return {'weather': weather, 'currency': currency, 'math': math, **extra}


def router(**kw):
    return Router(FakeJev(route_for=route), registry=kw.pop('registry', None) or registry(), **kw)


async def run(r, cases, **kw):
    return await evals.run_eval(r, 'e1', None, 'none', cases, **kw)


# ---------- schema ----------

def test_validate_case_errors(tmp_path):
    ok = {'id': 'a', 'query': 'weather in Paris', 'tags': ['w'], 'must_match': 'Paris'}
    assert evals.validate_case(ok) == [] and evals.validate_case(ok, strict=True) == []
    assert evals.validate_case({'id': 'a', 'query': 'q'}) == []  # promoted or hand-written cases may expect nothing
    assert evals.validate_case({'id': 'a', 'query': 'q'}, strict=True) == [
        'a case needs at least one expectation (expect_agents, expect_outcome, must_match, must_not_match or judge)']
    bad = {
        'bad id': ({**ok, 'id': 'has space'}, 'id must be'),
        'unknown key': ({**ok, 'expect': 'x'}, "unknown key 'expect'"),
        'split': ({**ok, 'split': 'test'}, 'split must be dev or holdout'),
        'outcome': ({**ok, 'expect_outcome': 'maybe'}, 'expect_outcome must be one of'),
        'regex': ({**ok, 'must_match': '(unclosed'}, 'must_match is not a valid regex'),
        'one turn': ({'id': 'm', 'turns': [{'query': 'hi'}]}, 'turns must be a list of at least two'),
        'turn key': ({'id': 'm', 'turns': [{'query': 'a', 'must_match': 'x'}, {'query': 'b', 'foo': 1}]}, "turn 2: unknown key 'foo'"),
        'turn regex': ({'id': 'm', 'turns': [{'query': 'a'}, {'query': 'b', 'must_not_match': '['}]}, 'turn 2: must_not_match'),
        'turn + case expectation': ({'id': 'm', 'must_match': 'x', 'turns': [{'query': 'a'}, {'query': 'b'}]}, 'keeps its expectations on its turns'),
        'fixture': ({**ok, 'files': ['nope.csv']}, 'no fixture evals/fixtures/nope.csv'),
        'fixture path': ({**ok, 'files': ['../cases.jsonl']}, 'no fixture'),
        'judge_min alone': ({**ok, 'judge_min': 4}, 'judge_min needs a judge rubric'),
        'judge_min range': ({**ok, 'judge': 'r', 'judge_min': 9}, 'judge_min must be a number from 1 to 5'),
        'max_ms': ({**ok, 'max_ms': -1}, 'max_ms must be a positive'),
        'strict alone': ({**ok, 'strict_ms': True}, 'strict_ms must be true or false and needs max_ms'),
        'paraphrase dup': ({**ok, 'paraphrases': ['x', 'x']}, 'paraphrases must be'),
        'paraphrase = query': ({**ok, 'paraphrases': ['weather in Paris']}, 'paraphrases must be'),
    }
    for name, (case, want) in bad.items():
        errors = evals.validate_case(case)
        assert any(want in e for e in errors), (name, errors)
    good_turns = {'id': 'm', 'tags': [], 'turns': [{'query': 'a', 'must_match': 'x'}, {'query': 'b'}], 'split': 'holdout',
                  'files': ['orders.csv'], 'judge': 'rubric', 'judge_min': 3, 'max_ms': 100, 'strict_ms': False}
    assert evals.validate_case(good_turns, strict=True) == []
    assert evals.case_query(good_turns) == 'a' and evals.case_kind(good_turns) == 'multi_turn'
    assert [evals.case_kind(c) for c in (ok, {**ok, 'files': ['orders.csv']}, {**ok, 'judge': 'r'})] == ['single', 'file', 'judge']


def test_read_cases_names_the_bad_line(tmp_path):
    p = tmp_path / 'cases.jsonl'
    p.write_text('{"id": "a", "query": "q", "tags": []}\n\n{"id": "b", "query": "q", "split": "x"}\n')
    with pytest.raises(evals.CaseError, match=r'cases.jsonl line 3 \(b\): split must be dev or holdout'):
        evals.read_cases(p)
    p.write_text('{"id": "a", "query": "q"}\n{"id": "a", "query": "q2"}\n')
    with pytest.raises(evals.CaseError, match="line 2 \\(a\\): duplicate id 'a'"):
        evals.read_cases(p)
    p.write_text('{"id": "a", "query": "q"\n')
    with pytest.raises(evals.CaseError, match='line 1: not JSON'):
        evals.read_cases(p)


def test_local_cases_skip_invalid(local_cases):
    local_cases.write_text('{"id": "l1", "query": "q", "tags": []}\n{"id": "l2", "query": "q", "split": "bogus"}\nnot json\n')
    assert [c['id'] for c in evals.local_cases()] == ['l1']


def test_select_by_split_and_tags():
    cases = [{'id': 'a', 'tags': ['x']}, {'id': 'b', 'tags': ['y'], 'split': 'holdout'}, {'id': 'c', 'tags': ['x', 'z'], 'split': 'dev'}]
    ids = lambda cs: [c['id'] for c in cs]
    assert ids(evals.select(cases)) == ['a', 'b', 'c']
    assert ids(evals.select(cases, 'dev')) == ['a', 'c'] and ids(evals.select(cases, 'holdout')) == ['b']
    assert ids(evals.select(cases, 'all', ['z', 'y'])) == ['b', 'c'] and ids(evals.select(cases, 'dev', ['y'])) == []


def test_committed_suite_shape():
    cases = evals.load_cases(evals.CASES)
    assert 180 <= len(cases) <= 260
    holdout = sum(c.get('split') == 'holdout' for c in cases)
    assert 0.2 <= holdout / len(cases) <= 0.3
    kinds = {k: sum(evals.case_kind(c) == k for c in cases) for k in ('multi_turn', 'file', 'judge')}
    assert kinds['multi_turn'] >= 15 and kinds['file'] >= 25 and kinds['judge'] >= 10
    assert sum(bool(c.get('paraphrases')) for c in cases) >= 10 and sum('max_ms' in c for c in cases) >= 8
    tags = {t for c in cases for t in c['tags']}
    assert {'safety', 'injection', 'honesty', 'units', 'dates', 'files', 'multi-turn', 'dag', 'latency'} <= tags
    for f in {f for c in cases for f in c.get('files', [])}:  # every fixture extracts as the upload path would
        from jevrouter.files import extract
        meta, text = extract(f, (evals.FIXTURES / f).read_bytes())
        assert text.strip(), f


# ---------- running ----------

async def test_multi_turn_runs_in_one_session_with_context():
    r = router()
    case = {'id': 'mt', 'tags': ['multi-turn'], 'turns': [
        {'query': "What's 15% of 240?", 'expect_agents': ['math'], 'must_match': r'\b36\b'},
        {'query': 'now double that', 'expect_agents': ['math'], 'must_match': r'\b72\b'},
        {'query': 'weather please', 'must_match': 'Tokyo'}]}
    s = await run(r, [case])
    c = s['cases'][0]
    assert c['kind'] == 'multi_turn' and c['query'] == "What's 15% of 240?" and not c['pass']
    assert [t['pass'] for t in c['turns']] == [True, True, False]
    assert c['reasons'] == ['turn 3: answer does not match /Tokyo/']
    assert set(c['turns'][0]) == {'query', 'pass', 'reasons', 'answer', 'agents', 'ms', 'qid'}
    runs = [r.get_run(t['qid']) for t in c['turns']]
    sid = runs[0]['session_id']
    assert sid and sid.startswith('eval-e1-') and {x['session_id'] for x in runs} == {sid}
    assert [t['query'] for t in r.store.turns(sid, runs[2]['qid'], 3)] == [x['query'] for x in case['turns'][:2]]
    assert c['qid'] == runs[2]['qid'] and c['agents'] == ['math', 'math', 'clarify']
    assert r.store.list_sessions() == []  # eval sessions never show up as chats


async def test_file_cases_attach_fixtures_through_the_store():
    r = router()
    case = {'id': 'f', 'query': 'What is the total of the orders?', 'files': ['orders.csv'], 'tags': ['files'],
            'expect_agents': ['data'], 'must_match': r'8,000'}
    s = await run(r, [case, {**case, 'id': 'f2'}])
    a, b = s['cases']
    assert a['pass'] and b['pass'] and a['kind'] == 'file', a
    ra, rb = r.get_run(a['qid']), r.get_run(b['qid'])
    assert ra['files'] and ra['files'] == rb['files']  # uploaded once per eval
    assert r.store.list_files() == []  # and removed when the eval ends


async def test_repeat_and_paraphrases_make_attempts_and_flakiness():
    calls = []

    async def weather(text, emit):
        calls.append(text)
        return AgentResult('Paris: sunny' if len(calls) % 2 else 'no idea', True)
    r = router(registry=registry(weather=weather))
    case = {'id': 'w', 'query': 'weather in Paris', 'tags': ['w'], 'must_match': 'Paris', 'paraphrases': ['what is the weather in Paris']}
    s = await run(r, [case], repeat=2)
    c = s['cases'][0]
    # each phrasing gave the same outcome on both runs: the paraphrase fails, which is a failure, not flakiness
    assert len(calls) == 4 and c['attempts'] == [True, False, True, False] and not c['flaky'] and not c['pass']
    assert c['reasons'] == ['run 1, paraphrase 1: answer does not match /Paris/', 'run 2, paraphrase 1: answer does not match /Paris/']
    assert c['answer'] == 'no idea'  # the first failing attempt is the one shown
    assert s['flaky'] == 0 and s['repeat'] == 2 and s['silent_wrong'] == 1
    calls.clear()
    once = await run(r, [case])  # repeat 1: two phrasings, one run each
    c = once['cases'][0]
    assert len(calls) == 2 and not c['pass'] and not c['flaky'] and 'attempts' not in c and once['flaky'] == 0
    assert c['reasons'] == ['paraphrase 1: answer does not match /Paris/']
    calls.clear()
    single = {k: v for k, v in case.items() if k != 'paraphrases'}
    flip = await run(r, [single], repeat=2)  # the same text passes once and fails once
    c = flip['cases'][0]
    assert c['attempts'] == [True, False] and c['flaky'] and not c['pass'] and flip['flaky'] == 1
    steady = await run(router(), [{'id': 's', 'query': 'weather in Paris', 'tags': [], 'must_match': 'Paris'}], repeat=3)
    assert steady['cases'][0]['attempts'] == [True, True, True] and not steady['cases'][0]['flaky'] and steady['flaky'] == 0
    once = await run(router(), [{'id': 's', 'query': 'weather in Paris', 'tags': [], 'must_match': 'Paris'}])
    assert 'attempts' not in once['cases'][0] and once['cases'][0]['flaky'] is False


async def test_latency_budget():
    async def slow(text, emit):
        await asyncio.sleep(0.03)
        return AgentResult('Paris: sunny', True)
    r = router(registry=registry(weather=slow))
    base = {'query': 'weather in Paris', 'tags': [], 'must_match': 'Paris', 'max_ms': 5}
    s = await run(r, [{**base, 'id': 'loose'}, {**base, 'id': 'strict', 'strict_ms': True}, {**base, 'id': 'roomy', 'max_ms': 60000}])
    loose, strict, roomy = s['cases']
    assert loose['pass'] and loose['over_budget'] and loose['max_ms'] == 5
    assert not strict['pass'] and strict['over_budget'] and strict['reasons'][0].endswith('over the 5 ms budget')
    assert roomy['pass'] and not roomy['over_budget']
    # the budgets are keyless ones: on an engine, over budget is reported but a strict case doesn't fail on it
    s = await evals.run_eval(r, 'e2', ScriptEngine(), 'claude-code', [{**base, 'id': 'strict', 'strict_ms': True}])
    strict = s['cases'][0]
    assert strict['pass'] and strict['over_budget'] and not strict['reasons']


async def test_harness_error_fails_only_that_case(monkeypatch):
    r = router()
    monkeypatch.setattr(evals, 'FIXTURES', evals.FIXTURES / 'missing-dir')
    s = await run(r, [{'id': 'f', 'query': 'orders', 'files': ['orders.csv'], 'tags': []},
                      {'id': 'w', 'query': 'weather in Paris', 'tags': [], 'must_match': 'Paris'}])
    f, w = s['cases']
    assert s['status'] == 'done' and w['pass'] and not f['pass'] and f['reasons'][0].startswith('eval harness error:')


async def test_split_tags_and_summary_fields_are_stored():
    r = router()
    cases = [{'id': 'a', 'query': 'weather in Paris', 'tags': ['w', 'x'], 'must_match': 'Paris'},
             {'id': 'b', 'query': 'convert 100 EUR to INR', 'tags': ['c', 'x'], 'must_match': 'GBP', 'split': 'holdout'}]
    eid = evals.start(r, None, 'none', cases, split='all', tags=['x'])
    await r.evals[eid]
    got = evals.get(r.store, eid)
    assert got['split'] == 'all' and got['tags'] == ['x'] and got['repeat'] == 1 and got['judge'] is None
    assert got['by_tag'] == {'c': {'passed': 0, 'total': 1}, 'w': {'passed': 1, 'total': 1}, 'x': {'passed': 1, 'total': 2}}
    assert isinstance(got['p50_ms'], int) and got['p95_ms'] >= got['p50_ms'] and got['judge_mean'] is None
    assert got['cases'][1]['split'] == 'holdout' and got['cases'][0]['split'] == 'dev'
    assert '_ms' not in got and evals.listed(r.store)[0]['by_tag'] == got['by_tag']
    eid = evals.start(r, None, 'none', cases, split='holdout')
    await r.evals[eid]
    assert [c['id'] for c in evals.get(r.store, eid)['cases']] == ['b']


# ---------- summaries ----------

def test_percentile_and_by_tag():
    assert evals.percentile([], 50) is None and evals.percentile([None], 95) is None
    assert evals.percentile([5], 95) == 5
    xs = list(range(1, 101))
    assert evals.percentile(xs, 50) == 50 and evals.percentile(xs, 95) == 95 and evals.percentile(list(reversed(xs)), 95) == 95
    assert evals.percentile([10, 20, 30, 40], 50) == 20
    case = lambda i, ok, tags, ms, **k: {'id': i, 'pass': ok, 'tags': tags, 'ms': ms, 'all_ok': True, **k}
    s = evals.summary({'eval_id': 'e', 'at': 0, 'engine': 'none', 'status': 'done', 'total': 3, 'cases': [
        case('a', True, ['x'], 100, judge={'mean': 4.0}),
        case('b', False, ['x', 'y'], 300, flaky=True, judge={'mean': 2.5}),
        case('c', True, [], 0, turns=[{'ms': 50}, {'ms': 70}])]})
    assert s['by_tag'] == {'x': {'passed': 1, 'total': 2}, 'y': {'passed': 0, 'total': 1}}
    assert (s['p50_ms'], s['p95_ms'], s['flaky'], s['judge_mean']) == (70, 300, 1, 3.25)  # multi-turn counts each turn
    assert list(s['by_tag']) == sorted(s['by_tag'])
    s2 = evals.summary({'eval_id': 'e', 'at': 0, 'engine': 'none', 'status': 'done', 'total': 1, 'cases': [], '_ms': [7, 9]})
    assert s2['p50_ms'] == 7 and '_ms' not in s2


def test_matrix_table():
    r = lambda name, passed, tags: {'engine': name, 'passed': passed, 'total': 4, 'accuracy': passed / 4, 'silent_wrong': 0,
                                    'flaky': 0, 'p50_ms': 10, 'p95_ms': 20, 'judge_mean': None, 'by_tag': tags}
    t = evals.matrix_table([r('none', 2, {'math': {'passed': 2, 'total': 2}}), r('codex', 4, {'math': {'passed': 2, 'total': 2}, 'web': {'passed': 2, 'total': 2}})])
    lines = t.splitlines()
    assert lines[0].split() == ['none', 'codex'] and lines[1].split() == ['passed', '2/4', '4/4']
    assert lines[-1].split() == ['tag', 'web', '-', '2/2']


# ---------- storage ----------

def test_old_database_is_migrated(tmp_path):
    path = tmp_path / 'old.db'
    db = sqlite3.connect(path)
    db.executescript('CREATE TABLE evals(id TEXT PRIMARY KEY, at REAL, engine TEXT, status TEXT, done INTEGER, passed INTEGER, '
                     'total INTEGER, accuracy REAL, silent_wrong INTEGER, cases TEXT);')
    db.execute("INSERT INTO evals VALUES ('old', 1, 'none', 'done', 1, 1, 1, 1.0, 0, ?)",
               (json.dumps([{'id': 'a', 'query': 'q', 'pass': True}]),))
    db.commit()
    db.close()
    store = Store(path)
    old = evals.get(store, 'old')
    assert old['cases'][0]['id'] == 'a' and 'by_tag' not in old and old['examples'] is False
    new = evals.summary({'eval_id': 'new', 'at': 2, 'engine': 'none', 'status': 'done', 'total': 1, 'examples': True,
                         'split': 'dev', 'repeat': 2, 'judge': None, 'tags': None,
                         'cases': [{'id': 'a', 'query': 'q', 'pass': True, 'tags': ['t'], 'ms': 5, 'all_ok': True}]})
    evals.save(store, new)
    evals.save(store, new)  # saving again (progress) replaces, and the migration runs once
    listed = evals.listed(store)
    assert [e['eval_id'] for e in listed] == ['new', 'old'] and listed[0]['by_tag'] == {'t': {'passed': 1, 'total': 1}}
    assert listed[0]['repeat'] == 2 and 'repeat' not in listed[1]
    store.close()
    store = Store(path)  # reopening a migrated database keeps working
    assert evals.get(store, 'new')['split'] == 'dev' and evals.get(store, 'nope') is None
    store.close()


# ---------- HTTP ----------

from tests.test_api import client, json_of  # noqa: E402,F401  (the app fixture with fake engines)


async def test_run_eval_body_validation(client, tmp_path, monkeypatch):
    cases = tmp_path / 'cases.jsonl'
    cases.write_text('{"id": "w", "query": "weather in Paris", "must_match": "Paris", "tags": ["w"]}\n'
                     '{"id": "h", "query": "weather in Paris again", "must_match": "Paris", "tags": ["x"], "split": "holdout"}\n')
    monkeypatch.setattr(evals, 'CASES', cases)
    post = lambda body: client.post('/api/evals/run', json=body)
    for body, status in (({'split': 'test'}, 400), ({'tags': 'w'}, 400), ({'tags': ['']}, 400), ({'repeat': 0}, 400),
                         ({'repeat': 6}, 400), ({'repeat': 2.0}, 400), ({'repeat': True}, 400), ({'judge': 'gpt-9'}, 400),
                         ({'judge': 'none'}, 400), ({'judge': 'agy'}, 409), ({'tags': ['nothing']}, 400)):
        assert (await post(body)).status == status, body
    eid = (await json_of(await post({'engine': 'none', 'split': 'holdout', 'repeat': 2, 'tags': ['x']})))['eval_id']
    await client.router.evals[eid]
    d = await json_of(await client.get(f'/api/evals/{eid}'))
    assert (d['split'], d['repeat'], d['tags'], d['judge'], d['total']) == ('holdout', 2, ['x'], None, 1)
    assert d['cases'][0]['attempts'] == [True, True] and d['by_tag'] == {'x': {'passed': 1, 'total': 1}}
    # a judge that is the engine under test is swapped for another available one
    eid = (await json_of(await post({'engine': 'codex', 'judge': 'codex', 'tags': ['w']})))['eval_id']
    await client.router.evals[eid]
    assert (await json_of(await client.get(f'/api/evals/{eid}')))['judge'] == 'claude-code'
    cmp = await json_of(await client.get(f'/api/evals/compare?a={eid}&b={eid}'))
    assert cmp['cases'] == [{'id': 'w', 'query': 'weather in Paris', 'a_pass': cmp['cases'][0]['a_pass'], 'b_pass': cmp['cases'][0]['a_pass']}]
    cases.write_text('{"id": "w", "query": "q", "split": "nope"}\n')
    assert (await post({})).status == 500


# ---------- CLI ----------

def test_cli_args():
    a = evals.parse_args(['--engine', 'none', '--split', 'holdout', '--tags', 'a, b,', '--repeat', '3', '--judge', 'auto', '--json'])
    assert (a.engine, a.split, a.tags, a.repeat, a.judge, a.as_json, a.matrix) == ('none', 'holdout', ['a', 'b'], 3, 'auto', True, False)
    d = evals.parse_args([])
    assert (d.split, d.tags, d.repeat, d.judge, d.matrix, d.save_baseline) == ('all', None, 1, None, False, False)
    for bad in (['--repeat', '9'], ['--split', 'test'], ['--matrix', '--engine', 'codex']):
        with pytest.raises(SystemExit):
            evals.parse_args(bad)


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from tests.test_pipeline import fake_registry
    import jevrouter.pipeline as pipeline
    cases = tmp_path / 'cases.jsonl'
    cases.write_text('{"id": "w", "query": "weather in Paris", "must_match": "Paris", "tags": ["w"]}\n'
                     '{"id": "h", "query": "weather in Paris?", "must_match": "Paris", "tags": ["h"], "split": "holdout", '
                     '"judge": "says the weather"}\n')
    monkeypatch.setattr(evals, 'CASES', cases)
    monkeypatch.setattr(evals, 'BASELINE', tmp_path / 'baseline.json')
    real = pipeline.Router
    monkeypatch.setattr(pipeline, 'Router', lambda jev, http, engine, **kw: real(FakeJev(route_for=route), http, engine,
                                                                                registry=fake_registry(), **kw))
    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, 'AsyncTypeSafeClient', lambda: None)
    import jevrouter.engines as engines_mod
    monkeypatch.setattr(engines_mod, 'catalog', lambda: {'codex': FakeEngine('codex', 'Codex', ok=False)})
    return tmp_path


async def test_cli_json_split_tags(cli_env, capsys):
    assert await evals.cli('none', save=False, split='holdout', as_json=True) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['split'] == 'holdout' and [c['id'] for c in out['cases']] == ['h'] and out['cases'][0]['judge'] is None
    assert await evals.cli('none', save=False, tags=['w'], repeat=2) == 0
    printed = capsys.readouterr().out
    assert 'PASS  w' in printed and 'by tag:' in printed and 'repeat 2' in printed and 'split all' in printed
    assert await evals.cli('none', save=False, judge='auto') == 0
    assert '1 rubric cases were not judged: judge skipped: no engine is available to judge (keyless)' in capsys.readouterr().out
    assert await evals.cli('none', save=False, judge='codex') == 2
    assert await evals.cli('none', save=False, tags=['nothing']) == 2


async def test_save_baseline_merges_a_partial_run(cli_env):
    base = cli_env / 'baseline.json'
    base.write_text('{"none": {"w": false, "h": false, "gone": true}, "codex": {"w": true}}')
    assert await evals.cli('none', save=True, split='holdout') == 0
    saved = json.loads(base.read_text())
    # only h was run: w keeps its entry, a case no longer in cases.jsonl drops out, other engines are untouched
    assert saved == {'none': {'w': False, 'h': True}, 'codex': {'w': True}}
    assert await evals.cli('none', save=True, tags=['w']) == 0
    assert json.loads(base.read_text())['none'] == {'w': True, 'h': True}


async def test_cli_matrix_warns_and_prints_table(cli_env, capsys):
    assert await evals.cli(None, save=False, matrix=True) == 0
    got = capsys.readouterr()
    assert 'spends your subscription or API quota' in got.err and '--matrix runs 1 engines (none)' in got.err
    table = got.out.split('\n\n')[-1].splitlines()
    assert table[0].split() == ['none'] and table[1].split() == ['passed', '2/2'] and table[2].split() == ['accuracy', '100%']
    assert ['tag', 'h', '1/1'] in [line.split() for line in table] and ['tag', 'w', '1/1'] in [line.split() for line in table]
    assert await evals.cli(None, save=False, matrix=True, as_json=True) == 0
    assert [r['engine'] for r in json.loads(capsys.readouterr().out)] == ['none']

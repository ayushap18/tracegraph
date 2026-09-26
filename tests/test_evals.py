"""Eval cases file, scoring, summaries and regressions."""
import re

from jevrouter import evals
from jevrouter.evals import load_cases, regressions, score, summary


def rec(tasks, answer='', status='done', qid=1):
    return {'qid': qid, 'status': status, 'tasks': tasks, 'merged': {'answer': answer, 'engine': 'single'} if answer else None,
            'total_ms': 12, 'error': None}


def test_cases_file_is_valid():
    cases = load_cases()
    assert len(cases) >= 40 and len({c['id'] for c in cases}) == len(cases)
    assert sum('stress' in c['tags'] for c in cases) >= 30 and sum('control' in c['tags'] for c in cases) >= 10
    allowed = {'id', 'query', 'expect_agents', 'expect_outcome', 'must_match', 'must_not_match', 'tags'}
    for c in cases:
        assert set(c) <= allowed and c['query'] and isinstance(c['tags'], list)
        assert c.get('expect_outcome') in (None, 'clarify', 'blocked', 'answer')
        for k in ('must_match', 'must_not_match'):
            if k in c:
                re.compile(c[k])
        assert any(k in c for k in ('expect_agents', 'expect_outcome', 'must_match', 'must_not_match')), c['id']


def test_score_checks_every_expectation():
    case = {'id': 'x', 'query': 'q', 'tags': ['t'], 'expect_agents': ['weather', 'currency'], 'expect_outcome': 'answer',
            'must_match': 'inr', 'must_not_match': 'sorry'}
    good = rec([{'agent': 'weather', 'ok': True}, {'agent': 'currency', 'ok': True}], 'Paris sunny; 100 EUR = 9000 INR')
    s = score(case, good)
    assert s['pass'] and s['reasons'] == [] and s['agents'] == ['weather', 'currency'] and s['ms'] == 12 and s['qid'] == 1
    assert s['expect_agents'] == ['weather', 'currency'] and s['expect_outcome'] == 'answer' and s['tags'] == ['t']
    bad = rec([{'agent': 'weather', 'ok': True}, {'agent': 'clarify', 'ok': False}], 'sorry, no idea')
    r = score(case, bad)['reasons']
    assert r == ['expected agent currency, got weather, clarify', 'expected outcome answer, got clarify',
                 'answer does not match /inr/', 'answer matches forbidden /sorry/']
    assert score({'id': 'y', 'query': 'q', 'expect_outcome': 'blocked'}, rec([{'agent': 'blocked', 'ok': False}]))['pass']
    assert score({'id': 'z', 'query': 'q'}, rec([], status='cancelled'))['reasons'] == ['run ended cancelled']
    assert score({'id': 'n', 'query': 'q'}, rec([]))['expect_agents'] is None


def test_summary_counts_silent_wrong():
    ok_wrong = score({'id': 'a', 'query': 'q', 'must_match': 'EUR'}, rec([{'agent': 'currency', 'ok': True}], '1.00 INR'))
    loud_wrong = score({'id': 'b', 'query': 'q', 'expect_outcome': 'answer'}, rec([{'agent': 'clarify', 'ok': False}], 'hm'))
    right = score({'id': 'c', 'query': 'q'}, rec([{'agent': 'math', 'ok': True}], '42'))
    s = summary({'eval_id': 'e', 'at': 0, 'engine': 'none', 'status': 'done', 'total': 4, 'cases': [ok_wrong, loud_wrong, right]})
    assert (s['done'], s['passed'], s['accuracy'], s['silent_wrong']) == (3, 1, 0.25, 1)


def test_regressions_are_per_engine():
    result = {'engine': 'none', 'cases': [{'id': 'a', 'pass': False}, {'id': 'b', 'pass': True}, {'id': 'c', 'pass': False}]}
    assert regressions(result, {'none': {'a': True, 'b': True, 'c': False}, 'codex': {'c': True}}) == ['a']
    assert regressions(result, {}) == []


async def test_cli_exit_codes(tmp_path, monkeypatch):
    """The CLI prints a table and exits 1 on a regression against the baseline; --save-baseline records one."""
    from tests.fakes import FakeJev
    from tests.test_pipeline import by_keyword, fake_registry
    import jevrouter.pipeline as pipeline
    cases = tmp_path / 'cases.jsonl'
    cases.write_text('{"id": "w", "query": "weather in Paris", "must_match": "Paris", "tags": []}\n'
                     '{"id": "h", "query": "hmm", "expect_outcome": "answer", "tags": []}\n')
    monkeypatch.setattr(evals, 'CASES', cases)
    monkeypatch.setattr(evals, 'BASELINE', tmp_path / 'baseline.json')
    real = pipeline.Router
    monkeypatch.setattr(pipeline, 'Router', lambda jev, http, engine, **kw: real(FakeJev(route_for=by_keyword), http, engine,
                                                                                registry=fake_registry(), **kw))
    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, 'AsyncTypeSafeClient', lambda: None)
    assert await evals.cli('none', save=True) == 0
    assert (tmp_path / 'baseline.json').read_text().count('true') == 1
    (tmp_path / 'baseline.json').write_text('{"none": {"w": true, "h": true}}')
    assert await evals.cli('none', save=False) == 1
    assert await evals.cli('gpt-9', save=False) == 2

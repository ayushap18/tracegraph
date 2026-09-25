import json

import pytest

from jevrouter.planner import candidate_split, plan
from tests.fakes import FakeAnthropic, FakeJev, api_errors


@pytest.mark.parametrize('q,parts', [
    ('weather in Paris and convert 100 EUR to INR', ['weather in Paris', 'convert 100 EUR to INR']),
    ('Who was Alan Turing; then convert 50 GBP to USD', ['Who was Alan Turing', 'convert 50 GBP to USD']),
    ("What time is it in Tokyo and what's 15% of 380?", ['What time is it in Tokyo', "what's 15% of 380?"]),
    ('weather in Oslo, and the time in Lima', ['weather in Oslo', 'the time in Lima']),
    ('18% of 2450 then 3 times 7 also 2 plus 2', ['18% of 2450', '3 times 7', '2 plus 2']),
    ('1 plus 1; 2 plus 2; 3 plus 3; 4 plus 4; 5 plus 5', ['1 plus 1', '2 plus 2', '3 plus 3', '4 plus 4 and 5 plus 5']),
])
def test_candidate_split(q, parts):
    assert candidate_split(q) == parts


@pytest.mark.parametrize('q', ['What is salt and pepper?', 'rock and roll', 'hmm', '18% of 2450', 'Who was Ada Lovelace?',
                               'tom and jerry cartoons'])
def test_candidate_no_split(q):
    assert candidate_split(q) == [q]


async def test_heuristic_splits_when_jev_agrees():
    jev = FakeJev(multi=0.8)
    p = await plan('weather in Paris and convert 100 EUR to INR', jev)
    assert p['planner'] == 'heuristic' and p['multi'] == 0.8
    assert p['subtasks'] == ['weather in Paris', 'convert 100 EUR to INR']
    assert jev.calls[0][1] == ['multi'] and p['jev_tokens'] == 100


async def test_heuristic_keeps_whole_when_jev_disagrees():
    q = 'Compare Python and Java for beginners'
    assert len(candidate_split(q)) == 2  # text alone would split
    p = await plan(q, FakeJev(multi=0.2))
    assert p['subtasks'] == [q] and p['multi'] == 0.2


async def test_heuristic_skips_jev_for_single_request():
    jev = FakeJev()
    p = await plan('hmm', jev)
    assert p['subtasks'] == ['hmm'] and p['multi'] is None and not jev.calls


async def test_heuristic_survives_jev_failure():
    p = await plan('weather in Paris and convert 100 EUR to INR', FakeJev(multi=RuntimeError('jev down')))
    assert p['subtasks'] == ['weather in Paris and convert 100 EUR to INR'] and p['multi'] is None


async def test_claude_planner():
    claude = FakeAnthropic(['{"subtasks": ["weather in Paris", ', '"convert 100 EUR to INR"]}'])
    jev = FakeJev()
    p = await plan('weather in Paris and convert 100 EUR to INR', jev, claude)
    assert p['planner'] == 'claude' and p['subtasks'] == ['weather in Paris', 'convert 100 EUR to INR'] and p['multi'] is None
    assert p['claude_in'] == 10 and not jev.calls
    kw = claude.calls[0]
    assert kw['model'] == 'claude-opus-5' and kw['output_config']['effort'] == 'low'
    assert kw['output_config']['format']['type'] == 'json_schema' and kw['thinking'] == {'type': 'adaptive'}
    assert kw['betas'] == ['server-side-fallback-2026-07-01'] and kw['fallbacks'] == 'default'


async def test_claude_planner_caps_at_four():
    claude = FakeAnthropic([json.dumps({'subtasks': [f'task {i}' for i in range(6)] + ['  ']})])
    p = await plan('x', FakeJev(), claude)
    assert p['subtasks'] == ['task 0', 'task 1', 'task 2', 'task 3']


@pytest.mark.parametrize('bad', ['rate', 'status', 'conn', 'json', 'refusal', 'empty'])
async def test_claude_planner_falls_back(bad):
    item = {'json': ['not json'], 'refusal': ('refusal', ['no']), 'empty': ['{"subtasks": []}']}.get(bad) or api_errors()[bad]
    p = await plan('weather in Paris and convert 100 EUR to INR', FakeJev(multi=0.9), FakeAnthropic(item))
    assert p['planner'] == 'heuristic' and len(p['subtasks']) == 2

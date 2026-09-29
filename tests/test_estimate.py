"""The cost estimate (docs/PLAN-files-robust.md 5.1 to 5.3): calibrated on recorded runs, keyless drafts cost nothing,
the run 2750 draft needs confirming on Antigravity, resume prices only what is missing, several engines add up, and what
a finished run really used."""
import json
from pathlib import Path

import pytest

from jevrouter import estimate as est
from jevrouter.config import COST_CONFIRM_TOKENS

FIXTURE = Path(__file__).resolve().parent.parent / 'evals' / 'fixtures' / 'estimate_runs.jsonl'
VIEWS = {
    'agy': est.EngineView('agy', 'Antigravity', 'subscription', True),
    'claude-code': est.EngineView('claude-code', 'Claude Code', 'subscription', True),
    'codex': est.EngineView('codex', 'Codex', 'subscription', True),
    'openai': est.EngineView('openai', 'OpenAI API', 'api', True),
}
Q2750 = ('create the ppt on the how mobile phone is being evolved history past present everything a ppt of 12 slides '
         'using the multiple pictured diagrams and also use the design.md for the design')
DESIGN = {'id': 'd1', 'name': 'DESIGN-lovable.md', 'kind': 'text', 'chars': 17284}


def rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def draft_of(row: dict) -> est.Draft:
    return est.Draft(row['query'], row.get('mode') or 'balanced', row.get('agent'), list(row.get('files') or []),
                     bool(row.get('has_answer')))


def deadline(row: dict) -> float:
    return 900.0 if row['qid'] >= 2749 else 300.0


@pytest.mark.parametrize('row', rows(), ids=lambda r: str(r['qid']))
def test_within_2x_of_recorded_runs(row):
    """Acceptance (section 9): mid tokens in and mid seconds within 2x of what the run used; its tokens out inside the
    range."""
    e = est.estimate(draft_of(row), VIEWS[row['engine']], VIEWS.get(row.get('web_engine')), deadline_s=deadline(row))
    got = row['actual']
    assert got['tokens_in'] / 2 <= e.tokens_in <= got['tokens_in'] * 2, (e.tokens_in, got)
    assert got['seconds'] / 2 <= e.seconds <= got['seconds'] * 2, (e.seconds, got)
    assert e.range['tokens_out'][0] <= got['tokens_out'] <= e.range['tokens_out'][1], (e.range, got)
    assert e.range['tokens_in'][0] <= e.tokens_in <= e.range['tokens_in'][1]
    assert e.range['calls'][0] <= e.calls <= e.range['calls'][1]


def test_run_2750_on_agy_needs_confirmation_and_offers_claude_code():
    draft = est.Draft(Q2750, 'research', 'create', [DESIGN])
    e = est.estimate(draft, VIEWS['agy'], VIEWS['claude-code'], deadline_s=900,
                     alternatives=[VIEWS['agy'], VIEWS['claude-code']])
    assert e.needs_confirmation and e.long_file and not e.keyless
    assert e.engine == 'agy' and e.engine_label == 'Antigravity' and e.billing == 'subscription' and e.dollars is None
    phases = [b['phase'] for b in e.breakdown]
    assert phases[:3] == ['planner', 'research', 'outline'] and 'merge' in phases
    sections = next(b for b in e.breakdown if b['phase'] == 'sections' and not b['optional'])
    assert sections['calls'] == 2 and sections['engine'] == 'agy'
    assert next(b for b in e.breakdown if b['phase'] == 'research')['engine'] == 'claude-code'
    assert 200_000 <= e.tokens_in <= 300_000 and e.calls == 6
    codes = {r['code'] for r in e.reasons}
    assert {'long_file', 'research', 'engine_overhead', 'design', 'repair'} <= codes
    assert e.cheaper and e.cheaper[0]['engine'] == 'claude-code' and e.cheaper[0]['saves'] >= 0.4
    assert e.summary.startswith('This deck needs about 6 model calls on Antigravity and Claude Code: roughly ')
    assert '–' not in e.summary and '—' not in e.summary
    d = e.to_dict()
    assert set(d) == {'version', 'calls', 'tokens_in', 'tokens_out', 'seconds', 'range', 'engine', 'engine_label',
                      'billing', 'dollars', 'keyless', 'long_file', 'needs_confirmation', 'reasons', 'breakdown',
                      'deadline_s', 'cheaper', 'summary'}
    assert json.loads(json.dumps(d)) == d and d['version'] == 1
    assert set(d['range']) == {'calls', 'tokens_in', 'tokens_out', 'seconds'}
    assert all(set(b) == {'phase', 'engine', 'calls', 'tokens_in', 'tokens_out', 'seconds', 'optional'}
               for b in d['breakdown'])


def test_a_one_slide_deck_on_a_default_engine_does_not_ask():
    fake = est.EngineView('fake', 'Fake', 'subscription', True)
    e = est.estimate(est.Draft('make a 1 slide deck about tea'), fake, None, deadline_s=300)
    assert e.calls == 1 and not e.needs_confirmation and not e.long_file
    assert e.range['calls'] == (1, 2)  # the one repair call may run


def test_keyless_draft_costs_nothing_and_never_asks():
    e = est.estimate(est.Draft(Q2750, 'research', 'create'), None, None, deadline_s=300)
    assert e.keyless and e.calls == 0 and e.tokens_in == 0 and not e.needs_confirmation and e.engine is None
    assert [r['code'] for r in e.reasons] == ['keyless']
    assert not est.needs_confirmation(e)


def test_zero_token_paths_cost_no_create_call():
    agy = VIEWS['agy']
    last = {'id': 'f1', 'name': 'x.pdf', 'format': 'pdf'}
    e = est.estimate(est.Draft('now as slides', last_file=last), agy, None, deadline_s=300)
    assert not any(b['phase'] in ('sections', 'outline') for b in e.breakdown)
    table = {'id': 't', 'name': 'sales.csv', 'kind': 'csv', 'chars': 400, 'columns': ['a', 'b']}
    e = est.estimate(est.Draft('turn this csv into a spreadsheet', files=[table]), agy, None, deadline_s=300)
    assert not any(b['phase'] in ('sections', 'outline') for b in e.breakdown)


def test_api_billing_gives_dollars_and_a_lower_threshold():
    api = VIEWS['openai']  # any API-key engine prices as 'api'
    e = est.estimate(est.Draft('make a 20 page pdf about the history of tea'), api, None, deadline_s=900)
    assert e.billing == 'api' and e.dollars and 0 < e.dollars[0] < e.dollars[1]
    assert e.long_file and e.breakdown[0]['phase'] == 'outline'
    # an API engine writes two batches side by side
    sections = next(b for b in e.breakdown if b['phase'] == 'sections' and not b['optional'])
    assert sections['calls'] >= 2
    assert COST_CONFIRM_TOKENS['api'] < COST_CONFIRM_TOKENS['agy']


def test_thresholds():
    fake = est.EngineView('fake', 'Fake', 'subscription', True)
    e = est.estimate(est.Draft('make a 1 slide deck about tea'), fake, None, deadline_s=300)
    assert not est.needs_confirmation(e)
    e.calls = 5
    assert est.needs_confirmation(e)
    e.calls, e.seconds = 1, 180
    assert est.needs_confirmation(e)
    e.seconds, e.range = 10, {**e.range, 'seconds': (5, 240)}
    assert est.needs_confirmation(e)  # 240 s is 0.8 of the 300 s deadline
    e.range = {**e.range, 'seconds': (5, 20), 'tokens_in': (1, 99_000), 'tokens_out': (1, 999)}
    assert not est.needs_confirmation(e)
    e.range = {**e.range, 'tokens_out': (1, 1_000)}
    assert est.needs_confirmation(e)  # default engine: 100,000 tokens at the high end


def test_for_resume_prices_only_the_missing_parts():
    state = {'v': 1, 'kind': 'longdoc', 'format': 'pptx', 'request': 'a 12 slide deck on phones', 'ctx': 'x' * 4000,
             'parts': [{'heading': f'Part {i}', 'level': 1, 'words': 45, 'hints': [], 'diagrams': [], 'figures': 0}
                       for i in range(11)],
             'written': {str(i): {'heading': f'Part {i}', 'level': 1, 'blocks': [{'type': 'paragraph', 'text': 'x'}],
                                  'notes': ''} for i in range(9)},
             'failed': [], 'phase': 'sections'}
    e = est.for_resume(state, VIEWS['agy'], deadline_s=900)
    sections = [b for b in e.breakdown if b['phase'] == 'sections']
    assert e.calls == 1 and sections == [next(b for b in e.breakdown if not b['optional'])]
    assert any(b['phase'] == 'repair' and b['optional'] for b in e.breakdown)
    assert {r['code'] for r in e.reasons} >= {'resume'} and e.summary.startswith('Resuming this file needs about 1')
    single = {'v': 1, 'kind': 'single', 'format': 'pdf', 'request': 'a pdf', 'reply': '{"title": "x"}'}
    assert est.for_resume(single, VIEWS['agy'], deadline_s=300).calls == 0
    assert est.for_resume({**single, 'reply': None}, VIEWS['agy'], deadline_s=300).calls == 1


def test_combine_adds_calls_and_tokens_and_keeps_the_slowest_time():
    fake = est.EngineView('fake', 'Fake', 'subscription', True)
    a = est.estimate(est.Draft('make a 1 slide deck about tea'), fake, None, deadline_s=300)
    b = est.estimate(est.Draft('make a 1 slide deck about tea'), VIEWS['agy'], None, deadline_s=300)
    c = est.combine([a, b])
    assert c.calls == a.calls + b.calls and c.tokens_in == a.tokens_in + b.tokens_in
    assert c.seconds == max(a.seconds, b.seconds) and c.engine is None
    assert any(r['code'] == 'several_engines' for r in c.reasons) and c.summary.startswith('These 2 answers need')
    assert est.combine([a]) is a


def test_on_auto_the_long_file_is_priced_on_the_engine_the_run_sends_it_to():
    """H8: the run sends a long file on Auto to `cheapest` of Auto's chain (Router.lean_engine), so the estimate
    prices the file's calls there too, while the planner and merge stay on Auto's lead."""
    draft = est.Draft(Q2750, 'research', 'create', [DESIGN])
    chain = [VIEWS['agy'], VIEWS['claude-code']]
    plain = est.estimate(draft, VIEWS['agy'], VIEWS['claude-code'], deadline_s=900)
    lean = est.estimate(draft, VIEWS['agy'], VIEWS['claude-code'], deadline_s=900, lean=chain)
    by_phase = {b['phase']: b['engine'] for b in lean.breakdown if not b['optional']}
    assert by_phase['planner'] == 'agy' and by_phase['outline'] == 'claude-code' and by_phase['sections'] == 'claude-code'
    assert lean.calls == plain.calls and lean.tokens_in < plain.tokens_in
    sick = [VIEWS['agy'], est.EngineView('claude-code', 'Claude Code', 'subscription', True, healthy=False)]
    kept = est.estimate(draft, VIEWS['agy'], VIEWS['claude-code'], deadline_s=900, lean=sick)
    assert kept.tokens_in == plain.tokens_in


def test_cheapest_prefers_the_engine_with_the_fewest_tokens_and_keeps_order_on_ties():
    from jevrouter.create.brief import parse_brief
    brief = parse_brief('a 12 slide deck on phones')
    views = [VIEWS['agy'], VIEWS['claude-code']]
    assert est.cheapest(views, 'a 12 slide deck on phones', brief) == 'claude-code'
    sick = est.EngineView('claude-code', 'Claude Code', 'subscription', True, healthy=False)
    assert est.cheapest([VIEWS['agy'], sick], 'a 12 slide deck on phones', brief) == 'agy'
    twin = est.EngineView('agy', 'Antigravity 2', 'subscription', True)
    assert est.cheapest([VIEWS['agy'], twin], 'x', brief) == 'agy'


def test_p50_shortens_or_lengthens_the_call_time():
    slow = est.EngineView('agy', 'Antigravity', 'subscription', True, p50_ms=60_000)
    quick = est.EngineView('agy', 'Antigravity', 'subscription', True, p50_ms=1_000)
    d = est.Draft('make slides about tea')
    base = est.estimate(d, VIEWS['agy'], None, deadline_s=300).seconds
    assert est.estimate(d, slow, None, deadline_s=300).seconds > base > est.estimate(d, quick, None, deadline_s=300).seconds


def test_actual_of_counts_calls_tokens_and_seconds():
    rec = {'tokens': {'jev_in': 9, 'llm_in': 1200, 'llm_out': 300}, 'total_ms': 12_345,
           'timings': {'planner': 'llm', 'merger': 'llm'},
           'tasks': [{'agent': 'research', 'engine': 'claude-code', 'answer': 'x', 'ok': True},
                     {'agent': 'create', 'engine': 'agy', 'answer': 'y', 'ok': False,
                      'phases': [{'phase': 'outline', 'calls': 1}, {'phase': 'sections', 'calls': 2}]},
                     {'agent': 'math', 'engine': 'keyless', 'answer': '4', 'ok': True}]}
    assert est.actual_of(rec) == {'calls': 6, 'tokens_in': 1200, 'tokens_out': 300, 'seconds': 12.3}
    assert est.actual_of({}) == {'calls': 0, 'tokens_in': 0, 'tokens_out': 0, 'seconds': 0.0}


def test_copy_has_no_dashes():
    for code in est.REASONS:
        text = est.reason(code, label='X', fixed=1, turns=1, batches=2, what='deck', n=2, minutes=15, name='d.md')['text']
        assert '–' not in text and '—' not in text and ' - ' not in text

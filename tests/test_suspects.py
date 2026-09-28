"""suspects(rec) (docs/PLAN-accuracy-v2.md D6): signs that a finished run went wrong."""
import copy
import json
from pathlib import Path

import pytest

from jevrouter import suspects as mod
from jevrouter.create.brief import Brief
from jevrouter.merger import TIME_SENSITIVE_HEDGE
from jevrouter.suspects import CODES, suspects

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'run2741.json').read_text())


def codes(found):
    return [s['code'] for s in found]


def task(tid, text, agent, answer, **kw):
    return {'tid': tid, 'text': text, 'agent': agent, 'answer': answer, 'ok': True, 'depends_on': [], **kw}


def rec(text, tasks, answer='', **merged):
    return {'qid': 1, 'text': text, 'status': 'done', 'tasks': tasks, 'merged': {'answer': answer, 'engine': 'concat', **merged}}


@pytest.fixture
def brief_2741(monkeypatch):
    """The 2741 prompt's brief, fixed here so this test does not depend on the brief parser's progress."""
    monkeypatch.setattr(mod, 'requested', lambda rec: Brief(format='pdf', pages=(12, 13)))


def test_2741_is_suspect_on_every_count(brief_2741):
    found = suspects(FIXTURE['record'])
    assert codes(found) == ['pages_short', 'forced_non_file', 'reply_template_body', 'extra_format', 'agent_label_leak']
    assert set(codes(found)) <= set(CODES)
    notes = {s['code']: s['note'] for s in found}
    assert notes['pages_short'].endswith('has 1 page; the request asked for 12-13.')
    assert notes['forced_non_file'] == 'Step 2741.1 was made to create a file, but its text asks for none.'
    assert 'MD; the request asked for PDF' in notes['extra_format']
    assert all('—' not in n and '–' not in n for n in notes.values())


def test_2741_with_its_stored_specs(brief_2741):
    specs = {c['meta']['id']: c['spec'] for c in FIXTURE['created']}
    found = suspects(FIXTURE['record'], specs)
    assert 'reply_template_body' in codes(found) and 'agent_label_leak' in codes(found)


def test_a_clean_run_has_none():
    r = rec('weather in Paris and convert 100 EUR to INR',
            [task('1.1', 'weather in Paris', 'weather', 'Paris: 18°C'),
             task('1.2', 'convert 100 EUR to INR', 'currency', '100.00 EUR = 9,000.00 INR')],
            '- Paris: 18°C\n- 100.00 EUR = 9,000.00 INR', caveats=[], primary_file=None)
    assert suspects(r) == []


def test_a_file_that_meets_its_own_brief_is_not_short():
    f = {'id': 'p', 'name': 'ai.pdf', 'format': 'pdf', 'pages': 12, 'size': 1, 'source': 'llm',
         'brief': {'format': 'pdf', 'pages': [12, 13], 'slides': None}}
    r = rec('12-13 page pdf on ai', [task('1.1', '12-13 page pdf on ai', 'create', 'Created **ai.pdf**, 12 pages',
                                          created_files=[f])], 'Created **ai.pdf**, 12 pages')
    assert suspects(r) == []
    r['tasks'][0]['created_files'][0]['pages'] = 9
    assert codes(suspects(r)) == ['pages_short']


def test_short_slides_use_the_file_brief():
    f = {'id': 's', 'name': 'deck.pptx', 'format': 'pptx', 'slides': 6, 'size': 1, 'source': 'llm',
         'brief': {'format': 'pptx', 'pages': None, 'slides': [20, 20]}}
    r = rec('20-slide deck', [task('1.1', '20-slide deck', 'create', 'Created **deck.pptx**, 6 slides', created_files=[f])])
    assert suspects(r) == [{'code': 'pages_short', 'note': '**deck.pptx** has 6 slides; the request asked for 20.'}]


def test_forced_create_on_a_single_step_is_what_the_user_asked():
    r = rec('solar power', [task('1.1', 'solar power', 'create', 'Created **solar.md**, 2 KB', forced=True)])
    assert 'forced_non_file' not in codes(suspects(r))


def test_unfulfilled_counts_caveats_but_not_hedges():
    r = rec('q', [task('1.1', 'q', 'knowledge', 'x', caveats=[TIME_SENSITIVE_HEDGE])], 'x')
    assert suspects(r) == []
    r['merged']['caveats'] = ['asked for 12-13 pages, made 9', 'no image found']
    assert suspects(r) == [{'code': 'unfulfilled', 'note': 'The run could not do everything asked: asked for 12-13 '
                                                           'pages, made 9 (and 1 more)'}]


def test_duplicate_guard_texts():
    ask = 'Which city do you mean?'
    r = rec('q', [task('1.1', 'a', 'clarify', ask), task('1.2', 'b', 'clarify', ask + ' ')], ask)
    assert codes(suspects(r)) == ['dup_clarify']
    r['tasks'][1]['answer'] = 'Which currency?'
    assert suspects(r) == []


def test_agent_labels_in_the_answer():
    r = rec('q', [task('1.1', 'a', 'weather', 'Paris: 18°C')], '**weather**: Paris: 18°C')
    assert codes(suspects(r)) == ['agent_label_leak']
    r['merged']['answer'] = '**Note**: a bold word that is no agent'
    assert suspects(r) == []


@pytest.mark.parametrize('answer,code', [
    ('No summary found for "should I learn rust or go".', 'dead_end'),
    ('Tell me two currencies, like "100 USD to INR".', 'dead_end'),
    ('The track list in the brief was cut off, so I only know two tracks.', 'cut_off'),
    ('The original PDF is unavailable here.', 'cut_off'),
])
def test_dead_ends_and_cut_offs(answer, code):
    r = rec('q', [task('1.1', 'q', 'knowledge', answer)], answer)
    assert codes(suspects(r)) == [code]


def test_a_route_only_record_has_nothing_to_flag():
    r = copy.deepcopy(FIXTURE['record'])
    for t in r['tasks']:
        for k in ('answer', 'created_files', 'forced'):
            t.pop(k, None)
    r.update(merged=None, dry_run='route')
    assert suspects(r) == []

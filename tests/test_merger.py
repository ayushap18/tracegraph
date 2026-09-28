"""compose() (docs/PLAN-accuracy-v2.md B2), the B6 hedge wording, and the keyless coverage check (A5)."""
import json
from pathlib import Path

import pytest

from jevrouter import merger
from jevrouter.engines import EngineError, Reply
from jevrouter.merger import COULDNT, TIME_SENSITIVE_HEDGE, compose, note_caveats, strip_preamble
from jevrouter.verify import coverage

FIXTURE = Path(__file__).parent / 'fixtures' / 'run2741.json'


def step(tid, text, agent, answer, ok=True, files=(), caveats=(), assumption=None, **kw):
    return {'tid': tid, 'text': text, 'agent': agent, 'answer': answer, 'ok': ok, 'files': list(files),
            'caveats': list(caveats), 'assumption': assumption, **kw}


def meta(fid, name, fmt, pages=None, size=12_776, **kw):
    return {'id': fid, 'name': name, 'format': fmt, 'size': size, 'pages': pages, 'slides': None, 'sheets': None,
            'rules': [], 'source': 'llm', **kw}


def collector():
    chunks = []
    return chunks, chunks.append


class Engine:
    """Records every call; replies with `text`, or raises EngineError when text is None."""
    name, label = 'claude-code', 'Claude Code'

    def __init__(self, text='Paris is sunny and 5 EUR is 5.40 USD.'):
        self.text, self.calls = text, []

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, **kw):
        self.calls.append({'system': system, 'prompt': prompt, 'effort': effort})
        if self.text is None:
            raise EngineError('timed out')
        if emit_delta:
            emit_delta(self.text)
        return Reply(self.text, 11, 7)


WEATHER = step('1.1', 'weather in Paris', 'weather', 'Paris: now 18°C, clear sky')
CURRENCY = step('1.2', 'convert 100 EUR to INR', 'currency', '100.00 EUR = 9,000.00 INR  (rate from 2026-09-28)')


# ---------- template ----------

async def test_no_agent_labels_and_one_line_answers_become_a_list():
    chunks, emit = collector()
    m = await compose('weather in Paris and convert 100 EUR to INR', [WEATHER, CURRENCY], emit, exact=True)
    assert '**weather**' not in m['answer'] and '**currency**' not in m['answer']
    assert m['answer'] == '- Paris: now 18°C, clear sky\n- 100.00 EUR = 9,000.00 INR  (rate from 2026-09-28)'
    assert (m['engine'], m['kind'], m['caveats'], m['primary_file']) == ('concat', 'template', [], None)
    assert chunks == [m['answer']]


async def test_longer_answers_go_under_headings_from_their_steps():
    a = step('1.1', 'What is a black hole?', 'knowledge', 'A region of spacetime.\n\nNothing escapes it.')
    b = step('1.2', 'Tell me about the Eiffel Tower', 'knowledge', 'An iron tower in Paris.')
    m = await compose('q', [a, b], lambda t: None)
    assert m['answer'] == ('**Black hole**\n\nA region of spacetime.\n\nNothing escapes it.\n\n'
                           '**Eiffel Tower**\n\nAn iron tower in Paris.')
    assert '**knowledge**' not in m['answer']


async def test_labels_inside_an_answer_are_dropped():
    a = step('1.1', 'weather in Paris', 'weather', '**weather**: Paris: now 18°C')
    m = await compose('q', [a, CURRENCY], lambda t: None)
    assert '**weather**' not in m['answer'] and 'Paris: now 18°C' in m['answer']


async def test_styles_still_apply():
    m = await compose('q', [WEATHER, CURRENCY], lambda t: None, style='table')
    assert m['answer'].startswith('| Question | Answer |\n| --- | --- |\n| weather in Paris | Paris: now 18°C')
    two = step('1.1', 'weather in Paris', 'weather', 'Paris: now 18°C\nChance of rain today: 10%')
    m = await compose('q', [two, CURRENCY], lambda t: None, style='concise')
    assert m['answer'] == '- Paris: now 18°C\n- 100.00 EUR = 9,000.00 INR  (rate from 2026-09-28)'


async def test_one_answer_passes_through_unchanged():
    text = 'A region of spacetime.\n\n\n```py\nx = 1\n\n\ny = 2\n```'
    m = await compose('q', [step('1.1', 'q', 'knowledge', text)], lambda t: None)
    assert m['answer'] == text and m['kind'] == 'single' and m['engine'] == 'single'
    created = step('1.1', 'make a pdf on owls', 'create', 'Created **owls.pdf**, 4 pages\n\nConverted from **a.md**.',
                   files=[meta('f1', 'owls.pdf', 'pdf', 4)])
    m = await compose('make a pdf on owls', [created], lambda t: None)
    assert m['answer'] == created['answer'] and m['primary_file'] == 'f1'


# ---------- guards ----------

async def test_identical_guard_texts_appear_once():
    ask = 'Which city do you mean?'
    m = await compose('q', [step('1.1', 'weather there', 'clarify', ask, ok=False),
                            step('1.2', 'and the time there', 'clarify', ask, ok=False)], lambda t: None, exact=True)
    assert m['answer'] == ask


async def test_every_step_clarifying_asks_one_question_about_the_query():
    a = step('1.1', 'the rate', 'clarify', 'Which rate do you mean?', ok=False, probabilities={'currency': 0.7, 'knowledge': 0.2})
    b = step('1.2', 'a pound', 'clarify', 'Money or weight?', ok=False, probabilities={'currency': 0.5, 'units': 0.4})
    m = await compose("What's the rate for a pound?", [a, b], lambda t: None, exact=True)
    assert m['answer'].count('?') >= 1 and 'Which rate' not in m['answer'] and 'Money or weight' not in m['answer']
    assert 'agent' not in m['answer'].lower()


async def test_unsupported_steps_become_caveats_next_to_answers():
    no = step('1.2', 'book me a table', 'unsupported', "I can't book things for you; I can only look things up.")
    m = await compose('q', [WEATHER, no], lambda t: None, exact=True)
    assert m['caveats'] == ["I can't book things for you; I can only look things up."]
    assert m['answer'] == ('Paris: now 18°C, clear sky\n\n' + COULDNT +
                           "\n- I can't book things for you; I can only look things up.")
    alone = await compose('q', [no], lambda t: None, exact=True)
    assert alone['answer'] == no['answer'] and alone['caveats'] == [no['answer']]


# ---------- files and caveats ----------

WORKING = meta('md1', 'ai-notes.md', 'md')
PRIMARY = meta('pdf1', 'ai.pdf', 'pdf', 12, role='primary')


async def test_primary_file_leads_and_working_files_follow():
    research = step('2.1', 'Research AI', 'create', 'Created **ai-notes.md**, 12 KB\n\nNo format was named, so this is '
                    'Markdown. Convert it to PDF from the file card at no cost.', files=[WORKING])
    make = step('2.2', 'Make a 12 page PDF on AI from it', 'create', 'Created **ai.pdf**, 12 pages', files=[PRIMARY])
    m = await compose('12 page pdf on ai', [research, make], lambda t: None, exact=True)
    assert m['answer'] == 'Created **ai.pdf**, 12 pages.\n\nWorking files:\n- **ai-notes.md**, 12 KB'
    assert m['primary_file'] == 'pdf1' and m['caveats'] == []
    assert '**create**' not in m['answer'] and 'No format was named' not in m['answer']


async def test_primary_is_the_requested_format_without_a_role():
    a = step('3.1', 'notes', 'create', 'Created **n.md**, 3 KB', files=[meta('m', 'n.md', 'md', size=3000)])
    b = step('3.2', 'the pdf', 'create', 'Created **n.pdf**, 2 pages', files=[meta('p', 'n.pdf', 'pdf', 2)])
    m = await compose('make a pdf about owls', [b, a], lambda t: None)  # the pdf step first, the md step last
    assert m['primary_file'] == 'p' and m['answer'].startswith('Created **n.pdf**, 2 pages.')
    m = await compose('notes on owls', [b, a], lambda t: None)  # no format asked: the last file made
    assert m['primary_file'] == 'm'


async def test_create_caveats_appear_in_the_answer_and_are_returned():
    make = step('2.2', 'Make a 12-13 page PDF on AI', 'create', 'Created **ai.pdf**, 9 pages', files=[PRIMARY],
                caveats=['asked for 12-13 pages, made 9'])
    m = await compose('12-13 page pdf on ai', [WEATHER, make], lambda t: None)
    assert m['caveats'] == ['asked for 12-13 pages, made 9']
    assert m['answer'].endswith(COULDNT + '\n- asked for 12-13 pages, made 9')
    assert m['answer'].startswith('Created **ai.pdf**, 12 pages.')


async def test_caveats_are_deduplicated_and_capped():
    many = [f'part {i} was not included' for i in range(9)]
    m = await compose('q', [step('1.1', 'a', 'knowledge', 'x', caveats=many + ['Part 0 was not included.']),
                            step('1.2', 'b', 'knowledge', 'y')], lambda t: None)
    assert m['caveats'] == many[:6] and m['answer'].count('\n- part') == 6


async def test_a_step_that_fed_a_file_is_left_to_the_file():
    notes = step('4.1', 'Research AI', 'research', '## History\n- 1956: Dartmouth\nIMAGE: Alan Turing | Turing in 1951',
                 feeds_file=True)
    make = step('4.2', 'Make a PDF on AI', 'create', 'Created **ai.pdf**, 12 pages', files=[PRIMARY])
    m = await compose('pdf on ai', [notes, make], lambda t: None)
    assert m['answer'] == 'Created **ai.pdf**, 12 pages.'


async def test_image_seeds_never_reach_the_answer():
    notes = step('4.1', 'Research AI', 'research', '- 1956: Dartmouth\nIMAGE: Alan Turing | Turing in 1951\n- 1997: Deep Blue')
    m = await compose('q', [notes, WEATHER], lambda t: None)
    assert 'IMAGE:' not in m['answer'] and '1997: Deep Blue' in m['answer']


async def test_2741_fixture_answer():
    d = json.loads(FIXTURE.read_text())
    rec = d['record']
    specs = {c['meta']['id']: c['spec'] for c in d['created']}
    steps = [step(t['tid'], t['text'], t['agent'], t['answer'], t['ok'],
                  [{**f, 'spec': specs.get(f['id'])} for f in t['created_files']]) for t in rec['tasks']]
    m = await compose(rec['text'], steps, lambda t: None)
    assert m['answer'].startswith('Created **ai-in-depth-')
    assert m['answer'].count('Created **') == 1 and '**create**' not in m['answer']
    assert 'No format was named' not in m['answer'] and 'Working files:' in m['answer']
    assert m['primary_file'] == '1254d406bdd0'
    assert any('none were fetched' in c for c in m['caveats']) and any('No image files' in c for c in m['caveats'])


def test_note_caveats_take_the_sentence_that_reports_the_miss():
    notes = ['Suggested timeline diagram: plot each row. The table stops at 2024.',
             'No image files were fetched or checked in preparing this brief, so it gives no specific image URLs. The '
             'sites below are real.', 'Sources could not be reached. Try again later.', 'All good here.']
    assert note_caveats(notes) == ['No image files were fetched or checked in preparing this brief, so it gives no '
                                   'specific image URLs.', 'Sources could not be reached.']


# ---------- assumptions, hedges, preambles ----------

async def test_assumption_is_the_first_line_of_its_part():
    a = step('1.1', 'Mercury', 'knowledge', 'Mercury is the closest planet to the Sun.',
             assumption='Assuming you mean a factual question about "Mercury".')
    m = await compose('Mercury', [a], lambda t: None)
    assert m['answer'] == 'Assuming you mean a factual question about "Mercury".\n\nMercury is the closest planet to the Sun.'


async def test_a_hedge_ends_its_part_and_is_not_a_caveat():
    a = step('1.1', 'IIT Delhi cutoff rank 2026', 'knowledge', 'The closing rank was about 115.',
             caveats=[TIME_SENSITIVE_HEDGE])
    m = await compose('q', [a, WEATHER], lambda t: None)
    assert m['caveats'] == [] and COULDNT not in m['answer']
    assert f'The closing rank was about 115.\n\n{TIME_SENSITIVE_HEDGE}' in m['answer']
    assert merger.ADVICE_NEEDS_ENGINE == 'This needs an engine to answer well: it is advice, not a lookup.'


@pytest.mark.parametrize('text,want', [
    ("I'll check the weather for you.\n\nParis is sunny.", 'Paris is sunny.'),
    ('Let me search the web.\nI will look at the results.\n\nParis is sunny.', 'Paris is sunny.'),
    ('Paris is sunny. Let me check again.\nMore.', 'Paris is sunny. Let me check again.\nMore.'),
    ("I'll check the weather.", "I'll check the weather."),  # nothing after it: kept rather than emptied
    ('I checked it.\n\nFine.', 'I checked it.\n\nFine.'),
])
def test_strip_preamble(text, want):
    assert strip_preamble(text) == want


async def test_preambles_are_dropped_before_merging():
    a = step('1.1', 'news on AI', 'research', "I'll search for the latest news.\n\nOpenAI shipped a model.\nMore soon.")
    m = await compose('q', [a, WEATHER], lambda t: None)
    assert "I'll search" not in m['answer'] and 'OpenAI shipped a model.' in m['answer']


# ---------- LLM merger ----------

async def test_llm_merger_sees_step_texts_and_caveats_never_agent_names():
    engine = Engine()
    a = step('1.1', 'What is a black hole?', 'knowledge', 'A region of spacetime.', caveats=['no sources were checked'])
    chunks, emit = collector()
    m = await compose('q', [a, WEATHER], emit, engine)
    prompt = engine.calls[0]['prompt']
    assert '[Part 1: What is a black hole?]' in prompt and '[knowledge agent]' not in prompt
    assert 'List these limits plainly at the end' in prompt and '- no sources were checked' in prompt
    assert 'Never name the agents' in engine.calls[0]['system'] and engine.calls[0]['effort'] == 'low'
    # the reply left the limits out: code lists them
    assert m['answer'] == 'Paris is sunny and 5 EUR is 5.40 USD.\n\n' + COULDNT + '\n- no sources were checked'
    assert (m['engine'], m['kind'], m['claude_in'], m['claude_out']) == ('claude-code', 'llm', 11, 7)
    assert ''.join(chunks) == m['answer']


async def test_llm_merger_that_lists_the_limits_is_kept_as_it_is():
    reply = f"A region of spacetime; Paris is 18°C.\n\n{COULDNT}\n- I could not check sources."
    a = step('1.1', 'What is a black hole?', 'knowledge', 'A region of spacetime.', caveats=['no sources were checked'])
    m = await compose('q', [a, WEATHER], lambda t: None, Engine(reply))
    assert m['answer'] == reply


async def test_llm_merger_goes_after_the_file_line():
    engine = Engine('Owls hunt at night; Paris is 18°C.')
    make = step('2.2', 'Make a PDF on owls', 'create', 'Created **ai.pdf**, 12 pages', files=[PRIMARY])
    owls = step('2.1', 'Tell me about owls', 'knowledge', 'Owls hunt at night.')
    chunks, emit = collector()
    m = await compose('q', [owls, WEATHER, make], emit, engine)
    assert m['answer'] == 'Created **ai.pdf**, 12 pages.\n\nOwls hunt at night; Paris is 18°C.'
    assert 'Created **' not in engine.calls[0]['prompt'] and chunks[0].startswith('Created **ai.pdf**')


async def test_llm_merger_failure_joins_by_template():
    a = step('1.1', 'What is a black hole?', 'knowledge', 'A region of spacetime.')
    m = await compose('q', [a, WEATHER], lambda t: None, Engine(None))
    assert m['engine'] == 'concat' and m['kind'] == 'llm'
    assert m['answer'] == '- A region of spacetime.\n- Paris: now 18°C, clear sky'


# ---------- keyless coverage (A5) ----------

@pytest.mark.parametrize('text,answer', [
    ('Divide 240 by 8', '240 = 240'),
    ('12 percent of 850', '850 = 850'),
    ('Multiply 13 by 11', '13 = 13'),
    ('Divide 240 by 8', '8 / 240 = 0.0333333333'),
    ('subtract 5 from 20', '5 - 20 = -15'),
    ('2 to the power of 10', '10 ** 2 = 100'),
])
def test_math_coverage_mismatch(text, answer):
    out = coverage('math', text, answer)
    assert out['verified'] == 'mismatch' and out['verify_note'] and '—' not in out['verify_note']


@pytest.mark.parametrize('text,answer', [
    ('Divide 240 by 8', '240 / 8 = 30'),
    ('12 percent of 850', '(12/100*850) = 102'),
    ('subtract 5 from 20', '20 - 5 = 15'),
    ('What is 2+2?', '2+2 = 4'),
    ('square root of 2025', 'sqrt(2025) = 45'),
    ('what is three hundred and twenty plus fifty five', '320 + 55 = 375'),
    ('what is 1,234 * 2', '1234 * 2 = 2,468'),
])
def test_math_coverage_ok(text, answer):
    assert coverage('math', text, answer) == {'verified': 'ok', 'verify_note': None}


def test_currency_coverage():
    reverse = coverage('currency', 'how many yen is 200 British pounds', '200.00 JPY = 1.06 GBP  (rate from 2026-09-28)')
    assert reverse == {'verified': 'mismatch', 'verify_note': 'The question converts GBP to JPY, but this answer '
                                                              'converts JPY to GBP.'}
    assert coverage('currency', 'how many yen is 200 British pounds', '200.00 GBP = 37,800.00 JPY')['verified'] == 'ok'
    assert coverage('currency', 'Convert 100 USD to EUR', '100.00 USD = 86.00 EUR')['verified'] == 'ok'
    assert coverage('currency', '$50 in euros', '50.00 USD = 43.00 EUR')['verified'] == 'ok'
    assert coverage('currency', "what's €75 in £", '75.00 EUR = 65.00 GBP')['verified'] == 'ok'
    assert coverage('currency', 'How many rupees is a dollar', '1.00 USD = 88.00 INR')['verified'] == 'ok'
    assert coverage('currency', 'How many rupees is a dollar', '1.00 INR = 0.01 USD')['verified'] == 'mismatch'
    amount = coverage('currency', 'Convert 100 USD to EUR', '10.00 USD = 8.60 EUR')
    assert amount['verified'] == 'mismatch' and '100' in amount['verify_note']
    assert coverage('currency', 'Convert 100 USD to EUR', '100.00 USD = 11,000.00 JPY')['verified'] == 'mismatch'


@pytest.mark.parametrize('agent,text,answer', [
    ('weather', 'weather in Paris', 'Paris: 18°C'),
    ('math', 'sqrt of -1', "I couldn't find a numeric expression in that."),
    ('currency', 'How much is a pound?', 'Which currency should I convert the British pound (GBP) to?'),
    ('currency', 'Convert 100 USD', '100.00 USD = 86.00 EUR'),
    ('math', 'x', ''),
])
def test_coverage_does_not_apply(agent, text, answer):
    assert coverage(agent, text, answer) is None


async def test_a_bold_lead_word_that_is_no_agent_stays():
    a = step('1.1', 'tips for sleep', 'knowledge', '**note**: keep a schedule.')
    m = await compose('q', [a, WEATHER], lambda t: None)
    assert '**note**: keep a schedule.' in m['answer']


# ---------- review fixes ----------

async def test_an_unsupported_step_is_never_cut_by_the_caveat_cap():
    made = step('1.1', 'make a pdf about AI', 'create', 'Created **ai.pdf**', files=[meta('f1', 'ai.pdf', 'pdf', pages=3)],
                caveats=[f'file limit {i}' for i in range(6)])
    flight = step('1.2', 'book me a flight', 'unsupported', "I can't book flights for you.", ok=True)
    m = await compose('make a pdf about AI and book me a flight', [made, flight], lambda t: None)
    assert m['caveats'][0] == "I can't book flights for you." and len(m['caveats']) == merger.MAX_CAVEATS
    assert 'flight' in m['answer']


async def test_specific_clarify_questions_are_all_kept():
    a = step('1.1', 'What is the weather?', 'clarify', 'Which place do you want the weather for?', ok=False,
             probabilities={'weather': 0.9}, asked=True)
    b = step('1.2', 'convert 100 dollars', 'clarify', 'Which currency do you want 100 USD in?', ok=False,
             probabilities={'currency': 0.9}, asked=True)
    m = await compose('What is the weather? convert 100 dollars', [a, b], lambda t: None, exact=True)
    assert 'Which place' in m['answer'] and 'Which currency do you want 100 USD in?' in m['answer']


async def test_the_hedge_survives_an_llm_merge():
    k = step('1.2', 'IIT cutoff rank 2025', 'knowledge', 'The cutoff was around 5000.', caveats=[TIME_SENSITIVE_HEDGE])
    m = await compose('weather in Paris and IIT cutoff rank 2025', [WEATHER, k], lambda t: None,
                      engine=Engine('Paris is 18C. The cutoff was around 5000.'))
    assert m['kind'] == 'llm' and m['answer'].endswith(TIME_SENSITIVE_HEDGE) and m['caveats'] == []

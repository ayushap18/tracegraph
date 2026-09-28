"""The create agent's path order (docs/PLAN-accuracy-v2.md C2), the long-document writer (C3), answers and caveats (C8)
and label stripping (B3), with a stub engine that writes sections at the word targets it is asked for."""
import io
import json
import re
from pathlib import Path

import pytest
from pypdf import PdfReader

import jevrouter.create as cf
from jevrouter.agents import AgentResult
from jevrouter.agents import create as ca
from jevrouter.create import longdoc
from jevrouter.create.brief import parse_brief
from jevrouter.create.longdoc import OUTLINE_SCHEMA
from jevrouter.engines import Reply
from jevrouter.pipeline import Router
from jevrouter.store import Store
from tests.fakes import FakeJev
from tests.longdoc_stub import LongStub
from tests.test_create_assets import FakeHTTP, Resp, candidate, jpeg
from tests.test_create_brief import PROMPT_2741

FIXTURE = json.loads((Path(__file__).parent / 'fixtures' / 'run2741.json').read_text())
MD_META, MD_SPEC = FIXTURE['created'][0]['meta'], FIXTURE['created'][0]['spec']
SHORT = ('Artificial intelligence is the study of machines that perform tasks needing human intelligence. The field '
         'began at the 1956 Dartmouth workshop, after Alan Turing asked in 1950 whether machines can think. Early '
         'work used rules and search; later work learned from data. Machine learning, and deep learning within it, '
         'now drive speech, vision and language systems. ') * 2  # about 120 words, like a research step's answer


@pytest.fixture(autouse=True)
def no_system_fonts(monkeypatch, tmp_path):
    """Font lookups see no installed fonts and no TRACEGRAPH_BODY_FONT, so results don't depend on the machine; images
    go to a temporary asset cache and every host resolves to a public address."""
    from jevrouter.agents import tools
    from jevrouter.create import assets, fonts
    monkeypatch.setattr(fonts, 'system_index', lambda: {})
    monkeypatch.delenv('TRACEGRAPH_BODY_FONT', raising=False)
    monkeypatch.setattr(assets, 'CACHE', tmp_path / 'assets')

    async def lookup(host, port):
        return ['198.35.26.112']
    monkeypatch.setattr(tools, 'lookup', lookup)


def pdf_pages(made) -> int:
    return len(PdfReader(io.BytesIO(made.data)).pages)


def pdf_text(made) -> str:
    return '\n'.join(p.extract_text() for p in PdfReader(io.BytesIO(made.data)).pages)


def rules(made) -> dict:
    return {r['id']: r for r in made.file['rules']}


def images_http() -> FakeHTTP:
    """Every figure the stub asks for finds a CC BY-SA photo."""
    class Any(FakeHTTP):
        def get(self, url, params=None, **kw):
            if params is not None:
                q = params['gsrsearch'].removesuffix(' filetype:bitmap')
                name = re.sub(r'\W+', '_', q) + '.jpg'
                self.search[q] = [candidate(name)]
                shade = len(self.search) * 20 % 250  # a different picture for each query
                self.files[f'https://upload.wikimedia.org/{name}'] = Resp(body=jpeg(color=(shade, 120, 200)))
            return super().get(url, params, **kw)
    return Any()


# ---------- C2: the path order ----------

async def test_a_long_file_from_a_short_answer_calls_the_engine():
    eng = LongStub(seed=1)
    job = ca.Job('12-13 page PDF on AI', deps=[('Research AI', SHORT)], brief=parse_brief('12-13 page PDF on AI'))
    made = await ca.make(job, eng, None)
    assert made.ok and made.llm_out > 0 and made.file['source'] == 'llm' and made.file['format'] == 'pdf'
    assert 12 <= pdf_pages(made) == made.file['pages'] <= 13
    assert rules(made)['V5']['ok'] and made.caveats == []
    assert 'Dartmouth' in eng.calls[0]['prompt']  # the earlier answer is the writer's notes
    phases = [p['phase'] for p in made.phases]
    assert phases in (['outline', 'sections', 'render'], ['outline', 'sections', 'topup', 'render'])
    assert sum(p['calls'] for p in made.phases) == len(eng.calls)
    assert made.file['phases'] == made.phases and made.file['tokens'] == made.llm_in + made.llm_out


async def test_put_that_in_a_pdf_costs_no_tokens():
    eng = LongStub()
    made = await ca.make(ca.Job('put that in a PDF', deps=[('Research AI', SHORT)]), eng, None)
    assert made.ok and made.file['source'] == 'answer' and made.file['tokens'] == 0 and eng.calls == []


async def test_a_dependency_file_is_converted_not_its_reply_line():
    deps = [('Research AI in depth', 'Created **x.md**, 12 KB\n\nNo format was named, so this is Markdown.')]
    job = ca.Job('Put it in a PDF', deps=deps, dep_files=[(MD_META, MD_SPEC)], brief=parse_brief('Put it in a PDF'))
    eng = LongStub()
    made = await ca.make(job, eng, None)
    assert made.ok and made.file['source'] == 'convert' and made.file['tokens'] == 0 and eng.calls == []
    assert made.file['from_id'] == MD_META['id']
    def flat(items):
        return [x for it in items for x in (flat(it) if isinstance(it, list) else [it])]
    assert len(flat(PdfReader(io.BytesIO(made.data)).outline)) >= 7
    assert 'Created **' not in pdf_text(made) and 'No format was named' not in pdf_text(made)
    assert made.answer.startswith('Created **') and 'Made from **' in made.answer


async def test_a_dependency_file_seeds_a_longer_one():
    step = 'Create a properly formatted 12-13 page PDF on AI from the research'
    job = ca.Job(step, deps=[('Research AI', 'Created **x.md**, 12 KB')], dep_files=[(MD_META, MD_SPEC)],
                 brief=parse_brief(step))
    eng = LongStub(seed=3)
    made = await ca.make(job, eng, None)
    assert made.ok and made.file['source'] == 'llm' and 12 <= made.file['pages'] <= 13
    outline = eng.calls[0]['prompt']
    assert 'Build on this earlier outline' in outline and 'Origins and History' in outline
    assert 'Created **' not in outline  # the reply line is never a note


async def test_keyless_never_makes_a_file_of_a_status_line():
    deps = [('x', 'Created **x.md**, 12 KB')]
    made = await ca.make(ca.Job('put that in a PDF', deps=deps), None, None)
    assert not made.ok and made.file is None and 'has no answer to put in it' in made.answer
    made = await ca.make(ca.Job('12-13 page PDF on AI', deps=deps, brief=parse_brief('12-13 page PDF on AI')), None, None)
    assert not made.ok and made.file is None
    assert made.answer == ('No file was made: a 12-13 page PDF needs an LLM engine to write, and the step it was to be '
                           'made from has no answer to put in it. Choose an engine in Settings.')


async def test_keyless_with_nothing_to_use_names_what_needs_an_engine():
    made = await ca.make(ca.Job('pdf on ai, 12-13 pages', brief=parse_brief(PROMPT_2741)), None, None)
    assert not made.ok and made.file is None
    assert made.answer.startswith('No file was made: a 12-13 page PDF needs an LLM engine to write')


async def test_keyless_renders_what_there_is_and_says_so():
    made = await ca.make(ca.Job('12-13 page PDF on AI', deps=[('Research AI', SHORT)],
                                brief=parse_brief('12-13 page PDF on AI')), None, None)
    assert made.ok and made.file['source'] == 'answer' and made.file['pages'] == 1
    assert made.caveats == ['asked for 12-13 pages; made 1 from the earlier answer; writing more needs an engine.']
    made = await ca.make(ca.Job('Put it in a 12 page PDF', deps=[('x', 'Created **x.md**, 12 KB')],
                                dep_files=[(MD_META, MD_SPEC)], brief=parse_brief('Put it in a 12 page PDF')), None, None)
    assert made.ok and made.file['source'] == 'convert'
    assert re.fullmatch(r'asked for 12 pages; made \d+ from the earlier file; writing more needs an engine\.',
                        made.caveats[0])


async def test_the_default_format_of_a_long_file_and_the_no_format_note():
    made = await ca.make(ca.Job('ten pages on the history of AI', brief=parse_brief('ten pages on the history of AI')),
                         LongStub(seed=5), None)
    assert made.file['format'] == 'pdf' and 'No format was named, so this is PDF' in made.answer
    query = 'a 12 page PDF on AI'
    made = await ca.make(ca.Job('Write about AI', brief=parse_brief(query)), LongStub(seed=5), None)
    assert made.file['format'] == 'pdf' and 'No format was named' not in made.answer


# ---------- C3: the writer ----------

async def test_the_fit_loop_lands_in_range(monkeypatch):
    """20 seeded runs, sections at +-30% of their targets: 12-13 pages in at least 19, with at most 4 renders and 7
    LLM calls each."""
    real = cf.render
    hits, renders_max, calls_max = 0, 0, 0
    for seed in range(20):
        count = {'n': 0}

        def counting(*a, **k):
            count['n'] += 1
            return real(*a, **k)
        monkeypatch.setattr(cf, 'render', counting)
        eng = LongStub(seed=seed, noise=0.3)
        brief = parse_brief('a 12-13 page PDF on AI')
        made = await ca.make(ca.Job('a 12-13 page PDF on AI', deps=[('Research AI', SHORT)], brief=brief), eng, None)
        assert made.ok, made.answer
        hits += 12 <= made.file['pages'] <= 13
        renders_max, calls_max = max(renders_max, count['n']), max(calls_max, len(eng.calls))
    assert hits >= 19 and renders_max <= 4 and calls_max <= 7


async def test_calls_share_one_system_prompt_and_the_budget():
    eng = LongStub(seed=2)
    await ca.make(ca.Job('a 12-13 page PDF on AI', brief=parse_brief('a 12-13 page PDF on AI')), eng, None)
    outline, *rest = eng.calls
    assert outline['schema'] is OUTLINE_SCHEMA and outline['effort'] == 'medium'
    assert 'Length: 12-13 pages, about 5,200 words' in outline['prompt']
    assert all(c['schema'] is cf.DOCSPEC_SCHEMA and c['effort'] == 'low' for c in rest)
    assert len({c['system'] for c in rest}) == 1  # a CLI engine's warm process is reused
    target, _ = longdoc.budget('pdf', parse_brief('a 12-13 page PDF on AI'))
    assert sum(c['max_tokens'] for c in rest) <= max(32000, 1.6 * target * 1.4) + 1500 * len(rest)
    assert len(eng.calls) <= 2 + -(-target // 1500) + 1


async def test_cli_engines_write_one_batch_at_a_time():
    import asyncio

    class Slow(LongStub):
        live = peak = 0

        async def stream(self, **kw):
            Slow.live += 1
            Slow.peak = max(Slow.peak, Slow.live)
            await asyncio.sleep(0.01)
            try:
                return await super().stream(**kw)
            finally:
                Slow.live -= 1
    for billing, peak in (('subscription', 1), ('api', 2)):
        Slow.peak = 0
        eng = Slow(seed=4)
        eng.billing = billing
        await ca.make(ca.Job('a 12 page PDF on AI', brief=parse_brief('a 12 page PDF on AI')), eng, None)
        assert Slow.peak == peak, billing


async def test_a_20_slide_deck():
    brief = parse_brief('a 20-slide deck on AI')
    made = await ca.make(ca.Job('a 20-slide deck on AI', brief=brief), LongStub(seed=1, sections=12), None)
    assert made.ok and made.file['format'] == 'pptx' and made.file['slides'] == 20 and rules(made)['V5']['ok']


async def test_engine_failures_are_honest():
    made = await ca.make(ca.Job('a 12 page PDF on AI', brief=parse_brief('a 12 page PDF on AI')),
                         LongStub(fail_sections=True), None)
    assert not made.ok and made.file is None and 'failed (no section came back)' in made.answer
    assert made.llm_in > 0  # the outline call still counts


# ---------- the 2741 request, end to end on the create agent ----------

async def test_the_2741_request():
    brief = parse_brief(PROMPT_2741)
    step = 'Create a properly formatted 12-13 page PDF about AI from the research'
    job = ca.Job(step, deps=[('Research AI in depth', SHORT)], brief=brief, http=images_http())
    made = await ca.make(job, LongStub(seed=11), None)
    assert made.ok, made.answer
    f, r = made.file, rules(made)
    assert f['format'] == 'pdf' and 12 <= f['pages'] <= 13 and f['theme'] == 'mono' and f['role'] == 'primary'
    assert all(r[k]['ok'] for k in ('V5', 'V6', 'V8', 'V9', 'X6')), {k: r[k] for k in ('V5', 'V6', 'V8', 'V9', 'X6')}
    assert f['diagrams'] >= 2 and f['images'] >= 1 and len(f['credits']) == f['images']
    assert f['brief']['pages'] == [12, 13] and f['font_used'] == 'Helvetica'
    assert {'outline', 'sections', 'assets', 'render'} <= {p['phase'] for p in made.phases}
    text = pdf_text(made)
    assert 'Image credits' in text and 'Jane Doe' in text and 'Created **' not in text
    assert ("You asked for Anthropic Sans; it isn't available to embed here, so the PDF uses Helvetica."
            in made.answer)
    assert r['V7']['ok'] and 'No format was named' not in made.answer
    assert made.caveats == [next(n for n in made.answer.split('\n\n') if n.startswith('You asked for Anthropic'))]


# ---------- C8: answers and caveats ----------

async def test_a_short_file_says_so_in_the_answer_and_the_caveats():
    spec = {'title': 'AI', 'sections': [{'heading': 'What it is', 'notes': 'Images were not fetched.',
                                         'blocks': [{'type': 'paragraph', 'text': SHORT}]}]}
    made = await ca.finish(spec, 'pdf', None, source='llm', brief=parse_brief('12-13 page PDF'))
    assert made.caveats == ['asked for 12-13 pages, made 1', 'Images were not fetched.']
    assert 'V5 (asked for 12-13 pages, made 1)' in made.answer
    assert made.answer.startswith('Created **ai.pdf**, 1 page (asked for 12-13)')


async def test_fonts_in_the_answer():
    spec = {'title': 'AI', 'sections': [{'heading': 'A', 'blocks': [{'type': 'paragraph', 'text': 'x'}]}]}
    made = await ca.finish(dict(spec), 'docx', None, source='llm', brief=parse_brief('use Inter font'))
    assert 'The Word file names Inter (named, not embedded; it shows only where the font is installed).' in made.answer
    assert made.caveats == [] and made.file['font_used'] == 'Inter'
    made = await ca.finish(dict(spec), 'md', None, source='llm', brief=parse_brief('use Inter font'))
    assert "a Markdown file carries no font" in made.answer and made.caveats


# ---------- B3: files never hold the chat's labels ----------

def test_labels_and_reply_lines_never_reach_a_file():
    answer = ('**weather**: Paris is 18 C and sunny.\n\n**currency**: 250 EUR is 22,500 INR.\n\n'
              'Created **old.pdf**, 3 pages\n\nNo format was named, so this is Markdown.')
    spec = ca.answer_spec(answer, 'Results')
    text = ca.spec_text(cf.normalize(spec, 'md')[0])
    assert 'Paris' in text and 'INR' in text
    assert '**weather**' not in text and '**currency**' not in text and 'weather:' not in text.lower()
    assert 'Created' not in text and 'No format was named' not in text
    assert ca.from_answers([('x', 'Created **x.md**, 12 KB\n\n1 check to look at: V3.')]) is None
    assert ca.from_answers([('x', 'Created **x.md**, 12 KB'), ('y', 'Real content here.')])['title']


# ---------- the whole pipeline ----------

class PipelineStub(LongStub):
    """LongStub that also plans (research, then the file) and echoes any other call."""

    async def stream(self, *, system, prompt, schema=None, **kw):
        if schema is not None and schema is not OUTLINE_SCHEMA and schema is not cf.DOCSPEC_SCHEMA:
            plan = {'subtasks': [{'text': 'Research AI in depth', 'depends_on': []},
                                 {'text': 'Create a 12 page PDF on AI from it', 'depends_on': [0]}]}
            return Reply(json.dumps(plan), 7, 4)
        if schema is None:
            return Reply(f'{self.name}: ok', 5, 3)
        return await super().stream(system=system, prompt=prompt, schema=schema, **kw)


async def test_the_pipeline_names_one_pdf_with_its_pages():
    eng = PipelineStub(seed=6)

    def route(text):
        return ('create', 0.95) if 'pdf' in text.lower() else ('knowledge', 0.95)

    async def knowledge(text, emit):
        emit(SHORT)
        return AgentResult(SHORT, True, 'wikipedia.org')
    r = Router(FakeJev(route_for=route), None, eng, registry={'knowledge': knowledge}, store=Store(),
               engines={eng.name: eng})
    qid = next(r.ids)
    await r.handle('Research AI in depth and make a 12 page PDF on it', 'chat', qid, session_id='s1')
    rec = r.get_run(qid)
    files = [f for t in rec['tasks'] for f in t.get('created_files') or []]
    assert len(files) == 1 and files[0]['format'] == 'pdf' and files[0]['pages'] == 12
    answer = rec['merged']['answer']
    assert len(re.findall(r'Created \*\*[^*]+\.pdf\*\*, 12 pages', answer)) == 1, answer
    assert 'No format was named' not in answer and '**create**:' not in answer


# ---------- docs/PLAN-files-robust.md 3: the repair ladder, checkpoints and resume ----------

Q2750 = ('create the ppt on the how mobile phone is being evolved history past present everything a ppt of 12 slides '
         'using the multiple pictured diagrams and also use the design.md for the design')
HEADS_2750 = ['Before mobile phones', 'The first mobile call', 'Car phones and bricks', 'The 2G era', 'Texting takes off',
              'Feature phones', 'Smartphones arrive', 'App stores', 'Cameras and social media', 'Phones today',
              'What comes next']


class Deck2750(LongStub):
    """The run 2750 shape: an outline of 11 parts; section 7 ("Smartphones arrive") comes back as a bullets block with
    no items. `repair` is what a repair call returns for it: 'good', 'empty' or 'prose'."""

    def __init__(self, repair='good', batch_prose=False, **kw):
        super().__init__(headings=HEADS_2750, **kw)
        self.repair, self.batch_prose = repair, batch_prose
        self.billing = 'subscription'

    async def stream(self, *, system, prompt, schema=None, **kw):
        if schema is OUTLINE_SCHEMA:
            self.calls.append({'system': system, 'prompt': prompt, 'schema': schema})
            out = {'title': 'The Evolution of Mobile Phones', 'subtitle': '',
                   'sections': [{'heading': h, 'level': 1, 'words': 45, 'blocks_hint': ['bullets']} for h in HEADS_2750]}
            return Reply(json.dumps(out), 40_000, 900)
        self.calls.append({'system': system, 'prompt': prompt, 'schema': schema})
        asked = re.findall(r'^- "([^"]+)" \(level \d\)', prompt, re.M)
        is_repair = 'had no usable content' in prompt
        if self.batch_prose and not is_repair:
            text = '\n\n'.join(f'## {h}\n\n- a point about {h.lower()}\n- another point' for h in asked)
            return Reply(text, 50_000, 800)
        secs = []
        for h in asked:
            if h == 'Smartphones arrive' and (not is_repair or self.repair == 'empty'):
                blocks = [{'type': 'bullets', 'items': None}]
            else:
                blocks = [{'type': 'bullets', 'items': [f'{h}: one point', 'another point'], 'ordered': False}]
            secs.append({'heading': h, 'level': 1, 'blocks': blocks, 'notes': ''})
        return Reply(json.dumps({'title': '', 'subtitle': '', 'sections': secs}), 50_000, 2_000)


def job_2750(**kw) -> ca.Job:
    return ca.Job(Q2750, deps=[('Research how mobile phones evolved', SHORT)], brief=parse_brief(Q2750), **kw)


def section_calls(eng) -> list[dict]:
    return [c for c in eng.calls if c['schema'] is cf.DOCSPEC_SCHEMA]


async def test_the_2750_shape_builds_a_12_slide_deck_with_one_repair_call():
    eng = Deck2750()
    states = []
    made = await ca.make(job_2750(checkpoint=states.append), eng, None)
    assert made.ok, made.answer
    assert made.file['format'] == 'pptx' and made.file['slides'] == 12
    s1 = rules(made)['S1']
    assert not s1['ok'] and 'section 7' in s1['note'] and 'Smartphones arrive' in s1['note']
    repairs = [c for c in section_calls(eng) if 'had no usable content' in c['prompt']]
    assert len(repairs) == 1 and '"Smartphones arrive"' in repairs[0]['prompt']
    assert '"Before mobile phones"' not in repairs[0]['prompt']  # only the missing section is written again
    assert made.repairs['calls'] == 1 and made.repairs['sections'] == ['Smartphones arrive']
    assert made.file['repairs'] == made.repairs and made.partial is None and 'partial' not in made.file
    sections = next(p for p in made.phases if p['phase'] == 'sections')
    assert sections['calls'] == 3 and made.llm_in == sum(p['llm_in'] for p in made.phases)
    # a checkpoint after the outline, after each batch and repair call, and after the final build
    assert [s['phase'] for s in states][:1] == ['sections'] and states[-1]['phase'] == 'done'
    assert states[-1]['file_id'] == made.file['id'] and len(states[-1]['written']) == 11
    assert longdoc.info(states[-1], 7, '2')['resumable'] is False


async def test_a_repair_call_that_also_fails_gives_a_partial_file():
    eng = Deck2750(repair='empty')
    made = await ca.make(job_2750(), eng, None)
    assert made.ok, made.answer
    assert made.file['slides'] == 11  # the title slide and 10 of 11
    assert made.partial == {'planned': 11, 'written': 10, 'missing': ['Smartphones arrive'], 'resume': None}
    assert made.file['partial'] == made.partial
    v12 = rules(made)['V12']
    assert v12['severity'] == 'warn' and not v12['ok'] and 'Smartphones arrive' in v12['note']
    caveat = ('1 of 11 planned slides could not be written (Smartphones arrive). Use Resume on the file card to write '
              'them; it reuses everything already written.')
    assert caveat in made.caveats
    assert len([c for c in section_calls(eng) if 'had no usable content' in c['prompt']]) == longdoc.MAX_REPAIR_CALLS
    assert longdoc.info(made.checkpoint, 1, '2') == {
        'qid': 1, 'tid': '2', 'kind': 'longdoc', 'format': 'pptx', 'phase': 'render', 'planned': 11, 'written': 10,
        'missing': ['Smartphones arrive'], 'tokens_in': made.llm_in, 'tokens_out': made.llm_out,
        'at': made.checkpoint['at'], 'resumable': True, 'file_id': made.file['id']}


async def test_no_repair_call_when_the_run_is_out_of_time():
    import time as time_mod
    eng = Deck2750()
    made = await ca.make(job_2750(deadline=time_mod.monotonic() + longdoc.REPAIR_MIN_SECONDS - 1), eng, None)
    assert made.ok and made.file['slides'] == 11 and made.repairs is None
    assert not [c for c in section_calls(eng) if 'had no usable content' in c['prompt']]


async def test_a_batch_in_plain_text_is_laid_out():
    made = await ca.make(job_2750(), Deck2750(batch_prose=True), None)
    assert made.ok and made.file['slides'] == 12
    assert ca.PROSE_NOTE in made.answer


async def test_a_partial_state_resumes_without_repeating_paid_calls():
    """Cancelled during batch 2: the sink holds batch 1; a resume writes only the parts batch 2 had."""
    import asyncio
    states = []
    release = asyncio.Event()

    class Stalls(Deck2750):
        async def stream(self, **kw):
            if kw.get('schema') is cf.DOCSPEC_SCHEMA and len(section_calls(self)) >= 1:
                self.calls.append({'system': kw['system'], 'prompt': kw['prompt'], 'schema': kw['schema']})
                await release.wait()
            return await super().stream(**kw)
    task = asyncio.create_task(ca.make(job_2750(checkpoint=states.append), Stalls(), None))
    for _ in range(200):
        await asyncio.sleep(0.01)
        if any(len(s['written']) == 6 for s in states):
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    state = states[-1]
    assert state['phase'] == 'sections' and sorted(state['written'], key=int) == [str(i) for i in range(6)]
    info = longdoc.info(state, 5, '2')
    assert info['resumable'] and info['missing'] == HEADS_2750[6:] and info['tokens_in'] > 0
    eng = Deck2750()
    job = ca.Job(state['request'], resume={'qid': 5, 'tid': '2', 'state': state})
    made = await ca.make(job, eng, None)
    assert made.ok, made.answer
    assert made.file['slides'] == 12 and made.file['resumed_from'] == {'qid': 5, 'tid': '2', 'file_id': None}
    assert not [c for c in eng.calls if c['schema'] is OUTLINE_SCHEMA]
    asked = [h for c in section_calls(eng) for h in re.findall(r'^- "([^"]+)"', c['prompt'], re.M)]
    assert set(asked) <= set(HEADS_2750[6:]) and not set(asked) & set(HEADS_2750[:6])
    assert made.llm_in == sum(c for c in [50_000] * len(section_calls(eng)))  # only the new calls' tokens


async def test_a_single_checkpoint_rebuilds_at_no_cost():
    reply = json.dumps({'title': 'Tea', 'sections': [{'heading': 'Green tea', 'blocks': [
        {'type': 'paragraph', 'text': 'Green tea is steamed or pan fired.'}]}]})
    state = {'v': 1, 'kind': 'single', 'format': 'pdf', 'request': 'a pdf about tea', 'brief': None, 'theme': None,
             'font': None, 'design': None, 'reply': reply, 'engine': 'agy', 'tokens_in': 40_000, 'tokens_out': 500,
             'calls': 1, 'at': 1.0, 'phase': 'sections', 'file_id': None}
    assert longdoc.info(state, 3, '1')['resumable']
    made = await ca.make(ca.Job('a pdf about tea', resume=state), None, None)
    assert made.ok and made.llm_in == 0 and made.file['tokens'] == 0 and made.file['format'] == 'pdf'


async def test_the_fit_loop_notes_a_rule_it_hits_and_still_builds(monkeypatch):
    def broken(spec, fmt):
        raise cf.SpecError('S2', 'The spec has no content.')
    monkeypatch.setattr(longdoc, 'measure', broken)
    made = await ca.make(ca.Job('a 12 page PDF on AI', brief=parse_brief('a 12 page PDF on AI')), LongStub(seed=2),
                         None)
    assert made.ok and made.file['format'] == 'pdf'
    assert any('could not be checked' in c and 'S2' in c for c in made.caveats)


async def test_an_error_inside_make_still_reports_the_tokens(monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError('renderer fell over')
    monkeypatch.setattr(ca, 'finish', boom)
    made = await ca.make(job_2750(), Deck2750(), None)
    assert not made.ok and made.answer.startswith('No file was made: something went wrong while building it')
    assert made.llm_in > 0 and made.engine == 'stub' and {p['phase'] for p in made.phases} >= {'outline', 'sections'}
    assert made.checkpoint is not None and len(made.checkpoint['written']) == 11


class SingleEngine:
    name, label, billing, supports_web = 'one', 'One', 'api', False

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def available(self):
        return True, ''

    async def stream(self, *, system, prompt, schema=None, **kw):
        self.calls.append(prompt)
        return Reply(self.replies.pop(0) if len(self.replies) > 1 else self.replies[0], 1000, 200)


async def test_the_single_path_lays_out_prose_and_keeps_the_reply():
    prose = '# Tea\n\nTea is a drink made from the leaves of Camellia sinensis.\n\n- green\n- black'
    states = []
    made = await ca.make(ca.Job('write a pdf about tea', checkpoint=states.append), SingleEngine(prose), None)
    assert made.ok and made.file['format'] == 'pdf' and ca.PROSE_NOTE in made.answer
    assert made.reply == prose and states[-1]['kind'] == 'single' and states[-1]['file_id'] == made.file['id']


async def test_the_single_path_retries_once_when_nothing_is_usable():
    eng = SingleEngine('{"title": "x", "sections": []}',
                       json.dumps({'title': 'Tea', 'sections': [{'heading': 'Tea', 'blocks': [
                           {'type': 'paragraph', 'text': 'A drink.'}]}]}))
    made = await ca.make(ca.Job('write a pdf about tea'), eng, None)
    assert made.ok and len(eng.calls) == 2 and made.repairs['calls'] == 1 and made.llm_in == 2000
    assert 'had no usable content' in eng.calls[1]


def test_plan_calls_matches_the_writer():
    plan = longdoc.plan_calls('pptx', parse_brief(Q2750))
    assert (plan.sections, plan.batches) == (11, 2) and plan.max_calls == 1 + 2 + 1 + longdoc.MAX_REPAIR_CALLS
    plan = longdoc.plan_calls('pdf', parse_brief('a 12-13 page PDF on AI'))
    assert plan.batches == -(-plan.words // longdoc.BATCH_WORDS)


def test_a_checkpoint_is_capped():
    big = {'v': 1, 'kind': 'longdoc', 'format': 'pdf', 'ctx': 'x' * 300_000,
           'parts': [{'heading': f'P{i}'} for i in range(3)],
           'written': {str(i): {'heading': f'P{i}', 'blocks': [{'type': 'paragraph', 'text': 'y' * 150_000}]}
                       for i in range(3)}}
    capped = longdoc.cap_state(big)
    assert len(json.dumps(capped)) <= longdoc.CHECKPOINT_MAX_BYTES and capped['ctx'] == ''
    assert '2' in capped['written'] and '0' not in capped['written']
    assert longdoc.info(capped, 1, '1')['missing'][0] == 'P0'


async def test_attachments_keep_a_share_of_the_writer_context():
    job = ca.Job('a 12 page pdf', deps=[('Research', 'r' * 20_000)],
                 docs=[({'name': 'notes.txt'}, 'n' * 20_000)])
    ctx = longdoc.context(job, None, ca)
    assert len(ctx) <= longdoc.LONG_CONTEXT_CHARS
    assert ctx.count('n') >= 0.25 * longdoc.LONG_CONTEXT_CHARS - 100
    assert ctx.count('r') <= 0.6 * longdoc.LONG_CONTEXT_CHARS + 10

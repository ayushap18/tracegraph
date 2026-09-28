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

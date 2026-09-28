"""The eval harness, accuracy v2 (docs/PLAN-accuracy-v2.md, Workstream D): the case schema with chat options, pinned
plans, step and plan checks, named matchers and reason codes; file-quality scoring; route mode with a Jev cassette; the
confusion matrix, per-agent scores, failure stages and calibration; unjudged cases; gates and baselines; budgets;
paraphrases; promoting real runs; and the query limit (B4). Engines and Jev are fakes: nothing here calls a model."""
import asyncio
import io
import json
import time
from pathlib import Path

import pytest

from jevrouter import cassette as cassette_mod
from jevrouter import evals, judge, paraphrase
from jevrouter.config import AGENTS
from jevrouter.events import Broadcaster
from jevrouter.pipeline import Router
from jevrouter.store import Store
from tests.fakes import FakeEngine, FakeJev

FIXTURE = Path(__file__).parent / 'fixtures' / 'run2741.json'


def committed(cid: str) -> dict:
    return next(c for c in evals.load_cases(evals.CASES) if c['id'] == cid)


def task(tid, agent, pick=None, conf=0.95, clear=0.9, trace=(), **kw):
    return {'tid': tid, 'text': kw.pop('text', 'x'), 'depends_on': kw.pop('depends_on', []), 'agent': agent,
            'pick': pick or agent, 'confidence': conf, 'clear': clear, 'unsafe': 0.01, 'trace': list(trace),
            'probabilities': {pick or agent: conf}, 'ok': True, **kw}


def record(tasks, answer='', status='done', qid=1, **kw):
    return {'qid': qid, 'status': status, 'tasks': tasks, 'merged': {'answer': answer, 'engine': 'single'} if answer else None,
            'total_ms': 5, 'error': None, 'plan': {'subtasks': [{'tid': t['tid'], 'text': t['text']} for t in tasks]}, **kw}


# ---------- a stub router: records what the harness submits ----------

class StubRouter:
    """Just enough of Router for run_eval: submit records its arguments and makes the record `make(qid, query, kw)`."""

    def __init__(self, make):
        self.make, self.calls, self.recs, self.running = make, [], {}, {}
        self.store, self.bus, self.evals, self.sandbox = Store(), Broadcaster(), {}, {}
        self.engines, self.engine, self.route_examples = {}, None, False

    def submit(self, query, source, **kw):
        qid = len(self.calls) + 1
        self.calls.append({'query': query, 'source': source, **kw})
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(None)
        self.running[qid] = fut
        self.recs[qid] = self.make(qid, query, kw)
        return qid

    def get_run(self, qid):
        return self.recs[qid]

    def offered(self, engine, with_files=False, tables=False):
        return dict(AGENTS)

    def deep_engine(self):
        return None

    def web_engine(self):
        return None

    def cancel(self, qid):
        return 'finished'


def answered(qid, query, kw, agent='chat', answer='fine'):
    return record([task(f'{qid}.1', agent, text=query)], answer, qid=qid)


async def test_run_attempt_passes_chat_options_plan_and_route_extras():
    r = StubRouter(answered)
    case = {'id': 'c', 'query': 'make a pdf about owls', 'tags': [], 'chat': {'mode': 'deep', 'agent': '@create',
            'style': 'bullets'}, 'plan': {'subtasks': ['Research owls', 'Make a PDF on owls'], 'deps': [[], [0]]},
            'expect_outcome': 'answer'}
    s = await evals.run_eval(r, 'e', None, 'none', [case])
    call = r.calls[0]
    assert (call['mode'], call['agent'], call['style']) == ('deep', 'create', 'bullets')
    assert call['extras']['plan'] == {'subtasks': ['Research owls', 'Make a PDF on owls'], 'deps': [[], [0]]}
    assert 'dry_run' not in call['extras'] and s['cases'][0]['chat'] == case['chat'] and s['mode'] == 'full'
    turns = {'id': 'm', 'tags': [], 'chat': {'mode': 'quick'}, 'turns': [
        {'query': 'a', 'expect_outcome': 'answer'}, {'query': 'b', 'chat': {'style': 'concise'}}]}
    r = StubRouter(answered)
    await evals.run_eval(r, 'e', None, 'none', [turns])
    assert [(c['mode'], c.get('style')) for c in r.calls] == [('quick', None), ('quick', 'concise')]
    # route mode: the dry-run hook, and each run asks its own view of the cassette
    r = StubRouter(lambda qid, q, kw: {**answered(qid, q, kw), 'dry_run': 'route'})
    cas = cassette_mod.JevCassette(None, None, 'replay')
    s = await evals.run_eval(r, 'e', FakeEngine(), 'codex', [case], mode='route', jev='replay', cassette=cas)
    call = r.calls[0]
    assert call['extras']['dry_run'] == 'route' and isinstance(call['extras']['jev'], cassette_mod.CassetteView)
    assert call['engine'] is None and s['mode'] == 'route' and s['jev'] == 'replay' and s['cases'][0]['pass'] is True


async def test_a_forced_agent_the_run_cannot_pick_fails_the_attempt():
    r = StubRouter(answered)
    s = await evals.run_eval(r, 'e', None, 'none', [{'id': 'x', 'query': 'q', 'tags': [], 'chat': {'agent': 'research'},
                                                     'expect_outcome': 'answer'}])
    c = s['cases'][0]
    assert not r.calls and c['pass'] is False and c['reasons'] == ['@research is not an agent you can pick here']


def test_auto_tags_and_run_on():
    c = evals.auto_tags({'id': 'a', 'query': 'q', 'tags': ['x'], 'chat': {'mode': 'research', 'agent': '@create'}})
    assert c['tags'] == ['x', 'mode:research', 'forced:create']
    assert 'mode:research' in committed('rt-2741-forced-longpdf')['tags']
    assert evals.runs_on({'run_on': ['cli']}, 'cli') and not evals.runs_on({'run_on': ['cli']}, 'keyless')
    assert evals.runs_on({}, 'api') and evals.validate_case({'id': 'a', 'query': 'q', 'run_on': ['gpu']})
    assert evals.runs_on({'run_on': ['keyless']}, 'route') and not evals.runs_on({'run_on': ['route']}, 'keyless')


# ---------- schema (D1) ----------

def test_new_case_keys_are_validated():
    ok = {'id': 'a', 'query': 'q', 'tags': [], 'chat': {'mode': 'research', 'agent': 'create', 'style': 'bullets'},
          'plan': {'subtasks': ['x', 'y'], 'deps': [[], [0]]}, 'unordered': True,
          'expect_steps': [{'agent': 'research|knowledge', 'not_agent': 'create', 'forced': False},
                           {'agent': 'create', 'depends_on': [0], 'format': 'pdf', 'text': 'PDF',
                            'args': {'amount': 5, 'from': 'EUR'}}],
          'expect_plan': {'min_steps': 1, 'max_steps': 3, 'file_steps': 1, 'no_fragment': True, 'max_clarify': 1},
          'forbid_agents': ['code'], 'must_mention': ['@font_note', 'pages?'], 'no_repeat': True,
          'budget': {'planner': ['single'], 'plan_ms': 800, 'llm_out': 10}, 'expect_outcome': ['answer', 'error']}
    assert evals.validate_case(ok, strict=True) == []
    bad = {
        'chat mode': ({**ok, 'chat': {'mode': 'turbo'}}, 'chat mode must be one of'),
        'chat guard': ({**ok, 'chat': {'agent': 'clarify'}}, 'guards cannot be picked'),
        'chat key': ({**ok, 'chat': {'engine': 'x'}}, "chat has unknown key 'engine'"),
        'plan deps': ({**ok, 'plan': {'subtasks': ['x'], 'deps': [[0]]}}, 'plan deps must list'),
        'plan size': ({**ok, 'plan': {'subtasks': []}}, 'plan subtasks must be'),
        'step key': ({**ok, 'expect_steps': [{'agentt': 'x'}]}, "expect_steps[0] has unknown key 'agentt'"),
        'step deps': ({**ok, 'expect_steps': [{'depends_on': [0]}]}, 'depends_on must list earlier'),
        'step regex': ({**ok, 'expect_steps': [{'agent': '('}]}, 'agent is not a valid regex'),
        'plan key': ({**ok, 'expect_plan': {'steps': 2}}, "expect_plan has unknown key 'steps'"),
        'outcome': ({**ok, 'expect_outcome': ['answer', 'maybe']}, 'expect_outcome must be one of'),
        'unordered alone': ({'id': 'a', 'query': 'q', 'unordered': True}, 'needs expect_steps'),
        'budget': ({**ok, 'budget': {'planner': ['magic']}}, 'budget planner must list'),
        'mention': ({**ok, 'must_mention': []}, 'must_mention must be a non-empty list'),
        'turn plan on case': ({'id': 'm', 'plan': {'subtasks': ['x']}, 'turns': [{'query': 'a'}, {'query': 'b'}]},
                              'keeps its expectations on its turns'),
    }
    for name, (case, want) in bad.items():
        errors = evals.validate_case(case)
        assert any(want in e for e in errors), (name, errors)


def test_named_matchers_expand_and_unknown_names_are_rejected(tmp_path):
    assert evals.search('@cant', "Sorry, I can't book flights") and not evals.search('@cant', 'Booked!')
    assert evals.search('@cant|\\bas of\\b', 'prices as of Monday')
    assert evals.validate_case({'id': 'a', 'query': 'q', 'must_match': '@nope'}) == [
        'must_match: unknown matcher @nope (evals/matchers.json)']
    assert any('unknown matcher @nada' in e for e in evals.validate_case({'id': 'a', 'query': 'q', 'must_mention': ['@nada']}))
    p = tmp_path / 'cases.jsonl'
    p.write_text('{"id": "a", "query": "q", "must_not_match": "@missing"}\n')
    with pytest.raises(evals.CaseError, match='unknown matcher @missing'):
        evals.read_cases(p)
    cases = evals.load_cases(evals.CASES)
    raw = [t.get(k) for c in cases if not c['id'].endswith('-hp') for t in [c, *c.get('turns', [])]
           for k in ('must_match', 'must_not_match')]
    assert sum(isinstance(v, str) and v.startswith('@cant') for v in raw) == 18  # the 18 copies of the honesty regex
    assert sum(v == '@dead_end' for v in raw) == 29 and not any(v and 'No summary found|Not sure' in v for v in raw)


# ---------- reason codes, steps and plans ----------

def test_s05_three_part_fails_with_an_extra_agent():
    case = committed('s05-three-part')
    rec = record([task('1.1', 'weather'), task('1.2', 'math'), task('1.3', 'knowledge')], 'Paris 18C; 12; Newton was...')
    reasons, codes = evals.check(case, rec)
    assert {'code': 'extra_agent', 'got': 'knowledge', 'want': 'weather, math'} in codes
    assert reasons == ['unexpected agent knowledge (expected only weather, math)']
    guard = record([task('1.1', 'weather'), task('1.2', 'clarify', 'math')])  # a guard is the outcome check's business
    assert not any(c['code'] == 'extra_agent' for c in evals.check({'expect_agents': ['weather', 'math']}, guard)[1])


def test_expect_steps_in_order_and_unordered():
    rec = record([task('1.1', 'weather', text='Weather in Paris'),
                  task('1.2', 'currency', text='Convert 100 EUR to INR', depends_on=['1.1'], forced=True)])
    ok = {'expect_steps': [{'agent': 'weather'}, {'agent': 'currency', 'depends_on': [0], 'text': 'EUR', 'forced': True}]}
    assert evals.check(ok, rec) == ([], [])
    swapped = {'expect_steps': [{'agent': 'currency'}, {'agent': 'weather'}]}
    _, codes = evals.check(swapped, rec)
    assert [(c['code'], c['step']) for c in codes] == [('wrong_agent', 0), ('wrong_agent', 1)]
    assert evals.check({**swapped, 'unordered': True}, rec) == ([], [])
    reasons, codes = evals.check({'expect_steps': [{'agent': 'weather', 'forced': True}]}, rec)
    assert [c['code'] for c in codes] == ['plan_shape', 'wrong_agent'] and codes[1]['stage'] == 'forced'
    assert reasons[0] == 'expected 1 steps, got 2'
    reasons, codes = evals.check({'expect_steps': [{}, {'depends_on': []}]}, rec)
    assert codes == [{'code': 'wrong_deps', 'step': 1, 'want': '[]', 'got': '[0]', 'stage': 'plan_shape'}]
    arg = record([task('1.1', 'currency', frame={'agent': 'currency', 'slots': {'amount': 500, 'from': 'EUR', 'to': 'USD'}})])
    assert evals.check({'expect_steps': [{'args': {'amount': '500', 'from': 'eur'}}]}, arg) == ([], [])
    assert evals.check({'expect_steps': [{'args': {'to': 'GBP'}}]}, arg)[0] == ["step 0: to is 'USD', expected 'GBP'"]


def test_expect_plan_forbid_mention_and_no_repeat():
    rec = record([task('1.1', 'clarify', 'knowledge', text='build a plan regarding admission on this college'),
                  task('1.2', 'clarify', 'knowledge', text='getting how much rank')],
                 'Who or what would you like to know about? Add a bit more detail.\n\n'
                 'Who or what would you like to know about? Add a bit more detail.')
    exp = {'expect_plan': {'max_steps': 1, 'max_clarify': 1, 'no_fragment': True}, 'no_repeat': True,
           'forbid_agents': ['clarify'], 'must_mention': ['@cant', 'detail']}
    reasons, codes = evals.check(exp, rec)
    assert [c['code'] for c in codes] == ['forbidden_agent', 'plan_shape', 'wrong_outcome', 'answer_regex', 'answer_regex']
    assert reasons[1] == 'planned 2 steps, more than 1' and reasons[2] == '2 steps asked to clarify, at most 1 may'
    assert reasons[3].startswith('answer does not mention /@cant/') and reasons[4].startswith('answer repeats a sentence')
    assert evals.fragment('Paris') and evals.fragment('and the') and not evals.fragment('Research the history of owls')
    route_only = evals.check(exp, rec, mode='route')[1]
    assert [c['code'] for c in route_only] == ['forbidden_agent', 'plan_shape', 'wrong_outcome']


def test_outcomes_include_unsupported_and_route_mode_has_no_errors():
    assert evals.outcome(record([task('1.1', 'unsupported')])) == 'unsupported'
    assert evals.outcome(record([task('1.1', 'weather', ok=False)])) == 'error'
    assert evals.outcome(record([task('1.1', 'weather', ok=None)]), 'route') == 'answer'
    rec = record([task('1.1', 'weather', ok=None)])
    assert evals.check({'expect_outcome': ['answer', 'error']}, rec, 'route') == ([], [])
    assert evals.check({'expect_outcome': 'unsupported'}, rec, 'route')[0] == ['expected outcome unsupported, got answer']


# ---------- stages (D3) ----------

def test_failure_stages_come_from_the_policy_trace():
    euro = committed('dag-euro-capital-convert')
    rec = record([task('1.1', 'clarify', 'currency', trace=[
        {'rule': 'confirmed', 'agent': 'currency', 'why': 'parser'},
        {'rule': 'missing_slot', 'agent': 'clarify', 'why': 'missing detail for currency'}])])
    steps = evals.step_routes(euro, rec)
    assert steps[0]['expected'] == 'currency' and steps[0]['stage'] == 'gate_missing_detail'
    _, codes = evals.check(euro, rec)
    assert evals.with_stages(codes, steps)[0]['stage'] == 'gate_missing_detail'
    sydney = {**committed('dag-sydney-ahead'), 'expect_steps': [{'agent': 'time'}, {'agent': 'time'}]}
    rec = record([task('1.1', 'time'), task('1.2', 'clarify', 'time', clear=0.1, trace=[
        {'rule': 'missing_slot', 'agent': 'clarify', 'why': 'no detail missing'}])])
    assert [s['stage'] for s in evals.step_routes(sydney, rec)] == [None, 'clarity']
    low = record([task('1.1', 'clarify', 'time', conf=0.3, clear=0.9)])
    assert evals.step_routes({'expect_agents': ['time']}, low)[0]['stage'] == 'confidence'
    wrong = record([task('1.1', 'knowledge', trace=[{'rule': 'missing_slot', 'agent': 'knowledge', 'why': 'no'}])])
    assert evals.step_routes({'expect_agents': ['time']}, wrong)[0]['stage'] == 'jev_pick'
    cant = record([task('1.1', 'chat', 'time', trace=[{'rule': 'cant_do', 'agent': 'chat', 'why': "can't remind"}])])
    assert evals.step_routes({'expect_agents': ['time']}, cant)[0]['stage'] == 'gate_cant'


def test_rt_2741_on_the_run_as_it_happened():
    """The committed 2741 case against the stored run: step 0 was forced to create."""
    fx = json.loads(FIXTURE.read_text())
    case = committed('rt-2741-forced-longpdf')
    reasons, codes = evals.check(case, fx['record'], mode='route')
    first = next(c for c in codes if c.get('step') == 0)
    assert first['code'] == 'wrong_agent' and first['got'] == 'create'
    steps = evals.step_routes(case, fx['record'])
    assert steps[0]['expected'] == 'research' and steps[0]['stage'] == 'forced'
    assert evals.with_stages(codes, steps)[0]['stage'] == 'forced'


# ---------- file quality (D2) ----------

def pdf_bytes(pages, colour=(0, 0, 0), image=None):
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.bookmarkPage('p0')
    c.addOutlineEntry('Start', 'p0')
    for text in pages:
        c.setFillColorRGB(*colour)
        c.drawString(72, 720, text)
        if image is not None:
            c.drawImage(ImageReader(image), 72, 400, 50, 50)
        c.showPage()
    c.save()
    return buf.getvalue()


def png(mode):
    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, (4, 4), 128 if mode == 'L' else (200, 30, 30)).save(buf, 'PNG')
    buf.seek(0)
    return buf


def test_rt_2741_files_fail_on_what_went_wrong():
    fx = json.loads(FIXTURE.read_text())
    rec = fx['record']
    blobs = {'e34bda15f529': b'# Artificial Intelligence\n\n## What it is\n\nText.\n',
             '1254d406bdd0': pdf_bytes(['Created **artificial-intelligence.md**, 12 KB',
                                        'No format was named, so this is Markdown.'][:1])}
    case = committed('rt-2741-forced-longpdf')
    reasons, fs = evals.score_files(case['expect_file'], rec, blobs.__getitem__, lambda fid: None)
    joined = ' | '.join(reasons)
    assert '1 pages, outside 12-13' in joined and 'extra file .md' in joined
    assert 'file body is a create reply template' in joined and 'has 0 diagrams, fewer than 2' in joined
    assert fs['pages'] == 1 and fs['files'] == 2 and fs['format'] == 'pdf' and fs['diagrams'] == 0


def test_file_keys_reopen_and_score():
    gray = pdf_bytes(['One', 'Two', 'Three'], image=png('L'))
    colour = pdf_bytes(['One'], colour=(0.1, 0.2, 0.8), image=png('RGB'))
    got = evals.reopen('pdf', gray)
    # the same picture on every page is one image XObject
    assert (got['pages'], got['headings'], got['images'], got['gray']) == (3, 1, 1, True) and 'Helvetica' in got['fonts']
    assert evals.reopen('pdf', colour)['gray'] is False
    made = lambda fid, fmt, **kw: {'id': fid, 'name': f'{fid}.{fmt}', 'format': fmt, 'source': 'llm', **kw}
    spec = {'sections': [{'blocks': [{'type': 'timeline'}, {'type': 'paragraph'}, {'type': 'tree'}]}]}
    rec = record([task('1.1', 'create', created_files=[made('g', 'pdf', role='primary')])])
    exp = {'format': 'pdf', 'pages': [3, 4], 'words_min': 3, 'headings_min': 1, 'images_min': 1, 'diagrams_min': 2,
           'fonts': 'helvet', 'grayscale': True, 'files_exact': 1, 'only_formats': ['pdf'], 'source': 'llm',
           'not_contains': ['Created **'], 'credits': False}
    reasons, fs = evals.score_files(exp, rec, {'g': gray}.__getitem__, {'g': spec}.get)
    assert reasons == [] and fs['diagrams'] == 2 and fs['images'] == 1 and fs['grayscale'] is True
    bad = {**exp, 'pages': [12, 13], 'words_min': 999, 'images_min': 9, 'fonts': 'anthropic', 'source': 'convert',
           'not_contains': ['two'], 'credits': True}
    reasons = evals.score_files(bad, rec, {'g': gray}.__getitem__, {'g': spec}.get)[0]
    assert any('outside 12-13' in r for r in reasons) and any('uses no font like /anthropic/' in r for r in reasons)
    assert any("contains 'two'" in r for r in reasons) and any('without an image credits section' in r for r in reasons)
    rec2 = record([task('1.1', 'create', created_files=[made('c', 'pdf')])])
    assert 'c.pdf is not all grey' in evals.score_files({'format': 'pdf', 'grayscale': True}, rec2, {'c': colour}.__getitem__)[0]


async def test_files_exact_defaults_to_one_and_every_file_is_checked():
    """A turn with expect_file allows exactly one file unless its steps name several; a file whose body is the create
    agent's reply line fails any case that made it, even one that asks nothing of files."""
    blobs = {'a': b'# Report\n\nAll good.\n', 'b': pdf_bytes(['Report'])}
    t = {'query': 'q', 'expect_file': {'format': 'pdf'}}
    two = record([task('1.1', 'create', created_files=[{'id': 'a', 'name': 'a.md', 'format': 'md'},
                                                      {'id': 'b', 'name': 'b.pdf', 'format': 'pdf'}])])
    out = evals.turn_result(t, two, blobs.__getitem__)
    assert out['reasons'] == ['extra file .md (a.md)'] and not out['pass']
    several = {**t, 'expect_steps': [{'format': 'md'}, {'format': 'pdf'}]}
    assert evals.file_expected(several, several) is None and evals.file_expected(t, t) == 1
    template = {'a': b'Created **x.pdf**, 1 page\n'}
    one = record([task('1.1', 'create', created_files=[{'id': 'a', 'name': 'a.md', 'format': 'md'}])])
    out = evals.turn_result({'query': 'q', 'must_match': '.'}, {**one, 'merged': {'answer': 'ok'}}, template.__getitem__)
    assert out['reasons'] == ['a.md file body is a create reply template']


# ---------- route mode through the real pipeline (D3) ----------

def raising_registry():
    async def boom(text, emit):
        raise AssertionError(f'an agent ran in route mode: {text}')
    return {a: boom for a in ('math', 'weather', 'time', 'currency', 'knowledge', 'code', 'chat', 'units', 'dates', 'url',
                              'create')}


def route_for(text):
    for word, agent in (('weather', 'weather'), ('EUR', 'currency'), ('%', 'math'), ('Who', 'knowledge')):
        if word in text:
            return agent, 0.9
    return 'chat', 0.3


ROUTE_CASES = [
    {'id': 'w', 'query': 'weather in Paris', 'tags': ['a'], 'expect_agents': ['weather'], 'must_match': 'never checked'},
    {'id': 'two', 'query': 'weather in Paris and convert 100 EUR to INR', 'tags': ['a'],
     'expect_steps': [{'agent': 'weather'}, {'agent': 'currency'}]},
    {'id': 'hm', 'query': 'hmm', 'tags': ['b'], 'expect_outcome': 'answer'},
    {'id': 'answers-only', 'query': 'Who was Ada?', 'tags': ['b'], 'must_match': 'Ada'},
]


def scrub(s):
    """An eval's result without what changes from run to run (times and ids)."""
    drop = {'at', 'ms', 'p50_ms', 'p95_ms', 'jev_ms', 'eval_id', 'qid', 'timings'}
    if isinstance(s, dict):
        return {k: scrub(v) for k, v in s.items() if k not in drop}
    if isinstance(s, list):
        return [scrub(v) for v in s]
    return s


async def test_route_mode_runs_no_agent_and_replays_identically(tmp_path):
    path = tmp_path / 'jev.jsonl'
    rec_cas = cassette_mod.JevCassette(FakeJev(route_for=route_for), path, 'record')
    r = Router(rec_cas, None, None, registry=raising_registry())
    s = await evals.run_eval(r, 'e', None, 'none', ROUTE_CASES, mode='route', jev='record', cassette=rec_cas)
    assert s['status'] == 'done' and s['total'] == 3  # the answers-only case has nothing to route
    assert {c['id']: c['pass'] for c in s['cases']} == {'w': True, 'two': True, 'hm': False}
    assert all(r.get_run(c['qid'])['dry_run'] == 'route' for c in s['cases'])
    assert s['confusion'] == {'currency': {'currency': 1}, 'weather': {'weather': 2}} and s['unrecorded'] == 0
    assert s['stages'] == {'confidence': 1} and s['answer_pass'] == {'passed': 0, 'total': 0}
    lines = path.read_text().splitlines()
    assert lines and len(lines) == len(rec_cas) and rec_cas.recorded == len(lines)
    runs = []
    for _ in range(2):  # replay needs no Jev at all, and gives the same result twice
        cas = cassette_mod.JevCassette(None, path, 'replay')
        router = Router(cas, None, None, registry=raising_registry())
        runs.append(await evals.run_eval(router, 'e', None, 'none', ROUTE_CASES, mode='route', jev='replay', cassette=cas))
    a, b = (json.dumps(scrub(x), sort_keys=True) for x in runs)
    assert a == b and scrub(runs[0])['cases'] == scrub(s)['cases']


async def test_a_cassette_miss_is_unrecorded_not_a_failure():
    cas = cassette_mod.JevCassette(None, None, 'replay')
    r = Router(cas, None, None, registry=raising_registry())
    s = await evals.run_eval(r, 'e', None, 'none', ROUTE_CASES[:1], mode='route', jev='replay', cassette=cas)
    c = s['cases'][0]
    assert c['pass'] is None and c['unrecorded'] and {'code': 'unrecorded'} in c['codes']
    assert (s['passed'], s['total'], s['unrecorded']) == (0, 0, 1) and cas.misses


# ---------- the cassette ----------

async def test_cassette_keys_record_replay_and_live():
    jev = FakeJev(route_for=lambda t: ('weather', 0.8))
    from jevrouter.jev import questions
    qs = questions({'weather': 'w', 'chat': 'c'})
    rev = dict(reversed(list(qs.items())))
    assert cassette_mod.key_for('hi', qs) == cassette_mod.key_for('hi', rev) != cassette_mod.key_for('hi!', qs)
    cas = cassette_mod.JevCassette(jev, None, 'record')
    a = await cas.system_one('weather in Paris', qs)
    b = await cas.system_one('weather in Paris', qs)
    assert len(jev.calls) == 1 and a.answers['route'].choice == b.answers['route'].choice == 'weather'
    assert a.answers['route'].probabilities['weather'] == 0.8 and a.usage.input_tokens == 100 and cas.hits == 1
    replay = cassette_mod.JevCassette(None, None, 'replay')
    replay.entries = dict(cas.entries)
    view = replay.view()
    assert (await view.system_one('weather in Paris', qs)).answers['clear'].noul == 0.9
    with pytest.raises(cassette_mod.CassetteMiss):
        await view.system_one('weather in Rome', qs)
    assert view.misses == ['weather in Rome'] and replay.misses == ['weather in Rome']
    live = cassette_mod.JevCassette(jev, None, 'live')
    await live.system_one('x', qs)
    assert len(jev.calls) == 2 and len(live) == 0
    with pytest.raises(ValueError):
        cassette_mod.JevCassette(jev, None, 'tape')


def test_cassette_file_is_append_only_and_later_lines_win(tmp_path):
    p = tmp_path / 'c.jsonl'
    p.write_text('{"key": "k", "text": "a", "answers": {}, "model": "m1", "input_tokens": 1}\nnot json\n'
                 '{"key": "k", "text": "a", "answers": {}, "model": "m2", "input_tokens": 2}\n')
    cas = cassette_mod.JevCassette(None, p, 'replay')
    assert len(cas) == 1 and cas.entries['k']['model'] == 'm2'


# ---------- summaries: confusion, per agent, calibration ----------

def test_per_agent_and_reliability():
    conf = {'weather': {'weather': 8, 'clarify': 2}, 'time': {'time': 5}, 'clarify': {'weather': 1}}
    rows = {r['agent']: r for r in evals.per_agent(conf)}
    assert rows['weather'] == {'agent': 'weather', 'tp': 8, 'fp': 1, 'fn': 2, 'precision': 0.8889, 'recall': 0.8,
                               'f1': 0.8421, 'support': 10}
    assert rows['clarify']['precision'] == 0.0 and rows['clarify']['recall'] == 0.0 and rows['clarify']['f1'] == 0.0
    cal = evals.reliability([(0.95, True), (0.92, False), (0.15, False), (1.0, True)], 'confidence')
    top, low = cal['bins'][9], cal['bins'][1]
    assert (top['n'], top['accuracy'], low['n'], low['accuracy']) == (3, 0.6667, 1, 0.0)
    assert cal['ece'] == pytest.approx(3 / 4 * abs(0.6667 - 0.9567) + 1 / 4 * 0.15, abs=1e-3)
    assert evals.reliability([], 'clear')['ece'] is None and len(evals.reliability([], 'clear')['bins']) == 10


def test_confusion_counts_a_route_mode_lone_term_as_the_clarify_the_case_passes_with():
    from jevrouter.pipeline import ROUTE_TERM
    term = {'agent': 'code', 'expected': 'clarify', 'trace': [{'rule': 'ambiguous_term', 'agent': 'code', 'why': ROUTE_TERM}]}
    wrong = {'agent': 'code', 'expected': 'clarify', 'trace': [{'rule': 'missing_slot', 'agent': 'code', 'why': 'x'}]}
    other = {**term, 'expected': 'knowledge'}  # only a clarify expectation is met by the deferred lookup
    conf = evals.confusion_of([{'steps': [term]}, {'steps': [wrong]}, {'steps': [other]}])
    assert conf == {'clarify': {'clarify': 1, 'code': 1}, 'knowledge': {'code': 1}}


def test_calibration_sweep_is_fast_and_reports_dev_and_holdout():
    steps = []
    for i in range(300):
        clear = (i % 10) / 10 + 0.05
        steps.append({'tid': f'{i}.1', 'agent': 'knowledge', 'pick': 'knowledge', 'confidence': 0.9, 'clear': clear,
                      'expected': 'knowledge' if clear > 0.2 else 'clarify',
                      'probabilities': {'knowledge': 0.9, 'chat': 0.1}, 'unsafe': 0.0, 'signals': {},
                      'input': 'tell me more about that thing', 'depends_on': [], 'trace': []})
    cases = [{'id': f'c{i}', 'query': 'q', 'tags': ['honesty'] if i % 3 == 0 else [],
              'split': 'holdout' if i % 4 == 0 else 'dev'} for i in range(300)]
    result = {'cases': [{'id': f'c{i}', 'steps': [s]} for i, s in enumerate(steps)]}
    t0 = time.perf_counter()
    out = evals.calibrate(result, cases)
    assert time.perf_counter() - t0 < 2.0
    mc = out['sweeps']['MIN_CLEAR']
    assert [r['value'] for r in mc['rows']] == [0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4] and out['n'] == 300
    by = {r['value']: r['dev'] for r in mc['rows']}
    assert by[0.25]['clarify_rate'] > by[0.1]['clarify_rate'] and mc['best_on_dev'] in (0.2, 0.25)
    assert set(out['sweeps']) >= {'MIN_CLEAR', 'CONFIRM_AT'} and 'holdout_at_best' in mc
    from jevrouter import config, jev
    assert config.MIN_CLEAR == 0.25 and jev.MIN_CLEAR == 0.25  # every patched threshold is restored


# ---------- unjudged (D5) ----------

async def test_keyless_eval_leaves_every_rubric_case_unjudged():
    judged = [c for c in evals.load_cases(evals.CASES) if c.get('judge')]
    r = StubRouter(lambda qid, q, kw: record([task(f'{qid}.1', 'knowledge', text=q)], 'A fine answer.', qid=qid))
    s = await evals.run_eval(r, 'e', None, 'none', [{k: v for k, v in c.items() if k != 'files'} for c in judged])
    assert s['unjudged'] == len(judged) == 14 and s['passed'] == 0 and s['total'] == 0
    assert all(c['pass'] is None and c['unjudged'] for c in s['cases'])
    assert 'unjudged 14' in evals.headline('none', s)


async def test_file_aware_judging_gets_a_file_block():
    class J(FakeEngine):
        prompts = []

        async def stream(self, *, system, prompt, **kw):
            from jevrouter.engines import Reply
            self.prompts.append(prompt)
            return Reply('{"correct": 4, "complete": 4, "grounded": 4, "concise": 4}', 1, 1)
    e = J('codex', 'Codex')
    s = await judge.grade(e, 'q', 'a', 'r', file_summary='format pdf, 12 pages')
    assert s['mean'] == 4.0 and '<<<FILE\nformat pdf, 12 pages\nFILE>>>' in J.prompts[-1]
    await judge.grade(e, 'q', 'a', 'r')
    assert 'FILE' not in J.prompts[-1]
    fs = {'format': 'pdf', 'pages': 12, 'slides': None, 'words': 5000, 'headings': 9, 'images': 3, 'diagrams': 2,
          'fonts': ['Helvetica'], '_sample': 'Intro text'}
    block = evals.file_summary(fs)
    assert block.startswith('format pdf, 12 pages, 5000 words, 9 headings') and 'diagrams 2' in block and 'Intro text' in block


def test_judge_auto_takes_the_strongest_healthy_other_engine():
    cc, codex, agy = FakeEngine('claude-code', 'Claude Code'), FakeEngine('codex', 'Codex'), FakeEngine('agy', 'Antigravity')
    engines = {'agy': agy, 'codex': codex, 'claude-code': cc}
    assert judge.pick(engines, 'auto', agy)[0] is cc
    assert judge.pick(engines, 'auto', cc)[0] is codex
    assert judge.pick(engines, 'auto', agy, healthy=lambda e: e is not cc)[0] is codex


async def test_judge_calibrate_on_the_gold_set(tmp_path):
    rows = evals.read_gold(evals.JUDGE_GOLD)
    assert len(rows) == 30 and {r['qid'] for r in rows} >= {2741, 1580, 380, 59, 385}

    class Echo(FakeEngine):
        async def stream(self, *, system, prompt, **kw):
            from jevrouter.engines import Reply
            row = next(r for r in rows if r['rubric'] in prompt and r['answer'][:200] in prompt)
            return Reply(json.dumps(row['scores']), 1, 1)
    out = await evals.judge_agreement(Echo('codex', 'Codex'), rows)
    assert out['agreement'] == 1.0 and out['mae'] == {c: 0.0 for c in judge.CRITERIA}
    assert out['drafts'] == 30 and not out['trusted']  # unreviewed scores never make the judge trusted
    reviewed = [{**r, 'scored_by': 'lead'} for r in rows]
    assert (await evals.judge_agreement(Echo('codex', 'Codex'), reviewed))['trusted']
    bad = tmp_path / 'g.jsonl'
    bad.write_text('{"question": "q", "answer": "a", "rubric": "r", "scores": {"correct": 1}}\n')
    with pytest.raises(evals.CaseError, match='needs question, answer, rubric and scores'):
        evals.read_gold(bad)


# ---------- gates and baselines (D7) ----------

def test_wilson_and_gates():
    assert evals.wilson_lower(10, 10) == pytest.approx(0.7225, abs=1e-3) and evals.wilson_lower(0, 0) == 0.0
    assert evals.wilson_lower(95, 100) == pytest.approx(0.8882, abs=1e-3)
    result = {'mode': 'route', 'by_tag_route': {'safety': {'passed': 9, 'total': 10}, 'boundary': {'passed': 38, 'total': 40},
                                                 'dag': {'passed': 4, 'total': 13}, 'misc': {'passed': 2, 'total': 2},
                                                 'half': {'passed': 1, 'total': 2}}}
    rules = {'safety': {'min': 1.0}, 'boundary': {'min': 0.9, 'max_drop': 0.05}, '*': {'min': 0.99, 'max_drop': 0.1}}
    g = {x['tag']: x for x in evals.gate_tags(result, rules, {'rates': {'boundary': 0.975, 'dag': 0.3077}})}
    assert not g['safety']['ok'] and g['boundary']['ok'] and g['dag']['ok']
    assert g['misc']['ok'] and not g['half']['ok']  # no baseline: every case passed, or half of them against min 0.99
    assert g['boundary']['lower'] < 0.9 and g['boundary']['baseline'] == 0.975
    result['by_tag_route']['boundary'] = {'passed': 37, 'total': 40}
    g = {x['tag']: x for x in evals.gate_tags(result, rules, {'rates': {'boundary': 1.0}})}
    assert not g['boundary']['ok']  # 0.925 is more than 0.05 below 1.0
    committed_gates = evals.read_json(evals.GATES)
    for suite in ('route', 'keyless'):
        for tag in ('safety', 'injection', 'control'):
            assert committed_gates[suite][tag]['min'] == 1.0


def test_save_baseline_and_tag_drops():
    r = {'mode': 'route', 'engine': 'none', 'by_tag_route': {'a': {'passed': 1, 'total': 2}, 'b': {'passed': 0, 'total': 0}}}
    b = evals.save_baseline({}, 'route', r, 'sha')
    assert b['route']['rates'] == {'a': 0.5} and b['route']['suite_sha'] == 'sha'
    assert evals.tag_drops({'by_tag': {'a': {'passed': 1, 'total': 3}}}, b['route']) == ['a']
    assert evals.tag_drops({'by_tag': {'a': {'passed': 2, 'total': 5}}}, b['route']) == []


def test_committed_cassette_matches_the_suite_or_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(evals, 'CASSETTE', tmp_path / 'none.jsonl')
    assert 'no Jev cassette' in evals.cassette_problem('replay', 'x')
    (tmp_path / 'none.jsonl').write_text('')
    monkeypatch.setattr(evals, 'CASSETTE_META', tmp_path / 'meta.json')
    (tmp_path / 'meta.json').write_text('{"suite_sha": "old"}')
    assert 'changed since the cassette was recorded' in evals.cassette_problem('replay', 'new')
    assert evals.cassette_problem('replay', 'old') is None and evals.cassette_problem('live', 'x') is None


# ---------- budgets and determinism (D8) ----------

async def test_budgets_warn_and_the_planner_is_checked():
    def make(qid, q, kw):
        rec = record([task(f'{qid}.1', 'time', text=q)], '10:00', qid=qid)
        rec['total_ms'], rec['tokens'] = 900, {'jev_in': 10, 'llm_in': 0, 'llm_out': 50}
        rec['timings'] = {'planner': 'llm', 'plan_ms': 1200}
        return rec
    case = {'id': 't', 'query': 'What time is it in New York?', 'tags': ['latency'], 'expect_agents': ['time'],
            'budget': {'planner': ['single', 'heuristic'], 'plan_ms': 800, 'llm_out': 20}}
    s = await evals.run_eval(StubRouter(make), 'e', None, 'none', [case], budgets={})
    c = s['cases'][0]
    assert c['pass'] is False and c['reasons'] == ['planned with llm, expected single or heuristic']
    assert c['budget_warnings'] == ['used 50 llm_out tokens, over the 20 budget', 'planning took 1200 ms, over the 800 ms budget']
    assert c['tokens'] == {'jev_in': 10, 'llm_in': 0, 'llm_out': 50} and s['tokens']['llm_out'] == 50
    s = await evals.run_eval(StubRouter(make), 'e', None, 'none', [case], budgets={}, strict_budgets=True)
    assert len(s['cases'][0]['reasons']) == 3 and s['stages'] == {'budget': 1}
    budgets = {'latency': {'keyless': {'p95_ms': 100}}}
    assert evals.budget_for({**case, 'budget': {}}, budgets, 'keyless') == {'p95_ms': 100}
    assert evals.budget_for(case, budgets, 'cli')['plan_ms'] == 800
    assert evals.engine_class(None) == 'keyless' and evals.engine_class(FakeEngine(billing='api')) == 'api'


def test_suite_and_case_sha_and_compare_warnings(tmp_path):
    p = tmp_path / 'c.jsonl'
    p.write_text('{"id": "b", "query": "q"}\n{"id": "a", "query": "q"}\n')
    one = evals.suite_sha(p)
    p.write_text('{"id": "a", "query": "q"}\n\n{"id": "b", "query": "q"}\n')
    assert evals.suite_sha(p) == one  # sorted lines: order and blank lines don't matter
    c = {'id': 'a', 'query': 'q', 'tags': ['x']}
    assert evals.case_sha(c) == evals.case_sha({**c, 'tags': ['x', 'mode:quick']})  # auto tags are not the case's own
    assert evals.case_sha(c) != evals.case_sha({**c, 'query': 'q2'})
    side = lambda sha, csha: {'eval_id': 'e', 'engine': 'none', 'accuracy': 1, 'passed': 1, 'total': 1, 'silent_wrong': 0,
                              'suite_sha': sha, 'cases': [{'id': 'a', 'query': 'q', 'pass': True, 'case_sha': csha}]}
    out = evals.compare(side('s1', 'x'), side('s2', 'y'))
    assert out['warnings'] == ['the two evals ran different versions of the case suite', '1 cases changed between the two evals: a']
    assert 'warnings' not in evals.compare(side('s1', 'x'), side('s1', 'x'))


def test_flaky_reads_stored_evals(tmp_path):
    db = tmp_path / 'tg.db'
    store = Store(db)
    for i, ok in enumerate((True, False, True)):
        evals.save(store, {'eval_id': f'e{i}', 'at': i, 'engine': 'none', 'status': 'done', 'done': 2, 'passed': 1,
                           'total': 2, 'accuracy': 0.5, 'silent_wrong': 0, 'suite_sha': 'S', 'mode': 'route',
                           'cases': [{'id': 'flip', 'pass': ok}, {'id': 'steady', 'pass': True}, {'id': 'skip', 'pass': None}]})
    evals.save(store, {'eval_id': 'other', 'at': 9, 'engine': 'none', 'status': 'done', 'done': 1, 'passed': 0, 'total': 1,
                       'accuracy': 0, 'silent_wrong': 0, 'suite_sha': 'T', 'cases': [{'id': 'steady', 'pass': False}]})
    store.close()
    assert evals.flaky(db, 10) == [{'id': 'flip', 'suite_sha': 'S', 'engine': 'none', 'mode': 'route', 'passes': 2, 'fails': 1}]


# ---------- paraphrases (D8) ----------

def test_paraphrases_are_deterministic_and_keep_expectations():
    cases = [{'id': 'cur', 'query': 'Convert 100 USD to EUR and what is the weather in Paris?', 'tags': ['x'],
              'expect_agents': ['currency', 'weather'], 'split': 'holdout'},
             {'id': 'one', 'query': 'Python', 'tags': []},
             {'id': 'w', 'query': 'What is the weather in Tokyo right now?', 'tags': [], 'expect_agents': ['weather']},
             {'id': 'mt', 'tags': [], 'turns': [{'query': 'a b'}, {'query': 'c d'}]}]
    a = paraphrase.generate(cases, 7, 3)
    b = paraphrase.generate(cases, 7, 3)
    assert json.dumps(a) == json.dumps(b) and json.dumps(a) != json.dumps(paraphrase.generate(cases, 8, 3))
    assert {v['id'].split('.g-')[0] for v in a} == {'cur', 'w'} and len([v for v in a if v['id'].startswith('w.')]) == 3
    for v in a:
        src = next(c for c in cases if v['id'].startswith(c['id'] + '.'))
        assert v['query'] != src['query'] and v['expect_agents'] == src['expect_agents'] and 'paraphrase-gen' in v['tags']
        assert v.get('split', 'dev') == src.get('split', 'dev') and evals.validate_case(v) == []
    one = lambda t, q='What is the weather in Tokyo right now?', **c: paraphrase.FUNCS[t](q, paraphrase.random.Random(1), c)
    assert one('lower') == 'what is the weather in tokyo right now'
    assert one('agent', expect_agents=['weather']) == '@weather What is the weather in Tokyo right now?'
    assert one('currency', 'Convert $100 to EUR') in ('Convert 100 USD to EUR', 'Convert 100 dollars to EUR',
                                                     'Convert 100 bucks to EUR', 'Convert 100 US dollars to EUR')
    assert 40 <= len(one('long').split()) <= 80 and one('reorder', 'weather in Paris and time in Tokyo') == \
        'Time in Tokyo and weather in Paris'
    assert one('reorder', 'Convert 50 EUR and then the time there') is None
    typo = one('typos')
    assert typo != 'What is the weather in Tokyo right now?' and len(typo.split()) == 8
    with pytest.raises(ValueError):
        paraphrase.generate(cases, 1, 1, ['shout'])


def test_paraphrase_cli_prints_json_lines(capsys, monkeypatch):
    assert evals.main(['paraphrase', '--seed', '7', '--n', '2', '--tags', 'control']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines and all(json.loads(line)['id'].count('.g-') == 1 for line in lines)
    assert evals.main(['paraphrase', '--seed', '7', '--n', '2', '--tags', 'control']) == 0
    assert capsys.readouterr().out.splitlines() == lines


# ---------- real traffic (D6) ----------

def test_harvest_lists_runs_with_suspects_newest_first(tmp_path, capsys):
    fx = json.loads(FIXTURE.read_text())
    db = tmp_path / 'tg.db'
    store = Store(db)
    now = fx['record']['at'] + 60
    base = {'status': 'done', 'source': 'chat', 'plan': None, 'merged': None, 'total_ms': 1, 'error': None, 'engine': None,
            'session_id': None, 'compare_id': None, 'files': [], 'tokens': {}, 'mode': 'balanced', 'style': 'default',
            'agent': None, 'group_id': None, 'chosen': True}
    store.save_run({**base, 'qid': 2000, 'text': 'older', 'at': now - 86400, 'tasks': [],
                    'suspects': [{'code': 'dead_end', 'note': 'No summary found'}]})
    store.save_run({**base, 'qid': 2100, 'text': 'fine', 'at': now - 100, 'tasks': [], 'suspects': []})
    store.save_run({**base, 'qid': 1, 'text': 'too old', 'at': now - 40 * 86400, 'tasks': [],
                    'suspects': [{'code': 'cut_off', 'note': 'x'}]})
    store.save_run({**fx['record'], 'suspects': [{'code': 'pages_short', 'note': 'asked for 12-13 pages, made 1'},
                                                 {'code': 'forced_non_file', 'note': 'step 1'}]})
    store.close()
    rows = evals.harvest(db, evals.parse_since('30d'), now=now)
    assert [r['qid'] for r in rows] == [2741, 2000] and rows[0]['suspects'][0]['code'] == 'pages_short'
    assert evals.parse_since('12h') == 43200 and evals.parse_since('2w') == 1209600
    with pytest.raises(ValueError):
        evals.parse_since('soon')
    assert evals.main(['harvest', '--since', '30d', '--db', str(db), '--json']) == 0
    assert isinstance(json.loads(capsys.readouterr().out), list)


def test_case_from_run_drafts_the_plan_steps_and_note():
    fx = json.loads(FIXTURE.read_text())
    case = evals.case_from_run({**fx['record'], 'agent': 'create', 'mode': 'research'}, [],
                               [{'code': 'forced_non_file', 'note': 'step 1 asked for no file'}])
    assert case['id'] == 'run-2741' and case['chat'] == {'mode': 'research', 'agent': 'create'}
    assert case['plan']['deps'] == [[], [0]] and len(case['plan']['subtasks']) == 2
    assert case['expect_steps'] == [{'agent': 'create', 'forced': True}, {'agent': 'create', 'forced': True, 'depends_on': [0]}]
    assert 'forced_non_file: step 1 asked for no file' in case['note'] and evals.validate_case(case) == []


# ---------- HTTP: promote, suspect filter, eval run options, query limit ----------

from tests.test_api import client, json_of  # noqa: E402,F401  (the app fixture with fake engines)


async def finish(client, qid):
    task_ = client.router.running.get(qid)
    if task_:
        await asyncio.wait_for(task_, 5)


async def test_promote_a_run_to_a_draft_case(client, local_cases):
    qid = (await json_of(await client.post('/ask', json={'query': 'weather in Paris and convert 100 EUR to INR'})))['qid']
    await finish(client, qid)
    body = await json_of(await client.post(f'/api/runs/{qid}/promote'))
    assert body == {'case_id': f'run-{qid}', 'created': True, 'path': body['path']} and body['path'].endswith('cases.local.jsonl')
    case = json.loads(local_cases.read_text().splitlines()[0])
    assert case['query'] == 'weather in Paris and convert 100 EUR to INR' and 'real-traffic' in case['tags']
    assert [s['agent'] for s in case['expect_steps']] == ['weather', 'currency'] and case['plan']['subtasks']
    assert [c['id'] for c in evals.local_cases()] == [f'run-{qid}']
    assert (await client.post(f'/api/runs/{qid}/promote')).status == 409
    assert (await client.post('/api/runs/999/promote')).status == 404
    assert (await client.post('/api/runs/abc/promote')).status == 404


async def test_runs_can_be_filtered_to_suspects(client):
    store = client.router.store
    base = {'status': 'done', 'source': 'chat', 'plan': None, 'merged': None, 'total_ms': 1, 'error': None, 'engine': None,
            'session_id': None, 'compare_id': None, 'files': [], 'tokens': {}, 'tasks': [], 'at': 1.0}
    for qid in range(1, 260):
        store.save_run({**base, 'qid': qid, 'text': f'q{qid}',
                        'suspects': [{'code': 'dead_end', 'note': 'x'}] if qid in (3, 250) else []})
    runs = (await json_of(await client.get('/api/runs?suspect=1&limit=5')))['runs']
    assert [r['qid'] for r in runs] == [250, 3]
    assert len((await json_of(await client.get('/api/runs?limit=5')))['runs']) == 5
    assert [r['qid'] for r in (await json_of(await client.get('/api/runs?suspect=1&limit=1')))['runs']] == [250]


async def test_a_run_saved_before_suspects_were_stored_is_listed_with_them(client):
    store = client.router.store
    base = {'status': 'done', 'source': 'chat', 'plan': None, 'total_ms': 1, 'error': None, 'engine': None,
            'session_id': None, 'compare_id': None, 'files': [], 'tokens': {}, 'at': 1.0}
    store.save_run({**base, 'qid': 7, 'text': 'Who founded it?', 'merged': {'answer': 'No summary found for it'},
                    'tasks': [{'tid': '7.1', 'agent': 'knowledge', 'ok': False,
                               'answer': 'No summary found for it'}]})
    rec = store.get_run(7)
    rec.pop('suspects', None)
    want = evals.suspects_of(rec)
    assert want  # the run's answer is a dead end
    for url in ('/api/runs?suspect=1', '/api/runs'):
        runs = (await json_of(await client.get(url)))['runs']
        assert [(r['qid'], r['suspects']) for r in runs if r['qid'] == 7] == [(7, want)]


async def test_eval_run_body_mode_and_jev(client, tmp_path, monkeypatch):
    cases = tmp_path / 'cases.jsonl'
    cases.write_text('{"id": "w", "query": "weather in Paris", "expect_agents": ["weather"], "tags": ["w"]}\n'
                     '{"id": "a", "query": "weather in Paris", "must_match": "Paris", "tags": ["w"]}\n')
    monkeypatch.setattr(evals, 'CASES', cases)
    monkeypatch.setattr(evals, 'CASSETTE', tmp_path / 'none.jsonl')
    post = lambda body: client.post('/api/evals/run', json=body)
    for body, status in (({'mode': 'fast'}, 400), ({'jev': 'record', 'mode': 'route'}, 400), ({'jev': 'replay'}, 400),
                         ({'mode': 'route', 'jev': 'replay'}, 409), ({'mode': 'route', 'judge': 'auto'}, 400)):
        assert (await post(body)).status == status, body
    eid = (await json_of(await post({'mode': 'route'})))['eval_id']
    await client.router.evals[eid]
    d = await json_of(await client.get(f'/api/evals/{eid}'))
    assert (d['mode'], d['jev'], d['total'], d['passed']) == ('route', 'live', 1, 1)  # only the case with a route check
    assert client.router.get_run(d['cases'][0]['qid'])['dry_run'] == 'route' and d['confusion'] == {'weather': {'weather': 1}}


async def test_query_limit(client):
    from jevrouter.config import MAX_QUERY_CHARS
    text = ('weather in Paris ' * 50)[:700]
    qid = (await json_of(await client.post('/api/ask', json={'query': text})))['qid']
    await finish(client, qid)
    assert client.router.get_run(qid)['text'] == text.strip() and len(text.strip()) > 500
    r = await client.post('/ask', json={'query': 'x' * 4213})
    assert r.status == 400 and await r.json() == {'error': 'Your message is 4,213 characters; the limit is 4,000.',
                                                 'limit': MAX_QUERY_CHARS}
    r = await client.post('/api/compare', json={'query': 'x' * 4100, 'engines': ['codex', 'claude-code']})
    assert r.status == 400 and (await r.json())['limit'] == 4000
    assert (await client.post('/ask', json={'query': 'x' * 4000})).status == 200
    config = await json_of(await client.get('/api/config'))
    assert config['limits'] == {'query_chars': 4000} and config['fonts'] == {'body': None}


def test_cassette_follows_only_the_cases_route_mode_runs(monkeypatch, tmp_path):
    routed = {'id': 'r', 'query': 'weather in Paris', 'expect_agents': ['weather']}
    p = tmp_path / 'cases.jsonl'
    p.write_text(json.dumps(routed) + '\n')
    before, whole = evals.route_sha(p), evals.suite_sha(p)
    cli_only = {'id': 'c', 'query': 'make a deck', 'expect_agents': ['create'], 'run_on': ['cli', 'api']}
    p.write_text(json.dumps(routed) + '\n' + json.dumps(cli_only) + '\n')
    assert evals.route_sha(p) == before  # a case route mode never runs doesn't touch the cassette
    assert evals.suite_sha(p) != whole  # while the whole-suite sha does move
    p.write_text(json.dumps({**routed, 'query': 'weather in Rome'}) + '\n')
    assert evals.route_sha(p) != before  # a routed case changing does
    monkeypatch.setattr(evals, 'CASSETTE', tmp_path / 'c.jsonl')
    (tmp_path / 'c.jsonl').write_text('')
    monkeypatch.setattr(evals, 'CASSETTE_META', tmp_path / 'meta.json')
    (tmp_path / 'meta.json').write_text(json.dumps({'suite_sha': 'old', 'route_sha': before}))
    assert evals.cassette_problem('replay', 'new', before) is None
    assert 'routed cases changed' in evals.cassette_problem('replay', 'new', 'other')
    assert 'changed since the cassette' in evals.cassette_problem('replay', 'new')  # no route sha given: whole suite


def test_committed_cassette_meta_matches_the_routed_cases():
    meta = json.loads(evals.CASSETTE_META.read_text())
    assert meta.get('route_sha') == evals.route_sha()
    assert evals.cassette_problem('replay', evals.suite_sha(), evals.route_sha()) is None

"""Studio integration and API (docs/PLAN-designer.md 5 and 9.9-9.10, builder I): the create agent's design stage behind
TG_STUDIO with its fallback to the standard renderer, D1-D8 from the design report, render_designed, the design and
font endpoints, thumbnails, restyle (0 tokens), polish (priced and guarded), their sandbox mirrors, and workspaces that
go with their file or sandbox.

Studio's internals (builders F, L, Q, V) are replaced by fakes here, so these tests hold whatever state theirs are in:
a fake design agent that "paints" with the legacy renderer and a fake in-memory workspace.
"""
import asyncio
import io
import json
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from jevrouter import app as appmod
from jevrouter import create as cf
from jevrouter import estimate as estimate_mod
from jevrouter.agents import create as ca
from jevrouter.create import rules as rules_mod
from jevrouter.create.render import render_designed
from jevrouter.pipeline import Router
from jevrouter.store import Store
from jevrouter.studio import agent as studio_agent
from jevrouter.studio import critic as studio_critic
from jevrouter.studio import fonts as studio_fonts
from jevrouter.studio import presets as studio_presets
from jevrouter.studio import thumbs as studio_thumbs
from jevrouter.studio import workspace as studio_ws
from jevrouter.studio.fonts import FontInfo
from jevrouter.studio.plan import DesignPlan, DesignResult, FontUse, PagePlan
from jevrouter.studio.presets import PresetInfo, TemplateInfo
from tests.fakes import FakeEngine, FakeJev

SID = 'sandbox-studio-1'
SPEC = {'title': 'Photosynthesis', 'subtitle': 'Year 9 biology',
        'sections': [{'heading': 'What it is', 'level': 1, 'notes': '',
                      'blocks': [{'type': 'paragraph', 'text': 'Plants turn light into sugar.'},
                                 {'type': 'bullets', 'items': ['Chloroplasts', 'Needs light'], 'ordered': False}]},
                     {'heading': 'Inputs', 'level': 1, 'notes': '',
                      'blocks': [{'type': 'table', 'columns': ['In', 'Out'], 'rows': [['CO2', 'Glucose']]}]}]}


def png(w=48, h=27) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (w, h), (20, 20, 40)).save(buf, 'PNG')
    return buf.getvalue()


# ---------- fakes for Studio's internals ----------


class FakeWS:
    """An in-memory stand-in for studio.workspace.Workspace (thumbnails as real files under a temp dir)."""

    def __init__(self, fid, sandbox, root: Path):
        self.file_id, self.sandbox, self.root = fid, sandbox, root / (sandbox or '_') / fid
        self.plan = self.report = None
        self.deleted = False

    def exists(self):
        return self.plan is not None or self.report is not None or self.root.is_dir()

    def load_plan(self, previous=False):
        return self.plan

    def load_report(self):
        return self.report

    def save_thumb(self, page, data):
        (self.root / 'thumbs').mkdir(parents=True, exist_ok=True)
        (self.root / 'thumbs' / f'{page + 1:03d}.png').write_bytes(data)

    def thumbs(self):
        d = self.root / 'thumbs'
        return sorted(d.glob('*.png')) if d.is_dir() else []

    def copy_to(self, fid):
        other = WS.open(fid, self.sandbox)
        other.plan, other.report = self.plan, self.report
        return other

    def delete(self):
        self.deleted = True
        self.plan = self.report = None


class Spaces:
    def __init__(self):
        self.all, self.root = {}, None
        self.dropped, self.dropped_sandboxes = [], []

    def open(self, fid, sandbox=None):
        return self.all.setdefault((sandbox, fid), FakeWS(fid, sandbox, self.root))


WS = Spaces()


def fake_plan(fid, fmt, pages, preset='bold-dark', score=85):
    layouts = ['cover-hero'] + ['title-bullets'] * (pages - 1)
    size = (960, 540) if fmt == 'pptx' else (595.27, 841.89)
    return DesignPlan(file_id=fid, format=fmt, preset=preset,
                      system={'colors': {'bg': '101820', 'text': 'F5F5F5', 'accent': 'FF5A5F', 'overlay': '000000'},
                              'chart_palette': ['0072B2', 'E69F00'],
                              'families': {'display': 'Poppins', 'heading': 'Poppins', 'body': 'Inter'}},
                      direction={'preset': preset, 'pages': [{'layout': x} for x in layouts]},
                      pages=[PagePlan(i, x, *size, section=i - 1 if i else None) for i, x in enumerate(layouts)],
                      fonts=[FontUse('Inter', 'body', 'cache', 'OFL-1.1', False, 'Calibri'),
                             FontUse('Poppins', 'heading', 'fontsource', 'OFL-1.1', False, 'Arial')],
                      score=score, rounds=1, stop='pass')


def fake_report(fmt, pages, *, d1_ok=False, phases=()):
    checks = [{'id': f'D{i}', 'name': f'check {i}', 'ok': i != 1 or d1_ok, 'failures': 0 if i != 1 or d1_ok else 2,
               'note': '' if i != 1 or d1_ok else 'text overflows its box on slide 2'} for i in range(1, 9)]
    return {'version': 1, 'score': 80, 'preset': 'bold-dark', 'format': fmt, 'fonts': [], 'rounds': 1, 'stop': 'pass',
            'tokens': {}, 'checks': checks, 'results': [], 'layouts': {'cover-hero': 1, 'title-bullets': pages - 1},
            'fallbacks': [], 'critic': {'ran': False, 'why': 'keyless'}, 'thumbs': pages, 'phases': list(phases),
            'notes': []}


def painted_result(spec, fmt, fid, sandbox=None, *, engine=None, painted=True, tokens=(100, 50), d1_ok=False,
                   preset='bold-dark'):
    """What a real design() would give: a plan, a report, thumbnails in the workspace and the painted file (here the
    legacy renderer's bytes, which pass verify())."""
    norm, _ = cf.normalize(spec, fmt)
    pages = 1 + len(norm['sections'])
    phases = [{'phase': 'direct', 'calls': 1 if engine else 0, 'llm_in': tokens[0] if engine else 0,
               'llm_out': tokens[1] if engine else 0, 'ms': 4}, {'phase': 'paint', 'calls': 0, 'llm_in': 0,
                                                                  'llm_out': 0, 'ms': 3}]
    plan = fake_plan(fid, fmt, pages, preset=preset)
    report = fake_report(fmt, pages, d1_ok=d1_ok, phases=phases)
    ws = WS.open(fid, sandbox)
    ws.plan, ws.report = plan, report
    for i in range(pages):
        ws.save_thumb(i, png())
    meta = {'studio': True, 'preset': preset, 'fonts': [{'family': 'Inter', 'role': 'body'}], 'score': plan.score,
            'notes': [], 'thumbs': pages}
    return DesignResult(plan=plan, painted_bytes=cf.render(spec, fmt) if painted else None, report=report,
                        phases=phases, notes=['Laid out in the bold-dark preset.'], caveats=[], meta_design=meta)


@pytest.fixture(autouse=True)
def studio_fakes(monkeypatch, tmp_path):
    """Studio's internals as fakes; every call is recorded in `calls`."""
    global WS
    WS = Spaces()
    WS.root = tmp_path / 'ws'
    calls = {'design': [], 'restyle': [], 'polish': []}

    async def design(spec, fmt, *, file_id, brief=None, request='', tokens_src=None, engine=None, http=None,
                     mode='balanced', sandbox=None, deadline=None, preset=None):
        calls['design'].append({'spec': spec, 'fmt': fmt, 'file_id': file_id, 'request': request, 'engine': engine,
                                'http': http, 'mode': mode, 'sandbox': sandbox, 'deadline': deadline,
                                'preset': preset, 'tokens_src': tokens_src})
        return painted_result(spec, fmt, file_id, sandbox, engine=engine)

    def restyle(spec, fmt, ws, options, *, file_id):
        calls['restyle'].append({'fmt': fmt, 'ws': ws, 'options': options, 'file_id': file_id})
        return painted_result(spec, fmt, file_id, ws.sandbox, preset=options.preset or 'minimal', d1_ok=True)

    async def polish(spec, fmt, ws, engine, *, file_id, deadline=None):
        calls['polish'].append({'ws': ws, 'engine': engine, 'file_id': file_id})
        return painted_result(spec, fmt, file_id, ws.sandbox, engine=engine, tokens=(3000, 400), d1_ok=True)

    monkeypatch.setattr(studio_agent, 'design', design)
    monkeypatch.setattr(studio_agent, 'restyle', restyle)
    monkeypatch.setattr(studio_agent, 'polish', polish)
    monkeypatch.setattr(studio_ws, 'open_workspace', lambda fid, *, sandbox=None: WS.open(fid, sandbox))
    monkeypatch.setattr(studio_ws, 'drop', lambda fid: WS.dropped.append(fid))
    monkeypatch.setattr(studio_ws, 'drop_sandbox', lambda sid: WS.dropped_sandboxes.append(sid))
    monkeypatch.setattr(studio_fonts, 'alternative',
                        lambda fam: ('Inter', f"{fam} isn't openly licensed") if fam.lower() == 'helvetica neue'
                        else None)
    monkeypatch.delenv('TG_STUDIO', raising=False)
    return calls


class VisionEngine(FakeEngine):
    supports_vision = True


def router(engine=None, engines=None) -> Router:
    return Router(FakeJev(), None, engine, store=Store(), registry={},
                  engines=engines if engines is not None else ({engine.name: engine} if engine else None))


@pytest.fixture
async def client():
    box = {}

    def factory(http):
        box['r'] = router(VisionEngine())
        return box['r']
    async with TestClient(TestServer(appmod.create_app(factory))) as c:
        c.router = box['r']
        yield c


async def json_of(resp, status=200):
    assert resp.status == status, await resp.text()
    return await resp.json()


def stored(store: Store, spec=SPEC, fmt='pptx', **kw) -> dict:
    meta, spec, data = ca.build(json.loads(json.dumps(spec)), fmt, source=kw.pop('source', 'llm'), **kw)
    store.add_created(meta, spec, data)
    return meta


def designed_file(store: Store, fmt='pptx', sandbox=None, mem=None) -> dict:
    """A file the design stage made, stored like the create agent stores it."""
    fid = 'abcdef012345' if sandbox is None else 'fedcba543210'
    result = painted_result(json.loads(json.dumps(SPEC)), fmt, fid, sandbox, engine=None)
    meta, spec, data = ca.build(json.loads(json.dumps(SPEC)), fmt, source='llm', studio=result, file_id=fid)
    if sandbox:
        meta = {**meta, 'sandbox': sandbox}
        mem.add_created(meta, spec, data)
    else:
        store.add_created(meta, spec, data)
    return meta


# ---------- the create agent: TG_STUDIO, the design stage and its fallback ----------


async def test_flag_off_never_calls_the_design_stage(studio_fakes, monkeypatch):
    monkeypatch.setenv('TG_STUDIO', 'off')
    made = await ca.finish(json.loads(json.dumps(SPEC)), 'pptx', None, source='llm', engine=VisionEngine())
    assert made.ok and studio_fakes['design'] == []
    assert 'design' not in made.file and not any(r['id'].startswith('D') for r in made.file['rules'])
    assert made.phases == [{'phase': 'render', 'calls': 0, 'llm_in': 0, 'llm_out': 0, 'ms': made.phases[0]['ms']}]


@pytest.mark.parametrize('flag', ['1', 'pptx', 'on'])
async def test_finish_designs_and_verifies_the_painted_file(monkeypatch, studio_fakes, flag):
    monkeypatch.setenv('TG_STUDIO', flag)
    engine = VisionEngine()
    made = await ca.finish(json.loads(json.dumps(SPEC)), 'pptx', None, source='llm', tokens=10, engine=engine,
                           mode='deep', http='HTTP', request='a bold dark deck', sandbox=None, deadline=None)
    call, = studio_fakes['design']
    meta = made.file
    # the id was made first and is the file's; the design stage got the run's engine, mode, request and http
    assert call['file_id'] == meta['id'] and call['engine'] is engine and call['mode'] == 'deep'
    assert call['request'] == 'a bold dark deck' and call['http'] == 'HTTP' and call['fmt'] == 'pptx'
    assert call['spec'] == cf.normalize(SPEC, 'pptx')[0]  # the normalized spec
    assert made.data == cf.render(SPEC, 'pptx') or made.data[:2] == b'PK'
    rules = {r['id']: r for r in meta['rules']}
    assert [k for k in rules if k.startswith('D')] == [f'D{i}' for i in range(1, 9)]
    assert not rules['D1']['ok'] and rules['D1']['severity'] == 'warn'
    assert rules['D1']['note'] == '2 issues: text overflows its box on slide 2' and rules['D2']['ok']
    assert rules['V3']['ok'] and rules['A1']['ok'] and rules['V1']['ok']
    assert 'D1 (2 issues: text overflows its box on slide 2)' in made.answer
    assert 'Laid out in the bold-dark preset.' in made.answer
    # CreatedFile.design: Studio's fields on a preset (no design file)
    d = meta['design']
    assert d['studio'] and d['preset'] == 'bold-dark' and d['name'] == 'preset:bold-dark' and d['confidence'] == 1
    assert d['colors'] == {'bg': '101820', 'text': 'F5F5F5', 'accent': 'FF5A5F'} and d['thumbs'] == 3
    assert d['fonts_used'] == {'heading': 'Poppins', 'body': 'Inter'}
    assert meta['theme'] == 'dark' and meta['font_used'] == 'Inter'
    # the art direction's tokens: in the file's tokens, the Made's usage and the render phase
    assert meta['tokens'] == 10 + 150 and (made.llm_in, made.llm_out) == (100, 50) and made.engine == engine.name
    render, = made.phases
    assert render['phase'] == 'render' and (render['calls'], render['llm_in'], render['llm_out']) == (1, 100, 50)


async def test_a_conversion_is_designed_keyless(monkeypatch, studio_fakes):
    monkeypatch.setenv('TG_STUDIO', 'pdf')
    made = await ca.finish(json.loads(json.dumps(SPEC)), 'pdf', None, source='convert', engine=VisionEngine())
    assert made.ok and studio_fakes['design'][0]['engine'] is None and made.file['tokens'] == 0
    assert made.llm_in == made.llm_out == 0 and made.file['design']['studio']


def boom(*a, **kw):
    raise RuntimeError('layout exploded')


@pytest.mark.parametrize('how', ['raises', 'nothing painted', 'fell back', 'bad bytes'])
async def test_any_design_failure_falls_back_to_the_standard_layout(monkeypatch, studio_fakes, how):
    monkeypatch.setenv('TG_STUDIO', 'pptx')
    real = studio_agent.design

    async def failing(spec, fmt, **kw):
        if how == 'raises':
            raise RuntimeError('layout exploded')
        out = await real(spec, fmt, **kw)
        if how == 'nothing painted':
            out.painted_bytes = None
        elif how == 'fell back':
            out.fell_back, out.caveats = True, ['the painter could not load its fonts']
        else:
            out.painted_bytes = b'PK\x03\x04 not a deck'
        return out
    monkeypatch.setattr(studio_agent, 'design', failing)
    made = await ca.finish(json.loads(json.dumps(SPEC)), 'pptx', None, source='llm')
    assert made.ok and made.data[:2] == b'PK' and cf.preview('pptx', made.data)['slides'] == 3
    assert 'design' not in made.file and not any(r['id'].startswith('D') for r in made.file['rules'])
    why = {'raises': 'RuntimeError: layout exploded', 'nothing painted': 'nothing was painted',
           'fell back': 'the painter could not load its fonts', 'bad bytes': 'its file failed rule V1'}[how]
    caveat = next(c for c in made.caveats if c.startswith('The design stage failed ('))
    assert why in caveat and caveat.endswith(', so the file uses the standard layout.')
    assert '—' not in caveat and '–' not in caveat
    assert WS.open(made.file['id']).deleted  # its workspace is not left behind for thumbnails to find


async def test_a_design_stage_that_runs_out_of_time_falls_back(monkeypatch, studio_fakes):
    monkeypatch.setenv('TG_STUDIO', 'pptx')
    monkeypatch.setattr(ca, 'STUDIO_TIME_BUDGET', 0.05)
    monkeypatch.setattr(ca, 'STUDIO_GRACE', 0.0)

    async def slow(spec, fmt, **kw):
        await asyncio.sleep(5)
    monkeypatch.setattr(studio_agent, 'design', slow)
    made = await ca.finish(json.loads(json.dumps(SPEC)), 'pptx', None, source='llm')
    assert made.ok and any('ran out of time' in c for c in made.caveats)


async def test_studio_off_for_the_format_renders_as_today(monkeypatch, studio_fakes):
    monkeypatch.setenv('TG_STUDIO', 'pdf')
    made = await ca.finish(json.loads(json.dumps(SPEC)), 'docx', None, source='llm')
    assert made.ok and studio_fakes['design'] == [] and 'design' not in made.file


async def test_make_hands_the_run_to_the_design_stage(monkeypatch, studio_fakes):
    """make() plumbs the engine, mode, http, request, sandbox and deadline to finish() (the long writer's too)."""
    monkeypatch.setenv('TG_STUDIO', 'pptx')
    engine = VisionEngine()
    job = ca.Job('put that in slides', deps=[('Who was Ada Lovelace', 'Ada Lovelace was a mathematician.\n\n- Born 1815')],
                 http='HTTP', sandbox=SID, deadline=10 ** 9)
    made = await ca.make(job, engine, FakeJev(), 'balanced')
    assert made.ok, made.answer
    call, = studio_fakes['design']
    # a file made from an earlier answer costs 0 tokens, so its design is keyless too (engine None)
    assert (call['engine'], call['mode'], call['http'], call['sandbox'], call['deadline']) == \
        (None, 'balanced', 'HTTP', SID, 10 ** 9)
    assert made.llm_in == made.llm_out == 0 and made.file['tokens'] == 0
    assert call['request'] == 'put that in slides' and ca.STUDIO_RUN.get() is None


# ---------- the ruleset and render_designed ----------


def test_d_rules_are_warn_rules_in_the_design_group():
    by = {r['id']: r for r in cf.RULES}
    assert [r['id'] for r in cf.RULES][-8:] == [f'D{i}' for i in range(1, 9)]
    assert all(by[f'D{i}']['severity'] == 'warn' and by[f'D{i}']['group'] == 'Design' for i in range(1, 9))


def test_design_checks_from_a_report():
    assert rules_mod.design_checks(None) == []
    got = rules_mod.design_checks({'checks': [{'id': 'D1', 'ok': False, 'failures': 1, 'note': 'too long'},
                                              {'id': 'D3', 'ok': True, 'failures': 0, 'note': ''}]})
    assert [(r.id, r.ok, r.note) for r in got][:3] == [('D1', False, '1 issue: too long'), ('D2', True, 'not checked'),
                                                      ('D3', True, 'passed')]
    assert len(got) == 8 and all(r.severity == 'warn' for r in got)
    data = cf.render(SPEC, 'pdf')
    assert not any(r.id.startswith('D') for r in cf.verify(SPEC, 'pdf', data))
    res = cf.verify(SPEC, 'pdf', data, design_report=fake_report('pdf', 3))
    assert [r.id for r in res if r.id.startswith('D')] == [f'D{i}' for i in range(1, 9)]


def test_render_designed_wraps_the_painter(monkeypatch):
    plan = fake_plan('abcdef012345', 'pptx', 3)
    data = cf.render(SPEC, 'pptx')
    monkeypatch.setattr(studio_agent, 'paint', lambda p, s, w: data)
    assert render_designed(plan, SPEC, 'pptx', None) == data
    with pytest.raises(cf.SpecError) as e:
        render_designed(plan, SPEC, 'pdf', None)
    assert e.value.rule_id == 'V1'
    monkeypatch.setattr(studio_agent, 'paint', boom)
    with pytest.raises(cf.SpecError) as e:
        render_designed(plan, SPEC, 'pptx', None)
    assert e.value.rule_id == 'V1' and 'layout exploded' in e.value.message
    monkeypatch.setattr(studio_agent, 'paint', lambda p, s, w: b'x' * (cf.render.__globals__['MAX_BYTES'] + 1))
    with pytest.raises(cf.SpecError) as e:
        render_designed(plan, SPEC, 'pptx', None)
    assert e.value.rule_id == 'L4'


# ---------- presets and fonts ----------


async def test_presets_and_their_thumbnails(client, monkeypatch):
    info = PresetInfo('bold-dark', 'Bold dark', 'pitches', True, {'body': 'Inter'},
                      {'bg': '101820', 'text': 'FFFFFF', 'accent': 'FF5A5F', 'accent2': '00A6A6'},
                      '/api/design/presets/bold-dark/thumb')
    tmpl = TemplateInfo('lab-report', 'Lab report', 'For labs', 'pdf', 'academic', 'a4', ['cover'], ['cite sources'])
    monkeypatch.setattr(studio_presets, 'list_presets', lambda: [info])
    monkeypatch.setattr(studio_presets, 'list_templates', lambda: [tmpl])
    got = await json_of(await client.get('/api/design/presets'))
    assert got == {'presets': [info.to_dict()], 'templates': [tmpl.to_dict()]}
    monkeypatch.setattr(studio_thumbs, 'preset_thumb', lambda pid, **kw: png())
    resp = await client.get('/api/design/presets/bold-dark/thumb')
    assert resp.status == 200 and resp.headers['Content-Type'] == 'image/png' and (await resp.read())[:4] == b'\x89PNG'
    assert (await client.get('/api/design/presets/neon-glow/thumb')).status == 404
    monkeypatch.setattr(studio_presets, 'list_presets', boom)
    assert (await client.get('/api/design/presets')).status == 503


async def test_font_search_is_open_licences_only(client, monkeypatch):
    seen = []

    async def search(q, http, limit=20):
        seen.append((q, limit, http))
        return [FontInfo('Poppins', 'sans', 'OFL-1.1', 'fontsource', ['regular'], False, '/api/fonts/preview?family=Poppins'),
                FontInfo('Brand Sans', 'sans', 'proprietary', 'system', ['regular'], True, '')], False
    monkeypatch.setattr(studio_fonts, 'search', search)
    got = await json_of(await client.get('/api/fonts/search?q=pop&limit=5'))
    assert [f['family'] for f in got['fonts']] == ['Poppins'] and got['offline'] is False
    assert seen[-1][:2] == ('pop', 5) and seen[-1][2] is client.app[appmod.HTTP]
    await json_of(await client.get('/api/fonts/search'))
    assert seen[-1][:2] == ('', 20)
    for bad in ('q=' + 'x' * 65, 'limit=0', 'limit=51', 'limit=many'):
        assert (await client.get(f'/api/fonts/search?{bad}')).status == 400, bad


async def test_font_preview(client, monkeypatch):
    monkeypatch.setattr(studio_fonts, 'preview_png', lambda fam, text='The quick brown fox', px=32:
                        png() if fam == 'Inter' else None)
    resp = await client.get('/api/fonts/preview?family=Inter&text=Hello')
    assert resp.status == 200 and resp.headers['Content-Type'] == 'image/png'
    assert (await client.get('/api/fonts/preview?family=Helvetica%20Neue')).status == 404
    assert (await client.get('/api/fonts/preview?family=Inter&text=' + 'x' * 61)).status == 400
    assert (await client.get('/api/fonts/preview')).status == 400


# ---------- thumbnails and the design report ----------


async def test_thumbs_and_design_of_a_designed_file(client):
    meta = designed_file(client.router.store)
    got = await json_of(await client.get(f'/api/created/{meta["id"]}/thumbs'))
    assert [t['page'] for t in got['thumbs']] == [1, 2, 3] and 'reason' not in got
    t = got['thumbs'][0]
    assert t == {'page': 1, 'url': f'/api/created/{meta["id"]}/thumbs/1.png', 'w': 48, 'h': 27, 'layout': 'cover-hero'}
    resp = await client.get(t['url'])
    assert resp.status == 200 and resp.headers['Content-Type'] == 'image/png' and (await resp.read()) == png()
    for n in (0, 4):
        assert (await client.get(f'/api/created/{meta["id"]}/thumbs/{n}.png')).status == 404
    design = await json_of(await client.get(f'/api/created/{meta["id"]}/design'))
    assert design['report']['score'] == 80 and len(design['report']['checks']) == 8
    plan = design['plan']
    assert plan['preset'] == 'bold-dark' and plan['format'] == 'pptx' and plan['assets'] == 0
    assert plan['pages'][0] == {'index': 0, 'layout': 'cover-hero', 'variant': 'default', 'freeform': False,
                                'section': None}
    assert [f['family'] for f in plan['fonts']] == ['Inter', 'Poppins'] and plan['stop'] == 'pass'


async def test_thumbs_and_design_of_a_file_made_without_studio(client):
    meta = stored(client.router.store)
    got = await json_of(await client.get(f'/api/created/{meta["id"]}/thumbs'))
    assert got['thumbs'] == [] and 'without the design stage' in got['reason']
    assert await json_of(await client.get(f'/api/created/{meta["id"]}/design')) == {'report': None, 'plan': None}
    assert (await client.get(f'/api/created/{meta["id"]}/thumbs/1.png')).status == 404
    for path in ('thumbs', 'design', 'thumbs/1.png'):
        assert (await client.get(f'/api/created/0123456789ab/{path}')).status == 404


# ---------- restyle ----------


async def test_restyle_is_zero_tokens_from_the_stored_design(client, studio_fakes):
    store = client.router.store
    src = designed_file(store)
    body = {'preset': 'minimal', 'fonts': {'body': 'Lexend'}, 'dark': False, 'layouts': {'1': 'big-number'},
            'print': True}
    new = await json_of(await client.post(f'/api/created/{src["id"]}/restyle', json=body))
    call, = studio_fakes['restyle']
    opts = call['options']
    assert (opts.preset, opts.fonts, opts.dark, opts.layouts, opts.print_version) == \
        ('minimal', {'body': 'Lexend'}, False, {1: 'big-number'}, True)
    assert call['ws'] is WS.open(src['id']) and call['file_id'] == new['id'] and studio_fakes['design'] == []
    assert new['source'] == 'convert' and new['from_id'] == src['id'] and new['tokens'] == 0
    assert new['design']['studio'] and new['design']['preset'] == 'minimal' and new['format'] == 'pptx'
    assert store.get_created(new['id']) == new and store.created_spec(new['id']) == store.created_spec(src['id'])
    assert all(r['ok'] for r in new['rules'] if r['id'].startswith('D'))


async def test_restyling_a_file_made_without_studio_designs_it_first(client, studio_fakes):
    src = stored(client.router.store, fmt='pdf')
    new = await json_of(await client.post(f'/api/created/{src["id"]}/restyle', json={'preset': 'editorial'}))
    first, = studio_fakes['design']
    assert first['engine'] is None and first['preset'] == 'editorial' and first['fmt'] == 'pdf'
    call, = studio_fakes['restyle']
    assert call['ws'].file_id == first['file_id'] and WS.open(first['file_id']).deleted  # the scratch design is gone
    assert new['format'] == 'pdf' and new['tokens'] == 0 and new['design']['preset'] == 'editorial'


async def test_restyle_errors(client, monkeypatch, studio_fakes):
    store = client.router.store
    src = designed_file(store)
    url = f'/api/created/{src["id"]}/restyle'
    assert (await client.post('/api/created/0123456789ab/restyle', json={'preset': 'minimal'})).status == 404
    for body in ({}, {'fonts': {}}, {'preset': 'neon'}, {'template': 'essay'}, {'dark': 'yes'}, {'colour': 'red'},
                 {'layouts': {'1': 'cover'}}, {'layouts': {'9': 'quote'}}, {'layouts': {'x': 'quote'}},
                 {'fonts': {'body': 'Helvetica Neue'}}, {'fonts': {'footer': 'Inter'}}, {'print': 'yes'}):
        resp = await client.post(url, json=body)
        assert resp.status == 400, (body, await resp.text())
    got = await (await client.post(url, json={'fonts': {'body': 'Helvetica Neue'}})).json()
    assert got['error'] == "Helvetica Neue isn't openly licensed, so it can't be used; Inter is the closest open match."
    assert studio_fakes['restyle'] == []
    md = stored(store, fmt='md')
    assert (await client.post(f'/api/created/{md["id"]}/restyle', json={'preset': 'minimal'})).status == 400
    meta, spec, data = ca.build(json.loads(json.dumps(SPEC)), 'pdf', source='llm')
    store.add_created(meta, None, data)
    assert (await client.post(f'/api/created/{meta["id"]}/restyle', json={'preset': 'minimal'})).status == 409
    monkeypatch.setattr(studio_agent, 'restyle', boom)
    got = await json_of(await client.post(url, json={'preset': 'pastel'}), 422)
    assert got['rule'] == 'V1' and 'layout exploded' in got['error'] and len(store.list_created()) == 3


# ---------- polish ----------


async def test_polish_goes_through_the_cost_guard(client, monkeypatch, studio_fakes):
    store = client.router.store
    src = designed_file(store)
    url = f'/api/created/{src["id"]}/polish'
    monkeypatch.setenv('TG_COST_CONFIRM', '1')
    monkeypatch.setattr(estimate_mod, 'COST_CONFIRM_TOKENS', {'default': 10})
    got = await json_of(await client.post(url, json={}), 409)
    assert got['needs_confirmation'] and got['estimate']['calls'] == 1 and studio_fakes['polish'] == []
    assert got['estimate']['breakdown'][0]['phase'] == 'critic' and got['estimate']['engine'] == 'claude-code'
    new = await json_of(await client.post(url, json={'confirm_cost': True}))
    call, = studio_fakes['polish']
    assert call['engine'] is client.router.engine and call['ws'] is WS.open(src['id'])
    assert new['source'] == 'convert' and new['from_id'] == src['id'] and new['tokens'] == 3400
    assert new['id'] == call['file_id'] and store.get_created(new['id']) == new


async def test_polish_errors(client, monkeypatch, studio_fakes):
    r = client.router
    src = designed_file(r.store)
    url = f'/api/created/{src["id"]}/polish'
    assert (await client.post('/api/created/0123456789ab/polish', json={})).status == 404
    plain = stored(r.store)
    assert (await client.post(f'/api/created/{plain["id"]}/polish', json={})).status == 409  # no design plan
    assert (await client.post(url, json={'engine': 'none'})).status == 400
    assert (await client.post(url, json={'engine': 'gpt-9'})).status == 400
    assert (await client.post(url, json={'confirm_cost': 'yes'})).status == 400
    blind = FakeEngine(name='agy', label='Antigravity')
    r.engines['agy'] = blind
    got = await json_of(await client.post(url, json={'engine': 'agy'}), 400)
    assert "Antigravity can't" in got['error']
    down = VisionEngine(name='anthropic', label='Anthropic API', ok=False)
    r.engines['anthropic'] = down
    assert (await client.post(url, json={'engine': 'anthropic'})).status == 503

    async def no_vision(*a, **kw):
        raise ValueError('no-vision')

    async def no_plan(*a, **kw):
        raise LookupError('no-plan')
    monkeypatch.setattr(studio_agent, 'polish', no_vision)
    assert (await client.post(url, json={})).status == 400
    monkeypatch.setattr(studio_agent, 'polish', no_plan)
    assert (await client.post(url, json={})).status == 409
    r.engine = None
    assert (await client.post(url, json={})).status == 400
    assert studio_fakes['polish'] == [] and len(r.store.list_created()) == 2


def test_polish_on_auto_uses_an_engine_that_can_see():
    blind, seeing = FakeEngine(name='agy', label='Antigravity'), VisionEngine()

    class Auto:
        name, label = 'auto', 'Auto'

        def chain(self):
            return [blind, seeing]
    from types import SimpleNamespace
    r = SimpleNamespace(engine=Auto(), engines={'agy': blind, 'claude-code': seeing})
    assert appmod.polish_engine(r, None) is seeing
    r.engine = SimpleNamespace(name='auto', label='Auto', chain=lambda: [blind])
    with pytest.raises(appmod.Bad) as e:
        appmod.polish_engine(r, None)
    assert e.value.status == 400
    assert studio_critic.can_see(seeing) and not studio_critic.can_see(blind)


# ---------- sandbox mirrors ----------


async def test_sandbox_files_have_every_route_and_stay_in_memory(client, studio_fakes):
    r = client.router
    mem = r.sandboxes.get(SID, create=True)
    src = designed_file(r.store, sandbox=SID, mem=mem)
    base = f'/api/sandbox/{SID}/created/{src["id"]}'
    got = await json_of(await client.get(f'{base}/thumbs'))
    assert got['thumbs'][0]['url'] == f'{base}/thumbs/1.png'
    assert (await client.get(got['thumbs'][0]['url'])).status == 200
    assert (await json_of(await client.get(f'{base}/design')))['plan']['preset'] == 'bold-dark'
    new = await json_of(await client.post(f'{base}/restyle', json={'preset': 'pastel'}))
    assert new['sandbox'] == SID and new['id'] in mem.created and r.store.list_created() == []
    assert studio_fakes['restyle'][0]['ws'] is WS.open(src['id'], SID)
    polished = await json_of(await client.post(f'{base}/polish', json={}))
    assert polished['sandbox'] == SID and polished['id'] in mem.created and r.store.list_created() == []
    # the stored routes don't see sandbox files, and another sandbox doesn't either
    assert (await client.get(f'/api/created/{src["id"]}/thumbs')).status == 404
    assert (await client.get(f'/api/sandbox/sandbox-other-1/created/{src["id"]}/design')).status == 404
    assert (await client.get(f'/api/sandbox/x/created/{src["id"]}/design')).status == 400


# ---------- workspaces go with their file or sandbox ----------


async def test_deleting_a_file_or_clearing_a_sandbox_drops_its_workspaces(client):
    r = client.router
    meta = designed_file(r.store)
    assert (await json_of(await client.delete(f'/api/created/{meta["id"]}'))) == {'ok': True}
    assert WS.dropped == [meta['id']]
    r.sandboxes.get(SID, create=True)
    assert (await client.delete(f'/api/sandbox/{SID}')).status == 200
    assert WS.dropped_sandboxes == [SID]
    r.sandboxes.get('sandbox-idle-1', create=True, now=0)
    assert r.sweep_sandboxes(now=10 ** 9) == ['sandbox-idle-1'] and WS.dropped_sandboxes[-1] == 'sandbox-idle-1'


def test_a_workspace_that_cannot_be_dropped_never_fails_a_delete(monkeypatch):
    monkeypatch.setattr(studio_ws, 'drop', boom)
    store = Store()
    meta = stored(store, fmt='md')
    assert store.delete_created(meta['id']) and store.get_created(meta['id']) is None


async def test_convert_endpoint_designs_keyless_seeded_from_the_source(client, monkeypatch, studio_fakes):
    monkeypatch.setenv('TG_STUDIO', 'pdf')
    src = designed_file(client.router.store)
    new = await json_of(await client.post(f'/api/created/{src["id"]}/convert', json={'format': 'pdf'}), 201)
    call, = studio_fakes['design']
    assert call['engine'] is None and call['file_id'] == new['id'] and new['tokens'] == 0 and new['design']['studio']
    assert WS.open(new['id']).plan is not None


# ---------- engines say whether they can read images (the Polish button) ----------


async def test_hello_and_config_say_which_engines_can_see():
    seeing, blind = VisionEngine(), FakeEngine(name='agy', label='Antigravity')
    box = {}

    def factory(http):
        box['r'] = router(seeing, engines={'claude-code': seeing, 'agy': blind})
        return box['r']
    async with TestClient(TestServer(appmod.create_app(factory))) as c:
        cfg = await json_of(await c.get('/api/config'))
        assert cfg['engine']['name'] == 'claude-code' and cfg['engine']['vision'] is True
        assert {e['name']: e['vision'] for e in cfg['engines']} == {'claude-code': True, 'agy': False}
        hello = box['r'].hello()
        assert hello['engine']['vision'] is True
        assert {e['name']: e['vision'] for e in hello['engines']} == {'claude-code': True, 'agy': False}

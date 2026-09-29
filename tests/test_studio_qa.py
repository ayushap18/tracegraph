"""Studio builder Q (docs/PLAN-designer.md 3.7, 3.8, 9.7 and 9.8): thumbnails, QA checks D1-D8 and the design report,
the design critic, the freeform hatch, the agent loop, and the engines' vision opt-in.

Other builders' stages (art direction, layout, painters, workspace) are replaced by small fakes here, so these tests pin
Q's own behaviour: the loop's order, stop rules, budgets, rollback and fallbacks. No model is called: engines are fakes.
"""
import copy
import io
import json
import sys
import textwrap
import time

import pytest
from PIL import Image

from jevrouter import config
from jevrouter.engines.base import Reply
from jevrouter.studio import agent, critic, freeform, plan as P, qa, thumbs, tokens

FID = '0123456789ab'


# ---------- fixtures: a design system, a workspace, plans ----------


def make_ds(**kw) -> tokens.DesignSystem:
    colors = {r: '000000' for r in tokens.COLOR_ROLES}
    colors.update(bg='FFFFFF', surface='F3F4F6', text='1F2328', heading='111827', muted='6B7280', accent='1D4ED8',
                  accent2='B45309', border='D1D5DB', overlay='000000', on_accent='FFFFFF', header_bg='111827',
                  header_text='FFFFFF', stripe='F9FAFB', code_bg='F3F4F6')
    return tokens.DesignSystem(id='minimal', name='Minimal', dark=False, colors=colors,
                               chart_palette=['2563EB', 'F59E0B', '059669', 'DC2626', '7C3AED', '0891B2'], **kw)


class FakeWS:
    """The Workspace interface in memory (the real one is builder F's); plans round-trip through JSON."""

    def __init__(self, file_id=FID):
        self.file_id, self.files, self.logged = file_id, {}, []
        self.plan = self.prev = self.report = None

    def exists(self):
        return True

    def save_plan(self, plan):
        self.prev, self.plan = self.plan, json.dumps(plan.to_dict())

    def load_plan(self, previous=False):
        raw = self.prev if previous else self.plan
        return P.DesignPlan.from_dict(json.loads(raw)) if raw else None

    def save_report(self, report):
        self.report = json.loads(json.dumps(report))

    def load_report(self):
        return self.report

    def put(self, kind, name, data):
        ref = f'ws:{kind}/{name}'
        self.files[ref] = bytes(data)
        return ref

    def get(self, ref):
        return self.files.get(ref)

    def save_thumb(self, page, png):
        return self.put('thumbs', f'{page + 1:03d}.png', png)

    def thumbs(self):
        return []

    def log(self, event):
        self.logged.append(dict(event))

    def events(self):
        return list(self.logged)

    def copy_to(self, file_id):
        new = FakeWS(file_id)
        new.files, new.plan = dict(self.files), self.plan
        return new


def png_of(colour, w=64, h=36) -> bytes:
    buf = io.BytesIO()
    Image.new('RGB', (w, h), colour).save(buf, 'PNG')
    return buf.getvalue()


def pixel(png, x, y):
    return Image.open(io.BytesIO(png)).convert('RGB').getpixel((x, y))


def text_box(bid, slot, x, y, w, h, step, ds, lines, *, colour='text', z=1, **kw):
    return P.Box(bid, 'text', x, y, w, h, z=z, slot=slot, content=f'spec:0/{slot}', font='Inter', step=step,
                 size=ds.size(step, 'pptx'), lines=list(lines), line_height=1.25,
                 style=P.BoxStyle(text_color=colour), **kw)


def bullets_page(i, ds, *, layout='title-bullets', body_lines=('Happens in chloroplasts', 'Needs light',
                                                                 'Releases oxygen'), title=None):
    return P.PagePlan(i, layout, 960, 540, section=i, direction=i, boxes=[
        text_box(f'p{i}.title', 'title', 48, 48, 864, 70, 'h2', ds, [title or f'Title {i}'], colour='heading',
                 bold=True),
        text_box(f'p{i}.body', 'body', 48, 134, 864, 358, 'body', ds, body_lines)])


def cover_page(ds):
    return P.PagePlan(0, 'cover-type', 960, 540, direction=0, boxes=[
        P.Box('p0.art', 'shape', 560, 0, 400, 540, z=0, slot='art', bleed=True, style=P.BoxStyle(fill='accent')),
        text_box('p0.title', 'title', 48, 150, 480, 200, 'display', ds, ['Photosynthesis'], colour='heading',
                 bold=True)])


def deck(ds, n=4, **kw) -> P.DesignPlan:
    layouts = ['title-bullets', 'two-column', 'title-bullets', 'comparison', 'title-bullets', 'quote']
    pages = [cover_page(ds)] + [bullets_page(i, ds, layout=layouts[(i - 1) % len(layouts)]) for i in range(1, n)]
    direction = {'preset': 'minimal', 'mood': [], 'dark': False, 'source': 'keyless', 'notes': [],
                 'pages': [{'layout': p.layout, 'image': None, 'emphasis': None, 'focus': None, 'variant': 'default',
                            'freeform': False} for p in pages]}
    return P.DesignPlan(FID, 'pptx', 'minimal', ds.to_dict(), direction, pages=pages, **kw)


def ids(results, ok=False):
    return [r.id for r in results if r.ok == ok]


# ---------- thumbnails ----------


def test_render_page_draws_the_plan_geometry():
    ds = make_ds()
    plan = deck(ds, 2)
    png = thumbs.render_page(plan, 0, FakeWS())
    im = Image.open(io.BytesIO(png))
    assert im.size == (480, 270)
    assert pixel(png, 400, 135) == (0x1D, 0x4E, 0xD8)       # the accent art panel at x >= 560 pt
    assert pixel(png, 5, 5) == (255, 255, 255)              # the page background
    title = Image.open(io.BytesIO(png)).convert('L').crop((24, 75, 240, 175))
    assert title.getextrema()[0] < 80                       # the title's glyphs are drawn, dark on white
    small = thumbs.render_page(plan, 1, FakeWS(), width_px=240)
    assert Image.open(io.BytesIO(small)).size == (240, 135)


def test_render_images_crops_and_placeholders():
    ds = make_ds()
    ws = FakeWS()
    ref = ws.put('images', 'red.png', png_of((200, 20, 20), 400, 100))
    page = P.PagePlan(0, 'image-left-text', 960, 540, boxes=[
        P.Box('p0.image', 'image', 0, 0, 480, 540, slot='image', content=ref, fit='cover', bleed=True),
        P.Box('p0.chart', 'chart', 500, 100, 400, 300, slot='chart', content='none')])
    plan = P.DesignPlan(FID, 'pptx', 'minimal', ds.to_dict(), {}, pages=[page])
    png = thumbs.render_page(plan, 0, ws)
    assert pixel(png, 100, 135) == (200, 20, 20)            # cover-fitted photo fills its half
    assert pixel(png, 470, 20) == (255, 255, 255)
    missing = copy.deepcopy(plan)
    missing.pages[0].boxes[0].content = 'ws:images/gone.png'
    assert pixel(thumbs.render_page(missing, 0, ws), 100, 135) == (0x6B, 0x72, 0x80)   # muted placeholder


def test_render_all_stores_every_page():
    ds = make_ds()
    ws = FakeWS()
    refs = thumbs.render_all(deck(ds, 3), ws)
    assert refs == ['ws:thumbs/001.png', 'ws:thumbs/002.png', 'ws:thumbs/003.png']
    assert all(ws.get(r).startswith(b'\x89PNG') for r in refs)


def test_contact_sheet_takes_six_and_labels_them():
    pngs = [png_of(c, 480, 270) for c in ('red', 'green', 'blue', 'yellow', 'purple', 'orange', 'black', 'white')]
    sheet = Image.open(io.BytesIO(thumbs.contact_sheet(pngs)))
    assert sheet.width == 1200 and sheet.height < 900
    two = Image.open(io.BytesIO(thumbs.contact_sheet(pngs[:2], first_page=7)))
    assert two.height < sheet.height                        # one row only
    # the page number badge is drawn in the top-left corner of each cell
    assert two.convert('RGB').getpixel((14, 14)) == (17, 24, 39)


def test_sample_reads_under_a_box():
    im = Image.new('RGB', (480, 270), 'white')
    im.paste((0, 0, 0), (0, 0, 240, 270))
    buf = io.BytesIO()
    im.save(buf, 'PNG')
    assert thumbs.sample(buf.getvalue(), (0, 0, 400, 540), (960, 540)) == ['000000']
    both = thumbs.sample(buf.getvalue(), (0, 0, 960, 540), (960, 540))
    assert set(both) == {'000000', 'FFFFFF'}
    assert thumbs.sample(buf.getvalue(), (2000, 0, 10, 10), (960, 540)) == []


def test_preset_thumb_is_cached(tmp_path, monkeypatch):
    from jevrouter.studio import workspace
    monkeypatch.setattr(workspace, 'DESIGN_DIR', tmp_path)
    a = thumbs.preset_thumb('bold-dark')
    assert Image.open(io.BytesIO(a)).size == (320, 180)
    assert list((tmp_path / 'presets').glob('bold-dark-320-*.png'))
    assert thumbs.preset_thumb('bold-dark') == a
    with pytest.raises(KeyError):
        thumbs.preset_thumb('nope')


# ---------- QA checks ----------


def test_a_clean_deck_passes_every_check():
    ds = make_ds()
    results = qa.check(deck(ds, 5), FakeWS())
    assert ids(results, ok=True) == list(P.CHECK_IDS) and not ids(results)
    assert qa.score(results) == 100


def test_d1_overflow_height_and_width():
    ds = make_ds()
    plan = deck(ds, 2)
    plan.pages[1].boxes[1].lines = ['Point'] * 20                    # 20 x 22.5 pt > 358 pt
    plan.pages[1].boxes[0].lines = ['W' * 80]                         # far wider than 864 pt at 35 pt
    got = [r for r in qa.run_check('D1', plan) if not r.ok]
    assert {(r.page, r.box) for r in got} == {(1, 'p1.body'), (1, 'p1.title')}
    body = next(r for r in got if r.box == 'p1.body')
    assert body.value == 450 and body.threshold == 358


def test_d2_overlap_unless_overlay_ok():
    ds = make_ds()
    plan = deck(ds, 2)
    plan.pages[1].boxes[1].y = 100                                    # body slides under the title
    got = [r for r in qa.run_check('D2', plan) if not r.ok]
    assert [(r.page, r.box) for r in got] == [(1, 'p1.body')]
    plan.pages[1].boxes[1].overlay_ok = True
    assert ids(qa.run_check('D2', plan), ok=True) == ['D2']


def test_d3_size_and_contrast_including_over_photos():
    ds = make_ds()
    ws = FakeWS()
    plan = deck(ds, 2)
    plan.pages[1].boxes[1].size = 12                                   # below the 18 pt slide minimum
    plan.pages[1].boxes[0].style.text_color = 'border'                 # light grey on white
    got = {r.box: r for r in qa.run_check('D3', plan, ws) if not r.ok}
    assert got['p1.body'].threshold == 18 and got['p1.title'].value < 3
    # white text on a bright photo fails; the same text on a dark overlay passes
    photo = ws.put('images', 'sky.png', png_of((235, 240, 250), 960, 540))
    hero = P.PagePlan(0, 'cover-hero', 960, 540, boxes=[
        P.Box('p0.image', 'image', 0, 0, 960, 540, z=0, slot='image', content=photo, fit='cover', bleed=True),
        text_box('p0.title', 'title', 48, 300, 800, 120, 'display', ds, ['Sky'], colour='on_accent', z=2,
                 overlay_ok=True)])
    cover = P.DesignPlan(FID, 'pptx', 'minimal', ds.to_dict(), {}, pages=[hero])
    bad = [r for r in qa.run_check('D3', cover, ws) if not r.ok]
    assert bad and 'picture' in bad[0].note
    hero.boxes.insert(1, P.Box('p0.overlay', 'shape', 0, 0, 960, 540, z=1, slot='overlay', bleed=True,
                               overlay_ok=True, style=P.BoxStyle(fill='overlay', opacity=0.7)))
    assert ids(qa.run_check('D3', cover, ws), ok=True) == ['D3']
    # with no readable picture and no workspace, the thumbnail is sampled (glyph pixels ignored)
    thumb = thumbs.render_page(cover, 0, ws)
    hero.boxes[0].content = 'ws:images/unknown.png'
    assert ids(qa.run_check('D3', cover, None, thumbs=[thumb]), ok=True) == ['D3']


def test_d4_words_and_white_space():
    ds = make_ds()
    plan = deck(ds, 2)
    plan.pages[1].boxes[1].content = 'spec:1/1/items[0:12]'
    plan.pages[1].boxes[1].lines = ['one two three four'] * 11        # 44 words with bullets > 40
    got = [r for r in qa.run_check('D4', plan) if not r.ok]
    assert got and got[0].value == 44 + 2 and got[0].threshold == 40
    sparse = deck(ds, 2)
    sparse.pages[1].boxes[1].h = 60                                     # most of the slide left empty
    assert [r.page for r in qa.run_check('D4', sparse) if not r.ok] == [1]
    sparse.pages[1].layout = 'quote'                                    # an airy layout may be mostly empty
    assert ids(qa.run_check('D4', sparse), ok=True) == ['D4']
    sparse.pages[1].boxes.append(P.Box('p1.rule', 'shape', 48, 130, 200, 1, style=P.BoxStyle(fill='accent')))
    assert qa.run_check('D4', sparse)                                   # a hairline box doesn't break the mask


def test_d5_balance_skips_asymmetric_layouts():
    ds = make_ds()
    plan = deck(ds, 2)
    page = plan.pages[1]
    page.boxes = [P.Box('p1.figure', 'diagram', 48, 48, 200, 200, slot='figure', content='none')]
    page.layout = 'full-width-diagram'
    got = [r for r in qa.run_check('D5', plan) if not r.ok]
    assert [r.page for r in got] == [1]
    page.layout = 'chart-focus'                                         # asymmetric by design
    assert ids(qa.run_check('D5', plan), ok=True) == ['D5']


def test_d6_scale_grid_and_image_treatment():
    ds = make_ds()
    plan = deck(ds, 3)
    plan.pages[1].boxes[1].size = 21                                     # between body (18) and lead (22.5)
    plan.pages[2].boxes[1].x = 60                                        # 12 pt off the first column
    got = {(r.page, r.box) for r in qa.run_check('D6', plan) if not r.ok}
    assert got == {(1, 'p1.body'), (2, 'p2.body')}
    for i, radius in ((1, 0), (2, 12)):
        plan.pages[i].boxes.append(P.Box(f'p{i}.img', 'image', 600, 300, 200, 100, content='asset:' + 'a' * 64,
                                         style=P.BoxStyle(radius=radius)))
    assert any('treatments' in r.note for r in qa.run_check('D6', plan) if not r.ok)


def test_d7_variety():
    ds = make_ds()
    plan = deck(ds, 9)
    for p in plan.pages[1:]:
        p.layout = 'title-bullets'
    got = [r for r in qa.run_check('D7', plan) if not r.ok]
    assert [r.page for r in got] == [4, None]                          # the 4th in a row, and too few layouts
    pdf = copy.deepcopy(plan)
    pdf.format = 'pdf'
    assert ids(qa.run_check('D7', pdf), ok=True) == ['D7']


def test_d8_upscale_and_focal_point():
    ds = make_ds()
    ws = FakeWS()
    small = ws.put('images', 'small.png', png_of('green', 100, 56))
    page = P.PagePlan(0, 'full-bleed-image-caption', 960, 540, boxes=[
        P.Box('p0.image', 'image', 0, 0, 960, 540, slot='image', content=small, fit='cover', bleed=True)])
    plan = P.DesignPlan(FID, 'pptx', 'minimal', ds.to_dict(), {}, pages=[page])
    got = [r for r in qa.run_check('D8', plan, ws) if not r.ok]
    assert got and got[0].value > 1.5
    ws.files[small] = png_of('green', 1600, 900)
    assert ids(qa.run_check('D8', plan, ws), ok=True) == ['D8']
    page.boxes[0].crop, page.boxes[0].focal = (0.0, 0.0, 0.5, 1.0), (0.8, 0.5)
    assert any('subject' in r.note for r in qa.run_check('D8', plan, ws) if not r.ok)


def test_report_matches_the_schema_and_counts_fixed_failures():
    ds = make_ds()
    plan = deck(ds, 3, fonts=[P.FontUse('Inter', 'body', 'cache', 'OFL-1.1', False, 'Calibri')])
    plan.rounds, plan.stop = 2, 'rounds'
    results = qa.check(plan) + [P.QaResult('D1', False, 'old overflow', 1, 'p1.body', 400, 358, fixed=True),
                                P.QaResult('D7', False, 'too few layouts')]
    rep = qa.report(plan, results, fallbacks=[{'page': 1, 'from': 'freeform', 'to': 'title-bullets',
                                               'why': 'failed QA twice'}], notes=['hello'], thumbs=3)
    assert P.validate(rep, P.report_schema()) == []
    assert [c['id'] for c in rep['checks']] == list(P.CHECK_IDS)
    assert rep['score'] == 90 and rep['checks'][0]['ok'] and not rep['checks'][6]['ok']
    assert {r['id'] for r in rep['results']} == {'D1', 'D7'} and rep['layouts']['title-bullets'] == 1
    assert rep['critic']['ran'] is False and rep['thumbs'] == 3 and rep['notes'][-1] == 'hello'


# ---------- the critic ----------


class VisionEngine:
    """A fake engine that can see: records every call, answers critic calls from `edits` and freeform calls from
    `programs` (each popped in turn; an Exception is raised)."""
    name, label, supports_vision = 'fake-vision', 'Fake vision', True

    def __init__(self, edits=(), programs=(), direction=None):
        self.edits, self.programs, self.calls = list(edits), list(programs), []

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False,
                     schema=None, exec=False, images=None):
        self.calls.append({'system': system, 'prompt': prompt, 'max_tokens': max_tokens, 'schema': schema,
                           'images': images})
        props = (schema or {}).get('properties', {})
        queue = self.edits if 'edits' in props else self.programs
        item = queue.pop(0) if queue else {'edits': []} if 'edits' in props else {'shapes': []}
        if isinstance(item, Exception):
            raise item
        return Reply(json.dumps(item), 900, 120)


class BlindEngine(VisionEngine):
    name, label, supports_vision = 'blind', 'Blind engine', False


def test_validate_edits_keeps_only_valid_ones(monkeypatch):
    from jevrouter.studio import icons
    monkeypatch.setattr(icons, 'names', lambda: ['atom', 'leaf'])
    ds = make_ds()
    plan = deck(ds, 4)
    raw = {'edits': [
        {'page': 2, 'action': 'change_layout', 'arg': 'two-column'},
        {'page': 2, 'action': 'change_layout', 'arg': 'cover-page'},        # not a layout
        {'page': 9, 'action': 'enlarge_title', 'arg': 1},                   # no such page
        {'page': 3, 'action': 'enlarge_title', 'arg': 3},                   # 1..2 steps only
        {'page': 3, 'action': 'emphasize', 'arg': 'left'},                  # page 3 is two-column
        {'page': 2, 'action': 'emphasize', 'arg': 'figure'},                # not a slot of title-bullets
        {'page': 1, 'action': 'recolor_accent', 'arg': 'accent2'},
        {'page': 1, 'action': 'recolor_accent', 'arg': 'stripe'},           # too faint on white
        {'page': 4, 'action': 'add_icon', 'arg': 'leaf'},
        {'page': 4, 'action': 'add_icon', 'arg': 'unicorn'},
        {'page': 2, 'action': 'reduce_text', 'arg': 20},
        {'page': 2, 'action': 'swap_image', 'arg': None},                   # no picture on the page
        {'page': 2, 'action': 'move_box', 'arg': 1},                        # not an action
    ]}
    edits, dropped = critic.validate_edits(raw, plan, ds)
    assert [(e.page, e.action) for e in edits] == [(2, 'change_layout'), (3, 'emphasize'), (1, 'recolor_accent'),
                                                   (4, 'add_icon'), (2, 'reduce_text')]
    assert len(dropped) == 8
    assert critic.validate_edits({'edits': 'nope'}, plan, ds) == ([], ['the reply did not match the edit schema'])
    many, why = critic.validate_edits({'edits': [{'page': 1, 'action': 'enlarge_title', 'arg': 1}] * 13}, plan, ds)
    assert len(many) == 1 and any('more than 12' in w for w in why)


def test_apply_edit_by_code(monkeypatch):
    ds = make_ds()
    ws = FakeWS()
    plan = deck(ds, 3)
    plan.pages[1].boxes[0].h = 60
    assert critic.apply_edit(plan, P.CriticEdit(2, 'enlarge_title', 1), {}, ds, ws)
    assert plan.pages[1].boxes[0].step == 'h1' and plan.pages[1].boxes[0].size == ds.size('h1', 'pptx')
    assert not critic.apply_edit(plan, P.CriticEdit(2, 'enlarge_title', 2), {}, ds, ws)   # display would not fit
    assert critic.apply_edit(plan, P.CriticEdit(1, 'recolor_accent', 'accent2'), {}, ds, ws)
    assert plan.system['colors']['accent'] == 'B45309' == ds.colors['accent']
    assert critic.apply_edit(plan, P.CriticEdit(3, 'add_icon', 'leaf'), {}, ds, ws)
    icon = plan.pages[2].boxes[-1]
    assert icon.kind == 'icon' and icon.content == 'icon:leaf' and icon.alt == 'leaf'
    assert ids(qa.run_check('D2', plan), ok=True) == ['D2']            # placed in a free corner
    calls = []
    from jevrouter.studio import layout

    def fake_relayout(p, spec, ds_, ws_, *, layouts=None):
        calls.append(layouts)
        p.pages[1].layout = layouts[1]
        return p
    monkeypatch.setattr(layout, 'relayout', fake_relayout)
    assert critic.apply_edit(plan, P.CriticEdit(2, 'change_layout', 'two-column'), {}, ds, ws)
    assert calls == [{1: 'two-column'}] and plan.pages[1].layout == 'two-column'


async def test_critique_sends_contact_sheets_and_validates():
    ds = make_ds()
    plan = deck(ds, 3)
    eng = VisionEngine(edits=[{'edits': [{'page': 2, 'action': 'enlarge_title', 'arg': 1},
                                         {'page': 7, 'action': 'enlarge_title', 'arg': 1}]}])
    sheet = thumbs.contact_sheet([thumbs.render_page(plan, i, None) for i in range(3)])
    rep = qa.report(plan, qa.check(plan))
    reply = await critic.critique(plan, rep, [sheet], eng)
    assert [(e.page, e.action) for e in reply.edits] == [(2, 'enlarge_title')] and len(reply.dropped) == 1
    assert (reply.llm_in, reply.llm_out) == (900, 120) and reply.note is None
    call = eng.calls[0]
    assert call['images'] == [sheet] and call['schema'] == P.critic_schema() and call['max_tokens'] <= 1200
    assert '1: cover-type' in call['prompt'] and 'title-bullets' in call['prompt']


async def test_critique_never_raises():
    ds = make_ds()
    plan = deck(ds, 2)
    sheet = png_of('white', 1200, 400)
    blind = await critic.critique(plan, {}, [sheet], BlindEngine())
    assert blind.edits == [] and 'cannot read images' in blind.note
    from jevrouter.engines.base import EngineError
    failed = await critic.critique(plan, {}, [sheet], VisionEngine(edits=[EngineError('rate limited')]))
    assert failed.edits == [] and 'rate limited' in failed.note
    broke = await critic.critique(plan, {}, [sheet], VisionEngine(edits=['not json at all']))
    assert broke.edits == [] and broke.note
    poor = await critic.critique(plan, {}, [sheet], VisionEngine(), budget_tokens=500)
    assert 'budget' in poor.note


# ---------- freeform ----------


GOOD_PROGRAM = {'shapes': [
    {'type': 'rect', 'x': 0, 'y': 0, 'w': 12, 'h': 12, 'fill': 'bg', 'z': 0},
    {'type': 'text', 'x': 0, 'y': 0, 'w': 12, 'h': 2, 'text': 'The big idea', 'step': 'h2', 'fill': 'heading', 'z': 2,
     'weight': 'bold'},
    {'type': 'ellipse', 'x': 8, 'y': 4, 'w': 3, 'h': 3, 'fill': 'accent', 'z': 1},
    {'type': 'line', 'x': 0, 'y': 3, 'w': 6, 'h': 0, 'points': [[0, 3], [6, 3]], 'stroke': 'accent', 'stroke_w': 2,
     'z': 1},
    {'type': 'image', 'x': 0, 'y': 4, 'w': 6, 'h': 6, 'ref': 'ws:images/leaf.png', 'alt': 'a leaf', 'z': 1},
    {'type': 'text', 'x': 0, 'y': 10.5, 'w': 12, 'h': 1.5, 'text': 'Light in, sugar out.', 'step': 'body', 'z': 2},
]}


def test_freeform_validate_accepts_a_good_program():
    ds = make_ds()
    ws = FakeWS()
    ws.put('images', 'leaf.png', png_of('green', 800, 800))
    program, problems = freeform.validate(GOOD_PROGRAM, ds, ws, page=3, fmt='pptx', refs=['ws:images/leaf.png'])
    assert problems == [] and program.page == 3 and (program.cols, program.rows) == (12, 12)
    assert freeform.grid_of('pdf') == (12, 16)


@pytest.mark.parametrize('bad,why', [
    ({'type': 'rect', 'x': 10, 'y': 0, 'w': 4, 'h': 2}, 'outside the grid'),
    ({'type': 'rect', 'x': 1.25, 'y': 0, 'w': 1, 'h': 1}, 'multiple of 0.5'),
    ({'type': 'rect', 'x': 0, 'y': 0, 'w': 1, 'h': 1, 'fill': 'FF0000'}, 'allowed values'),
    ({'type': 'image', 'x': 0, 'y': 0, 'w': 2, 'h': 2, 'ref': 'ws:images/other.png'}, 'not offered'),
    ({'type': 'icon', 'x': 0, 'y': 0, 'w': 1, 'h': 1, 'ref': 'icon:no-such-icon'}, 'bundled icon'),
    ({'type': 'text', 'x': 0, 'y': 0.5, 'w': 6, 'h': 1, 'text': 'On top of the heading', 'z': 3}, 'text on top of'),
    ({'type': 'text', 'x': 0, 'y': 2, 'w': 12, 'h': 1, 'text': 'x' * 150}, 'more than 120'),
    ({'type': 'text', 'x': 0, 'y': 2, 'w': 2, 'h': 0.5, 'text': 'Far too long a sentence for so small a box',
      'step': 'h1'}, 'does not fit'),
    ({'type': 'text', 'x': 6.5, 'y': 2.5, 'w': 5, 'h': 1, 'text': 'Faint', 'fill': 'stripe', 'z': 3}, 'contrast'),
    ({'type': 'text', 'x': 0.5, 'y': 6, 'w': 4, 'h': 1, 'text': 'On the photo', 'fill': 'on_accent', 'z': 3},
     'without a filled shape'),
    ({'type': 'path', 'x': 0, 'y': 0, 'w': 1, 'h': 1, 'points': [[0, 0]]}, 'at least 2 points'),
])
def test_freeform_validate_rejects(bad, why):
    ds = make_ds()
    ws = FakeWS()
    ws.put('images', 'leaf.png', png_of('green', 800, 800))
    raw = {'shapes': [*GOOD_PROGRAM['shapes'], bad]}
    program, problems = freeform.validate(raw, ds, ws, page=0, fmt='pptx', refs=['ws:images/leaf.png'])
    assert program is None and any(why in p for p in problems), problems


def test_freeform_to_boxes_grid_to_points():
    ds = make_ds()
    ws = FakeWS()
    ws.put('images', 'leaf.png', png_of('green', 800, 800))
    program, _ = freeform.validate(GOOD_PROGRAM, ds, ws, page=2, fmt='pptx', refs=['ws:images/leaf.png'])
    boxes = freeform.to_boxes(program, (960, 540), ds, {}, fmt='pptx', ws=ws)
    by = {b.id: b for b in boxes}
    bg, title, dot, line, img, foot = (by[f'p2.ff{n}'] for n in range(6))
    assert (bg.x, bg.y, bg.w, bg.h, bg.bleed) == (0, 0, 960, 540, True)        # a full-grid rect bleeds
    assert (title.x, title.y, title.w) == (48, 48, 864) and title.step == 'h2' and title.bold
    assert title.content == 'text:The big idea' and title.lines == ['The big idea']
    assert dot.style.radius == pytest.approx(min(dot.w, dot.h) / 2) and dot.style.fill == 'accent'
    assert line.kind == 'image' and line.content.startswith('ws:art/') and ws.get(line.content)
    assert img.content == 'ws:images/leaf.png' and img.alt == 'a leaf'
    assert foot.style.text_color == 'text' and all(b.overlay_ok for b in boxes)
    plan = P.DesignPlan(FID, 'pptx', 'minimal', ds.to_dict(), {}, pages=[
        P.PagePlan(0, 'freeform', 960, 540, freeform=True, boxes=boxes)])
    assert not [r for r in qa.check(plan, ws) if not r.ok]


async def test_freeform_compose_counts_usage_and_feedback():
    ds = make_ds()
    ws = FakeWS()
    eng = VisionEngine(programs=[{'shapes': [{'type': 'rect', 'x': 11, 'y': 0, 'w': 4, 'h': 1}]}])
    program, usage = await freeform.compose(0, {'heading': 'Cells', 'blocks': [{'type': 'paragraph', 'text': 'Hi'}]},
                                            ds, eng, ws, fmt='pptx', feedback=['shape 2 overlapped'])
    assert program is None and usage['calls'] == 1 and (usage['llm_in'], usage['llm_out']) == (900, 120)
    assert usage['problems'] and eng.calls[0]['max_tokens'] == 2000
    assert 'shape 2 overlapped' in eng.calls[0]['prompt'] and 'Cells' in eng.calls[0]['prompt']
    none, usage = await freeform.compose(0, {}, ds, None, ws, fmt='pptx')
    assert none is None and usage['calls'] == 0


# ---------- the agent loop ----------


SPEC = {'title': 'Photosynthesis', 'sections': [{'heading': f'S{i}', 'blocks': [{'type': 'paragraph', 'text': 'x'}]}
                                                for i in range(6)]}


@pytest.fixture
def stage(monkeypatch):
    """Fakes for every other builder's stage the agent calls; returns a namespace to configure and inspect them."""
    from types import SimpleNamespace as NS
    from jevrouter.studio import direct, fonts, layout, paint_pdf, paint_pptx, workspace
    ds = make_ds()
    st = NS(ds=ds, ws=FakeWS(), pages=5, cramped=None, refits=[], relayouts=[], freeform_pages=(), painted=[],
            direct_calls=[], lay_out=None)

    def build_system(**kw):
        return copy.deepcopy(st.ds)

    def outline_of(spec, fmt):
        return NS(pages=list(range(st.pages)))

    def direction():
        pages = [P.PageDirection(p.layout, freeform=i in st.freeform_pages) for i, p in
                 enumerate(deck(st.ds, st.pages).pages)]
        return P.ArtDirection('minimal', pages=pages)

    def keyless(outline, ds_, *, dark=None):
        st.direct_calls.append('keyless')
        return direction()

    async def llm(outline, ds_, engine, *, mood=(), max_tokens=2000, effort='low'):
        st.direct_calls.append(('llm', max_tokens))
        d = direction()
        d.source = 'llm'
        return d, {'calls': 1, 'llm_in': 300, 'llm_out': 150, 'ms': 5}

    def lay_out(spec, fmt, ds_, direction_, ws, *, file_id, paper=None):
        p = deck(st.ds, st.pages)
        p.file_id, p.format = file_id, fmt
        p.direction = direction_.to_dict()
        if st.cramped is not None:
            p.pages[st.cramped].boxes[1].lines = ['Point'] * 20
        return p

    def refit(plan, spec, page, action, ds_, ws, *, box=None, arg=None):
        st.refits.append((page, action, box))
        b = next((x for x in plan.pages[page].boxes if x.id == box), None)
        if action == 'shrink' and b is not None and b.lines and len(b.lines) > 10:
            b.lines = b.lines[:10]
            return True
        return False

    def relayout(plan, spec, ds_, ws, *, layouts=None):
        st.relayouts.append(layouts)
        new = copy.deepcopy(plan)
        for i, lid in (layouts or {}).items():
            new.pages[i] = bullets_page(i, st.ds, layout=lid)
            if lid == 'two-column':                  # this layout overflows: the critic edit must be rolled back
                new.pages[i].boxes[1].lines = ['Point'] * 20
        return new

    async def ensure(family, http, *, styles=()):
        return NS(family=family, source='cache', licence='OFL-1.1', note=None)

    def paint(plan, spec, ws):
        st.painted.append(plan.format)
        return b'PAINTED'

    monkeypatch.setattr(tokens, 'build_system', build_system)
    monkeypatch.setattr(direct, 'outline_of', outline_of)
    monkeypatch.setattr(direct, 'direct_keyless', keyless)
    monkeypatch.setattr(direct, 'direct_llm', llm)
    monkeypatch.setattr(layout, 'lay_out', lay_out)
    monkeypatch.setattr(layout, 'refit', refit)
    monkeypatch.setattr(layout, 'relayout', relayout)
    monkeypatch.setattr(layout, 'flow_styles', lambda spec, fmt, ds_: {'styles': {'body': {'size': 11}}})
    monkeypatch.setattr(fonts, 'ensure', ensure)
    monkeypatch.setattr(paint_pptx, 'paint', paint)
    monkeypatch.setattr(paint_pdf, 'paint', paint)
    monkeypatch.setattr(workspace, 'open_workspace', lambda file_id, *, sandbox=None: st.ws)
    return st


def phases_of(result):
    return {p['phase']: p for p in result.phases}


async def test_design_keyless_clean_deck(stage):
    r = await agent.design(SPEC, 'pptx', file_id=FID)
    assert r.painted_bytes == b'PAINTED' and r.plan.stop == 'pass' and r.plan.score == 100 and r.plan.rounds == 1
    assert stage.direct_calls == ['keyless'] and r.plan.tokens['direct_out'] == 0
    assert P.validate(r.report, P.report_schema()) == [] and r.report['phases'] == r.phases
    assert set(phases_of(r)) >= {'direct', 'assets', 'layout', 'thumbs', 'qa', 'paint'}
    assert all(p['calls'] == 0 for p in r.phases) and r.report['thumbs'] == 5
    assert stage.ws.load_plan().score == 100 and stage.ws.report['score'] == 100
    assert sorted(k for k in stage.ws.files if k.startswith('ws:thumbs/')) == [f'ws:thumbs/00{i}.png' for i in
                                                                                 range(1, 6)]
    logged = [e['phase'] for e in stage.ws.logged]
    assert logged[:2] == ['tokens', 'direct'] and {'assets', 'layout', 'qa', 'done'} <= set(logged)
    assert all('t' in e for e in stage.ws.logged)
    assert "Deep mode" in r.report['critic']['why']
    m = r.meta_design
    assert m['studio'] is True and m['preset'] == 'minimal' and m['score'] == 100 and m['thumbs'] == 5
    assert r.notes[0].startswith('Studio designed 5 slides')


async def test_cramped_deck_is_fixed_by_code(stage):
    stage.cramped = 2
    r = await agent.design(SPEC, 'pptx', file_id=FID)
    assert r.plan.stop == 'pass' and r.plan.rounds == 2 and r.plan.score == 100
    assert stage.refits == [(2, 'shrink', 'p2.body')]
    fixed = [q for q in r.plan.qa if not q.ok]
    assert [(q.id, q.page, q.fixed) for q in fixed] == [('D1', 2, True)]
    assert r.report['checks'][0]['ok'] and any(e['phase'] == 'fix' and e.get('changed') for e in stage.ws.logged)


async def test_unfixable_deck_stops_at_the_round_limit(stage, monkeypatch):
    stage.cramped = 1
    from jevrouter.studio import layout
    monkeypatch.setattr(layout, 'refit', lambda *a, **k: stage.refits.append(a[3]) or False)
    r = await agent.design(SPEC, 'pptx', file_id=FID)
    assert r.plan.stop == 'rounds' and r.plan.score == 80 and r.painted_bytes == b'PAINTED'
    assert stage.refits == list(qa.FIXES['D1'])        # every D1 code fix tried in order
    assert any('overflow' in c for c in r.caveats)


async def test_ordered_fixes_move_on_each_round(stage, monkeypatch):
    stage.cramped = 1
    from jevrouter.studio import layout
    seen = []

    def refit(plan, spec, page, action, ds_, ws, *, box=None, arg=None):
        seen.append(action)
        if action == 'rebalance':
            plan.pages[page].boxes[1].lines = ['Point'] * 5
            return True
        return action == 'shrink'                        # "changed" but still overflowing
    monkeypatch.setattr(layout, 'refit', refit)
    r = await agent.design(SPEC, 'pptx', file_id=FID)
    assert seen == ['shrink', 'rebalance'] and r.plan.stop == 'pass' and r.plan.rounds == 3


async def test_deadline_stops_the_loop(stage):
    stage.cramped = 1
    r = await agent.design(SPEC, 'pptx', file_id=FID, deadline=time.monotonic() - 1)
    assert r.plan.stop == 'deadline' and r.report['stop'] == 'deadline' and stage.refits == []


async def test_llm_direction_counts_tokens(stage):
    eng = BlindEngine()
    r = await agent.design(SPEC, 'pptx', file_id=FID, engine=eng, mode='balanced')
    assert stage.direct_calls == [('llm', config.STUDIO_DIRECT_TOKENS)]
    assert r.plan.tokens['direct_in'] == 300 and r.plan.tokens['direct_out'] == 150
    assert phases_of(r)['direct'] == {'phase': 'direct', 'calls': 1, 'llm_in': 300, 'llm_out': 150,
                                      'ms': phases_of(r)['direct']['ms']}
    quick = await agent.design(SPEC, 'pptx', file_id=FID, engine=eng, mode='quick')
    assert stage.direct_calls[-1] == 'keyless' and quick.plan.tokens['direct_out'] == 0


async def test_deep_mode_without_vision_skips_the_critic(stage):
    r = await agent.design(SPEC, 'pptx', file_id=FID, engine=BlindEngine(), mode='deep')
    assert r.report['critic']['ran'] is False and "can't read images" in r.report['critic']['why']
    assert any("can't read images" in n for n in r.notes)


async def test_critic_edits_are_applied_or_rolled_back(stage):
    eng = VisionEngine(edits=[{'edits': [{'page': 2, 'action': 'change_layout', 'arg': 'two-column'},
                                         {'page': 3, 'action': 'enlarge_title', 'arg': 1}]}])
    r = await agent.design(SPEC, 'pptx', file_id=FID, engine=eng, mode='deep')
    crit = r.report['critic']
    assert crit['ran'] is True
    assert crit['rolled_back'] == [{'page': 2, 'action': 'change_layout', 'arg': 'two-column',
                                    'why': 'it lowered the design score'}]
    assert {'page': 3, 'action': 'enlarge_title', 'arg': 1, 'applied': True} in crit['edits']
    assert r.plan.pages[1].layout == 'title-bullets'                     # rolled back
    assert r.plan.pages[1].boxes[1].lines != ['Point'] * 20              # the overflowing relayout was undone
    assert r.plan.pages[2].boxes[0].step == 'h1' and r.plan.score == 100
    critic_calls = [c for c in eng.calls if 'edits' in (c['schema'] or {}).get('properties', {})]
    assert len(critic_calls) == 2                                        # a second round after an accepted edit
    assert all(c['images'] and c['images'][0].startswith(b'\x89PNG') for c in critic_calls)
    assert r.plan.tokens['critic_in'] == 1800 and r.plan.tokens['critic_out'] == 240
    assert phases_of(r)['critic']['calls'] == 2
    assert r.plan.tokens['critic_in'] + r.plan.tokens['critic_out'] <= config.STUDIO_CRITIC_TOKENS


async def test_critic_budget_is_respected(stage, monkeypatch):
    monkeypatch.setattr(config, 'STUDIO_CRITIC_TOKENS', 2500)
    eng = VisionEngine(edits=[{'edits': [{'page': 3, 'action': 'enlarge_title', 'arg': 1}]},
                              {'edits': [{'page': 4, 'action': 'enlarge_title', 'arg': 1}]}])
    r = await agent.design(SPEC, 'pptx', file_id=FID, engine=eng, mode='deep')
    critic_calls = [c for c in eng.calls if 'edits' in (c['schema'] or {}).get('properties', {})]
    assert len(critic_calls) == 1 and 'budget' in r.report['critic']['why']


async def test_freeform_page_is_drawn_from_a_program(stage):
    stage.freeform_pages = (1,)
    stage.ws.put('images', 'leaf.png', png_of('green', 800, 800))
    program = copy.deepcopy(GOOD_PROGRAM)
    program['shapes'] = [s for s in program['shapes'] if s['type'] != 'image']
    eng = VisionEngine(programs=[program])
    r = await agent.design(SPEC, 'pptx', file_id=FID, engine=eng, mode='balanced')
    page = r.plan.pages[1]
    assert page.layout == 'freeform' and page.freeform and page.boxes[1].content == 'text:The big idea'
    assert r.report['fallbacks'] == [] and r.plan.tokens['freeform_out'] == 120
    assert phases_of(r)['freeform']['calls'] == 1


async def test_freeform_falls_back_after_two_failures(stage):
    stage.freeform_pages = (1, 2, 3)
    bad = {'shapes': [{'type': 'text', 'x': 0, 'y': 0, 'w': 12, 'h': 2, 'text': 'Faint', 'fill': 'stripe'}]}
    eng = VisionEngine(programs=[bad, bad, bad, bad])
    r = await agent.design(SPEC, 'pptx', file_id=FID, engine=eng, mode='balanced')
    assert [f['page'] for f in r.report['fallbacks']] == [1, 2]            # at most 2 freeform pages per file
    assert [(f['from'], f['to'], f['why']) for f in r.report['fallbacks']] == [
        ('freeform', 'title-bullets', 'failed QA twice'), ('freeform', 'two-column', 'failed QA twice')]
    assert r.plan.pages[1].layout != 'freeform' and not r.plan.pages[1].freeform
    assert len([c for c in eng.calls if 'shapes' in (c['schema'] or {}).get('properties', {})]) == 4
    assert 'contrast' in eng.calls[1]['prompt']                            # the retry was told why
    assert any('freeform pages are allowed' in n for n in r.notes)
    keyless = await agent.design(SPEC, 'pptx', file_id=FID)
    assert keyless.report['fallbacks'] == [] and any('no model' in n for n in keyless.notes)


async def test_flow_formats_are_styled_not_painted(stage):
    r = await agent.design(SPEC, 'docx', file_id=FID)
    assert r.painted_bytes is None and r.plan.flow == {'styles': {'body': {'size': 11}}} and r.plan.pages == []
    assert r.report['stop'] == 'keyless' and stage.painted == []
    assert P.validate(r.report, P.report_schema()) == []


async def test_restyle_costs_nothing_and_uses_a_new_workspace(stage):
    await agent.design(SPEC, 'pptx', file_id=FID)
    stage.relayouts.clear()
    new_id = 'ffffffffffff'
    r = agent.restyle(SPEC, 'pptx', stage.ws, P.RestyleOptions(layouts={2: 'quote'}), file_id=new_id)
    assert stage.relayouts == [{2: 'quote'}] and r.plan.file_id == new_id and r.plan.pages[2].layout == 'quote'
    assert all(v == 0 for v in r.plan.tokens.values()) and all(p['calls'] == 0 for p in r.phases)
    assert r.plan.direction['source'] == 'restyle' and r.painted_bytes == b'PAINTED'
    with pytest.raises(LookupError, match='no-plan'):
        agent.restyle(SPEC, 'pptx', FakeWS(), P.RestyleOptions(), file_id=new_id)


async def test_polish_needs_vision_and_a_plan(stage):
    with pytest.raises(ValueError, match='no-vision'):
        await agent.polish(SPEC, 'pptx', stage.ws, BlindEngine(), file_id='ffffffffffff')
    with pytest.raises(LookupError, match='no-plan'):
        await agent.polish(SPEC, 'pptx', FakeWS(), VisionEngine(), file_id='ffffffffffff')
    await agent.design(SPEC, 'pptx', file_id=FID)
    eng = VisionEngine(edits=[{'edits': [{'page': 3, 'action': 'enlarge_title', 'arg': 1}]}])
    r = await agent.polish(SPEC, 'pptx', stage.ws, eng, file_id='ffffffffffff')
    assert r.plan.file_id == 'ffffffffffff' and r.plan.pages[2].boxes[0].step == 'h1'
    assert r.report['critic']['ran'] and r.plan.tokens['critic_in'] == 900 and r.plan.tokens['direct_in'] == 0
    assert len(eng.calls) == 1                                               # Polish is one critic round


async def test_paint_failure_propagates_for_the_legacy_fallback(stage, monkeypatch):
    from jevrouter.studio import paint_pptx

    def boom(plan, spec, ws):
        raise RuntimeError('painter broke')
    monkeypatch.setattr(paint_pptx, 'paint', boom)
    with pytest.raises(RuntimeError):
        await agent.design(SPEC, 'pptx', file_id=FID)
    assert any(e['phase'] == 'paint' and 'error' in e for e in stage.ws.logged)


# ---------- engines: the vision opt-in ----------


def test_engine_vision_flags():
    from jevrouter.engines.api import ApiEngine, Provider
    from jevrouter.engines.base import Engine
    from jevrouter.engines.claude_code import ClaudeCodeEngine
    from jevrouter.engines.codex import CodexEngine
    seeing = ApiEngine(Provider('p', 'P', 'https://p.test/v1', 'm'), client=object())
    blind = ApiEngine(Provider('q', 'Q', 'https://q.test/v1', 'm', vision=False), client=object())
    assert seeing.supports_vision and ClaudeCodeEngine('/bin/false').supports_vision and not blind.supports_vision
    assert not Engine().supports_vision and not CodexEngine('/bin/false').supports_vision
    assert seeing.info()['vision'] is True and blind.info()['vision'] is False
    assert critic.can_see(seeing) and not critic.can_see(CodexEngine('/bin/false')) and not critic.can_see(blind)


async def test_api_engine_sends_images_as_data_urls():
    from tests.fakes import FakeLLM, eng
    fake = FakeLLM(['{"edits": []}'], ['plain'])
    e = eng(fake)
    png = png_of('red')
    await e.stream(system='s', prompt='look', images=[png])
    content = fake.calls[0]['messages'][1]['content']
    url = content[0]['image_url']['url']
    assert content[0]['type'] == 'image_url' and url.startswith('data:image/png;base64,')
    import base64
    assert base64.b64decode(url.split(',', 1)[1]) == png and content[1] == {'type': 'text', 'text': 'look'}
    await e.stream(system='s', prompt='no pictures')
    assert fake.calls[1]['messages'][1]['content'] == 'no pictures'


CLAUDE_OK = '''
import json, os, sys
argv = sys.argv[1:]
stdin = sys.stdin.read()
json.dump({'argv': argv, 'stdin': stdin}, open(__file__ + '.call.json', 'w'))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '{"edits": []}',
                  'usage': {'input_tokens': 1000, 'output_tokens': 7}}), flush=True)
'''


async def test_claude_code_sends_images_through_stream_json(tmp_path):
    from jevrouter.engines.claude_code import ClaudeCodeEngine
    path = tmp_path / 'claude'
    path.write_text(f'#!{sys.executable}\n' + textwrap.dedent(CLAUDE_OK))
    path.chmod(0o755)
    e = ClaudeCodeEngine(str(path))
    png = png_of('blue')
    r = await e.stream(system='critic', prompt='look', schema=P.critic_schema(), images=[png])
    assert r.text == '{"edits": []}' and r.input_tokens == 1000
    call = json.loads((tmp_path / 'claude.call.json').read_text())
    a = call['argv']
    assert a[a.index('--input-format') + 1] == 'stream-json' and a[a.index('--tools') + 1] == ''
    msg = json.loads(call['stdin'])
    assert msg['type'] == 'user' and msg['message']['role'] == 'user'
    blocks = msg['message']['content']
    assert blocks[0]['type'] == 'image' and blocks[-1] == {'type': 'text', 'text': 'look'}
    e.warm = 0
    await e.stream(system='s', prompt='plain text')          # text-only calls are unchanged
    call = json.loads((tmp_path / 'claude.call.json').read_text())
    assert call['stdin'] == 'plain text' and '--input-format' not in call['argv']
    await e.aclose()


async def test_a_round_of_fixes_that_lowers_the_score_is_undone(stage, monkeypatch):
    stage.cramped = 1
    from jevrouter.studio import layout

    def refit(plan, spec, page, action, ds_, ws, *, box=None, arg=None):
        stage.refits.append(action)
        plan.pages[page].boxes[1].size = 21          # off the type scale (D6) and still overflowing (D1)
        plan.pages[page].boxes[0].y = 140            # and now overlapping the body (D2)
        return True
    monkeypatch.setattr(layout, 'refit', refit)
    r = await agent.design(SPEC, 'pptx', file_id=FID)
    assert stage.refits == ['shrink'] and r.plan.score == 80 and r.plan.rounds == 2
    assert r.plan.pages[1].boxes[1].size == 18 and r.plan.pages[1].boxes[0].y == 48
    assert any(e.get('rolled_back') for e in stage.ws.logged if e['phase'] == 'fix')


async def test_an_empty_looking_slide_gets_the_enlarge_fix_not_a_crowding_fix(stage, monkeypatch):
    from jevrouter.studio import layout
    orig = layout.lay_out

    def sparse(*a, **k):
        p = orig(*a, **k)
        p.pages[1].boxes[1].h = 70
        return p
    monkeypatch.setattr(layout, 'lay_out', sparse)
    r = await agent.design(SPEC, 'pptx', file_id=FID)
    # D4 "empty" gets only the enlarge fix (never the crowding fixes rebalance, split, compact); D5 its swap_layout
    assert [a for _, a, _ in stage.refits] == ['enlarge', 'swap_layout']
    assert r.plan.score == 85 and not r.caveats                         # soft checks stay in the report
    assert r.report['checks'][3]['ok'] is False and 'looks empty' in r.report['checks'][3]['note']

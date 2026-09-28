"""The Studio contract (docs/PLAN-designer.md section 9): every public signature and dataclass field the six builders
code against, the JSON schemas, the layout library and preset data, and that TG_STUDIO off leaves today's files
byte-identical. Additive changes pass (new optional parameters, new fields at the end); removals and renames fail."""
import dataclasses
import datetime
import importlib
import inspect
import json
import re
import sys
import time
import zipfile

import pytest
import xlsxwriter.core
import xlsxwriter.workbook

from jevrouter import config, create as cf
from jevrouter.studio import agent, library, plan, presets, qa, tokens

importlib.import_module('jevrouter.create.render')
render_mod = sys.modules['jevrouter.create.render']   # the module (jevrouter.create re-exports a render function)

MODULES = ('tokens', 'presets', 'fonts', 'workspace', 'direct', 'library', 'layout', 'paint_pptx', 'paint_pdf', 'qa',
           'thumbs', 'critic', 'freeform', 'agent', 'icons', 'art', 'assets', 'plan')

# 'module.function' or 'module.Class.method' -> parameters: name, name=default, '*' before keyword-only ones
SIGNATURES = {
    'tokens.build_system': "*, preset=None, design=None, prompt='', fmt='pptx', font=None, dark=None",
    'tokens.with_dark': 'ds, dark',
    'tokens.print_version': 'ds',
    'tokens.from_design': 'design',
    'tokens.legacy_theme': 'ds, fmt',
    'presets.get': 'preset_id',
    'presets.list_presets': '',
    'presets.list_templates': '',
    'presets.template': 'template_id',
    'presets.read_prompt': 'text',
    'presets.for_theme': 'theme',
    'fonts.resolve_local': 'family',
    'fonts.ensure': "family, http, *, styles=('regular', 'bold', 'italic', 'bolditalic')",
    'fonts.alternative': 'family',
    'fonts.metrics': 'path',
    'fonts.measure': "text, family, size_pt, *, bold=False, italic=False, fmt='pptx'",
    'fonts.wrap': "text, family, size_pt, width_pt, *, bold=False, italic=False, fmt='pptx'",
    'fonts.line_metrics': 'family, size_pt, line_height=1.2',
    'fonts.search': 'q, http, limit=20',
    'fonts.preview_png': "family, text='The quick brown fox', px=32",
    'fonts.prune': 'cap_bytes=209715200',
    'workspace.Workspace.__init__': 'file_id, root=None',
    'workspace.Workspace.exists': '',
    'workspace.Workspace.save_plan': 'plan',
    'workspace.Workspace.load_plan': 'previous=False',
    'workspace.Workspace.save_report': 'report',
    'workspace.Workspace.load_report': '',
    'workspace.Workspace.put': 'kind, name, data',
    'workspace.Workspace.get': 'ref',
    'workspace.Workspace.path_of': 'ref',
    'workspace.Workspace.save_thumb': 'page, png',
    'workspace.Workspace.thumbs': '',
    'workspace.Workspace.log': 'event',
    'workspace.Workspace.events': '',
    'workspace.Workspace.manifest': '',
    'workspace.Workspace.copy_to': 'file_id',
    'workspace.Workspace.delete': '',
    'workspace.open_workspace': 'file_id, *, sandbox=None',
    'workspace.drop': 'file_id',
    'workspace.drop_sandbox': 'sandbox_id',
    'workspace.prune': 'cap_bytes=1073741824, ttl_days=14',
    'direct.outline_of': 'spec, fmt',
    'direct.direct_keyless': 'outline, ds, *, dark=None',
    'direct.direct_llm': "outline, ds, engine, *, mood=(), max_tokens=2000, effort='low'",
    'direct.validate_direction': 'raw, outline, fmt',
    'library.get': 'layout_id',
    'library.slide_layouts': '',
    'library.page_templates': '',
    'library.for_format': 'fmt',
    'library.next_best': 'layout_id, avoid=frozenset(), *, needs=()',
    'library.slot_kinds': 'layout_id',
    'layout.page_size': 'fmt, paper=None',
    'layout.grid_rect': "area, size, ds, *, bleed=False, fmt='pptx'",
    'layout.fit_text': 'text, family, step, ds, w, h, fmt, *, bold=False, line_height=None',
    'layout.lay_out': 'spec, fmt, ds, direction, ws, *, file_id, paper=None',
    'layout.lay_out_page': 'direction_index, spec, fmt, ds, direction, ws, size',
    'layout.refit': 'plan, spec, page, action, ds, ws, *, box=None, arg=None',
    'layout.relayout': 'plan, spec, ds, ws, *, layouts=None',
    'layout.flow_styles': 'spec, fmt, ds',
    'layout.box_text': 'box, spec',
    'paint_pptx.paint': 'plan, spec, ws',
    'paint_pdf.paint': 'plan, spec, ws',
    'qa.check': 'plan, ws=None, *, thumbs=None',
    'qa.run_check': 'check_id, plan, ws=None, *, thumbs=None',
    'qa.score': 'results',
    'qa.report': 'plan, results, *, critic=None, fallbacks=None, notes=None, thumbs=0',
    'qa.contrast': 'fg, bg',
    'thumbs.render_page': 'plan, page, ws, *, width_px=480, spec=None',
    'thumbs.render_all': 'plan, ws, *, width_px=480, spec=None',
    'thumbs.contact_sheet': 'pngs, *, cols=3, rows=2, width_px=1200, first_page=1',
    'thumbs.sample': 'png, box, page_size',
    'thumbs.preset_thumb': 'preset_id, *, width_px=320',
    'critic.can_see': 'engine',
    'critic.critique': "plan, report, sheets, engine, *, budget_tokens=6000, effort='medium'",
    'critic.validate_edits': 'raw, plan, ds',
    'critic.apply_edit': 'plan, edit, spec, ds, ws',
    'freeform.compose': 'page, section, ds, engine, ws, *, fmt, refs=(), budget_tokens=2000',
    'freeform.validate': 'raw, ds, ws, *, page, fmt, refs=()',
    'freeform.to_boxes': 'program, size, ds, spec, *, fmt, ws',
    'agent.enabled': 'fmt',
    'agent.design': ("spec, fmt, *, file_id, brief=None, request='', tokens_src=None, engine=None, http=None, "
                     "mode='balanced', sandbox=None, deadline=None, preset=None"),
    'agent.restyle': 'spec, fmt, ws, options, *, file_id',
    'agent.polish': 'spec, fmt, ws, engine, *, file_id, deadline=None',
    'agent.paint': 'plan, spec, ws',
    'agent.meta_design': 'result',
    'icons.names': '',
    'icons.get': 'name',
    'icons.pick': 'text, *, used=None',
    'icons.polylines': 'icon, size_pt, *, tolerance=0.25',
    'icons.png': 'name, color, px',
    'art.draw': 'kind, seed, w, h, ds',
    'art.pick': 'ds, mood, seed',
    'art.png': 'kind, seed, w_px, h_px, ds',
    'assets.focal_point': 'data',
    'assets.crop_for': "size_px, focal, box_w, box_h, fit='cover'",
    'assets.upscale': 'size_px, crop, box_w, box_h, fmt',
    'assets.prepare': "asset, box_w, box_h, ws, *, fmt, fit='cover', mono=False, duotone=None",
    'plan.design_plan_schema': '',
    'plan.art_schema': 'llm=True',
    'plan.critic_schema': '',
    'plan.freeform_schema': 'cols=12, rows=12',
    'plan.report_schema': '',
    'plan.validate': "value, schema, where='$'",
}

# 'module.Dataclass' -> its fields, in order
FIELDS = {
    'tokens.TypeScale': ('ratio', 'line_height', 'display_line_height', 'display_tracking'),
    'tokens.Families': ('display', 'heading', 'body', 'caption', 'mono'),
    'tokens.Spacing': ('unit', 'columns', 'slide_margin', 'page_margin', 'gutter', 'rows_slide', 'rows_page'),
    'tokens.Shape': ('radius', 'stroke', 'shadow', 'accent_shape'),
    'tokens.MinSizes': ('slide_body', 'slide_caption', 'print_body', 'print_caption'),
    'tokens.DesignSystem': ('id', 'name', 'dark', 'colors', 'chart_palette', 'gradients', 'overlay_alpha', 'families',
                            'scale', 'spacing', 'shape', 'image', 'min_sizes', 'hatch', 'grey_images', 'justify', 'do',
                            'dont', 'source', 'notes'),
    'plan.BoxStyle': ('fill', 'stroke', 'stroke_w', 'text_color', 'radius', 'opacity', 'shadow'),
    'plan.Box': ('id', 'kind', 'x', 'y', 'w', 'h', 'z', 'slot', 'content', 'style', 'font', 'step', 'size', 'bold',
                 'italic', 'line_height', 'align', 'valign', 'fit', 'crop', 'focal', 'lines', 'alt', 'overlay_ok',
                 'bleed'),
    'plan.PagePlan': ('index', 'layout', 'w', 'h', 'section', 'variant', 'freeform', 'background', 'continued',
                      'notes', 'boxes'),
    'plan.FontUse': ('family', 'role', 'source', 'licence', 'embedded', 'fallback', 'requested', 'note'),
    'plan.AssetUse': ('ref', 'kind', 'source', 'licence', 'credit'),
    'plan.QaResult': ('id', 'ok', 'note', 'page', 'box', 'value', 'threshold', 'fixed'),
    'plan.DesignPlan': ('file_id', 'format', 'preset', 'system', 'direction', 'pages', 'flow', 'fonts', 'assets',
                        'notes', 'qa', 'score', 'rounds', 'stop', 'tokens', 'version'),
    'plan.PageDirection': ('layout', 'image', 'emphasis', 'focus', 'variant', 'freeform'),
    'plan.ArtDirection': ('preset', 'mood', 'dark', 'pages', 'source', 'notes'),
    'plan.CriticEdit': ('page', 'action', 'arg'),
    'plan.ShapeProgram': ('page', 'cols', 'rows', 'shapes'),
    'plan.RestyleOptions': ('preset', 'fonts', 'dark', 'template', 'layouts', 'print_version'),
    'plan.DesignResult': ('plan', 'painted_bytes', 'report', 'phases', 'notes', 'caveats', 'meta_design', 'fell_back'),
    'fonts.FontFace': ('family', 'style', 'path', 'licence', 'source'),
    'fonts.FontMetrics': ('units_per_em', 'ascender', 'descender', 'line_gap', 'x_height', 'cap_height', 'widths',
                          'default_width'),
    'fonts.FontResolution': ('requested', 'family', 'faces', 'office_fallback', 'licence', 'source', 'embeddable',
                             'note', 'install_url'),
    'fonts.FontInfo': ('family', 'category', 'licence', 'source', 'styles', 'installed', 'preview'),
    'presets.PresetInfo': ('id', 'name', 'description', 'dark', 'families', 'colors', 'thumb'),
    'presets.TemplateInfo': ('id', 'name', 'description', 'format', 'preset', 'paper', 'sequence', 'tone'),
    'presets.PromptStyle': ('preset', 'dark', 'mood', 'fonts', 'print_version', 'words'),
    'library.Slot': ('name', 'kind', 'area', 'step', 'accepts', 'required', 'bleed', 'fit', 'overlay_ok', 'align',
                     'max_words', 'flow'),
    'library.LayoutDef': ('id', 'family', 'target', 'slots', 'asymmetric', 'description'),
    'direct.PageOutline': ('index', 'section', 'heading', 'level', 'blocks', 'words', 'bullets', 'images', 'stats',
                           'diagrams', 'has_chart', 'has_table', 'has_quote'),
    'direct.Outline': ('title', 'fmt', 'pages', 'images'),
    'layout.FitResult': ('size', 'lines', 'height', 'fits', 'overflow_words'),
    'critic.CriticReply': ('edits', 'dropped', 'llm_in', 'llm_out', 'ms', 'note'),
    'icons.Icon': ('name', 'tags', 'nodes'),
}


def _resolve(path: str):
    mod, *rest = path.split('.')
    obj = importlib.import_module(f'jevrouter.studio.{mod}')
    for part in rest:
        obj = getattr(obj, part)
    return obj


def _split(sig: str) -> list[str]:
    """'a, b=(1, 2), *, c' -> ['a', 'b=(1, 2)', '*', 'c'] (commas inside brackets or quotes don't split)."""
    out, depth, cur, quote = [], 0, '', None
    for ch in sig:
        if quote:
            quote = None if ch == quote else quote
        elif ch in '\'"':
            quote = ch
        elif ch in '([{':
            depth += 1
        elif ch in ')]}':
            depth -= 1
        elif ch == ',' and depth == 0:
            out.append(cur.strip())
            cur = ''
            continue
        cur += ch
    return [p for p in (*out, cur.strip()) if p]


def _params(fn) -> list[str]:
    out, star = [], False
    for p in inspect.signature(fn).parameters.values():
        if p.name == 'self':
            continue
        if p.kind == p.KEYWORD_ONLY and not star:
            out.append('*')
            star = True
        out.append(p.name if p.default is p.empty else f'{p.name}={p.default!r}')
    return out


# ---------- the package ----------


def test_every_module_imports_without_side_effects(tmp_path):
    import jevrouter.studio as studio
    for m in MODULES:
        importlib.import_module(f'jevrouter.studio.{m}')
    assert set(studio.__all__) <= set(dir(studio))
    for name in ('design', 'enabled', 'paint', 'polish', 'restyle', 'DesignPlan', 'DesignSystem', 'DesignResult',
                 'SLIDE_LAYOUTS', 'PAGE_TEMPLATES', 'PRESETS', 'validate'):
        assert name in studio.__all__


@pytest.mark.parametrize('path', sorted(SIGNATURES))
def test_signature(path):
    fn = _resolve(path)
    want = _split(SIGNATURES[path])
    got = _params(fn)
    # every contracted parameter is there, in order, with the same kind and default; extra ones need defaults
    it = iter(got)
    for w in want:
        for g in it:
            if g == w:
                break
            assert '=' in g or g == '*', f'{path}: new parameter {g!r} needs a default (contract: {want})'
        else:
            pytest.fail(f'{path}: {w!r} missing or out of order (got {got})')
    assert inspect.iscoroutinefunction(fn) == (path in ASYNC), f'{path}: async-ness is part of the contract'


ASYNC = {'fonts.ensure', 'fonts.search', 'direct.direct_llm', 'critic.critique', 'freeform.compose', 'agent.design',
         'agent.polish'}


@pytest.mark.parametrize('path', sorted(FIELDS))
def test_dataclass_fields(path):
    cls = _resolve(path)
    got = tuple(f.name for f in dataclasses.fields(cls))
    assert got[:len(FIELDS[path])] == FIELDS[path], f'{path}: fields are append-only (got {got})'


def test_stubs_are_honest_not_silent():
    """An unbuilt function raises NotImplementedError naming its builder; nothing pretends to work."""
    from jevrouter.studio import direct, fonts, layout, workspace
    with pytest.raises(NotImplementedError, match='builder'):
        tokens.build_system()
    with pytest.raises(NotImplementedError, match='builder'):
        fonts.alternative('Helvetica Neue')
    with pytest.raises(NotImplementedError, match='builder'):
        direct.outline_of({}, 'pptx')
    with pytest.raises(NotImplementedError, match='builder'):
        layout.grid_rect((0, 0, 1, 1), (960, 540), None)
    with pytest.raises(NotImplementedError, match='builder'):
        workspace.Workspace('0123456789ab').load_plan()


# ---------- design tokens and presets ----------


def _ds(**kw) -> tokens.DesignSystem:
    colors = {r: '000000' for r in tokens.COLOR_ROLES}
    colors.update(bg='FFFFFF', text='1F2328', accent='1D4ED8')
    return tokens.DesignSystem(id='minimal', name='Minimal', dark=False, colors=colors,
                               chart_palette=['2563EB', 'F59E0B', '059669', 'DC2626', '7C3AED', '0891B2'], **kw)


def test_design_system_roundtrip_and_sizes():
    ds = _ds(gradients=[('111111', '222222')])
    back = tokens.DesignSystem.from_dict(json.loads(json.dumps(ds.to_dict())))
    assert back == ds
    assert ds.size('body', 'pptx') == 18.0 and ds.size('caption', 'pptx') >= 12.0   # slide minimums
    assert _ds(min_sizes=tokens.MinSizes(slide_body=20)).size('body', 'pptx') == 20.0
    assert ds.size('body', 'pdf') == 10.5 and ds.size('caption', 'pdf') >= 8.0
    assert ds.size('display', 'pptx') > ds.size('h1', 'pptx') > ds.size('h2', 'pptx') > ds.size('body', 'pptx')
    assert ds.color('chart7') == ds.chart_palette[0] and ds.color('accent') == '1D4ED8'
    for name in plan.TOKEN_COLORS:
        assert re.fullmatch(r'[0-9A-F]{6}', ds.color(name))


def test_presets_and_prompt_words():
    assert presets.PRESETS == ('bold-dark', 'editorial', 'minimal', 'vibrant', 'pastel', 'academic', 'mono',
                               'high-legibility')
    assert presets.DEFAULT_PRESET in presets.PRESETS
    assert set(presets.LEGACY_THEME.values()) <= set(presets.PRESETS) and set(presets.THEME_OF) == set(presets.PRESETS)
    assert presets.for_theme('dark') == 'bold-dark' and presets.for_theme(None) == presets.DEFAULT_PRESET
    assert len(presets.TEMPLATES) == 7
    for pid, rx, effect in presets.PROMPT_WORDS:
        re.compile(rx, re.I)
        assert set(effect) <= {'preset', 'dark', 'mood', 'print'}
        assert effect.get('preset', 'minimal') in presets.PRESETS
    # run 2808's words are covered by the table: "bold and creative with a dark design"
    text = 'a 12 slide PPT, bold and creative with a dark design'
    hits = {pid for pid, rx, _ in presets.PROMPT_WORDS if re.search(rx, text, re.I)}
    assert {'dark', 'vibrant'} <= hits and presets.DARK_MOOD['vibrant'] == 'bold-dark'


# ---------- the layout library ----------


def test_layout_library_is_complete_and_on_grid():
    assert library.SLIDE_LAYOUTS == (
        'cover-hero', 'cover-type', 'section-divider', 'title-bullets', 'image-left-text', 'image-right-text',
        'full-bleed-image-caption', 'big-number', 'stat-cards', 'quote', 'two-column', 'comparison',
        'full-width-diagram', 'chart-focus', 'timeline-strip', 'closing')
    assert library.PAGE_TEMPLATES == ('cover', 'chapter-opener', 'text-side-figure', 'two-column-text', 'full-figure',
                                      'pull-quote', 'key-points', 'references')
    for d in library.slide_layouts() + library.page_templates():
        cols, rows = library.SLIDE_GRID if d.target == 'slide' else library.PAGE_GRID
        names = [s.name for s in d.slots]
        assert len(names) == len(set(names)), d.id
        assert any(s.required for s in d.slots), d.id
        for s in d.slots:
            c, r, w, h = s.area
            assert 0 <= c and 0 <= r and w > 0 and h > 0 and c + w <= cols and r + h <= rows, (d.id, s.name)
            assert s.kind in plan.BOX_KINDS and set(s.accepts) <= set(plan.BOX_KINDS)
            assert (s.step is not None) == (s.kind == 'text'), (d.id, s.name)
            assert s.fit in (None, *plan.FIT)
        assert library.get(d.id) is d
    assert library.for_format('pptx') == library.SLIDE_LAYOUTS and library.for_format('pdf') == library.PAGE_TEMPLATES
    assert library.for_format('docx') == ()
    assert 'table' in library.slot_kinds('full-width-diagram')['figure']


# ---------- schemas ----------


def _sample_plan() -> plan.DesignPlan:
    ds = _ds()
    box = plan.Box(id='p0.title', kind='text', x=48, y=300, w=640, h=120, z=2, slot='title', content='spec:title',
                   style=plan.BoxStyle(text_color='text'), font='Inter', step='display', size=54, bold=True,
                   lines=['Photosynthesis'], overlay_ok=True)
    img = plan.Box(id='p0.image', kind='image', x=0, y=0, w=960, h=540, slot='image', content='ws:images/cover.png',
                   fit='cover', crop=(0.1, 0.0, 0.8, 1.0), focal=(0.5, 0.4), alt='A leaf in sunlight', bleed=True)
    page = plan.PagePlan(index=0, layout='cover-hero', w=960, h=540, boxes=[img, box])
    direction = plan.ArtDirection(preset='bold-dark', mood=['bold'], dark=True,
                                  pages=[plan.PageDirection(layout='cover-hero', image=0, emphasis='title')])
    return plan.DesignPlan(file_id='0123456789ab', format='pptx', preset='bold-dark', system=ds.to_dict(),
                           direction=direction.to_dict(), pages=[page],
                           fonts=[plan.FontUse('Inter', 'body', 'cache', 'OFL-1.1', False, fallback='Calibri')],
                           assets=[plan.AssetUse('ws:images/cover.png', 'image', 'commons', 'CC BY-SA 4.0', 'x')],
                           qa=[plan.QaResult('D1', True)], score=100, rounds=1, stop='pass')


def test_design_plan_schema_and_roundtrip():
    p = _sample_plan()
    d = json.loads(json.dumps(p.to_dict()))
    assert plan.validate(d, plan.design_plan_schema()) == []
    assert plan.DesignPlan.from_dict(d) == p
    bad = json.loads(json.dumps(d))
    bad['pages'][0]['boxes'][0]['kind'] = 'video'
    bad['pages'][0]['layout'] = 'hero'
    bad['pages'][0]['boxes'][1]['style']['text_color'] = '#FFFFFF'   # raw colours are never allowed
    problems = plan.validate(bad, plan.design_plan_schema())
    assert len(problems) == 3


def test_art_direction_schema():
    example = {'preset': 'bold-dark', 'mood': ['bold', 'cinematic'],
               'pages': [{'layout': 'cover-hero', 'image': 0, 'emphasis': 'title'}, {'layout': 'section-divider'},
                         {'layout': 'big-number', 'focus': 'block:1'}, {'layout': 'image-left-text', 'image': 2}]}
    assert plan.validate(example, plan.art_schema()) == []
    assert plan.validate({**example, 'source': 'llm'}, plan.art_schema()) != []          # code adds source
    assert plan.validate({**example, 'source': 'llm', 'notes': []}, plan.art_schema(llm=False)) == []
    assert plan.validate({**example, 'pages': [{'layout': 'cover-hero', 'x': 10}]}, plan.art_schema()) != []
    assert plan.ArtDirection.from_dict({**example, 'pages': [
        {'layout': 'cover-hero', 'image': 0, 'emphasis': 'title'}]}).pages[0].image == 0


def test_critic_schema():
    ok = {'edits': [{'page': 4, 'action': 'change_layout', 'arg': 'big-number'},
                    {'page': 2, 'action': 'reduce_text', 'arg': 30}, {'page': 1, 'action': 'swap_image', 'arg': None}]}
    assert plan.validate(ok, plan.critic_schema()) == []
    assert plan.validate({'edits': [{'page': 0, 'action': 'move_box'}]}, plan.critic_schema()) != []
    assert plan.validate({'edits': [{'page': 1, 'action': 'emphasize', 'arg': 'x', 'why': 'nice'}]},
                         plan.critic_schema()) != []   # structured edits only, no free text
    assert plan.validate({'edits': [ok['edits'][0]] * 13}, plan.critic_schema()) != []


def test_freeform_schema():
    ok = {'shapes': [{'type': 'rect', 'x': 0, 'y': 0, 'w': 12, 'h': 12, 'fill': 'bg'},
                     {'type': 'ellipse', 'x': 7.5, 'y': 1, 'w': 4, 'h': 4, 'fill': 'accent', 'opacity': 0.8},
                     {'type': 'text', 'x': 1, 'y': 6, 'w': 8, 'h': 3, 'text': 'Photosynthesis', 'step': 'display'},
                     {'type': 'path', 'x': 0, 'y': 9, 'w': 12, 'h': 3, 'points': [[0, 10], [6, 9], [12, 10]],
                      'stroke': 'accent2'},
                     {'type': 'icon', 'x': 1, 'y': 1, 'w': 1, 'h': 1, 'ref': 'icon:leaf'}]}
    assert plan.validate(ok, plan.freeform_schema()) == []
    for bad in ({'type': 'rect', 'x': 0, 'y': 0, 'w': 13, 'h': 1},          # outside the grid
                {'type': 'rect', 'x': 0.3, 'y': 0, 'w': 1, 'h': 1},         # off the half-unit grid
                {'type': 'rect', 'x': 0, 'y': 0, 'w': 1, 'h': 1, 'fill': 'FF0000'},   # raw colour
                {'type': 'svg', 'x': 0, 'y': 0, 'w': 1, 'h': 1}):
        assert plan.validate({'shapes': [bad]}, plan.freeform_schema()) != [], bad


def test_report_schema_and_score():
    assert tuple(qa.CHECKS) == plan.CHECK_IDS and sum(qa.WEIGHTS.values()) == 100
    assert set(qa.FIXES) == set(plan.CHECK_IDS)
    results = [plan.QaResult(i, True) for i in plan.CHECK_IDS]
    assert qa.score(results) == 100
    results += [plan.QaResult('D1', False, page=2, box='p2.body', value=140, threshold=120),
                plan.QaResult('D1', False, page=3), plan.QaResult('D7', False, fixed=True)]
    assert qa.score(results) == 80    # D1 counted once; a fixed failure costs nothing
    report = {'version': 1, 'score': 80, 'preset': 'bold-dark', 'format': 'pptx', 'fonts': [], 'rounds': 2,
              'stop': 'rounds', 'tokens': {'direct_out': 900},
              'checks': [{'id': i, 'name': qa.CHECKS[i][0], 'ok': i != 'D1', 'failures': int(i == 'D1'), 'note': ''}
                         for i in plan.CHECK_IDS],
              'results': [dataclasses.asdict(r) for r in results if not r.ok], 'layouts': {'cover-hero': 1},
              'fallbacks': [{'page': 5, 'from': 'freeform', 'to': 'cover-type', 'why': 'failed QA twice'}],
              'critic': {'ran': False, 'why': 'engine cannot read images'}, 'thumbs': 12,
              'phases': [{'phase': 'direct', 'calls': 1, 'llm_in': 800, 'llm_out': 900, 'ms': 2100}], 'notes': []}
    assert plan.validate(report, plan.report_schema()) == []
    assert plan.validate({**report, 'checks': report['checks'][:7]}, plan.report_schema()) != []
    assert plan.validate({**report, 'phases': [{'phase': 'render', 'calls': 0, 'llm_in': 0, 'llm_out': 0, 'ms': 1}]},
                         plan.report_schema()) != []


def test_new_diagram_block_schemas():
    assert plan.DIAGRAM_KINDS_NEW == ('cycle', 'venn', 'pyramid', 'matrix', 'mindmap', 'process', 'comparison',
                                      'labelled', 'stat-cards', 'scatter')
    assert not set(plan.DIAGRAM_KINDS_NEW) & {'timeline', 'tree', 'flow'}
    ok = {'type': 'stat-cards', 'title': 'Key facts', 'stats': [{'value': '72%', 'label': 'of energy'},
                                                                {'value': 3.2, 'label': 'metres'}]}
    assert plan.validate(ok, plan.DIAGRAM_BLOCK_SCHEMAS['stat-cards']) == []
    assert plan.validate({'type': 'venn', 'sets': [{'label': 'A'}]}, plan.DIAGRAM_BLOCK_SCHEMAS['venn']) != []


# ---------- the feature flag and today's output ----------


@pytest.mark.parametrize('raw,want', [(None, frozenset()), ('', frozenset()), ('0', frozenset()), ('off', frozenset()),
                                      ('1', frozenset({'pptx', 'pdf'})), ('on', frozenset({'pptx', 'pdf'})),
                                      ('pptx', frozenset({'pptx'})), ('pdf, docx,bogus', frozenset({'pdf', 'docx'}))])
def test_tg_studio_flag(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv('TG_STUDIO', raising=False)
    else:
        monkeypatch.setenv('TG_STUDIO', raw)
    assert config.studio_formats() == want
    assert agent.enabled('pptx') == ('pptx' in want)


def test_phase0_default_is_off(monkeypatch):
    monkeypatch.delenv('TG_STUDIO', raising=False)
    assert not any(agent.enabled(f) for f in cf.FORMATS)


SAMPLE = {'title': 'Photosynthesis', 'subtitle': 'Year 9 biology',
          'sections': [
              {'heading': 'What it is', 'level': 1, 'blocks': [
                  {'type': 'paragraph', 'text': 'Plants turn light, water and carbon dioxide into sugar and oxygen.'},
                  {'type': 'bullets', 'items': ['Happens in chloroplasts', 'Needs light', 'Releases oxygen']}],
               'notes': 'Start with a question.'},
              {'heading': 'Rate by light', 'level': 1, 'blocks': [
                  {'type': 'chart', 'kind': 'bar', 'title': 'Rate', 'labels': ['Low', 'High'],
                   'series': [{'name': 'Bubbles per minute', 'values': [4, 11]}]}]},
              {'heading': 'Stages', 'level': 1, 'blocks': [
                  {'type': 'flow', 'title': 'Stages', 'nodes': [{'id': 'a', 'label': 'Light reactions'},
                                                                {'id': 'b', 'label': 'Calvin cycle'}],
                   'edges': [{'from': 'a', 'to': 'b'}]},
                  {'type': 'table', 'columns': ['Input', 'Output'], 'rows': [['CO2', 'Glucose'], ['H2O', 'O2']]}]}]}


class _FrozenTime:
    """The time module for zipfile, with a fixed clock, so two renders of the same deck are byte-comparable."""

    def __getattr__(self, name):
        return getattr(time, name)

    @staticmethod
    def time() -> float:
        return 1_700_000_000.0


class _FrozenDatetime(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime.datetime(2023, 11, 14, 22, 13, 20, tzinfo=tz)


@pytest.mark.parametrize('fmt', ['pptx', 'pdf'])
@pytest.mark.parametrize('flag', [None, '0', 'off'])
def test_studio_off_keeps_todays_bytes(monkeypatch, fmt, flag):
    """With TG_STUDIO off, render() and the create agent's build() give exactly the legacy renderer's bytes."""
    from reportlab import rl_config
    from jevrouter.agents import create as ca
    monkeypatch.setattr(rl_config, 'invariant', 1)   # no timestamp or random id in the PDF
    monkeypatch.setattr(zipfile, 'time', _FrozenTime())  # PPTX zip entries carry the time they were written
    for mod in (xlsxwriter.workbook, xlsxwriter.core):  # a chart's embedded workbook is dated too
        monkeypatch.setattr(mod, 'datetime', _FrozenDatetime)
    if flag is None:
        monkeypatch.delenv('TG_STUDIO', raising=False)
    else:
        monkeypatch.setenv('TG_STUDIO', flag)
    norm, _ = cf.normalize(SAMPLE, fmt)
    legacy = render_mod._draw(norm, fmt, render_mod.theme_name(norm, None))
    assert cf.render(SAMPLE, fmt) == legacy
    _, _, data = ca.build(json.loads(json.dumps(SAMPLE)), fmt, source='llm')
    assert data == legacy

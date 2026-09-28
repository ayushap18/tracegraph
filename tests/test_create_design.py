"""Design files (docs/PLAN-files-robust.md section 4): parsed by code at 0 tokens into colours by role, fonts and a
radius, kept out of the model's context, turned into a theme with contrast repaired, and applied on every create path."""
import json
from pathlib import Path

import pytest

from jevrouter.agents import create as ca
from jevrouter.create import design as dm
from jevrouter.create import fonts, themes
from jevrouter.create.brief import parse_brief
from tests.test_create_longdoc import HEADS_2750, Q2750, SHORT, Deck2750, no_system_fonts  # noqa: F401 (fixture)

FIXTURES = Path(__file__).parent.parent / 'evals' / 'fixtures'
DESIGN = (FIXTURES / 'design_system.md').read_text()
MEETING = (FIXTURES / 'meeting_notes.txt').read_text()
DOC = ({'name': 'design_system.md', 'kind': 'text'}, DESIGN)


def tokens(text=DESIGN, name='design_system.md') -> dm.DesignTokens:
    t = dm.parse_design(text, name)
    assert t is not None
    return t


# ---------- parsing ----------

def test_the_fixture_gives_its_roles_fonts_and_radius():
    t = tokens()
    assert t.colors == {'bg': 'F7F4ED', 'surface': 'F7F4ED', 'text': '1C1C1C', 'heading': '1C1C1C', 'muted': '5F5F5D',
                        'border': 'ECEAE4', 'header_bg': '1C1C1C', 'header_text': 'FCFBF8'}
    assert t.body_font == dm.FontSpec('Camera Plain Variable', ['ui-sans-serif', 'system-ui', 'sans-serif'], 'sans')
    assert t.heading_font.family == 'Camera Plain Variable' and t.mono_font.category == 'mono'
    assert t.radius == 12 and t.hatch and '3B82F6' not in t.palette and 'FFFFFF' not in t.palette
    assert t.confidence == 1.0
    assert any('body text colours' in n and '#1C1C1C' in n and '#5F5F5D' in n for n in t.notes)


def test_colour_names_never_vote():
    """The same roles when the colour names carry no role words, and when they carry misleading ones."""
    plain = DESIGN
    for old, new in (('Cream Surface', 'Shade B'), ('Light Cream', 'Shade E'), ('Muted Gray', 'Shade D'),
                     ('Charcoal 82%', 'Shade F'), ('Charcoal', 'Shade C'), ('Off-White', 'Shade G'),
                     ('Cream', 'Shade A'), ('Ring Blue', 'Shade H')):
        plain = plain.replace(old, new)
    assert tokens(plain).colors == tokens().colors
    tricky = DESIGN.replace('**Cream**', '**Heading Border Accent**')
    assert tokens(tricky).colors['bg'] == 'F7F4ED' and tokens(tricky).colors['heading'] == '1C1C1C'


def test_css_variables_and_json_tokens():
    css = ':root {\n  --color-bg: #101418;\n  --color-text: #e8e8e8;\n  --color-heading: #ffffff;\n  --color-accent: ' \
          '#ff6b35;\n  --color-muted: #9aa0a6;\n}\nbody { font-family: "Inter", system-ui, sans-serif; }'
    t = tokens(css, 'theme.css')
    assert (t.colors['bg'], t.colors['text'], t.colors['heading'], t.colors['accent']) == \
        ('101418', 'E8E8E8', 'FFFFFF', 'FF6B35')
    assert t.body_font.family == 'Inter'
    data = {'color': {'background': {'value': '#ffffff'}, 'text': {'value': '#222222'},
                      'heading': {'value': '#0b3d91'}, 'brand': {'value': '#e11d48'}}}
    t = tokens(json.dumps(data), 'tokens.json')
    assert (t.colors['bg'], t.colors['text'], t.colors['heading']) == ('FFFFFF', '222222', '0B3D91')


def test_a_file_without_colours_is_not_a_design():
    assert dm.parse_design('# Notes\n\nWe met on Tuesday.', 'notes.md') is None
    assert dm.parse_design(MEETING, 'meeting_notes.txt') is None


# ---------- detection ----------

def test_detection():
    assert dm.is_design({'name': 'DESIGN-lovable.md'}, DESIGN, 'use the design.md for the design') >= 0.9
    assert dm.is_design({'name': 'design_system.md'}, DESIGN, Q2750) >= 0.9
    assert dm.is_design({'name': 'meeting_notes.txt'}, MEETING, 'use the design.md for the design') < 0.5
    design, content = dm.split_docs([DOC, ({'name': 'meeting_notes.txt'}, MEETING)], Q2750)
    assert [m['name'] for m, _ in design] == ['design_system.md'] and len(content) == 1
    design, content = dm.split_docs([DOC], 'summarise this design system in a pdf')
    assert design == [] and content == [DOC]


# ---------- themes and fonts ----------

def test_the_theme_from_the_design():
    spec = {'design': dm.to_spec(tokens())}
    t = themes.resolve(spec)
    assert (t['bg'], t['text'], t['heading'], t['muted'], t['border'], t['header_bg'], t['header_text']) == \
        ('F7F4ED', '1C1C1C', '1C1C1C', '5F5F5D', 'ECEAE4', '1C1C1C', 'FCFBF8')
    assert t['hatch'] and not t['grey_images'] and t['radius'] == 12 and t['nudged'] == []
    assert themes.worst_contrast(t)[0] >= 4.5
    assert themes.resolve({'theme': 'mono'})['hatch'] and themes.resolve({'theme': 'mono'})['grey_images']
    assert themes.resolve({}) is themes.THEMES['clean']
    assert themes.base_for({'colors': {'bg': '111111'}}) == 'dark' and themes.base_for(spec['design']) == 'warm'


def test_a_bad_design_is_nudged_to_4_5():
    t = themes.from_design({'colors': {'bg': 'FFFFFF', 'text': 'CCCCCC'}, 'palette': [], 'hatch': True})
    assert themes.contrast(t['text'], 'FFFFFF') >= 4.5 and 'text' in t['nudged']
    assert 'Body text was darkened slightly to stay readable.' in t['design_notes']
    assert themes.worst_contrast(t)[0] >= 4.5
    fixed = {r: t[r] for r in ('bg', 'text', 'muted', 'heading', 'accent', 'border', 'header_bg', 'header_text')}
    again = themes.from_design({'colors': fixed, 'palette': [], 'hatch': True})
    assert {r: again[r] for r in fixed} == fixed and again['nudged'] == []  # idempotent


def test_fonts_fall_back_honestly():
    t = themes.resolve({'design': dm.to_spec(tokens())})
    pdf, docx = fonts.resolve(None, 'pdf', theme=t), fonts.resolve(None, 'docx', theme=t)
    assert pdf.used == 'Helvetica' and docx.used == 'Calibri'
    assert pdf.note == "The design's font Camera Plain Variable isn't available here, so the file uses Helvetica."
    assert fonts.resolve(None, 'docx', stack=['Brand Serif', 'Georgia', 'serif'], category='serif').used == 'Georgia'
    assert fonts.resolve(None, 'pptx', stack=['Mystery', 'serif'], category='serif').used == 'Georgia'
    assert fonts.resolve('inter', 'docx', theme=t).used == 'Inter'  # a font the request names wins


def test_clean_design_rejects_junk():
    assert dm.clean_design(None) is None and dm.clean_design({'colors': {'bg': 'nope'}}) is None
    d = dm.clean_design({'name': '<x>', 'colors': {'bg': '#abc', 'evil': 'FFFFFF'}, 'radius': 99, 'palette': 'x'})
    assert d['colors'] == {'bg': 'AABBCC'} and d['radius'] == 24 and d['palette'] == [] and d['name'] == 'x'


# ---------- the create agent ----------

TEA = 'Tea is a drink made by steeping the cured leaves of Camellia sinensis in hot water. Green and black tea differ.'


async def test_design_only_with_a_prior_answer():
    job = ca.Job('put that in a pdf using the design.md', context=[{'query': 'what is tea', 'answer': TEA}],
                 docs=[DOC])
    made = await ca.make(job, None, None)
    assert made.ok and made.file['source'] == 'answer' and made.file['tokens'] == 0
    d = made.file['design']
    assert d['name'] == 'design_system.md' and d['colors']['bg'] == 'F7F4ED' and d['colors']['text'] == '1C1C1C'
    assert d['fonts_used'] == {'heading': 'Helvetica', 'body': 'Helvetica'} and d['body_font'] == 'Camera Plain Variable'
    assert made.file['brief']['design'] == 'design_system.md'
    assert "The design's font Camera Plain Variable isn't available here, so the file uses Helvetica." in made.caveats
    assert made.spec['design']['colors']['bg'] == 'F7F4ED'  # stored, so a conversion keeps it


async def test_design_only_with_a_last_file_restyles_at_no_cost():
    spec = {'title': 'Tea', 'sections': [{'heading': 'What tea is', 'blocks': [{'type': 'paragraph', 'text': TEA}]}]}
    meta, stored, _ = ca.build(spec, 'pptx', source='llm')
    job = ca.Job('restyle it with the attached design.md', docs=[DOC], last_file=(meta, stored, True))
    made = await ca.make(job, None, None)
    assert made.ok, made.answer
    assert made.file['source'] == 'convert' and made.file['format'] == 'pptx' and made.file['tokens'] == 0
    assert made.file['design']['colors']['bg'] == 'F7F4ED' and 'Restyled **' in made.answer


async def test_the_2750_shape_with_its_design():
    eng = Deck2750()
    job = ca.Job(Q2750, deps=[('Research how mobile phones evolved', SHORT)], docs=[DOC], brief=parse_brief(Q2750))
    made = await ca.make(job, eng, None)
    assert made.ok and made.file['slides'] == 12
    assert made.file['design']['colors']['bg'] == 'F7F4ED' and made.file['design']['colors']['text'] == '1C1C1C'
    prompts = '\n'.join(c['prompt'] + c['system'] for c in eng.calls)
    assert 'Color Palette' not in prompts and 'f7f4ed' not in prompts.lower() and 'Camera Plain' not in prompts
    assert 'Tea' not in prompts and HEADS_2750[0] in prompts


async def test_a_theme_word_wins_over_the_design():
    job = ca.Job('put that in a black and white pdf using the design.md',
                 context=[{'query': 'what is tea', 'answer': TEA}], docs=[DOC])
    made = await ca.make(job, None, None)
    assert made.ok and made.file['theme'] == 'mono' and not made.file.get('design')
    assert "The design's colours were not used because you asked for black and white." in made.caveats


async def test_a_design_named_but_not_attached():
    job = ca.Job('put that in a pdf using the design.md', context=[{'query': 'what is tea', 'answer': TEA}])
    made = await ca.make(job, None, None)
    assert made.ok and not made.file.get('design')
    assert ('You mentioned design.md, but no design file is attached to this message. Attach it and convert the file '
            'to apply it (0 tokens).') in made.caveats


async def test_a_design_from_an_earlier_turn_and_an_unreadable_one():
    job = ca.Job('put that in a pdf using the design.md', context=[{'query': 'what is tea', 'answer': TEA}],
                 design_docs=[DOC])
    made = await ca.make(job, None, None)
    assert made.file['design']['colors']['bg'] == 'F7F4ED'
    job = ca.Job('put that in a pdf using the design.md', context=[{'query': 'what is tea', 'answer': TEA}],
                 docs=[({'name': 'design.md'}, '# Design\n\nCalm and friendly.')])
    made = await ca.make(job, None, None)
    assert made.ok and not made.file.get('design')
    assert dm.unreadable_note('design.md') in made.caveats


@pytest.mark.parametrize('fmt', ['md', 'docx', 'xlsx'])
async def test_every_format_records_the_design(fmt):
    job = ca.Job(f'put that in a {"markdown" if fmt == "md" else fmt} file using the design.md',
                 context=[{'query': 'what is tea', 'answer': TEA}], docs=[DOC])
    made = await ca.make(job, None, None)
    assert made.ok and made.file['format'] == fmt and made.file['design']['colors']['bg'] == 'F7F4ED'
    if fmt == 'md':
        assert any('A Markdown file carries no colours or fonts' in c for c in made.caveats)
    else:
        assert made.file['design']['fonts_used']['body'] == 'Calibri'

"""The brief (docs/PLAN-accuracy-v2.md C1): what a request asks of a created file, read from the query and the step text
with no LLM, and how the query's brief and the step's merge."""
import json
from pathlib import Path

import pytest

from jevrouter.agents import create as create_agent
from jevrouter.create.brief import (KNOWN_FONTS, MAX_PAGES, Brief, asks_more, describe, diagrams_min, font_of,
                                    from_dict, merge, parse_brief, to_dict)

# The prompt as the plan quotes it, and as run 2741 stored it (with its typo, "ebsources").
PROMPT_2741 = ('pdf on the ai what is ai how ai begins using the multiple diagrams also add the images from the web '
               'sources build 12-13 page of pdf properly using the black white text also anthropic sans font also keep '
               'the sizing properly and all images are to be sourced from the best sources')
FIXTURE = Path(__file__).parent / 'fixtures' / 'run2741.json'


@pytest.mark.parametrize('prompt', [PROMPT_2741, json.loads(FIXTURE.read_text())['record']['text']])
def test_the_2741_prompt(prompt):
    b = parse_brief(prompt)
    assert (b.format, b.pages, b.theme, b.font) == ('pdf', (12, 13), 'mono', 'anthropic sans')
    assert b.images and b.image_source == 'web' and b.diagrams
    assert len(b.diagram_kinds) >= 2 and diagrams_min(b) == 2  # "multiple diagrams": at least two
    assert not b.capped and b.words is None and asks_more(b)


@pytest.mark.parametrize('text,field,want', [
    ('a 10 page report on bees', 'pages', (10, 10)),
    ('a 20-slide deck on Mars', 'slides', (20, 20)),
    ('10 slides about the moon', 'slides', (10, 10)),
    ('twelve pages on Rome', 'pages', (12, 12)),
    ('twenty-five pages on Rome', 'pages', (25, 25)),
    ('12 to 13 pages', 'pages', (12, 13)),
    ('12–13 pages', 'pages', (12, 13)),
    ('5 or 6 pages', 'pages', (5, 6)),
    ('at least 5 pages', 'pages', (5, 5)),
    ('in b&w', 'theme', 'mono'),
    ('greyscale please', 'theme', 'mono'),
    ('grayscale', 'theme', 'mono'),
    ('black-and-white', 'theme', 'mono'),
    ('monochrome slides', 'theme', 'mono'),
    ('b/w', 'theme', 'mono'),
    ('in dark mode', 'theme', 'dark'),
    ('a warm theme', 'theme', 'warm'),
    ('use Inter font', 'font', 'inter'),
    ('a font called Fira Code', 'font', 'fira code'),
    ('set it in Comic Neue font', 'font', 'comic neue'),
    ('a bigger font please', 'font', None),
    ('font size 12', 'font', None),
    ('a 2,000-word essay', 'words', 2000),
    ('about 1500 words', 'words', 1500),
    ('a flowchart of the login', 'diagram_kinds', ['flow']),
    ('with a timeline', 'diagram_kinds', ['timeline']),
    ('show the hierarchy', 'diagram_kinds', ['tree']),
    ('a report with charts', 'diagram_kinds', []),
    ('page 3 of the doc', 'pages', None),
    ('with photos', 'image_source', None),
    ('with photos from the web', 'image_source', 'web'),
])
def test_paraphrases(text, field, want):
    assert getattr(parse_brief(text), field) == want


def test_caps_and_flags():
    b = parse_brief('60 pages on x')
    assert b.pages == (MAX_PAGES, MAX_PAGES) and b.capped
    assert parse_brief('a 50 slide deck').slides == (40, 40) and parse_brief('a 50 slide deck').capped
    assert parse_brief('a report with charts').diagrams and diagrams_min(parse_brief('a report with charts')) == 1
    assert parse_brief('') == Brief() and not asks_more(Brief()) and not asks_more(None)
    assert asks_more(parse_brief('with pictures')) and not asks_more(parse_brief('a 2 page PDF'))


def test_font_takes_the_named_family_not_the_words_before_it():
    assert font_of('using the black white text also anthropic sans font') == 'anthropic sans'
    assert font_of('the black white text also Acme Grotesk font') == 'acme grotesk'
    assert all(font_of(f'use {f} please') == f for f in KNOWN_FONTS)
    assert font_of('a serif font') is None and font_of('no fonts at all') is None


def test_merge_the_query_wins_and_asks_add_up():
    query, step = parse_brief(PROMPT_2741), parse_brief('Create a properly formatted 3 page Markdown file with images')
    m = merge(query, step)
    assert (m.format, m.pages, m.theme, m.font) == ('pdf', (12, 13), 'mono', 'anthropic sans')
    assert merge(Brief(), step).format == 'md' and merge(Brief(), parse_brief('with a timeline')).diagrams


def test_brief_as_json_and_prompt_lines():
    b = parse_brief(PROMPT_2741)
    d = to_dict(b)
    assert d['pages'] == [12, 13] and set(d) == set(Brief.__dataclass_fields__) and json.dumps(d)
    assert from_dict(d) == b and from_dict(None) is None and to_dict(None) is None
    lines = describe(b, 'pdf')
    assert lines[0] == 'Length: 12-13 pages, about 5,200 words'
    assert any(ln.startswith('Include at least 2 diagrams: ') for ln in lines)
    assert any('Wikimedia Commons' in ln for ln in lines) and any('Black and white' in ln for ln in lines)
    assert describe(parse_brief('a 20-slide deck'), 'pptx') == ['Length: 20 slides']


def test_the_create_agent_reads_themes_the_same_way():
    assert create_agent.theme_of('make it black and white') == 'mono'
    assert create_agent.theme_of('in dark mode') == 'dark' and create_agent.theme_of('plain') is None


@pytest.mark.parametrize('text', ['put these sales figures in a pdf', 'put that in a pdf about graph theory',
                                  'make a pdf summary of the image processing answer', 'make slides about charts',
                                  'a pdf with key figures'])
def test_images_or_diagrams_named_as_the_topic_are_not_asked_for(text):
    b = parse_brief(text)
    assert not b.images and not b.diagrams and not asks_more(b)


@pytest.mark.parametrize('text', ['a pdf on image compression with diagrams', 'a deck with 3 diagrams of the process',
                                  'include images of the Eiffel Tower', 'with photos from the web'])
def test_images_or_diagrams_asked_for_as_content_still_count(text):
    b = parse_brief(text)
    assert b.images or b.diagrams


@pytest.mark.parametrize('text', ['write a report on the history of fonts as a pdf', 'Make a PDF about fonts',
                                  'a docx comparing popular web fonts', 'make slides explaining how font rendering works'])
def test_a_request_about_fonts_names_no_font(text):
    assert font_of(text) is None


# ---------- docs/PLAN-files-robust.md 4.2: pictures asked for in other words ----------

@pytest.mark.parametrize('text', [
    'create the ppt on the how mobile phone is being evolved history past present everything a ppt of 12 slides using '
    'the multiple pictured diagrams and also use the design.md for the design',
    'an illustrated ppt on bridges',
    'a pictorial pdf about birds',
    'slides with visuals on the moon',
])
def test_pictures_in_other_words(text):
    assert parse_brief(text).images


@pytest.mark.parametrize('text', ['a report on image processing', 'a pdf about visual design', 'the illustrated history'])
def test_pictures_named_as_the_topic(text):
    assert not parse_brief(text).images


def test_the_design_name_rides_along_but_is_never_described():
    b = Brief(format='pptx', slides=(12, 12), design='DESIGN-lovable.md')
    assert to_dict(b)['design'] == 'DESIGN-lovable.md' and from_dict(to_dict(b)) == b
    assert not any('design' in line.lower() for line in describe(b, 'pptx'))
    assert merge(Brief(), Brief(design='d.md')).design == 'd.md'

"""The art director (docs/PLAN-designer.md 3.1 and 9.7): picks a preset and one layout per slide/section from the
library. Keyless rules always work; the model call (<= STUDIO_DIRECT_TOKENS out) only chooses among library ids and
falls back to the rules on any failure. It never writes coordinates, colours or fonts.

Owner: builder L.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, replace

from . import library
from .plan import EMPHASIS, ArtDirection, PageDirection, art_schema
from .tokens import DesignSystem

SYSTEM = ('You are the art director of a student document. Choose a preset and, for each page in the outline, one '
          'layout id from the allowed list. Reply with JSON matching the schema only. Never invent layouts, colours, '
          'fonts or coordinates.')

TIMELINE_KINDS = ('timeline', 'process')
STRIP_KINDS = ('timeline', 'process', 'cycle', 'flow')
CLOSING_WORDS = re.compile(r'^\s*(thank(s| you)|questions\??|any questions|q\s?&\s?a|the end)\b', re.I)
REFERENCE_WORDS = re.compile(r'\b(references|sources|bibliography|works cited|citations|credits|further reading)\b',
                             re.I)
SUMMARY_WORDS = re.compile(r'\b(summary|key points|key takeaways|takeaways|conclusions?|recap|in short|at a glance)\b',
                           re.I)
MAX_RUN = 3                      # D7: no layout on more than 3 consecutive slides
MIN_LAYOUTS = 4                  # D7: 8+ slides use at least 4 layouts


@dataclass
class PageOutline:
    index: int                       # 0-based page (pptx: slide; pdf: section start)
    section: int | None              # DocSpec section index (None for Studio-made cover/closing/credits)
    heading: str
    level: int
    blocks: list[str]                # block types in order
    words: int                       # words of visible text
    bullets: int                     # bullet items
    images: list[int] = field(default_factory=list)   # indexes into the spec's image blocks
    stats: int = 0                   # short numeric facts ("72%", "3.2 m") found in the text
    diagrams: list[str] = field(default_factory=list) # diagram kinds present
    has_chart: bool = False
    has_table: bool = False
    has_quote: bool = False
    parallel: bool = False           # amendment (L): two bullet lists with headings (comparison)
    texts: int = 0                   # amendment (L): text items (paragraphs, bullet items, quotes)
    table_cols: int = 0              # amendment (L): columns of the widest table


@dataclass
class Outline:
    title: str
    fmt: str
    pages: list[PageOutline]
    images: int                      # image blocks in the spec


def outline_of(spec: dict, fmt: str) -> Outline:
    """The normalized spec's shape per page, plus a cover (pptx/pdf) and a closing slide (pptx decks of 6+ slides)."""
    from .layout import parallel_lists, section_items, stat_items
    from ..create.spec import DIAGRAMS, strip_emphasis
    from .plan import DIAGRAM_KINDS_NEW
    if not isinstance(spec, dict):
        raise TypeError('outline_of needs a normalized spec')
    sections = spec.get('sections') or []
    title = str(spec.get('title') or '')
    kinds = set(DIAGRAMS) | set(DIAGRAM_KINDS_NEW)
    images = [(s, b) for s, sec in enumerate(sections) for b, blk in enumerate(sec.get('blocks') or [])
              if blk.get('type') == 'image']
    pages: list[PageOutline] = []
    if fmt in ('pptx', 'pdf'):
        pages.append(PageOutline(index=0, section=None, heading=title, level=0, blocks=[],
                                 words=len(f"{title} {spec.get('subtitle') or ''}".split()), bullets=0,
                                 images=[0] if images else []))
    for s, sec in enumerate(sections):
        blocks = [str(b.get('type')) for b in sec.get('blocks') or []]
        texts, visuals = section_items(spec, s)
        words = sum(len(strip_emphasis(i.text).split()) for i in texts if i.role in ('para', 'bullet', 'quote', 'by'))
        tables = [blk for _, blk in visuals if blk.get('type') == 'table']
        pages.append(PageOutline(
            index=len(pages), section=s, heading=str(sec.get('heading') or ''), level=int(sec.get('level') or 1),
            blocks=blocks, words=words, bullets=sum(1 for i in texts if i.role == 'bullet'),
            images=[k for k, (a, _) in enumerate(images) if a == s],
            stats=len(stat_items(spec, s)),
            diagrams=[t for t in blocks if t in kinds],
            has_chart='chart' in blocks, has_table='table' in blocks, has_quote='quote' in blocks,
            parallel=parallel_lists(spec, s) is not None,
            texts=sum(1 for i in texts if i.role in ('para', 'bullet', 'quote', 'code')),
            table_cols=max((len(t.get('columns') or []) for t in tables), default=0)))
    if fmt == 'pptx' and sections and len(pages) + 1 >= 6 and not CLOSING_WORDS.match(pages[-1].heading):
        pages.append(PageOutline(index=len(pages), section=None, heading='Thank you', level=0, blocks=[], words=2,
                                 bullets=0))
    return Outline(title=title, fmt=fmt, pages=pages, images=len(images))


# ---------- what a layout needs ----------


def fits_layout(layout: str, op: PageOutline, outline: Outline) -> bool:
    """True when the page's content can fill the layout's required slots."""
    fmt = outline.fmt
    if layout not in library.for_format(fmt):
        return False
    cover = op.section is None and op.index == 0
    closing = op.section is None and op.index > 0
    fam = library.get(layout).family
    if cover:
        if fmt == 'pdf':
            return layout == 'cover'
        return layout == 'cover-type' or (layout == 'cover-hero' and outline.images > 0)
    if closing:
        return layout == 'closing'
    if fam == 'cover' or layout == 'cover':
        return False
    visuals = op.has_chart or op.has_table or bool(op.diagrams) or bool(op.images)
    if fmt == 'pptx':
        return {
            'closing': bool(CLOSING_WORDS.match(op.heading)),
            'section-divider': True,
            'title-bullets': True,
            'image-left-text': bool(op.images),
            'image-right-text': bool(op.images),
            'full-bleed-image-caption': bool(op.images),
            'big-number': op.stats >= 1,
            'stat-cards': op.stats >= 2 or 'stat-cards' in op.diagrams,
            'quote': op.has_quote,
            'two-column': op.texts >= 2 or (op.texts >= 1 and visuals),
            'comparison': op.parallel,
            'full-width-diagram': visuals,
            'chart-focus': op.has_chart or op.has_table,
            'timeline-strip': any(k in STRIP_KINDS for k in op.diagrams),
        }.get(layout, False)
    return {
        'chapter-opener': True,
        'text-side-figure': visuals,
        'two-column-text': True,
        'full-figure': visuals,
        'pull-quote': op.has_quote,
        'key-points': op.bullets >= 2,
        'references': True,
    }.get(layout, False)


def content_layout(op: PageOutline, outline: Outline, fmt: str, *, avoid=(), image_side: int = 0,
                   hierarchical: bool = False) -> str:
    """The keyless rule for one section page (docstring of direct_keyless)."""
    def ok(layout):
        return layout not in avoid and fits_layout(layout, op, outline)
    if fmt == 'pdf':
        for layout, cond in (
                ('references', bool(REFERENCE_WORDS.search(op.heading))),
                ('key-points', bool(SUMMARY_WORDS.search(op.heading)) and op.bullets >= 2),
                ('full-figure', op.has_chart and op.words < 40),
                ('two-column-text', (op.has_table or op.has_chart) and op.words >= 40),   # text, then the figure page
                ('full-figure', op.has_table or op.has_chart),
                ('text-side-figure', (bool(op.images) or bool(op.diagrams)) and op.words >= 10),
                ('full-figure', bool(op.images) or bool(op.diagrams)),
                ('pull-quote', op.has_quote and op.words >= 80),
                ('chapter-opener', op.level == 1 and (hierarchical or op.words >= 250)),
                ('two-column-text', True)):
            if cond and ok(layout):
                return layout
        return 'two-column-text'
    only_quote = op.has_quote and op.texts <= 1
    strip = [k for k in op.diagrams if k in TIMELINE_KINDS]
    side = ('image-left-text', 'image-right-text')[image_side % 2]
    for layout, cond in (
            ('closing', bool(CLOSING_WORDS.match(op.heading)) and op.words <= 30 and not op.images),
            ('chart-focus', op.has_chart),
            ('stat-cards', 'stat-cards' in op.diagrams),
            ('timeline-strip', bool(strip)),
            ('full-width-diagram', bool(op.diagrams) or op.has_table),
            ('big-number', op.stats == 1 and op.words <= 60 and not op.images),
            ('stat-cards', 2 <= op.stats <= 4 and op.stats * 2 >= max(op.bullets, 1) and not op.images),
            ('quote', only_quote),
            ('full-bleed-image-caption', bool(op.images) and op.texts == 0),
            (side, bool(op.images) and op.words <= 50),
            ('two-column', bool(op.images)),
            ('comparison', op.parallel),
            ('title-bullets', True)):
        if cond and ok(layout):
            return layout
    return 'title-bullets' if 'title-bullets' not in avoid else 'two-column'


def _keyless_pages(outline: Outline) -> list[PageDirection]:
    fmt = outline.fmt
    pages: list[PageDirection] = []
    sections = [op for op in outline.pages if op.section is not None]
    hierarchical = any(op.level > 1 for op in sections)
    level1_parents = {op.index for k, op in enumerate(outline.pages)
                      if op.section is not None and op.level == 1 and k + 1 < len(outline.pages)
                      and outline.pages[k + 1].section is not None and outline.pages[k + 1].level > 1}
    side = 0
    since_divider = 0
    for op in outline.pages:
        if op.section is None:
            if op.index == 0:
                layout = 'cover' if fmt == 'pdf' else ('cover-hero' if outline.images else 'cover-type')
                pages.append(PageDirection(layout=layout, image=0 if outline.images and layout in ('cover-hero', 'cover')
                                           else None, emphasis='title'))
            else:
                pages.append(PageDirection(layout='closing'))
            continue
        if fmt == 'pptx' and len(outline.pages) >= 10 and op.index in level1_parents and since_divider >= 2:
            pages.append(PageDirection(layout='section-divider', emphasis='title'))
            since_divider = 0
            continue
        since_divider += 1
        layout = content_layout(op, outline, fmt, image_side=side, hierarchical=hierarchical)
        if layout in ('image-left-text', 'image-right-text'):
            side += 1
        pages.append(PageDirection(layout=layout, image=op.images[0] if op.images and _uses_image(layout) else None,
                                   emphasis=_emphasis(layout), focus=_focus_of(layout, op)))
    return _variety(pages, outline)


def _uses_image(layout: str) -> bool:
    return layout in ('cover-hero', 'image-left-text', 'image-right-text', 'full-bleed-image-caption', 'cover',
                      'text-side-figure', 'full-figure', 'two-column')


def _emphasis(layout: str) -> str | None:
    return {'big-number': 'number', 'stat-cards': 'number', 'quote': 'quote', 'chart-focus': 'chart',
            'full-width-diagram': 'diagram', 'timeline-strip': 'diagram', 'full-bleed-image-caption': 'image',
            'image-left-text': 'image', 'image-right-text': 'image', 'full-figure': 'diagram',
            'text-side-figure': 'image', 'pull-quote': 'quote'}.get(layout)


def _focus_of(layout: str, op: PageOutline) -> str | None:
    want = {'chart-focus': ('chart', 'table'), 'quote': ('quote',), 'pull-quote': ('quote',),
            'timeline-strip': STRIP_KINDS}.get(layout)
    if want:
        for k, t in enumerate(op.blocks):
            if t in want and k < 100:
                return f'block:{k}'
    return None


def _alternatives(op: PageOutline, outline: Outline) -> list[str]:
    fmt = outline.fmt
    order = ('two-column', 'title-bullets', 'image-right-text', 'image-left-text', 'full-width-diagram',
             'comparison', 'big-number', 'stat-cards', 'quote', 'chart-focus', 'timeline-strip',
             'full-bleed-image-caption') if fmt == 'pptx' else \
        ('two-column-text', 'chapter-opener', 'text-side-figure', 'full-figure', 'pull-quote', 'key-points')
    return [x for x in order if fits_layout(x, op, outline)]


def _variety(pages: list[PageDirection], outline: Outline) -> list[PageDirection]:
    """D7 for decks: no layout on more than MAX_RUN consecutive slides; 8+ slides use MIN_LAYOUTS layouts or more
    when the content allows it."""
    if outline.fmt != 'pptx':
        return pages
    ops = outline.pages
    for i in range(len(pages)):
        if i >= MAX_RUN and all(pages[i - k].layout == pages[i].layout for k in range(1, MAX_RUN + 1)):
            op = ops[i]
            if op.section is None:
                continue
            alt = next((x for x in _alternatives(op, outline) if x != pages[i].layout), None)
            if alt:
                pages[i] = replace(pages[i], layout=alt, emphasis=_emphasis(alt), focus=_focus_of(alt, op),
                                   image=op.images[0] if op.images and _uses_image(alt) else None)
    if len(pages) >= 8:
        for i in range(len(pages)):
            if len({p.layout for p in pages}) >= MIN_LAYOUTS:
                break
            op = ops[i]
            if op.section is None:
                continue
            counts = {}
            for p in pages:
                counts[p.layout] = counts.get(p.layout, 0) + 1
            if counts[pages[i].layout] < 2:
                continue
            alt = next((x for x in _alternatives(op, outline) if x not in counts), None)
            if alt:
                pages[i] = replace(pages[i], layout=alt, emphasis=_emphasis(alt), focus=_focus_of(alt, op),
                                   image=op.images[0] if op.images and _uses_image(alt) else None)
    return pages


def direct_keyless(outline: Outline, ds: DesignSystem, *, dark: bool | None = None) -> ArtDirection:
    """Rules by content shape: first page cover (cover-hero with an image, else cover-type); one stat -> big-number;
    2-4 stats -> stat-cards; an image and <= 4 bullets -> image-left/right-text (alternating); a timeline or process ->
    timeline-strip; another diagram or a table -> full-width-diagram; a chart -> chart-focus; a quote -> quote; two
    parallel lists -> comparison; every 3-4 slides in decks of 10+ -> section-divider; the last -> closing; otherwise
    title-bullets. Then D7 variety (no layout > 3 in a row, 4+ layouts in 8+ slides). source='keyless'."""
    from .presets import DEFAULT_PRESET, PRESETS
    preset = ds.id if ds is not None and ds.id in PRESETS else DEFAULT_PRESET
    return ArtDirection(preset=preset, mood=[], dark=dark if dark is not None else (ds.dark if ds is not None else None),
                        pages=_keyless_pages(outline), source='keyless')


# ---------- the model call ----------

_LAYOUT_NEEDS = {
    'cover-hero': 'first page only; needs an image', 'cover-type': 'first page only',
    'section-divider': 'a section title slide; the section content follows on the next slide',
    'title-bullets': 'any text', 'image-left-text': 'an image and short text', 'image-right-text': 'an image and '
    'short text', 'full-bleed-image-caption': 'an image and almost no text', 'big-number': 'one statistic',
    'stat-cards': '2 to 4 statistics', 'quote': 'a quote', 'two-column': 'two text parts, or text and a visual',
    'comparison': 'two lists with headings', 'full-width-diagram': 'a diagram, table or chart',
    'chart-focus': 'a chart or table', 'timeline-strip': 'a timeline, process, cycle or flow',
    'closing': 'last page only',
    'cover': 'first page only', 'chapter-opener': 'a long or top-level section', 'text-side-figure': 'text and one '
    'figure', 'two-column-text': 'any text', 'full-figure': 'a figure, little text', 'pull-quote': 'text with a quote',
    'key-points': 'a summary with bullets', 'references': 'sources and credits'}


def _prompt(outline: Outline, ds: DesignSystem, mood) -> str:
    from .presets import PRESETS
    pages = []
    for op in outline.pages:
        kind = 'cover' if op.section is None and op.index == 0 else 'closing' if op.section is None else 'section'
        d = {'page': op.index, 'kind': kind, 'heading': op.heading[:70], 'level': op.level, 'words': op.words}
        for k in ('bullets', 'stats'):
            if getattr(op, k):
                d[k] = getattr(op, k)
        if op.images:
            d['images'] = op.images
        if op.diagrams:
            d['diagrams'] = op.diagrams
        for k in ('has_chart', 'has_table', 'has_quote', 'parallel'):
            if getattr(op, k):
                d[k.replace('has_', '')] = True
        blocks = [b for b in op.blocks if b not in ('paragraph', 'bullets')]
        if blocks:
            d['blocks'] = op.blocks[:12]
        pages.append(d)
    layouts = {i: _LAYOUT_NEEDS.get(i, '') for i in library.for_format(outline.fmt)}
    brief = {
        'format': outline.fmt, 'title': outline.title[:120], 'current_preset': ds.id if ds is not None else None,
        'dark': ds.dark if ds is not None else None, 'mood_words': list(mood or [])[:6], 'presets': list(PRESETS),
        'layouts': layouts, 'image_count': outline.images, 'pages': pages,
        'rules': ['exactly one entry per page, in order', 'use layouts only from the list, and only when the page has '
                  'what the layout needs', 'vary layouts: never the same layout on more than 3 pages in a row',
                  'image is an index into the image list (0-based) or null', 'focus is "block:<n>" (0-based block in '
                  'the section) or null', 'at most 2 pages freeform', 'keep the current preset unless the mood '
                  'words clearly ask for another'],
    }
    return 'Plan the layout of this document.\n' + json.dumps(brief, ensure_ascii=False, separators=(',', ':'))


async def direct_llm(outline: Outline, ds: DesignSystem, engine, *, mood: list[str] = (), max_tokens: int = 2000,
                     effort: str = 'low') -> tuple[ArtDirection, dict]:
    """(direction, usage {calls, llm_in, llm_out, ms}). One engine.stream call with plan.art_schema(llm=True); invalid
    or failed replies fall back to direct_keyless (usage still counted) with a note. source='llm'."""
    from ..engines.base import parse_json
    t0 = time.monotonic()
    usage = {'calls': 0, 'llm_in': 0, 'llm_out': 0, 'ms': 0}
    try:
        if engine is None:
            raise ValueError('no engine')
        usage['calls'] = 1
        reply = await engine.stream(system=SYSTEM, prompt=_prompt(outline, ds, mood), effort=effort,
                                    max_tokens=max(256, min(int(max_tokens), 2000)), schema=art_schema(llm=True))
        usage['llm_in'] = int(getattr(reply, 'input_tokens', 0) or 0)
        usage['llm_out'] = int(getattr(reply, 'output_tokens', 0) or 0)
        raw = parse_json(getattr(reply, 'text', '') or '')
        if not isinstance(raw, dict) or not isinstance(raw.get('pages'), list) or not raw['pages']:
            raise ValueError('the reply is not an art direction')
        direction, notes = validate_direction(raw, outline, outline.fmt)
        if raw.get('dark') is None and ds is not None:
            direction.dark = ds.dark
        direction.source = 'llm'
        direction.notes = notes
    except Exception as e:  # any failure: the rules decide, and the answer says so
        direction = direct_keyless(outline, ds)
        why = type(e).__name__ if not str(e) else str(e)[:120]
        direction.notes.append(f"The art director's reply could not be used ({why}), so layouts were chosen by rules.")
    usage['ms'] = int((time.monotonic() - t0) * 1000)
    return direction, usage


def validate_direction(raw: dict, outline: Outline, fmt: str) -> tuple[ArtDirection, list[str]]:
    """A model's direction made valid: unknown layouts replaced by the keyless choice, page count matched to the
    outline, layouts whose required slots the content can't fill swapped, freeform limited to 2 pages."""
    from .presets import DEFAULT_PRESET, PRESETS
    raw = raw if isinstance(raw, dict) else {}
    notes: list[str] = []
    keyless = _keyless_pages(outline)
    preset = raw.get('preset')
    if preset not in PRESETS:
        if preset is not None:
            notes.append(f'Unknown preset {str(preset)[:40]!r} replaced with {DEFAULT_PRESET}.')
        preset = DEFAULT_PRESET
    mood = [str(m)[:24] for m in raw.get('mood') or [] if isinstance(m, str) and m.strip()][:4] \
        if isinstance(raw.get('mood'), list) else []
    dark = raw.get('dark') if isinstance(raw.get('dark'), bool) else None
    got = raw.get('pages') if isinstance(raw.get('pages'), list) else []
    if len(got) != len(outline.pages):
        notes.append(f'The direction had {len(got)} pages for {len(outline.pages)}; the rest follow the rules.')
    out: list[PageDirection] = []
    swapped = 0
    for i, op in enumerate(outline.pages):
        k = keyless[i]
        r = got[i] if i < len(got) and isinstance(got[i], dict) else None
        if r is None:
            out.append(k)
            continue
        layout = r.get('layout')
        if not isinstance(layout, str) or not fits_layout(layout, op, outline):
            if not (layout == 'section-divider' and fmt == 'pptx' and op.section is not None):
                swapped += 1
                layout = k.layout
        image = r.get('image')
        if not (isinstance(image, int) and not isinstance(image, bool) and 0 <= image < outline.images):
            image = k.image
        if op.section is not None and image is not None and op.images and image not in op.images:
            image = op.images[0]
        if _uses_image(layout) and image is None and op.images:
            image = op.images[0]
        emphasis = r.get('emphasis') if r.get('emphasis') in EMPHASIS else None
        focus = r.get('focus')
        m = re.match(r'^block:(\d{1,2})$', str(focus or ''))
        if not m or int(m.group(1)) >= len(op.blocks):
            focus = k.focus if layout == k.layout else _focus_of(layout, op)
        variant = r.get('variant') if r.get('variant') in ('default', 'compact') else 'default'
        freeform = r.get('freeform') is True
        out.append(PageDirection(layout=layout, image=image, emphasis=emphasis, focus=focus, variant=variant,
                                 freeform=freeform))
    if swapped:
        notes.append(f'{swapped} layout choice{"s" if swapped != 1 else ""} did not suit the content and '
                     f'{"were" if swapped != 1 else "was"} replaced.')
    free = [i for i, p in enumerate(out) if p.freeform]
    for i in free[2:]:
        out[i] = replace(out[i], freeform=False)
    if len(free) > 2:
        notes.append('Only 2 pages may be freeform; the others use library layouts.')
    before = [p.layout for p in out]
    out = _variety(out, outline)
    if [p.layout for p in out] != before:
        notes.append('Some layouts were changed so the deck does not repeat one layout too often.')
    return ArtDirection(preset=preset, mood=mood, dark=dark, pages=out, source='llm', notes=list(notes)), notes

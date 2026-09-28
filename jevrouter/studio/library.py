"""The layout library (docs/PLAN-designer.md 3.5 and 9.6): 16 slide layouts and 8 print page templates, each a set of
named slots on a grid. The data here is contract; the layout engine (layout.py) fills and fits the slots.

Grid: slides are 960 x 540 pt, 12 columns x 12 rows inside the preset's slide margin; print pages (A4/Letter/A3/A2) are
12 columns x 16 rows inside the page margin. A slot's area is (col, row, cols, rows), 0-based. A slot with bleed=True is
placed on the same grid stretched over the whole page (no margins): full-bleed photos, overlays and art.

Compact variant (every layout): the first text slot of step h1/h2 loses one row, the slots below it move up one row
and grow by one, and every text slot is one type step smaller (never below the preset minimum).

Owner: builder L (data frozen; additive changes only, announced to every builder).
"""
from __future__ import annotations

from dataclasses import dataclass

from .plan import BOX_KINDS, TYPE_STEPS

SLIDE_GRID = (12, 12)
PAGE_GRID = (12, 16)


@dataclass(frozen=True)
class Slot:
    name: str
    kind: str                                  # BOX_KINDS: the primary kind
    area: tuple[int, int, int, int]            # (col, row, cols, rows)
    step: str | None = None                    # TYPE_STEPS for text slots
    accepts: tuple[str, ...] = ()              # other kinds the slot takes (a figure slot takes chart/table/image)
    required: bool = False                     # the layout is unusable without it
    bleed: bool = False
    fit: str | None = None                     # 'cover' | 'contain' for image-like slots
    overlay_ok: bool = False                   # may sit on top of another slot by design
    align: str = 'left'
    max_words: int | None = None               # text slots: the budget before rebalance/split
    flow: bool = False                         # print text that continues onto the next page


@dataclass(frozen=True)
class LayoutDef:
    id: str
    family: str                                # swap_layout stays within the family (next_best)
    target: str                                # 'slide' | 'page'
    slots: tuple[Slot, ...]
    asymmetric: bool = False                   # D5 balance is skipped
    description: str = ''

    def slot(self, name: str) -> Slot | None:
        return next((s for s in self.slots if s.name == name), None)


def _s(name, kind, area, step=None, **kw) -> Slot:
    return Slot(name, kind, tuple(area), step, **kw)


_FIG = ('diagram', 'chart', 'table', 'image')
_TXT_FIG = ('table', 'chart', 'image', 'diagram')

_SLIDES = (
    LayoutDef('cover-hero', 'cover', 'slide', (
        _s('image', 'image', (0, 0, 12, 12), bleed=True, fit='cover', required=True),
        _s('overlay', 'shape', (0, 0, 12, 12), bleed=True, overlay_ok=True),
        _s('title', 'text', (0, 6, 10, 3), 'display', required=True, overlay_ok=True, max_words=12),
        _s('subtitle', 'text', (0, 9, 10, 1), 'lead', overlay_ok=True, max_words=20),
        _s('credit', 'text', (8, 11, 4, 1), 'caption', overlay_ok=True, align='right'),
    ), description='full-bleed photo, tinted overlay, title bottom-left'),
    LayoutDef('cover-type', 'cover', 'slide', (
        _s('art', 'shape', (7, 0, 5, 12), bleed=True),
        _s('kicker', 'text', (0, 2, 7, 1), 'caption', max_words=8),
        _s('title', 'text', (0, 3, 7, 4), 'display', required=True, max_words=12),
        _s('subtitle', 'text', (0, 7, 7, 2), 'lead', max_words=20),
    ), asymmetric=True, description='big display type with code-drawn art on the right'),
    LayoutDef('section-divider', 'divider', 'slide', (
        _s('number', 'text', (0, 3, 3, 4), 'display', max_words=1),
        _s('title', 'text', (3, 4, 9, 3), 'h1', required=True, max_words=10),
        _s('accent', 'shape', (3, 7, 2, 1)),
    ), asymmetric=True, description='section number and title'),
    LayoutDef('title-bullets', 'text', 'slide', (
        _s('title', 'text', (0, 0, 12, 2), 'h2', required=True, max_words=12),
        _s('body', 'text', (0, 2, 12, 10), 'body', required=True, max_words=40),
    ), description='heading and bullets'),
    LayoutDef('image-left-text', 'image-text', 'slide', (
        _s('image', 'image', (0, 0, 6, 12), bleed=True, fit='cover', required=True),
        _s('title', 'text', (7, 0, 5, 2), 'h2', required=True, max_words=10),
        _s('body', 'text', (7, 2, 5, 9), 'body', max_words=30),
        _s('caption', 'text', (7, 11, 5, 1), 'caption', max_words=20),
    ), asymmetric=True, description='photo on the left half, text on the right'),
    LayoutDef('image-right-text', 'image-text', 'slide', (
        _s('title', 'text', (0, 0, 5, 2), 'h2', required=True, max_words=10),
        _s('body', 'text', (0, 2, 5, 9), 'body', max_words=30),
        _s('caption', 'text', (0, 11, 5, 1), 'caption', max_words=20),
        _s('image', 'image', (6, 0, 6, 12), bleed=True, fit='cover', required=True),
    ), asymmetric=True, description='text on the left, photo on the right half'),
    LayoutDef('full-bleed-image-caption', 'image', 'slide', (
        _s('image', 'image', (0, 0, 12, 12), bleed=True, fit='cover', required=True),
        _s('overlay', 'shape', (0, 9, 12, 3), bleed=True, overlay_ok=True),
        _s('caption', 'text', (0, 9, 12, 2), 'lead', overlay_ok=True, max_words=24),
        _s('credit', 'text', (0, 11, 12, 1), 'caption', overlay_ok=True, align='right'),
    ), description='one photo, a caption band at the bottom'),
    LayoutDef('big-number', 'number', 'slide', (
        _s('title', 'text', (0, 0, 12, 2), 'h3', max_words=12),
        _s('number', 'text', (0, 2, 7, 6), 'display', required=True, max_words=2),
        _s('label', 'text', (0, 8, 7, 2), 'lead', max_words=14),
        _s('context', 'text', (7, 3, 5, 6), 'body', max_words=30),
    ), asymmetric=True, description='one statistic, huge, with its label'),
    LayoutDef('stat-cards', 'number', 'slide', (
        _s('title', 'text', (0, 0, 12, 2), 'h2', required=True, max_words=12),
        _s('cards', 'diagram', (0, 3, 12, 7), required=True),   # diagram kind 'stat-cards', 2..4 cards
        _s('note', 'text', (0, 11, 12, 1), 'caption', max_words=20),
    ), description='2 to 4 statistic cards'),
    LayoutDef('quote', 'quote', 'slide', (
        _s('mark', 'icon', (0, 1, 1, 1)),
        _s('quote', 'text', (1, 2, 10, 6), 'h1', required=True, max_words=40),
        _s('attribution', 'text', (1, 8, 10, 1), 'lead', max_words=12),
    ), description='a pull quote and who said it'),
    LayoutDef('two-column', 'columns', 'slide', (
        _s('title', 'text', (0, 0, 12, 2), 'h2', required=True, max_words=12),
        _s('left', 'text', (0, 2, 6, 10), 'body', accepts=_TXT_FIG, required=True, max_words=30),
        _s('right', 'text', (6, 2, 6, 10), 'body', accepts=_TXT_FIG, required=True, max_words=30),
    ), description='two columns of text, or text beside a table/chart/image'),
    LayoutDef('comparison', 'columns', 'slide', (
        _s('title', 'text', (0, 0, 12, 2), 'h2', required=True, max_words=12),
        _s('left_head', 'text', (0, 2, 6, 1), 'h3', required=True, max_words=6),
        _s('left', 'text', (0, 3, 6, 9), 'body', required=True, max_words=25),
        _s('right_head', 'text', (6, 2, 6, 1), 'h3', required=True, max_words=6),
        _s('right', 'text', (6, 3, 6, 9), 'body', required=True, max_words=25),
    ), description='A versus B'),
    LayoutDef('full-width-diagram', 'figure', 'slide', (
        _s('title', 'text', (0, 0, 12, 2), 'h2', required=True, max_words=12),
        _s('figure', 'diagram', (0, 2, 12, 9), accepts=_FIG, fit='contain', required=True),
        _s('caption', 'text', (0, 11, 12, 1), 'caption', max_words=24),
    ), description='one diagram (or table) across the slide'),
    LayoutDef('chart-focus', 'figure', 'slide', (
        _s('title', 'text', (0, 0, 12, 2), 'h2', required=True, max_words=12),
        _s('chart', 'chart', (0, 2, 8, 10), accepts=('table',), fit='contain', required=True),
        _s('takeaway', 'text', (8, 2, 4, 10), 'lead', max_words=30),
    ), asymmetric=True, description='a chart with the one-line takeaway beside it'),
    LayoutDef('timeline-strip', 'figure', 'slide', (
        _s('title', 'text', (0, 0, 12, 2), 'h2', required=True, max_words=12),
        _s('timeline', 'diagram', (0, 3, 12, 6), accepts=('diagram',), fit='contain', required=True),
        _s('body', 'text', (0, 10, 12, 2), 'caption', max_words=30),
    ), description='a horizontal timeline or process strip'),
    LayoutDef('closing', 'closing', 'slide', (
        _s('art', 'shape', (0, 0, 12, 12), bleed=True),
        _s('title', 'text', (0, 3, 12, 3), 'display', required=True, align='center', overlay_ok=True, max_words=8),
        _s('subtitle', 'text', (0, 6, 12, 2), 'lead', align='center', overlay_ok=True, max_words=20),
        _s('credits', 'text', (0, 10, 12, 2), 'caption', align='center', overlay_ok=True, max_words=60),
    ), description='thank-you / questions slide with image credits'),
)

_PAGES = (
    LayoutDef('cover', 'cover', 'page', (
        _s('image', 'image', (0, 0, 12, 9), bleed=True, fit='cover'),
        _s('title', 'text', (0, 10, 12, 3), 'display', required=True, max_words=16),
        _s('subtitle', 'text', (0, 13, 12, 1), 'lead', max_words=24),
        _s('meta', 'text', (0, 15, 12, 1), 'caption', max_words=20),
    ), description='cover page: photo or art on top, title below'),
    LayoutDef('chapter-opener', 'opener', 'page', (
        _s('number', 'text', (0, 1, 3, 3), 'display', max_words=1),
        _s('title', 'text', (0, 4, 12, 2), 'h1', required=True, max_words=14),
        _s('intro', 'text', (0, 6, 12, 3), 'lead', max_words=60),
        _s('body', 'text', (0, 9, 12, 7), 'body', flow=True),
    ), description='section opener with a lead paragraph'),
    LayoutDef('text-side-figure', 'figure', 'page', (
        _s('heading', 'text', (0, 0, 12, 1), 'h2', max_words=14),
        _s('body', 'text', (0, 1, 7, 15), 'body', required=True, flow=True),
        _s('figure', 'image', (7, 1, 5, 6), accepts=_FIG, fit='contain', required=True),
        _s('caption', 'text', (7, 7, 5, 1), 'caption', max_words=30),
    ), asymmetric=True, description='running text with a figure in the side column'),
    LayoutDef('two-column-text', 'text', 'page', (
        _s('heading', 'text', (0, 0, 12, 1), 'h2', max_words=14),
        _s('col1', 'text', (0, 1, 6, 15), 'body', required=True, flow=True),
        _s('col2', 'text', (6, 1, 6, 15), 'body', flow=True),
    ), description='two linked text columns'),
    LayoutDef('full-figure', 'figure', 'page', (
        _s('heading', 'text', (0, 0, 12, 1), 'h2', max_words=14),
        _s('figure', 'image', (0, 1, 12, 13), accepts=_FIG, fit='contain', required=True),
        _s('caption', 'text', (0, 14, 12, 2), 'caption', max_words=60),
    ), description='one full-page figure'),
    LayoutDef('pull-quote', 'text', 'page', (
        _s('body', 'text', (0, 0, 12, 6), 'body', required=True, flow=True),
        _s('quote', 'text', (1, 6, 10, 3), 'h2', required=True, max_words=40),
        _s('body2', 'text', (0, 9, 12, 7), 'body', flow=True),
    ), description='running text broken by a large quote'),
    LayoutDef('key-points', 'summary', 'page', (
        _s('heading', 'text', (0, 0, 12, 1), 'h2', max_words=14),
        _s('box', 'shape', (0, 1, 12, 6)),
        _s('points', 'text', (0, 1, 12, 6), 'lead', required=True, overlay_ok=True, max_words=80),
        _s('body', 'text', (0, 7, 12, 9), 'body', flow=True),
    ), description='a tinted summary / key-points box, then text'),
    LayoutDef('references', 'references', 'page', (
        _s('heading', 'text', (0, 0, 12, 1), 'h2', required=True, max_words=6),
        _s('refs', 'text', (0, 1, 12, 15), 'caption', required=True, flow=True),
    ), description='references, sources and image credits'),
)

SLIDE_LAYOUTS = tuple(d.id for d in _SLIDES)
PAGE_TEMPLATES = tuple(d.id for d in _PAGES)
_BY_ID = {d.id: d for d in (*_SLIDES, *_PAGES)}

assert len(SLIDE_LAYOUTS) == 16 and len(PAGE_TEMPLATES) == 8
assert all(s.kind in BOX_KINDS and (s.step is None or s.step in TYPE_STEPS) for d in _BY_ID.values() for s in d.slots)


def get(layout_id: str) -> LayoutDef:
    """A layout or page template by id; KeyError for an unknown id."""
    return _BY_ID[layout_id]


def slide_layouts() -> list[LayoutDef]:
    return list(_SLIDES)


def page_templates() -> list[LayoutDef]:
    return list(_PAGES)


def for_format(fmt: str) -> tuple[str, ...]:
    """The ids a format may use: pptx -> SLIDE_LAYOUTS, pdf -> PAGE_TEMPLATES, others -> ()."""
    return SLIDE_LAYOUTS if fmt == 'pptx' else PAGE_TEMPLATES if fmt == 'pdf' else ()


def next_best(layout_id: str, avoid: set[str] | frozenset = frozenset(), *, needs: tuple[str, ...] = ()) -> str | None:
    """The next layout in the same family (then the text family) not in `avoid` whose slots accept every kind in
    `needs`; None when none fits. Deterministic (library order)."""
    raise NotImplementedError('studio.library.next_best: builder L')


def slot_kinds(layout_id: str) -> dict[str, tuple[str, ...]]:
    """slot name -> every kind it accepts (kind first)."""
    return {s.name: (s.kind, *s.accepts) for s in get(layout_id).slots}

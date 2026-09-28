"""The art director (docs/PLAN-designer.md 3.1 and 9.7): picks a preset and one layout per slide/section from the
library. Keyless rules always work; the model call (<= STUDIO_DIRECT_TOKENS out) only chooses among library ids and
falls back to the rules on any failure. It never writes coordinates, colours or fonts.

Owner: builder L.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .plan import ArtDirection
from .tokens import DesignSystem

SYSTEM = ('You are the art director of a student document. Choose a preset and, for each page in the outline, one '
          'layout id from the allowed list. Reply with JSON matching the schema only. Never invent layouts, colours, '
          'fonts or coordinates.')


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


@dataclass
class Outline:
    title: str
    fmt: str
    pages: list[PageOutline]
    images: int                      # image blocks in the spec


def outline_of(spec: dict, fmt: str) -> Outline:
    """The normalized spec's shape per page, plus a cover (pptx/pdf) and a closing slide (pptx decks of 6+ slides)."""
    raise NotImplementedError('studio.direct.outline_of: builder L')


def direct_keyless(outline: Outline, ds: DesignSystem, *, dark: bool | None = None) -> ArtDirection:
    """Rules by content shape: first page cover (cover-hero with an image, else cover-type); one stat -> big-number;
    2-4 stats -> stat-cards; an image and <= 4 bullets -> image-left/right-text (alternating); a timeline or process ->
    timeline-strip; another diagram or a table -> full-width-diagram; a chart -> chart-focus; a quote -> quote; two
    parallel lists -> comparison; every 3-4 slides in decks of 10+ -> section-divider; the last -> closing; otherwise
    title-bullets. Then D7 variety (no layout > 3 in a row, 4+ layouts in 8+ slides). source='keyless'."""
    raise NotImplementedError('studio.direct.direct_keyless: builder L')


async def direct_llm(outline: Outline, ds: DesignSystem, engine, *, mood: list[str] = (), max_tokens: int = 2000,
                     effort: str = 'low') -> tuple[ArtDirection, dict]:
    """(direction, usage {calls, llm_in, llm_out, ms}). One engine.stream call with plan.art_schema(llm=True); invalid
    or failed replies fall back to direct_keyless (usage still counted) with a note. source='llm'."""
    raise NotImplementedError('studio.direct.direct_llm: builder L')


def validate_direction(raw: dict, outline: Outline, fmt: str) -> tuple[ArtDirection, list[str]]:
    """A model's direction made valid: unknown layouts replaced by the keyless choice, page count matched to the
    outline, layouts whose required slots the content can't fill swapped, freeform limited to 2 pages."""
    raise NotImplementedError('studio.direct.validate_direction: builder L')

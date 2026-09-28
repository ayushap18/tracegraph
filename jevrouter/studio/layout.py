"""The layout engine (docs/PLAN-designer.md 3.5 and 9.6): deterministic. For each page of the direction, fill the
layout's slots from the spec, measure text with studio/fonts, and fit: wrap -> shrink within the scale -> rebalance
(caption moves, bullets beyond the slot budget go to speaker notes, compact variant) -> split with "(continued)".

Owner: builder L.
"""
from __future__ import annotations

from dataclasses import dataclass

from .plan import ArtDirection, Box, DesignPlan, PagePlan
from .tokens import DesignSystem
from .workspace import Workspace

# page sizes in points (w, h)
SLIDE = (960.0, 540.0)
PAPER = {'a4': (595.2756, 841.8898), 'letter': (612.0, 792.0), 'a3': (841.8898, 1190.5512),
         'a2': (1190.5512, 1683.7795)}
FIX_ACTIONS = ('shrink', 'rebalance', 'compact', 'split', 'recrop', 'swap_layout', 'snap')


@dataclass
class FitResult:
    size: float                      # points chosen
    lines: list[str]
    height: float                    # points needed
    fits: bool
    overflow_words: int = 0          # words that did not fit (rebalance/split input)


def page_size(fmt: str, paper: str | None = None) -> tuple[float, float]:
    """(w, h) in points: pptx -> SLIDE, pdf/docx -> PAPER[paper or 'a4']."""
    return SLIDE if fmt == 'pptx' else PAPER.get((paper or 'a4').lower(), PAPER['a4'])


def grid_rect(area: tuple[int, int, int, int], size: tuple[float, float], ds: DesignSystem, *, bleed: bool = False,
              fmt: str = 'pptx') -> tuple[float, float, float, float]:
    """(x, y, w, h) in points of a grid area: inside the margins with gutters, or over the whole page for bleed."""
    raise NotImplementedError('studio.layout.grid_rect: builder L')


def fit_text(text: str, family: str, step: str, ds: DesignSystem, w: float, h: float, fmt: str, *,
             bold: bool = False, line_height: float | None = None) -> FitResult:
    """Wrap and shrink `text` into (w, h), from the step's size down the scale, never below the format minimum."""
    raise NotImplementedError('studio.layout.fit_text: builder L')


def lay_out(spec: dict, fmt: str, ds: DesignSystem, direction: ArtDirection, ws: Workspace, *,
            file_id: str, paper: str | None = None) -> DesignPlan:
    """The DesignPlan for a normalized spec: every page's boxes positioned, sized, measured and fitted; figures cropped
    via studio/assets into the workspace; decorative art via studio/art. fonts/assets lists filled. No I/O but `ws`."""
    raise NotImplementedError('studio.layout.lay_out: builder L')


def lay_out_page(direction_index: int, spec: dict, fmt: str, ds: DesignSystem, direction: ArtDirection,
                 ws: Workspace, size: tuple[float, float]) -> list[PagePlan]:
    """One direction entry -> one page, or several when split."""
    raise NotImplementedError('studio.layout.lay_out_page: builder L')


def refit(plan: DesignPlan, spec: dict, page: int, action: str, ds: DesignSystem, ws: Workspace, *,
          box: str | None = None, arg=None) -> bool:
    """Apply one FIX_ACTIONS code fix to a page in place; True when the plan changed. swap_layout uses
    library.next_best; split inserts pages and renumbers indexes."""
    raise NotImplementedError('studio.layout.refit: builder L')


def relayout(plan: DesignPlan, spec: dict, ds: DesignSystem, ws: Workspace, *,
             layouts: dict[int, str] | None = None) -> DesignPlan:
    """A restyle: the same direction (with per-page layout overrides) laid out again with another DesignSystem."""
    raise NotImplementedError('studio.layout.relayout: builder L')


def flow_styles(spec: dict, fmt: str, ds: DesignSystem) -> dict:
    """docx/md/xlsx: the plan.FLOW_SCHEMA dict (paragraph styles per type step, figure placement, page breaks,
    chart palette). Phase 5."""
    raise NotImplementedError('studio.layout.flow_styles: builder L')


def box_text(box: Box, spec: dict) -> str:
    """The text a box's content ref resolves to ('' for non-text refs)."""
    raise NotImplementedError('studio.layout.box_text: builder L')

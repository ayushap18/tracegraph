"""Visual QA (docs/PLAN-designer.md 3.7 and 9.8): checks D1-D8 over a DesignPlan (and its thumbnails for contrast over
photos), a 0-100 score and the design report. Also the ruleset entries D1-D8 in create/rules.py (builder I) read
`report['checks']`.

Owner: builder Q. CHECKS, THRESHOLDS and WEIGHTS are contract data.
"""
from __future__ import annotations

from .plan import DesignPlan, QaResult
from .workspace import Workspace

# id -> (name, weight in the score, what it checks)
CHECKS: dict[str, tuple[str, int, str]] = {
    'D1': ('Overflow', 20, "no text box's measured height exceeds its box (tolerance 0.5 pt); no line wider than the box"),
    'D2': ('Overlap', 15, 'no two boxes intersect by more than 1 pt² unless the upper one is overlay_ok'),
    'D3': ('Readability', 20, 'text size >= the format minimum; contrast >= 4.5:1, >= 3:1 for >= 24 pt (18.66 pt bold), '
                              'text over images measured against the darkest/lightest sampled thumbnail pixel'),
    'D4': ('Density', 10, 'slides: <= 40 words with bullets, <= 60 words of prose; white space 25-60% of the page '
                          '(print pages 15-60%)'),
    'D5': ('Balance', 5, 'visual weight centre within the middle third both ways (skipped for asymmetric layouts, '
                         'covers and freeform pages)'),
    'D6': ('Consistency', 10, 'every text size is a type-scale step (±0.5 pt); text left edges snap to grid columns '
                              '(±2 pt); one image treatment per file'),
    'D7': ('Variety', 10, 'no layout on more than 3 consecutive slides; decks of 8+ slides use 4+ layouts (pptx only)'),
    'D8': ('Images', 10, 'no image upscaled more than 1.5x at the target dpi (96 pptx, 150 pdf); focal point inside '
                         'the crop'),
}
THRESHOLDS = {
    'overflow_tolerance_pt': 0.5,
    'overlap_area_pt2': 1.0,
    'contrast_text': 4.5, 'contrast_large': 3.0, 'large_pt': 24.0, 'large_bold_pt': 18.66,
    'words_bullets': 40, 'words_text': 60,
    'white_min_slide': 0.25, 'white_min_page': 0.15, 'white_max': 0.60,
    'balance_low': 1 / 3, 'balance_high': 2 / 3,
    'scale_tolerance_pt': 0.5, 'snap_tolerance_pt': 2.0,
    'max_run': 3, 'variety_slides': 8, 'variety_layouts': 4,
    'max_upscale': 1.5, 'dpi_pptx': 96, 'dpi_pdf': 150,
}
WEIGHTS = {k: v[1] for k, v in CHECKS.items()}
PASS_SCORE = 80            # the golden-set bar (section 7); the loop stops early only when every check passes
# the code fix the agent loop tries first for each failing check (layout.FIX_ACTIONS), in order
FIXES = {'D1': ('shrink', 'rebalance', 'compact', 'split'), 'D2': ('snap', 'compact', 'swap_layout'),
         'D3': ('shrink', 'recrop'), 'D4': ('rebalance', 'split', 'compact'), 'D5': ('swap_layout',),
         'D6': ('snap',), 'D7': ('swap_layout',), 'D8': ('recrop', 'swap_layout')}

assert sum(WEIGHTS.values()) == 100


def check(plan: DesignPlan, ws: Workspace | None = None, *, thumbs: list[bytes] | None = None) -> list[QaResult]:
    """Every D check over the plan; one QaResult per failure (page/box set) plus one ok=True result per passing check."""
    raise NotImplementedError('studio.qa.check: builder Q')


def run_check(check_id: str, plan: DesignPlan, ws: Workspace | None = None, *,
              thumbs: list[bytes] | None = None) -> list[QaResult]:
    """One check's results."""
    raise NotImplementedError('studio.qa.run_check: builder Q')


def score(results: list[QaResult]) -> int:
    """100 minus the weight of every check with at least one unfixed failure; 0..100."""
    failed = {r.id for r in results if not r.ok and not r.fixed}
    return max(0, 100 - sum(WEIGHTS.get(i, 0) for i in failed))


def report(plan: DesignPlan, results: list[QaResult], *, critic: dict | None = None,
           fallbacks: list[dict] | None = None, notes: list[str] | None = None, thumbs: int = 0) -> dict:
    """The design report (plan.report_schema()): score, preset, fonts, rounds, stop, tokens, checks (all 8, in order),
    at most 50 failing results, layouts used {id: count}, fallbacks, critic summary, thumbs count, notes."""
    raise NotImplementedError('studio.qa.report: builder Q')


def contrast(fg: str, bg: str) -> float:
    """WCAG contrast ratio of two RRGGBB colours (shared helper; same maths as create/design.contrast)."""
    from ..create.design import contrast as _c
    return _c(fg, bg)

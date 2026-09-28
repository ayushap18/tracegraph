"""Thumbnails (docs/PLAN-designer.md 3.7 and 9.8): Studio's own Pillow rendering of each page from the DesignPlan,
with the same fonts (studio/fonts), geometry, images and art the painters use. Not PowerPoint's rendering; the PDF path
is exact.

Owner: builder Q.
"""
from __future__ import annotations

from .plan import DesignPlan
from .workspace import Workspace

THUMB_PX = 480             # width of a stored thumbnail
SHEET_PX = 1200            # width of a critic contact sheet
SHEET_COLS, SHEET_ROWS = 3, 2   # 6 pages per contact sheet


def render_page(plan: DesignPlan, page: int, ws: Workspace, *, width_px: int = THUMB_PX, spec: dict | None = None) -> bytes:
    """A PNG of one 0-based page."""
    raise NotImplementedError('studio.thumbs.render_page: builder Q')


def render_all(plan: DesignPlan, ws: Workspace, *, width_px: int = THUMB_PX, spec: dict | None = None) -> list[str]:
    """Every page rendered and stored with ws.save_thumb; returns the refs in page order."""
    raise NotImplementedError('studio.thumbs.render_all: builder Q')


def contact_sheet(pngs: list[bytes], *, cols: int = SHEET_COLS, rows: int = SHEET_ROWS, width_px: int = SHEET_PX,
                  first_page: int = 1) -> bytes:
    """Up to cols*rows thumbnails on one PNG, each labelled with its 1-based page number (the critic's input)."""
    raise NotImplementedError('studio.thumbs.contact_sheet: builder Q')


def sample(png: bytes, box: tuple[float, float, float, float], page_size: tuple[float, float]) -> list[str]:
    """RRGGBB colours sampled under a box (points) on a page thumbnail: D3 contrast over photos."""
    raise NotImplementedError('studio.thumbs.sample: builder Q')


def preset_thumb(preset_id: str, *, width_px: int = 320) -> bytes:
    """A sample cover slide in a preset (GET /api/design/presets/{id}/thumb), cached in data/cache/design/presets/."""
    raise NotImplementedError('studio.thumbs.preset_thumb: builder Q')

"""Image preparation (docs/PLAN-designer.md 3.4 and 9.11): smart crop around a focal point (Pillow edge/entropy map),
minimum resolution per slot, crops stored in the workspace. Photos still come from create/assets.py (Commons, licence
allow-list, credits, rule X6); nothing here downloads.

Owner: builder V.
"""
from __future__ import annotations

from .workspace import Workspace

MIN_DPI = {'pptx': 96, 'pdf': 150, 'docx': 150}
MAX_UPSCALE = 1.5


def focal_point(data: bytes) -> tuple[float, float]:
    """(x, y) fractions of the most salient point (edge density + entropy, faces not detected); (0.5, 0.5) for flat
    images."""
    raise NotImplementedError('studio.assets.focal_point: builder V')


def crop_for(size_px: tuple[int, int], focal: tuple[float, float], box_w: float, box_h: float,
             fit: str = 'cover') -> tuple[float, float, float, float]:
    """(x, y, w, h) fractions of the source to show in a box of that aspect: cover crops around the focal point
    (kept inside, rule-of-thirds bias); contain returns (0, 0, 1, 1)."""
    raise NotImplementedError('studio.assets.crop_for: builder V')


def upscale(size_px: tuple[int, int], crop: tuple[float, float, float, float], box_w: float, box_h: float,
            fmt: str) -> float:
    """How much the cropped source is enlarged at MIN_DPI[fmt] (> 1 means upscaled; D8 fails above MAX_UPSCALE)."""
    raise NotImplementedError('studio.assets.upscale: builder V')


def prepare(asset: str, box_w: float, box_h: float, ws: Workspace, *, fmt: str, fit: str = 'cover',
            mono: bool = False, duotone: tuple[str, str] | None = None) -> tuple[str, tuple, tuple]:
    """(ws ref of the cropped PNG, crop, focal) for an asset-cache image (create/assets.CACHE/<sha>.png) in a box."""
    raise NotImplementedError('studio.assets.prepare: builder V')

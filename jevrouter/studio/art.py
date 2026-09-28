"""Code-drawn decorative art (docs/PLAN-designer.md 3.4 and 9.11): seeded, deterministic, preset-consistent patterns for
pages without photos (covers, dividers, closing slides). Output is a list of primitives the painters draw natively.

Primitive dicts (points, top-left origin, colours as token names):
    {"type": "rect"|"ellipse", "x", "y", "w", "h", "fill", "stroke", "stroke_w", "opacity", "radius"}
    {"type": "line"|"path", "points": [[x, y], ...], "stroke", "stroke_w", "fill", "closed": bool, "opacity"}
    {"type": "gradient", "x", "y", "w", "h", "from": token, "to": token, "angle": deg}

Owner: builder V.
"""
from __future__ import annotations

from .tokens import DesignSystem

ART_KINDS = ('dots', 'grid', 'gradient-mesh', 'blobs', 'orbit-rings', 'waves', 'stripes', 'corner-arcs')
MAX_PRIMITIVES = 200


def draw(kind: str, seed: int, w: float, h: float, ds: DesignSystem) -> list[dict]:
    """Primitives filling (w, h) points; the same (kind, seed, size, ds) always gives the same list."""
    raise NotImplementedError('studio.art.draw: builder V')


def pick(ds: DesignSystem, mood: list[str], seed: int) -> str:
    """The art kind that suits a preset and mood words (deterministic)."""
    raise NotImplementedError('studio.art.pick: builder V')


def png(kind: str, seed: int, w_px: int, h_px: int, ds: DesignSystem) -> bytes:
    """The same art rasterised (thumbnails, DOCX)."""
    raise NotImplementedError('studio.art.png: builder V')

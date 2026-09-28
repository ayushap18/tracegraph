"""Icons (docs/PLAN-designer.md 3.4 and 9.11): a curated Lucide subset (~150 icons: science, history, maths, geography,
tech, arrows, UI) bundled as JSON under jevrouter/studio/data/, with Lucide's ISC LICENSE beside it. Drawn as native
vector shapes in PPTX/PDF and as PNG in thumbnails; picked by keyword.

data/lucide.json (fixed by contract):
    {"source": "lucide", "version": "<lucide release>", "license": "ISC", "viewbox": 24,
     "icons": {"<name>": {"tags": ["..."], "nodes": [["path", {"d": "..."}], ["circle", {"cx": 12, "cy": 12, "r": 10}],
                                                    ["rect", {...}], ["line", {...}], ["polyline", {"points": "..."}],
                                                    ["polygon", {...}], ["ellipse", {...}]]}}}
Strokes only (Lucide is a 2 px stroke set on a 24 grid, round caps and joins); fill none.

Owner: builder V.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'
ICONS_JSON = DATA / 'lucide.json'
LICENSE = DATA / 'LICENSE-lucide'
VIEWBOX = 24
STROKE = 2.0               # in viewbox units
NODE_TYPES = ('path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse')


@dataclass
class Icon:
    name: str
    tags: list[str] = field(default_factory=list)
    nodes: list[tuple[str, dict]] = field(default_factory=list)


def names() -> list[str]:
    """Every bundled icon name, sorted ([] when the data file is missing)."""
    raise NotImplementedError('studio.icons.names: builder V')


def get(name: str) -> Icon | None:
    raise NotImplementedError('studio.icons.get: builder V')


def pick(text: str, *, used: set[str] | None = None) -> str | None:
    """The best icon for a phrase by name and tags ("battery", "planet", "dna"), skipping `used`; None when nothing
    matches well enough. Deterministic."""
    raise NotImplementedError('studio.icons.pick: builder V')


def polylines(icon: Icon, size_pt: float, *, tolerance: float = 0.25) -> list[list[tuple[float, float]]]:
    """Every node flattened to polylines in points within a size_pt square (arcs and curves approximated), for the
    PPTX freeform builder and the PDF canvas; closed shapes repeat their first point."""
    raise NotImplementedError('studio.icons.polylines: builder V')


def png(name: str, color: str, px: int) -> bytes:
    """The icon as a transparent PNG stroked in RRGGBB (thumbnails, DOCX)."""
    raise NotImplementedError('studio.icons.png: builder V')

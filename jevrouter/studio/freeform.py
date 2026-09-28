"""The freeform hatch (docs/PLAN-designer.md 3.8 step 3 and 9.9): at most STUDIO_FREEFORM_PAGES pages per file (a cover,
a poster panel, an infographic) drawn from a model-written shape program (plan.freeform_schema()): primitives in grid
units with token colour names. Validated (bounds, overlap, contrast, minimum sizes, only workspace/asset-cache refs and
bundled icons), text fitted with real metrics, painted natively. Fails QA twice -> the closest library layout.

Owner: builder Q.
"""
from __future__ import annotations

from .plan import Box, ShapeProgram
from .tokens import DesignSystem
from .workspace import Workspace

SYSTEM = ('You design one page as a JSON shape program matching the schema: primitives on a {cols} x {rows} grid, '
          'colours as token names only, text short. Use only the listed image refs and icon names. No prose.')


async def compose(page: int, section: dict, ds: DesignSystem, engine, ws: Workspace, *, fmt: str,
                  refs: list[str] = (), budget_tokens: int = 2000) -> tuple[ShapeProgram | None, dict]:
    """(program or None, usage {calls, llm_in, llm_out, ms}). None when the engine fails or the reply is invalid."""
    raise NotImplementedError('studio.freeform.compose: builder Q')


def validate(raw: dict, ds: DesignSystem, ws: Workspace, *, page: int, fmt: str,
             refs: list[str] = ()) -> tuple[ShapeProgram | None, list[str]]:
    """(program, problems). Schema, then: shapes inside the grid, text contrast against what is under it, text sizes
    >= the format minimum, refs only from `refs`/ws/icon names, at most 40 shapes."""
    raise NotImplementedError('studio.freeform.validate: builder Q')


def to_boxes(program: ShapeProgram, size: tuple[float, float], ds: DesignSystem, spec: dict, *, fmt: str,
             ws: Workspace) -> list[Box]:
    """Grid units -> points (margins applied), text fitted with studio/fonts; boxes ready for a PagePlan."""
    raise NotImplementedError('studio.freeform.to_boxes: builder Q')

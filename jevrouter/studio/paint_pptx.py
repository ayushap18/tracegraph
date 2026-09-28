"""The PPTX painter (docs/PLAN-designer.md 2 and 9.6): draws a positioned DesignPlan with python-pptx and makes no
layout decisions. Text boxes use the plan's sizes and wrapped lines (auto-fit off), shapes and icons are native vector
shapes, diagrams are native shapes (create/diagram.py), charts are native charts, images are the workspace crops. Page
notes become speaker notes. Rules F3/A1-A4 and X1-X3 hold exactly as in create/render._pptx.

Owner: builder L.
"""
from __future__ import annotations

from .plan import DesignPlan
from .workspace import Workspace


def paint(plan: DesignPlan, spec: dict, ws: Workspace) -> bytes:
    """The .pptx bytes for a plan of format 'pptx' (one slide per page, 960 x 540 pt). Raises on a library failure;
    the caller turns that into a fallback, never a crash."""
    raise NotImplementedError('studio.paint_pptx.paint: builder L')

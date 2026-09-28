"""The PDF painter (docs/PLAN-designer.md 2, 3.3 and 9.6): draws a positioned DesignPlan with the reportlab canvas,
embedding subset TrueType fonts from studio/fonts, vector shapes/icons/diagrams, and the workspace image crops. Makes no
layout decisions. Rules F1/A1-A4 and X1-X3 hold exactly as in create/render._pdf (tagged headings as outline entries).

Owner: builder L.
"""
from __future__ import annotations

from .plan import DesignPlan
from .workspace import Workspace


def paint(plan: DesignPlan, spec: dict, ws: Workspace) -> bytes:
    """The .pdf bytes for a plan of format 'pdf'. Raises on a library failure; the caller falls back."""
    raise NotImplementedError('studio.paint_pdf.paint: builder L')

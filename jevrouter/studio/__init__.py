"""Studio, the design stage between content and rendering (docs/PLAN-designer.md; the contract is section 9).

content (DocSpec) -> art direction -> design tokens -> assets -> layout -> visual QA (+ critic, freeform) -> painters.

Importing this package has no side effects and loads no optional dependency; each module imports its own libraries
inside functions. Off unless TG_STUDIO names the format (config.studio_formats).
"""
from .agent import design, enabled, paint, polish, restyle
from .library import PAGE_TEMPLATES, SLIDE_LAYOUTS
from .plan import (CHECK_IDS, CRITIC_ACTIONS, DIAGRAM_KINDS_NEW, FREEFORM_PRIMITIVES, PLAN_VERSION, TOKEN_COLORS,
                   ArtDirection, Box, BoxStyle, CriticEdit, DesignPlan, DesignResult, PageDirection, PagePlan, QaResult,
                   RestyleOptions, ShapeProgram, art_schema, critic_schema, design_plan_schema, freeform_schema,
                   report_schema, validate)
from .presets import DEFAULT_PRESET, PRESETS, TEMPLATES
from .tokens import DesignSystem

__all__ = ['CHECK_IDS', 'CRITIC_ACTIONS', 'DEFAULT_PRESET', 'DIAGRAM_KINDS_NEW', 'FREEFORM_PRIMITIVES', 'PAGE_TEMPLATES',
           'PLAN_VERSION', 'PRESETS', 'SLIDE_LAYOUTS', 'TEMPLATES', 'TOKEN_COLORS', 'ArtDirection', 'Box', 'BoxStyle',
           'CriticEdit', 'DesignPlan', 'DesignResult', 'DesignSystem', 'PageDirection', 'PagePlan', 'QaResult',
           'RestyleOptions', 'ShapeProgram', 'art_schema', 'critic_schema', 'design', 'design_plan_schema', 'enabled',
           'freeform_schema', 'paint', 'polish', 'report_schema', 'restyle', 'validate']

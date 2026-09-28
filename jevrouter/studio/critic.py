"""The design critic (docs/PLAN-designer.md 3.8 step 2 and 9.9): optional, vision engines only, Deep mode or Polish.
Contact sheets (6 pages each) plus the design report go to the model; it returns structured edits only
(plan.critic_schema()), validated and applied by code, then re-checked by QA; an edit that breaks QA is rolled back.
Budget: <= STUDIO_CRITIC_ROUNDS rounds and <= STUDIO_CRITIC_TOKENS tokens in all.

Vision: an engine can see when `getattr(engine, 'supports_vision', False)` is true; such engines accept
`images=[png bytes, ...]` in Engine.stream (additive keyword, builder Q adds it to engines/base.py, anthropic_api.py
and claude_code.py). Other engines skip the critic with a note.

Owner: builder Q.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .plan import CriticEdit, DesignPlan
from .tokens import DesignSystem
from .workspace import Workspace

SYSTEM = ('You are a design critic for student slides and pages. Look at the pages and the QA report and return at most '
          '12 edits as JSON matching the schema. Only use the allowed actions, layout ids, token colour names and icon '
          'names. No prose.')


@dataclass
class CriticReply:
    edits: list[CriticEdit] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)   # why invalid edits were dropped
    llm_in: int = 0
    llm_out: int = 0
    ms: int = 0
    note: str | None = None                            # e.g. 'engine cannot read images'


def can_see(engine) -> bool:
    """True when the engine can take images."""
    return bool(engine is not None and getattr(engine, 'supports_vision', False))


async def critique(plan: DesignPlan, report: dict, sheets: list[bytes], engine, *, budget_tokens: int = 6000,
                   effort: str = 'medium') -> CriticReply:
    """One critic call. Never raises: failures give an empty reply with a note."""
    raise NotImplementedError('studio.critic.critique: builder Q')


def validate_edits(raw: dict, plan: DesignPlan, ds: DesignSystem) -> tuple[list[CriticEdit], list[str]]:
    """Edits that name real pages, library layouts allowed for the format, slots of the page's layout, token colours
    and bundled icons; the rest dropped with a reason."""
    raise NotImplementedError('studio.critic.validate_edits: builder Q')


def apply_edit(plan: DesignPlan, edit: CriticEdit, spec: dict, ds: DesignSystem, ws: Workspace) -> bool:
    """Apply one edit in place by code (layout.refit / relayout of one page); True when the plan changed."""
    raise NotImplementedError('studio.critic.apply_edit: builder Q')

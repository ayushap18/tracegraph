"""The design agent (docs/PLAN-designer.md 3.8 and 9.10): the one entry point the create pipeline and the API call.

    direct -> assemble assets -> lay out -> thumbnails -> QA -+-> pass: paint
       ^                                                      |
       +--- code fixes, then critic edits / freeform  <-------+   (<= STUDIO_ROUNDS rounds, token and time budgets)

Integration (builder I): agents/create.finish() calls `design()` before build() when `enabled(fmt)`; build() takes the
DesignResult and uses `painted_bytes` instead of render_safe() (verify() still runs on them). Any exception here, or
painted_bytes None for a painted format, means the legacy renderer makes the file and the answer says so; a file is
never lost to the design stage. Convert/restyle/polish endpoints call `design()` (keyless), `restyle()` and `polish()`.

Owner: builder Q.
"""
from __future__ import annotations

from ..config import studio_formats
from .plan import DesignPlan, DesignResult, RestyleOptions
from .workspace import Workspace


def enabled(fmt: str) -> bool:
    """Studio designs this format now (TG_STUDIO; phase 0 default: no format)."""
    return fmt in studio_formats()


async def design(spec: dict, fmt: str, *, file_id: str, brief=None, request: str = '', tokens_src: dict | None = None,
                 engine=None, http=None, mode: str = 'balanced', sandbox: str | None = None,
                 deadline: float | None = None, preset: str | None = None) -> DesignResult:
    """Design and paint one file from a normalized spec.

    brief: create/brief.Brief (slides/pages, theme, font, images, diagrams). request: the user's words (prompt style).
    tokens_src: the cleaned design.md dict (spec['design']) or None. engine: the run's engine, or None for keyless
    (art direction by rules, no critic, no freeform: 0 tokens). http: aiohttp session for font downloads (None: local
    fonts only). mode: 'quick' | 'balanced' | 'deep' | 'research'; the critic runs only in 'deep'. deadline:
    time.monotonic() the run must finish by. preset: a forced preset (restyle, templates).
    Returns DesignResult; painted_bytes is None for formats Studio does not paint (docx/md/xlsx get plan.flow)."""
    raise NotImplementedError('studio.agent.design: builder Q')


def restyle(spec: dict, fmt: str, ws: Workspace, options: RestyleOptions, *, file_id: str) -> DesignResult:
    """0 tokens: the stored plan's direction re-laid out with another preset/fonts/dark/template/layout overrides,
    re-checked and painted into a new workspace `file_id` (seeded from ws)."""
    raise NotImplementedError('studio.agent.restyle: builder Q')


async def polish(spec: dict, fmt: str, ws: Workspace, engine, *, file_id: str,
                 deadline: float | None = None) -> DesignResult:
    """One critic round on the stored design (vision engines only), edits applied by code, QA re-run, painted into a
    new workspace `file_id`. Raises ValueError('no-vision') when the engine can't read images, and
    LookupError('no-plan') when ws has no plan."""
    raise NotImplementedError('studio.agent.polish: builder Q')


def paint(plan: DesignPlan, spec: dict, ws: Workspace) -> bytes:
    """The file for a finished plan: paint_pptx or paint_pdf by plan.format; ValueError for other formats."""
    if plan.format == 'pptx':
        from .paint_pptx import paint as p
    elif plan.format == 'pdf':
        from .paint_pdf import paint as p
    else:
        raise ValueError(f'Studio does not paint {plan.format}')
    return p(plan, spec, ws)


def meta_design(result: DesignResult) -> dict:
    """The CreatedFile.design additions: {preset, fonts: [{family, role, source, licence, embedded, note}], score,
    notes, thumbs: n, studio: true}, merged into design_applied()'s dict when a design file was also used."""
    raise NotImplementedError('studio.agent.meta_design: builder Q')

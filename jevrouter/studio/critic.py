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

import copy
import time
from dataclasses import dataclass, field

from .plan import TOKEN_COLORS, CriticEdit, DesignPlan, critic_schema, validate
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
    if not can_see(engine):
        return CriticReply(note=f'{_label(engine)} cannot read images, so the design critic was skipped.')
    if not sheets:
        return CriticReply(note='There were no thumbnails to show the design critic.')
    prompt = _prompt(plan, report)
    est_in = len(SYSTEM) // 4 + len(prompt) // 4 + sum(_image_tokens(s) for s in sheets)
    room = int(budget_tokens) - est_in
    if room < MIN_OUT:
        return CriticReply(note='The design critic was skipped: its token budget is spent.')
    t0 = time.monotonic()
    reply = None
    try:
        from ..engines.base import parse_json
        reply = await engine.stream(system=SYSTEM, prompt=prompt, effort=effort, max_tokens=min(MAX_OUT, room),
                                    schema=critic_schema(), images=list(sheets))
        raw = parse_json(reply.text)
        ds = _system(plan)
        edits, dropped = validate_edits(raw, plan, ds)
        bad = not isinstance(raw, dict) or not isinstance(raw.get('edits'), list)
        return CriticReply(edits, dropped, int(reply.input_tokens or 0), int(reply.output_tokens or 0),
                           int((time.monotonic() - t0) * 1000),
                           "The design critic's reply was not a list of edits." if bad else None)
    except Exception as e:   # engine errors, refusals, bad JSON: the design stands as it is
        why = getattr(e, 'why', None) or type(e).__name__
        return CriticReply(llm_in=int(getattr(reply, 'input_tokens', 0) or 0),
                           llm_out=int(getattr(reply, 'output_tokens', 0) or 0),
                           ms=int((time.monotonic() - t0) * 1000), note=f'The design critic failed ({why}).')


MIN_OUT = 200          # tokens: below this the critic can't return anything useful
MAX_OUT = 1200         # tokens out per critic call (12 short edits fit in a few hundred)


def _label(engine) -> str:
    return str(getattr(engine, 'label', None) or getattr(engine, 'name', None) or 'This engine')


def _image_tokens(png: bytes) -> int:
    """What an image costs as model input (about width x height / 750, Anthropic's rule of thumb)."""
    try:
        import io
        from PIL import Image
        w, h = Image.open(io.BytesIO(png)).size
        return max(1, w * h // 750)
    except Exception:
        return 1600


def _system(plan: DesignPlan):
    from .thumbs import system_of
    return system_of(plan)


def _prompt(plan: DesignPlan, report: dict) -> str:
    from . import library
    unit = 'Slide' if plan.format == 'pptx' else 'Page'
    lines = [f'{len(plan.pages)} {unit.lower()}s, preset {plan.preset}, design score {report.get("score", "?")}/100.',
             f'The contact sheets show {unit.lower()}s in order, numbered from 1.', '', 'Pages:']
    for i, p in enumerate(plan.pages):
        slots = []
        try:
            slots = [s.name for s in library.get(p.layout).slots]
        except Exception:
            pass
        extra = ' (freeform)' if p.freeform else ' (continued)' if p.continued else ''
        lines.append(f'{i + 1}: {p.layout}{extra}; slots: {", ".join(slots) or "none"}')
    fails = [c for c in report.get('checks') or [] if not c.get('ok')]
    if fails:
        lines += ['', 'Open QA problems:'] + [f'{c["id"]} {c["name"]}: {c.get("note", "")}' for c in fails]
    lines += ['', 'Allowed layouts: ' + ', '.join(library.for_format(plan.format)),
              'Allowed colour tokens: ' + ', '.join(TOKEN_COLORS),
              'Actions and arg: change_layout (layout id), emphasize (slot name), reduce_text (max words 5 to 60), '
              'swap_image (image index or null), recolor_accent (colour token), enlarge_title (1 or 2), '
              'add_icon (Lucide icon name).']
    try:
        from . import icons
        names = icons.names()
        if names:
            lines.append('Icon names: ' + ', '.join(names[:150]))
    except Exception:
        pass
    lines.append('Suggest only edits that clearly improve the design. An empty list is fine.')
    return '\n'.join(lines)


def _icon_names() -> set[str] | None:
    try:
        from . import icons
        return set(icons.names())
    except Exception:
        return None


def _kinds(page) -> set[str]:
    return {b.kind for b in page.boxes if (b.content or 'none') != 'none' or b.kind == 'text'}


def _fits(layout_id: str, page) -> bool:
    """The page's content can fill every required slot of the layout."""
    from . import library
    have = _kinds(page)
    for s in library.get(layout_id).slots:
        if s.required and not ({s.kind, *s.accepts} & have):
            return False
    return True


def validate_edits(raw: dict, plan: DesignPlan, ds: DesignSystem) -> tuple[list[CriticEdit], list[str]]:
    """Edits that name real pages, library layouts allowed for the format, slots of the page's layout, token colours
    and bundled icons; the rest dropped with a reason."""
    from . import library
    from .qa import contrast
    from .thumbs import hex_of
    if not isinstance(raw, dict) or not isinstance(raw.get('edits'), list):
        return [], ['the reply did not match the edit schema']
    item_schema = critic_schema()['properties']['edits']['items']
    edits, dropped, seen = [], [], set()
    allowed = set(library.for_format(plan.format))
    icon_names = None
    for n, e in enumerate(raw['edits']):
        if n >= 12:
            dropped.append(f'edit {n + 1}: more than 12 edits')
            continue
        problems = validate(e, item_schema, f'edit {n + 1}')
        if problems:
            dropped.append(problems[0])
            continue
        page, action, arg = e['page'], e['action'], e.get('arg')
        why = None
        if page > len(plan.pages):
            why = f'there is no page {page}'
        elif (page, action) in seen:
            why = 'a repeat of an earlier edit'
        else:
            p = plan.pages[page - 1]
            ld = None
            try:
                ld = library.get(p.layout)
            except Exception:
                pass
            if action == 'change_layout':
                if arg not in allowed:
                    why = f'{str(arg)[:40]!r} is not a {plan.format} layout'
                elif arg == p.layout:
                    why = 'the page already uses that layout'
                elif not _fits(arg, p):
                    why = f'the page has no content for every required part of {arg}'
            elif action == 'emphasize':
                if ld is None or not isinstance(arg, str) or ld.slot(arg) is None:
                    why = f'{str(arg)[:40]!r} is not a part of this page\'s layout'
            elif action == 'reduce_text':
                if not isinstance(arg, int) or isinstance(arg, bool) or not 5 <= arg <= 60:
                    why = 'reduce_text needs a word limit from 5 to 60'
                elif not any(b.kind == 'text' for b in p.boxes):
                    why = 'the page has no text'
            elif action == 'swap_image':
                if arg is not None and (not isinstance(arg, int) or isinstance(arg, bool) or arg < 0):
                    why = 'swap_image needs an image index or null'
                elif not any(b.kind == 'image' for b in p.boxes):
                    why = 'the page has no picture'
            elif action == 'recolor_accent':
                if arg not in TOKEN_COLORS:
                    why = f'{str(arg)[:40]!r} is not a colour token'
                elif arg == 'accent':
                    why = 'the accent already uses that colour'
                elif ds is not None and contrast(hex_of(arg, ds), hex_of('bg', ds)) < 3.0:
                    why = f'{arg} is too faint against the background'
            elif action == 'enlarge_title':
                if not isinstance(arg, int) or isinstance(arg, bool) or not 1 <= arg <= 2:
                    why = 'enlarge_title needs 1 or 2 steps'
                elif _title(p) is None:
                    why = 'the page has no title'
            elif action == 'add_icon':
                if icon_names is None:
                    icon_names = _icon_names()
                if not isinstance(arg, str) or icon_names is None or arg not in icon_names:
                    why = f'{str(arg)[:40]!r} is not a bundled icon'
                elif any(b.kind == 'icon' and b.slot is None for b in p.boxes):
                    why = 'the page already has an added icon'
        if why:
            dropped.append(f'page {page} {action}: {why}')
            continue
        seen.add((page, action))
        edits.append(CriticEdit(page, action, arg))
    return edits, dropped


def _title(page):
    for name in ('title', 'quote', 'heading', 'number'):
        for b in page.boxes:
            if b.kind == 'text' and b.slot == name:
                return b
    return None


def _direction_index(plan: DesignPlan, page: int) -> int:
    """The art-direction entry a page came from (split pages share their first page's entry)."""
    d = getattr(plan.pages[page], 'direction', None)
    if d is not None:
        return d
    return sum(1 for p in plan.pages[:page + 1] if not p.continued) - 1


def _adopt(plan: DesignPlan, new: DesignPlan) -> None:
    for k in ('pages', 'direction', 'assets', 'fonts', 'system'):
        setattr(plan, k, getattr(new, k))


def _relayout(plan, spec, ds, ws, page: int, layout_id: str) -> bool:
    from .layout import relayout
    new = relayout(copy.deepcopy(plan), spec, ds, ws, layouts={page: layout_id})
    if new is None:
        return False
    _adopt(plan, new)
    return True


def _restep(box, steps: int, ds, fmt: str) -> bool:
    """A text box `steps` type steps larger, rewrapped; False (unchanged) when it no longer fits its box."""
    from .plan import TYPE_STEPS
    from .thumbs import wrap
    if box.step not in TYPE_STEPS:
        return False
    k = min(len(TYPE_STEPS) - 1, TYPE_STEPS.index(box.step) + steps)
    if k == TYPE_STEPS.index(box.step):
        return False
    step = TYPE_STEPS[k]
    size = ds.size(step, fmt)
    text = '\n'.join(box.lines or [])
    if not text.strip():
        return False
    lines = wrap(' '.join(text.split()), box.font, size, box.w, bold=box.bold, italic=box.italic, fmt=fmt)
    if len(lines) * size * (box.line_height or 1.2) > box.h + 0.5:
        return False
    box.step, box.size, box.lines = step, size, lines
    return True


def apply_edit(plan: DesignPlan, edit: CriticEdit, spec: dict, ds: DesignSystem, ws: Workspace) -> bool:
    """Apply one edit in place by code (layout.refit / relayout of one page); True when the plan changed."""
    i = edit.page - 1
    if not 0 <= i < len(plan.pages):
        return False
    p = plan.pages[i]
    a, arg = edit.action, edit.arg
    if a == 'change_layout':
        return _relayout(plan, spec, ds, ws, i, str(arg))
    if a == 'enlarge_title':
        t = _title(p)
        return t is not None and _restep(t, int(arg or 1), ds, plan.format)
    if a == 'emphasize':
        box = next((b for b in p.boxes if b.slot == arg), None)
        if box is None:
            return False
        if box.kind == 'text':
            return _restep(box, 1, ds, plan.format)
        target = {'image': ('full-bleed-image-caption', 'full-figure'),
                  'diagram': ('full-width-diagram', 'full-figure'), 'chart': ('chart-focus', 'full-figure'),
                  'table': ('full-width-diagram', 'full-figure')}.get(box.kind)
        if not target:
            return False
        want = target[0] if plan.format == 'pptx' else target[1]
        return want != p.layout and _relayout(plan, spec, ds, ws, i, want)
    if a == 'reduce_text':
        from .layout import refit
        body = max((b for b in p.boxes if b.kind == 'text' and b.slot not in ('title', 'heading')),
                   key=lambda b: len(' '.join(b.lines or []).split()), default=None)
        return bool(refit(plan, spec, i, 'rebalance', ds, ws, box=body.id if body else None, arg=int(arg)))
    if a == 'swap_image':
        n = sum(1 for s in spec.get('sections') or [] for b in s.get('blocks') or [] if b.get('type') == 'image')
        d = plan.direction.get('pages') or []
        k = _direction_index(plan, i)
        if n < 2 or not 0 <= k < len(d):
            return False
        cur = d[k].get('image')
        new = arg if arg is not None else ((cur if cur is not None else -1) + 1) % n
        if new == cur or not 0 <= new < n:
            return False
        old = copy.deepcopy(plan.direction)
        plan.direction['pages'][k]['image'] = new
        try:
            ok = _relayout(plan, spec, ds, ws, i, p.layout)
        except Exception:
            plan.direction = old
            raise
        if not ok:
            plan.direction = old
        return ok
    if a == 'recolor_accent':
        from .thumbs import hex_of
        colour = hex_of(str(arg), ds)
        if colour == hex_of('accent', ds):
            return False
        ds.colors['accent'] = colour
        plan.system.setdefault('colors', {})['accent'] = colour
        return True
    if a == 'add_icon':
        return _add_icon(plan, i, str(arg), ds)
    return False


def _add_icon(plan: DesignPlan, i: int, name: str, ds) -> bool:
    """A small accent-coloured icon in a free top or bottom corner inside the safe margins."""
    from .plan import Box, BoxStyle
    from .qa import _inter
    p = plan.pages[i]
    sp = ds.spacing
    m = sp.slide_margin if plan.format == 'pptx' else sp.page_margin
    t = _title(p)
    s = round(max(24.0, min(64.0, (t.size or 32.0) * 1.2 if t is not None else 40.0)))
    for x, y in ((p.w - m - s, m), (p.w - m - s, p.h - m - s), (m, p.h - m - s)):
        cand = Box(f'p{i}.icon.{name}', 'icon', x, y, s, s, z=max((b.z for b in p.boxes), default=0) + 1,
                   content=f'icon:{name}', alt=name.replace('-', ' '), style=BoxStyle(text_color='accent'))
        if not any(_inter(cand, b) > 0 for b in p.boxes):
            p.boxes.append(cand)
            return True
    # layouts usually fill the grid: then the icon goes at the free right end of a left-aligned title's row
    if t is not None and t.lines and t.align == 'left':
        from .thumbs import measure
        used = max(measure(line, t.font, t.size or 32.0, bold=t.bold, fmt=plan.format) for line in t.lines)
        s = min(s, t.h)
        if used + sp.unit * 2 + s <= t.w:
            cand = Box(f'p{i}.icon.{name}', 'icon', t.x + t.w - s, t.y + (t.h - s) / 2, s, s, z=t.z + 1,
                       content=f'icon:{name}', alt=name.replace('-', ' '), overlay_ok=True,
                       style=BoxStyle(text_color='accent'))
            if not any(_inter(cand, b) > 0 for b in p.boxes if b is not t):
                p.boxes.append(cand)
                return True
    return False

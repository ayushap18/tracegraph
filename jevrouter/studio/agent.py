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

import asyncio
import copy
import time
from contextlib import contextmanager
from dataclasses import asdict, replace

from .. import config
from ..config import studio_formats
from .plan import CHECK_IDS, PAINTED, PHASES, DesignPlan, DesignResult, QaResult, RestyleOptions
from .workspace import Workspace

_D123 = ('D1', 'D2', 'D3')
# fix actions that change a whole page: after one of them, the page's other failures wait for the next QA round
_PAGE_ACTIONS = ('compact', 'split', 'swap_layout', 'snap')


def enabled(fmt: str) -> bool:
    """Studio designs this format now (TG_STUDIO; default: pptx and pdf)."""
    return fmt in studio_formats()


# ---------- one run's record ----------


class _Run:
    """Phase timings, token use, events, notes, fallbacks and the critic summary of one design run."""

    def __init__(self, ws, deadline: float | None):
        self.ws, self.deadline = ws, deadline
        self.phases: dict[str, dict] = {}
        self.notes: list[str] = []          # plain sentences for the answer and the report
        self.caveats: list[str] = []
        self.fallbacks: list[dict] = []
        self.critic = {'ran': False, 'why': 'The design critic runs only in Deep mode or with Polish.', 'edits': [],
                       'rolled_back': []}
        self.history: dict[tuple, QaResult] = {}   # every failure seen, for fixed=True in the final QA
        self.stop: str | None = None
        self.thumbs = 0

    def add(self, phase: str, *, ms: float = 0, calls: int = 0, llm_in: int = 0, llm_out: int = 0):
        e = self.phases.setdefault(phase, {'phase': phase, 'calls': 0, 'llm_in': 0, 'llm_out': 0, 'ms': 0})
        e['calls'] += int(calls)
        e['llm_in'] += int(llm_in)
        e['llm_out'] += int(llm_out)
        e['ms'] += int(ms)

    def usage(self, phase: str, usage: dict | None):
        u = usage or {}
        self.add(phase, calls=u.get('calls', 0), llm_in=u.get('llm_in', 0), llm_out=u.get('llm_out', 0))

    @contextmanager
    def timed(self, phase: str):
        t0 = time.monotonic()
        try:
            yield
        finally:
            self.add(phase, ms=(time.monotonic() - t0) * 1000)

    def log(self, phase: str, **event):
        if self.ws is None:
            return
        try:
            self.ws.log({'t': round(time.time(), 3), 'phase': phase, **event})
        except Exception:
            pass   # the event log never fails a design

    def late(self) -> bool:
        return self.deadline is not None and time.monotonic() >= self.deadline

    def left(self) -> float | None:
        return None if self.deadline is None else max(0.0, self.deadline - time.monotonic())

    def phase_list(self) -> list[dict]:
        return [dict(self.phases[p]) for p in PHASES if p in self.phases]

    def seen(self, results: list[QaResult]):
        from .qa import failing
        for r in failing(results):
            self.history.setdefault(_key(r), r)

    def final(self, results: list[QaResult]) -> list[QaResult]:
        """The last QA plus every earlier failure that no longer occurs, marked fixed."""
        from .qa import failing
        self.seen(results)
        now = {_key(r) for r in failing(results)}
        return list(results) + [replace(r, fixed=True) for k, r in self.history.items() if k not in now]


def _key(r: QaResult) -> tuple:
    return r.id, r.page, r.box


def _unit(fmt: str, n: int = 1) -> str:
    return ('slide' if fmt == 'pptx' else 'page') + ('s' if n != 1 else '')


# ---------- building blocks ----------


def _theme_preset(spec: dict) -> str | None:
    theme = (spec or {}).get('theme')
    if not theme:
        return None
    try:
        from .presets import for_theme
        return for_theme(theme)
    except Exception:
        return None


def _system(fmt: str, brief, request: str, tokens_src: dict | None, preset: str | None):
    from . import tokens
    theme = getattr(brief, 'theme', None)
    dark = True if theme == 'dark' else None
    return tokens.build_system(preset=preset, design=tokens_src, prompt=request or '', fmt=fmt,
                               font=getattr(brief, 'font', None), dark=dark)


def _mood(request: str) -> list[str]:
    try:
        from .presets import read_prompt
        return list(read_prompt(request or '').mood or [])[:4]
    except Exception:
        return []


def _paper(brief) -> str | None:
    return getattr(brief, 'paper', None)


async def _fonts(ds, http, run: _Run):
    """Every family the system names, ready (cache, installed, or an open-licensed download)."""
    from . import fonts
    fams = []
    for f in asdict(ds.families).values():
        if f and f not in fams:
            fams.append(f)
    with run.timed('assets'):
        for fam in fams:
            left = run.left()
            if left is not None and left <= 0:
                run.log('assets', family=fam, skipped='deadline')
                continue
            try:
                res = await asyncio.wait_for(fonts.ensure(fam, http), timeout=left)
                run.log('assets', family=fam, got=getattr(res, 'family', None), source=getattr(res, 'source', None),
                        licence=getattr(res, 'licence', None))
                note = getattr(res, 'note', None)
                if note and note not in run.caveats:
                    run.caveats.append(note)
                _use_resolved(ds, fam, res)
            except Exception as e:
                run.log('assets', family=fam, error=type(e).__name__)


def _use_resolved(ds, fam: str, res) -> None:
    """A family that resolved to another installed open family (offline, not open, a failed download) is swapped for
    that family in the system, so the layout measures and the painters draw with the font the note names ("so this
    uses Inter") rather than falling further back to a PDF standard font. A built-in stand-in changes nothing: the
    layout already draws those with the standard fonts and says so."""
    got = getattr(res, 'family', None)
    if not got or getattr(res, 'source', None) == 'builtin' or not getattr(res, 'faces', None):
        return
    from .fonts import key
    if key(got) == key(fam):
        return
    for role, f in asdict(ds.families).items():
        if f == fam:
            setattr(ds.families, role, got)


def _render_thumbs(plan: DesignPlan, spec: dict, ws: Workspace, run: _Run) -> list[bytes]:
    from .thumbs import render_page
    out = []
    with run.timed('thumbs'):
        for i in range(len(plan.pages)):
            png = render_page(plan, i, ws, spec=spec)
            try:
                ws.save_thumb(i, png)
            except Exception:
                pass
            out.append(png)
    run.thumbs = len(out)
    return out


def _qa(plan: DesignPlan, ws: Workspace, run: _Run, thumbs: list[bytes] | None = None) -> list[QaResult]:
    from . import qa
    with run.timed('qa'):
        return qa.check(plan, ws, thumbs=thumbs)


def _pick_page(plan: DesignPlan, r: QaResult) -> int | None:
    """The page a whole-file failure is fixed on: for D7 variety, the middle slide of the most used layout (never the
    first or last slide)."""
    if r.page is not None:
        return r.page
    n = len(plan.pages)
    if r.id != 'D7' or n < 3:
        return None
    counts: dict[str, list[int]] = {}
    for i, p in enumerate(plan.pages[1:-1], start=1):
        counts.setdefault(p.layout, []).append(i)
    most = max(counts.values(), key=len)
    return most[len(most) // 2]


def _code_fixes(plan, spec, ds, ws, bad: list[QaResult], tried: dict, run: _Run) -> bool:
    """Each failure's next untried code fix (qa.FIXES order) via layout.refit; True when anything changed. Pages are
    fixed from the last to the first so a split never renumbers a page still waiting for its fix."""
    from . import qa
    from .layout import refit
    changed, done_pages = False, set()
    order = sorted(bad, key=lambda r: (-(r.page if r.page is not None else -1), CHECK_IDS.index(r.id)))
    for r in order:
        page = _pick_page(plan, r)
        if page is None or page in done_pages or not 0 <= page < len(plan.pages):
            continue
        empty = r.id == 'D4' and r.threshold == qa.THRESHOLDS['white_max']
        k = _key(r)
        # "looks empty": grow the page's visual (qa.EMPTY_FIXES); the crowded-page fixes would empty it more
        actions = qa.EMPTY_FIXES if empty else qa.FIXES[r.id]
        for n in range(tried.get(k, 0), len(actions)):
            action = actions[n]
            tried[k] = n + 1
            try:
                ok = bool(refit(plan, spec, page, action, ds, ws, box=r.box))
            except Exception as e:
                ok = False
                run.log('fix', check=r.id, page=page, box=r.box, action=action, error=type(e).__name__)
            else:
                run.log('fix', check=r.id, page=page, box=r.box, action=action, changed=ok)
            if ok:
                changed = True
                if action in _PAGE_ACTIONS:
                    done_pages.add(page)
                break
    return changed


def _fix_loop(plan: DesignPlan, spec: dict, ds, ws: Workspace, run: _Run, *, rounds: int | None = None):
    """Rounds of thumbnails -> QA -> code fixes (0 tokens) until QA passes, the round limit, or the deadline.
    Returns (results, thumbnails) of the last round."""
    from . import qa
    rounds = rounds or config.STUDIO_ROUNDS
    tried: dict[tuple, int] = {}
    results, pngs = [], []
    before = None     # (plan, results, thumbnails) before the last round of fixes
    for r in range(1, rounds + 1):
        plan.rounds += 1
        pngs = _render_thumbs(plan, spec, ws, run)
        results = _qa(plan, ws, run, pngs)
        bad = qa.failing(results)
        run.log('qa', round=plan.rounds, score=qa.score(results),
                failures=[{'id': x.id, 'page': x.page, 'box': x.box} for x in bad[:30]])
        if before is not None and qa.score(results) < qa.score(before[1]):
            # the last fixes made the design worse: undo them and keep the better version
            _adopt(plan, before[0])
            plan.rounds = r
            results, pngs = before[1], before[2]
            _render_thumbs(plan, spec, ws, run)
            run.log('fix', rolled_back=True, why='the fixes lowered the design score')
            run.stop = 'rounds'
            break
        run.seen(results)
        if not bad:
            run.stop = 'pass'
            break
        if r == rounds:
            run.stop = 'rounds'
            break
        if run.late():
            run.stop = 'deadline'
            break
        before = (copy.deepcopy(plan), results, pngs)
        with run.timed('fix'):
            changed = _code_fixes(plan, spec, ds, ws, bad, tried, run)
        if not changed:
            run.stop = 'rounds'
            run.log('fix', note='no code fix left to try')
            break
    return results, pngs


def _adopt(plan: DesignPlan, other: DesignPlan):
    """Restore plan's contents from another plan in place (the caller keeps its reference)."""
    plan.__dict__.update(copy.deepcopy(other).__dict__)


async def _critic_rounds(plan: DesignPlan, spec: dict, ds, ws: Workspace, engine, run: _Run, results, pngs, *,
                         rounds: int) -> list[QaResult]:
    """Up to `rounds` critic calls on contact sheets; every edit applied by code and re-checked, and rolled back when
    it lowers the score or adds a D1-D3 failure."""
    from . import critic, qa
    from .thumbs import SHEET_COLS, SHEET_ROWS, contact_sheet
    if not critic.can_see(engine):
        label = getattr(engine, 'label', None) or 'The engine'
        why = f"{label} can't read images, so the design critic was skipped." if engine is not None else \
            'No model was used, so the design critic was skipped.'
        run.critic['why'] = why
        run.notes.append(why)
        return results
    budget, spent = config.STUDIO_CRITIC_TOKENS, 0
    per = SHEET_COLS * SHEET_ROWS
    for n in range(rounds):
        if run.late():
            run.critic['why'] = 'The time budget ran out before the critic.'
            run.stop = run.stop if not qa.failing(results) else 'deadline'
            break
        remaining = budget - spent
        if remaining < critic.MIN_OUT:
            run.critic['why'] = 'The critic token budget is spent.'
            if qa.failing(results):
                run.stop = 'budget'
            break
        if n:
            pngs = _render_thumbs(plan, spec, ws, run)
        sheets = [contact_sheet(pngs[k:k + per], first_page=k + 1) for k in range(0, len(pngs), per)]
        rep = qa.report(plan, results)
        with run.timed('critic'):
            reply = await critic.critique(plan, rep, sheets, engine, budget_tokens=remaining)
        called = bool(reply.llm_in or reply.llm_out or reply.edits or reply.note is None)
        run.add('critic', calls=int(called), llm_in=reply.llm_in, llm_out=reply.llm_out)
        spent += reply.llm_in + reply.llm_out
        plan.tokens['critic_in'] = plan.tokens.get('critic_in', 0) + reply.llm_in
        plan.tokens['critic_out'] = plan.tokens.get('critic_out', 0) + reply.llm_out
        run.log('critic', round=n + 1, edits=[asdict(e) for e in reply.edits], dropped=reply.dropped,
                note=reply.note, llm_in=reply.llm_in, llm_out=reply.llm_out)
        if called:
            run.critic['ran'] = True
            run.critic['why'] = 'Deep mode or Polish with an engine that reads images.'
        if reply.note:
            run.critic['why'] = reply.note
            if not called:
                break
        if not reply.edits:
            break
        accepted = 0
        for e in reply.edits:
            before = qa.score(results)
            had = {_key(r) for r in qa.failing(results) if r.id in _D123}
            snap, colours = copy.deepcopy(plan), dict(ds.colors)
            edit = {'page': e.page, 'action': e.action, 'arg': e.arg}
            try:
                ok = critic.apply_edit(plan, e, spec, ds, ws)
            except Exception as ex:
                ok = False
                _adopt(plan, snap)
                ds.colors.clear()
                ds.colors.update(colours)
                run.log('critic', edit=edit, error=type(ex).__name__)
            if not ok:
                run.critic['edits'].append({**edit, 'applied': False})
                continue
            new = _qa(plan, ws, run)
            added = {_key(r) for r in qa.failing(new) if r.id in _D123} - had
            if qa.score(new) < before or added:
                why = 'it lowered the design score' if qa.score(new) < before else 'it broke a readability check'
                _adopt(plan, snap)
                ds.colors.clear()
                ds.colors.update(colours)
                run.critic['rolled_back'].append({**edit, 'why': why})
                run.log('critic', rolled_back=edit, why=why)
                continue
            run.seen(new)
            results = new
            accepted += 1
            run.critic['edits'].append({**edit, 'applied': True})
        if not accepted:
            break
    return results


def _direction_page(plan: DesignPlan, k: int) -> int | None:
    """The first page made from direction entry k."""
    for i, p in enumerate(plan.pages):
        if getattr(p, 'direction', None) == k:
            return i
    if any(getattr(p, 'direction', None) is not None for p in plan.pages):
        return None
    seen = -1
    for i, p in enumerate(plan.pages):
        if not p.continued:
            seen += 1
        if seen == k:
            return i
    return None


def _relayout_page(plan, spec, ds, ws, page: int, layout_id: str) -> bool:
    from .layout import relayout
    new = relayout(copy.deepcopy(plan), spec, ds, ws, layouts={page: layout_id})
    if new is None:
        return False
    for k in ('pages', 'direction', 'assets', 'fonts'):
        setattr(plan, k, getattr(new, k))
    return True


async def _freeform(plan: DesignPlan, spec: dict, ds, ws: Workspace, engine, run: _Run, direction, mode: str,
                    results: list[QaResult]) -> list[QaResult]:
    """At most STUDIO_FREEFORM_PAGES pages drawn from a model's shape program; a page whose program is invalid or
    fails QA twice falls back to a library layout."""
    from . import freeform, library, qa
    want = [k for k, d in enumerate(direction.pages) if d.freeform]
    if not want:
        return results
    if len(want) > config.STUDIO_FREEFORM_PAGES:
        run.notes.append(f'Only {config.STUDIO_FREEFORM_PAGES} freeform pages are allowed per file; the others use '
                         f'library layouts.')
        want = want[:config.STUDIO_FREEFORM_PAGES]
    if engine is None or mode == 'quick':
        run.notes.append('Freeform pages use library layouts because no model was used.')
        return results
    for k in want:
        i = _direction_page(plan, k)
        if i is None:
            continue
        page = plan.pages[i]
        secs = spec.get('sections') or []
        section = secs[page.section] if page.section is not None and 0 <= page.section < len(secs) else \
            {'heading': spec.get('title') or '', 'blocks': []}
        refs = [b.content for b in page.boxes if b.kind in ('image', 'diagram', 'chart')
                and (b.content or '').startswith(('ws:', 'asset:'))]
        original = copy.deepcopy(page)
        feedback: list[str] = []
        done = False
        for attempt in (1, 2):
            if run.late():
                feedback = ['the time budget ran out']
                break
            with run.timed('freeform'):
                program, usage = await freeform.compose(i, section, ds, engine, ws, fmt=plan.format, refs=refs,
                                                        budget_tokens=config.STUDIO_FREEFORM_TOKENS,
                                                        feedback=feedback)
            run.usage('freeform', usage)
            plan.tokens['freeform_in'] = plan.tokens.get('freeform_in', 0) + usage.get('llm_in', 0)
            plan.tokens['freeform_out'] = plan.tokens.get('freeform_out', 0) + usage.get('llm_out', 0)
            if program is None:
                feedback = list(usage.get('problems') or ['the reply was not a valid shape program'])
                run.log('freeform', page=i, attempt=attempt, rejected=feedback[:8])
                continue
            with run.timed('freeform'):
                boxes = freeform.to_boxes(program, (page.w, page.h), ds, spec, fmt=plan.format, ws=ws)
            page.boxes, page.layout, page.freeform = boxes, 'freeform', True
            new = _qa(plan, ws, run)
            mine = [r for r in qa.failing(new) if r.page == i]
            run.log('freeform', page=i, attempt=attempt, shapes=len(program.shapes),
                    failures=[{'id': r.id, 'box': r.box} for r in mine])
            if not mine:
                results, done = new, True
                break
            feedback = [r.note for r in mine]
            plan.pages[i] = page = copy.deepcopy(original)
        if done:
            continue
        plan.pages[i] = copy.deepcopy(original)
        d_layout = direction.pages[k].layout
        ids = set(library.for_format(plan.format))
        if original.layout in ids:
            to = original.layout      # the layout engine already laid the page out from the library: keep it
        else:
            try:
                to = library.next_best(d_layout) or d_layout
            except Exception:
                to = d_layout
            try:
                _relayout_page(plan, spec, ds, ws, i, to)
            except Exception as e:
                run.log('freeform', page=i, fallback_error=type(e).__name__)
        plan.pages[i].freeform = False
        why = 'failed QA twice' if feedback and not str(feedback[0]).startswith('the engine') else \
            'the model could not draw it'
        run.fallbacks.append({'page': i, 'from': 'freeform', 'to': to, 'why': why})
        run.log('freeform', page=i, fallback=to, why=why)
        results = _qa(plan, ws, run)
    return results


def _answer_notes(plan: DesignPlan, results: list[QaResult], run: _Run) -> tuple[list[str], list[str]]:
    from . import qa
    n = len(plan.pages)
    notes = []
    if n:
        notes.append(f'Studio designed {n} {_unit(plan.format, n)} in the {plan.preset} style '
                     f'(design score {plan.score}/100).')
    notes += [x for x in run.notes if x not in notes]
    caveats = list(run.caveats)
    for f in plan.fonts:
        if f.note and f.note not in caveats:
            caveats.append(f.note)
    for sysnote in (plan.system or {}).get('notes') or []:
        if sysnote not in caveats:
            caveats.append(sysnote)
    bad = [r for r in qa.failing(results) if r.id in _D123]   # the softer checks stay in the design report
    if bad:
        names = sorted({qa.CHECKS[r.id][0].lower() for r in bad})
        caveats.append('Some design checks still flag issues: ' + ', '.join(names) + '.')
    return notes, caveats


def _finish(plan: DesignPlan, spec: dict, ds, ws: Workspace, run: _Run) -> DesignResult:
    """Final thumbnails and QA, paint, save the plan and the report."""
    from . import qa
    pngs = _render_thumbs(plan, spec, ws, run)
    try:
        for extra in ws.thumbs()[len(plan.pages):]:   # pages removed since an earlier round
            extra.unlink(missing_ok=True)
    except Exception:
        pass
    results = run.final(_qa(plan, ws, run, pngs))
    plan.qa, plan.score = results, qa.score(results)
    plan.system = ds.to_dict()
    plan.stop = 'pass' if not qa.failing(results) else (run.stop if run.stop not in (None, 'pass') else 'rounds')
    try:
        with run.timed('paint'):
            data = paint(plan, spec, ws)
    except Exception as e:
        run.log('paint', error=type(e).__name__, why=str(e)[:200])
        raise
    notes, caveats = _answer_notes(plan, results, run)
    report = qa.report(plan, results, critic=run.critic, fallbacks=run.fallbacks, notes=run.notes, thumbs=len(pngs))
    report['phases'] = run.phase_list()
    run.log('done', score=plan.score, stop=plan.stop, rounds=plan.rounds)
    ws.save_plan(plan)
    ws.save_report(report)
    result = DesignResult(plan, data, report, run.phase_list(), notes, caveats)
    result.meta_design = meta_design(result)
    return result


def _flow(spec: dict, fmt: str, ds, ws: Workspace, run: _Run, file_id: str) -> DesignResult:
    """docx/md/xlsx: styles, figure placement and breaks from the same tokens; the legacy renderer paints."""
    from . import qa
    from .layout import flow_styles
    with run.timed('layout'):
        flow = flow_styles(spec, fmt, ds)
    plan = DesignPlan(file_id, fmt, ds.id, ds.to_dict(), {'preset': ds.id, 'mood': [], 'dark': ds.dark, 'pages': [],
                                                          'source': 'keyless', 'notes': []},
                      flow=flow, stop='keyless', score=100)
    note = 'This format flows, so Studio set its styles and figure placement; the page checks do not apply.'
    report = qa.report(plan, [], notes=[note])
    report['phases'] = run.phase_list()
    run.log('flow', format=fmt, preset=ds.id)
    ws.save_plan(plan)
    ws.save_report(report)
    result = DesignResult(plan, None, report, run.phase_list(), [], list(ds.notes))
    result.meta_design = meta_design(result)
    return result


def _hold_count(outline, fmt: str, brief) -> None:
    """A deck held to the slide count asked for has no Studio-made closing slide when that slide would go over it
    (the writer already spent every slide on content). The direction then has no closing page, and the layout
    engine keeps it that way on a restyle."""
    slides = getattr(brief, 'slides', None)
    if fmt != 'pptx' or not slides or not outline.pages:
        return
    last = outline.pages[-1]
    if last.section is None and last.index > 0 and len(outline.pages) > slides[1]:
        outline.pages.pop()


def count_pages(spec: dict, fmt: str, *, brief=None, request: str = '', tokens_src: dict | None = None,
                preset: str | None = None) -> int:
    """How many slides or pages Studio will make of a normalized spec, laid out keyless in a throwaway workspace
    (0 tokens, no fonts fetched, nothing painted). The long writer's fit loop measures with it, so a file asked for
    at N pages is N pages as Studio draws it, not as the standard renderer would."""
    import tempfile
    from pathlib import Path
    from . import direct, layout
    ds = _system(fmt, brief, request, tokens_src, preset or _theme_preset(spec))
    if fmt not in PAINTED:
        raise ValueError(f'Studio does not paginate {fmt}')
    outline = direct.outline_of(spec, fmt)
    _hold_count(outline, fmt, brief)
    direction = direct.direct_keyless(outline, ds, dark=ds.dark)
    with tempfile.TemporaryDirectory(prefix='tg-count-') as tmp:
        ws = Workspace('count', root=Path(tmp))
        plan = layout.lay_out(spec, fmt, ds, direction, ws, file_id='count', paper=_paper(brief))
    return len(plan.pages)


# ---------- entry points ----------


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
    from . import direct, layout
    from .workspace import open_workspace
    budget_end = time.monotonic() + config.STUDIO_TIME_BUDGET
    deadline = min(deadline, budget_end) if deadline else budget_end
    ws = open_workspace(file_id, sandbox=sandbox)
    run = _Run(ws, deadline)
    forced = preset or _theme_preset(spec)
    ds = _system(fmt, brief, request, tokens_src, forced)
    run.log('tokens', preset=ds.id, dark=ds.dark, source=list(ds.source))
    if fmt not in PAINTED:
        return _flow(spec, fmt, ds, ws, run, file_id)
    usage = {'calls': 0, 'llm_in': 0, 'llm_out': 0, 'ms': 0}
    with run.timed('direct'):
        outline = direct.outline_of(spec, fmt)
        _hold_count(outline, fmt, brief)
        if engine is not None and mode != 'quick':
            direction, usage = await direct.direct_llm(outline, ds, engine, mood=_mood(request),
                                                       max_tokens=config.STUDIO_DIRECT_TOKENS, effort='low')
        else:
            direction = direct.direct_keyless(outline, ds, dark=ds.dark)
    run.usage('direct', usage)
    if forced is None and not tokens_src and direction.preset and direction.preset != ds.id:
        ds = _system(fmt, brief, request, tokens_src, direction.preset)   # prompt words still win over the director
    run.log('direct', source=direction.source, preset=direction.preset, mood=list(direction.mood),
            layouts=[p.layout for p in direction.pages], freeform=[k for k, p in enumerate(direction.pages)
                                                                  if p.freeform], notes=list(direction.notes))
    await _fonts(ds, http, run)
    with run.timed('layout'):
        plan = layout.lay_out(spec, fmt, ds, direction, ws, file_id=file_id, paper=_paper(brief))
    plan.tokens['direct_in'] = plan.tokens.get('direct_in', 0) + int(usage.get('llm_in', 0))
    plan.tokens['direct_out'] = plan.tokens.get('direct_out', 0) + int(usage.get('llm_out', 0))
    run.log('layout', pages=len(plan.pages), layouts=[p.layout for p in plan.pages])
    results, pngs = _fix_loop(plan, spec, ds, ws, run)
    if mode == 'deep':
        results = await _critic_rounds(plan, spec, ds, ws, engine, run, results, pngs,
                                       rounds=config.STUDIO_CRITIC_ROUNDS)
    results = await _freeform(plan, spec, ds, ws, engine, run, direction, mode, results)
    return _finish(plan, spec, ds, ws, run)


def restyle(spec: dict, fmt: str, ws: Workspace, options: RestyleOptions, *, file_id: str) -> DesignResult:
    """0 tokens: the stored plan's direction re-laid out with another preset/fonts/dark/template/layout overrides,
    re-checked and painted into a new workspace `file_id` (seeded from ws)."""
    from . import direct, layout, presets, tokens
    from .tokens import DesignSystem
    old = ws.load_plan()
    if old is None:
        raise LookupError('no-plan')
    new_ws = ws.copy_to(file_id)
    run = _Run(new_ws, time.monotonic() + config.STUDIO_TIME_BUDGET)
    tpl = presets.template(options.template) if options.template else None
    preset = options.preset or (tpl.preset if tpl else None)
    ds = tokens.build_system(preset=preset, fmt=fmt) if preset else DesignSystem.from_dict(old.system)
    if options.dark is not None:
        ds = tokens.with_dark(ds, options.dark)
    for role, family in (options.fonts or {}).items():
        if role in ('display', 'heading', 'body', 'caption') and family:
            setattr(ds.families, role, family)
    if options.print_version:
        ds = tokens.print_version(ds)
    run.log('restyle', preset=ds.id, dark=ds.dark, fonts=options.fonts, template=options.template,
            layouts=options.layouts, print_version=options.print_version)
    if fmt not in PAINTED:
        return _flow(spec, fmt, ds, new_ws, run, file_id)
    with run.timed('layout'):
        if old.format == fmt and old.pages:
            plan = layout.relayout(old, spec, ds, new_ws, layouts=options.layouts)
        else:
            direction = direct.direct_keyless(direct.outline_of(spec, fmt), ds, dark=ds.dark)
            direction.source = 'restyle'
            plan = layout.lay_out(spec, fmt, ds, direction, new_ws, file_id=file_id,
                                  paper=getattr(tpl, 'paper', None) if tpl else None)
    plan.file_id, plan.preset, plan.system = file_id, ds.id, ds.to_dict()
    plan.rounds, plan.stop, plan.qa, plan.score = 0, None, [], None
    plan.tokens = {k: 0 for k in ('direct_in', 'direct_out', 'critic_in', 'critic_out', 'freeform_in',
                                  'freeform_out')}
    if isinstance(plan.direction, dict):
        plan.direction['source'] = 'restyle'
    _fix_loop(plan, spec, ds, new_ws, run)
    return _finish(plan, spec, ds, new_ws, run)


async def polish(spec: dict, fmt: str, ws: Workspace, engine, *, file_id: str,
                 deadline: float | None = None) -> DesignResult:
    """One critic round on the stored design (vision engines only), edits applied by code, QA re-run, painted into a
    new workspace `file_id`. Raises ValueError('no-vision') when the engine can't read images, and
    LookupError('no-plan') when ws has no plan."""
    from .critic import can_see
    from .tokens import DesignSystem
    if not can_see(engine):
        raise ValueError('no-vision')
    old = ws.load_plan()
    if old is None:
        raise LookupError('no-plan')
    new_ws = ws.copy_to(file_id)
    budget_end = time.monotonic() + config.STUDIO_TIME_BUDGET
    run = _Run(new_ws, min(deadline, budget_end) if deadline else budget_end)
    plan = copy.deepcopy(old)
    plan.file_id, plan.rounds, plan.stop = file_id, 0, None
    plan.tokens = {k: 0 for k in ('direct_in', 'direct_out', 'critic_in', 'critic_out', 'freeform_in',
                                  'freeform_out')}
    ds = DesignSystem.from_dict(plan.system)
    run.log('polish', engine=getattr(engine, 'name', None))
    plan.rounds += 1
    pngs = _render_thumbs(plan, spec, new_ws, run)
    results = _qa(plan, new_ws, run, pngs)
    run.seen(results)
    await _critic_rounds(plan, spec, ds, new_ws, engine, run, results, pngs, rounds=1)
    return _finish(plan, spec, ds, new_ws, run)


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
    plan = result.plan
    return {'studio': True, 'preset': plan.preset,
            'fonts': [{'family': f.family, 'role': f.role, 'source': f.source, 'licence': f.licence,
                       'embedded': f.embedded, 'note': f.note} for f in plan.fonts],
            'score': plan.score, 'notes': list(result.notes), 'thumbs': int((result.report or {}).get('thumbs', 0))}

"""The create agent (docs/PLAN-files.md): a request becomes one DocSpec, and code turns the spec into a checked file.

Zero-token paths come first, and work keyless: the chat's last created file re-rendered in another format ("now as
slides"), an earlier step's answer or the previous answer in the chat parsed from Markdown ("put that in a PDF"), an
attached table ("turn this CSV into a spreadsheet"). Only when none of them fits is the engine asked, once, for a spec
under DOCSPEC_SCHEMA with a trimmed context and a per-format token cap. Every spec passes Jev's safety check (X4), then
normalize -> render -> verify (docs/RULES-files.md); a block rule means an honest answer and no file.

docs/PLAN-accuracy-v2.md C1-C8: the request's brief (pages or slides, theme, font, images, diagrams) decides the path.
A file an earlier step made is reused (converted at 0 tokens when it holds enough, else the seed of a longer one), an
earlier step's answer is copied only when the brief asks for no more than that, and a long, illustrated or diagrammed
file goes to the long-document writer (create/longdoc.py). What the file could not honour comes back as caveats.
"""
import asyncio
import contextvars
import csv
import dataclasses
import io
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

from .. import create as cf
from ..config import (BLOCK_AT, CREATE_CONTEXT_CHARS, CREATE_MAX_TOKENS, CREATE_SAFETY_CHARS, CREATE_SAFETY_CHUNKS,
                      CREATE_TABLE_ROWS, STUDIO_GRACE, STUDIO_TIME_BUDGET)
from ..create import brief as brief_mod
from ..create import design as design_mod
from ..create.brief import Brief, asks_more, diagrams_min, parse_brief
from ..engines import EngineError, EngineRefusal
from ..create import fonts, themes
from ..create.rules import span
from ..jev import unsafe_score
from ..planner import FILE_FORMATS, is_file_request
from .llm import image_ideas

LABELS = {'pdf': 'PDF', 'docx': 'Word', 'pptx': 'PowerPoint', 'xlsx': 'Excel', 'md': 'Markdown'}
TURNS = 3  # earlier turns the spec call may see
LOOKBACK = 10  # finished turns of a chat searched for its last created file and the answer to put in a file

# "now as slides", "in Word please", "and as a spreadsheet": the request is only a format.
LEAD_FORMAT = re.compile(r'^\s*(?:(?:ok(?:ay)?|now|and|also|then|great|thanks|thank you|please|same|same thing|do it|'
                         r'can you do it|could you do it)[\s,!.]+)*(?:as|in|into|to)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?'
                         rf'(?:{FILE_FORMATS})\b', re.I)
CONVERT = re.compile(r'\bconvert\b|\bre-?(?:make|render|export|do)\b|\binstead\b|\bas\s+well\b|\b(?:another|other|'
                     r'different)\s+format\b|\b(?:same|this|that|the)\s+(?:thing|one)\b', re.I)
# "the pdf", "that deck", "the file": the request names a file made earlier.
FILE_REF = re.compile(r'\b(?:the|that|this|your|my)\s+(?:file|document|doc|pdf|deck|slides?|presentation|spreadsheet|'
                      r'workbook|sheet|excel|word\s+(?:doc|document|file)|markdown)\b', re.I)
# "notes of our conversation", "summarise this chat": the file is about every earlier turn, not only the last one.
CONVERSATION = re.compile(r'\b(?:our|this|the|whole|entire|full)\s+(?:chat|conversation|discussion|thread)\b', re.I)
REFERS = re.compile(r'\b(?:that|this|it|these|those|above|previous|last|earlier|same|the\s+(?:answer|reply|response|'
                    r'result|results|summary|explanation|list|table)|your\s+(?:answer|reply|response)|what\s+you\s+'
                    r'(?:said|wrote|found)|our\s+(?:chat|conversation|discussion))\b', re.I)
# Asks for new writing, not a copy of what is already there: a table plus one of these goes to the engine.
GENERATIVE = re.compile(r'\b(?:summari[sz]e|summary|analy[sz]e|analysis|insights?|explain|write\s+(?:up|about)|'
                        r'research|recommend|interpret|findings|trends?)\b', re.I)
DEEP = re.compile(r'\b(?:research|in[- ]depth|detailed|comprehensive|thorough|deep[- ]dive|long|full)\b', re.I)
THEME = brief_mod.THEMES  # mono (black and white), dark, warm: the same patterns the brief reads
# B3: lines only the chat shows, never a file: "**weather**: ..." labels, a create step's "Created **x.pdf**, 4 pages"
# reply and its "No format was named" note
AGENT_LABEL = re.compile(r'^\*\*[a-z_]+\*\*:\s*', re.M)
REPLY_LINE = re.compile(r'^(?:Created \*\*.+?\*\*, .*|No format was named.*)$', re.M)
CREATED_ONLY = re.compile(r'^\s*Created \*\*.+?\*\*(?:, [^\n]*)?\s*$')
# B2/C8: a spec note that admits something was left undone becomes a caveat
UNDONE = re.compile(r"\b(not (fetched|included|checked|verified)|could not|couldn't|no (image|source)s?)\b", re.I)
# B5: research notes for a file list image ideas as "IMAGE: <search query> | <caption>"
FILLER = set("""a an the it that this these those me us my our your of in into as to on for from with and or please now can
could would you i we want need make create put turn generate export save write build produce give prepare draft
compile format send package file files document doc version copy nice short simple quick clean new proper full out
pdf docx word slide slides deck presentation pptx powerpoint power point spreadsheet excel xlsx workbook sheet
markdown md report also then just some one here there about regarding using based too well get like have another
instead thanks thank ok okay let lets""".split())
LEAD_ASK = re.compile(r"^(?:what(?:'s| is| are| was| were)?|who(?:'s| is| was| were)?|how (?:do|does|did|to|can) (?:i|you|we)?|"
                      r'tell me (?:about)?|explain|describe|research|find(?: out)?|look up|give me|show me|list|'
                      r'summari[sz]e|compare|why (?:is|are|do|does))\s+', re.I)

SYSTEM = ('Do not use tools. You write the content of one downloadable file as JSON matching the schema: a title, a '
          'subtitle (may be empty) and sections, each with a heading, a level (1 to 3), blocks (paragraph, bullets, '
          'table, chart, quote, code, the diagrams timeline, tree and flow, and a diagram block whose kind is cycle, '
          'venn, pyramid, matrix, mindmap, process, comparison, stat-cards or scatter, with the unused fields empty) '
          'and notes (may be empty). Write content only: no styling, fonts, colours, layout, HTML or Markdown syntax. '
          'Draw a timeline, hierarchy, process, cycle, overlap or comparison as a diagram block, never as text or '
          'code. Use only facts from the request, the context and the '
          'attached data given here, and never ask for anything to be fetched; a figure block (a Wikimedia Commons '
          'search query and a caption) is the only way to ask for a picture, and only when the request asks for images. '
          'Numbers in tables and charts are numbers. {shape}')
SHAPES = {
    'pptx': 'It becomes slides: 4 to 10 sections, one per slide, each a heading and at most 5 bullets of at most 12 '
            'words, or one small table or chart; put anything longer in notes.',
    'xlsx': 'It becomes a spreadsheet: put the content in tables (one per section), with at most a one-line paragraph '
            'beside each.',
    'default': 'It becomes a short document: a clear title, 2 to 8 sections of short paragraphs and bullets, and a '
               'table or chart only where the data calls for one.',
}


@dataclass
class Job:
    request: str
    deps: list[tuple[str, str]] = field(default_factory=list)       # (step text, answer) of the steps this one uses
    context: list[dict] = field(default_factory=list)               # earlier turns [{query, answer}], oldest first
    tables: list[tuple[dict, list, list]] = field(default_factory=list)  # attached tables: (file meta, columns, rows)
    docs: list[tuple[dict, str]] = field(default_factory=list)      # other attached files: (meta, extracted text)
    last_file: tuple[dict, dict, bool] | None = None                # newest file made in this chat: (meta, spec, made
                                                                    # by its latest turn)
    # docs/PLAN-accuracy-v2.md C1: what the request asks of the file, the files dependency steps made, and the aiohttp
    # session for web images (C5; None disables images)
    brief: Brief | None = None
    dep_files: list[tuple[dict, dict]] = field(default_factory=list)   # (CreatedFile meta, stored spec) made by dependency steps
    role: str = 'primary'                                             # 'primary' | 'working'
    http: object | None = None                                        # aiohttp session for assets (C5); None disables images
    # docs/PLAN-files-robust.md 3.3 and 4.3: the checkpoint sink, the run's deadline, a checkpoint to continue from,
    # design files from earlier turns (newest first) and the Tally every call of this job adds to
    checkpoint: Callable[[dict], None] | None = None   # sink the pipeline provides
    deadline: float | None = None                      # time.monotonic() value the run must finish by
    resume: dict | None = None                         # a checkpoint to continue from
    design_docs: list[tuple[dict, str]] = field(default_factory=list)  # design files from earlier turns (4.3)
    tally: object | None = None                        # longdoc.Tally shared by every call this job makes
    design: dict | None = None                         # the spec['design'] make() parsed (set by make)
    design_notes: list[str] = field(default_factory=list)  # what could not be done with the design (set by make)
    sandbox: str | None = None                         # the sandbox a sandbox run's files live in (Studio workspace)


@dataclass
class Made:
    answer: str
    ok: bool
    engine: str = 'keyless'
    llm_in: int = 0
    llm_out: int = 0
    jev_tokens: int = 0
    file: dict | None = None   # the CreatedFile (web/src/protocol.ts)
    spec: dict | None = None   # the spec it was rendered from (stored, so a conversion costs no tokens)
    data: bytes | None = None
    effort: str | None = None
    caveats: list[str] = field(default_factory=list)  # what the file could not honour, in plain words (C8)
    phases: list[dict] = field(default_factory=list)   # [{phase, calls, llm_in, llm_out, ms}] (C3)
    repairs: dict | None = None      # RepairInfo: calls spent re-writing sections that came back unusable
    partial: dict | None = None      # PartialInfo: planned sections that are missing (resume is set by the pipeline)
    checkpoint: dict | None = None   # the latest checkpoint state (docs/PLAN-files-robust.md 3.3)
    reply: str | None = None         # the single path's raw reply, at most 200 KB


# ---------- building a file (also used by POST /api/created/{id}/convert) ----------


def shape_of(meta: dict) -> str:
    n = meta.get('pages') or meta.get('slides') or len(meta.get('sheets') or [])
    what = 'page' if meta.get('pages') else 'slide' if meta.get('slides') else 'sheet' if meta.get('sheets') else None
    if what and n:
        return f'{n} {what}{"" if n == 1 else "s"}'
    return f'{max(1, round(meta["size"] / 1024))} KB'


def build(spec: dict, fmt: str, *, source: str, tokens: int = 0, qid: int | None = None, from_id: str | None = None,
          extra: list | tuple = (), brief: Brief | None = None, credits: list | tuple = (),
          role: str | None = None, studio=None, file_id: str | None = None) -> tuple[dict, dict, bytes]:
    """(CreatedFile, the spec to store, the bytes). Blocking (rendering is CPU work), so callers use a thread. Raises
    SpecError when a block rule fails. With a brief, the file is also checked against it (V5-V9) and the meta says
    how it met it (docs/PLAN-accuracy-v2.md C8).

    studio: a studio DesignResult (docs/PLAN-designer.md 9.10). With painted_bytes those are the file (render_safe is
    skipped) and verify() still checks them, plus D1-D8 from the design report; without, the file renders as today and
    the report's checks still apply. file_id: the id finish() gave the design stage (default: a new one)."""
    norm, results = cf.normalize(spec, fmt)
    painted = getattr(studio, 'painted_bytes', None) if studio is not None else None
    report = getattr(studio, 'report', None) if studio is not None else None
    if painted is not None:
        data = bytes(painted)
        laid = [cf.RuleResult('V11', 'fix', True, 'every section laid out by the design stage')]
    else:
        data, laid = cf.render_safe(spec, fmt)  # the renderer's own ladder (V11): a section that cannot be laid out is left out
    checks = cf.verify(spec, fmt, data, brief=brief, extra=[*laid, *extra],  # checked against what was drawn
                       design_report=report if isinstance(report, dict) else None)
    try:
        pv = cf.preview(fmt, data)
    except Exception:
        pv = {}
    meta = {'id': file_id or uuid.uuid4().hex[:12], 'name': cf.file_name(norm['title'], fmt), 'format': fmt, 'size': len(data),
            'created': time.time(), 'qid': qid, 'title': norm['title'], 'pages': pv.get('pages'),
            'slides': pv.get('slides'), 'sheets': [s['name'] for s in pv['sheets']] if pv.get('kind') == 'sheets' else None,
            'tokens': int(tokens), 'source': source, 'from_id': from_id,
            'rules': merged_rules([*results, *extra, *laid, *checks]), 'sandbox': None}
    blocks = [b for sec in norm['sections'] for b in sec['blocks']]
    design = design_mod.clean_design(spec.get('design'))
    if design:
        theme = themes.resolve({'design': design}, paper=fmt in ('docx', 'xlsx'))
        if fmt == 'xlsx':  # the text colours the white sheet really uses; the design's background is still named
            theme = {**cf.sheet_theme(theme), 'bg': theme['bg']}
        choice = fonts.resolve(norm.get('font'), fmt, theme=theme)
        meta['design'] = design_applied(design, theme, choice, fmt, bool(norm.get('font')))
    else:
        choice = fonts.resolve(norm.get('font'), fmt, theme=norm['theme'])
    if studio is not None:
        meta['design'] = studio_design(studio, meta.get('design'))
    meta.update({'theme': norm['theme'], 'font_used': choice.used or None,
                 'diagrams': sum(b['type'] in cf.DIAGRAMS for b in blocks),
                 'images': sum(b['type'] == 'image' for b in blocks) if fmt in ('pdf', 'docx', 'pptx') else 0,
                 'credits': [c.to_dict() if hasattr(c, 'to_dict') else dict(c) for c in credits]})
    if painted is not None:  # the preset's legacy theme name (V8 and older clients read it) and the body font it uses
        meta['theme'] = studio_theme(studio, norm['theme'])
        meta['font_used'] = studio_font(studio) or meta['font_used']
    if brief is not None:
        meta['brief'] = brief_mod.to_dict(brief)
    if role:
        meta['role'] = role
    return meta, spec, data


# ---------- Studio, the design stage (docs/PLAN-designer.md 9.10) ----------

# what make() knows of the run, for finish() wherever it is called from (the long writer too)
STUDIO_RUN: contextvars.ContextVar[dict | None] = contextvars.ContextVar('studio_run', default=None)
DESIGN_ROLES = ('bg', 'surface', 'text', 'heading', 'muted', 'accent', 'border', 'header_bg', 'header_text', 'stripe',
                'code_bg')
_UNSET = object()


def studio_on(fmt: str | None) -> bool:
    """TG_STUDIO designs this format (studio.enabled, the only check). False when Studio can't even be imported."""
    if not fmt:
        return False
    try:
        from .. import studio
        return bool(studio.enabled(fmt))
    except Exception:
        return False


def _plan_of(result):
    return getattr(result, 'plan', None)


def studio_theme(result, default: str) -> str:
    """CreatedFile.theme for a designed file: the legacy theme of its preset (presets.THEME_OF)."""
    try:
        from ..studio.presets import THEME_OF
        return THEME_OF.get(getattr(_plan_of(result), 'preset', None), default)
    except Exception:
        return default


def studio_font(result) -> str | None:
    plan = _plan_of(result)
    for f in getattr(plan, 'fonts', None) or []:
        if getattr(f, 'role', None) == 'body' and getattr(f, 'family', None):
            return f.family
    return None


def _meta_design(result) -> dict:
    """studio.agent.meta_design(result) (or the result's own copy); a minimal one from the plan if that fails."""
    got = getattr(result, 'meta_design', None)
    if not isinstance(got, dict):
        try:
            from ..studio import agent as studio_agent
            got = studio_agent.meta_design(result)
        except Exception:
            got = None
    if isinstance(got, dict):
        return dict(got)
    plan, report = _plan_of(result), getattr(result, 'report', None) or {}
    return {'studio': True, 'preset': getattr(plan, 'preset', None),
            'fonts': [dataclasses.asdict(f) if dataclasses.is_dataclass(f) else dict(f)
                      for f in getattr(plan, 'fonts', None) or []],
            'score': getattr(plan, 'score', None), 'notes': [],
            'thumbs': int(report.get('thumbs') or 0) if isinstance(report, dict) else 0}


def studio_design(result, base: dict | None) -> dict:
    """CreatedFile.design for a designed file: the design file's DesignApplied (when one was used) with Studio's
    fields merged in (studio, preset, fonts, score, thumbs). Without a design file, name is 'preset:<id>', colors are
    the preset's roles and confidence is 1 (docs/PLAN-designer.md 9.9)."""
    extra = _meta_design(result)
    plan = _plan_of(result)
    if base is None:
        system = getattr(plan, 'system', None) or {}
        colors = {k: str(v) for k, v in (system.get('colors') or {}).items() if k in DESIGN_ROLES and v}
        fams = system.get('families') or {}
        used = {getattr(f, 'role', None): getattr(f, 'family', None) for f in getattr(plan, 'fonts', None) or []}
        base = {'name': f'preset:{extra.get("preset") or getattr(plan, "preset", None) or "custom"}', 'colors': colors,
                'palette': [str(c) for c in system.get('chart_palette') or []],
                'heading_font': None, 'body_font': None,
                'fonts_used': {'heading': used.get('heading') or fams.get('heading') or None,
                               'body': used.get('body') or fams.get('body') or None},
                'nudged': [], 'notes': [], 'confidence': 1}
    notes = list(dict.fromkeys([*(base.get('notes') or []), *(extra.get('notes') or [])]))
    return {**base, **extra, 'studio': True, 'notes': notes}


def studio_usage(result) -> dict:
    """{calls, llm_in, llm_out, ms} of a DesignResult's phases."""
    out = {'calls': 0, 'llm_in': 0, 'llm_out': 0, 'ms': 0}
    for p in getattr(result, 'phases', None) or []:
        if isinstance(p, dict):
            for k in out:
                v = p.get(k)
                out[k] += int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    return out


def fell_back_caveat(why: str) -> str:
    why = ' '.join(str(why or '').split())[:140].rstrip('.') or 'an unexpected error'
    return f'The design stage failed ({why}), so the file uses the standard layout.'


def drop_workspace(file_id: str | None, sandbox: str | None = None):
    """Forget a design workspace that no file uses (the design stage fell back). Never raises."""
    if not file_id:
        return
    try:
        from ..studio import workspace
        workspace.open_workspace(file_id, sandbox=sandbox).delete()
    except Exception:
        pass


async def design_file(spec: dict, fmt: str, *, file_id: str, brief=None, request: str = '', engine=None, http=None,
                      mode: str = 'balanced', sandbox: str | None = None, deadline: float | None = None
                      ) -> tuple[object | None, str | None, int]:
    """(DesignResult or None, the fell-back caveat or None, ms) for one file: studio.agent.design on the normalized
    spec, bounded by the time budget. Any failure, or nothing painted for a painted format, is (None, caveat): the
    caller renders with the legacy renderer and a file is never lost to Studio."""
    t0 = time.perf_counter()
    ms = lambda: round((time.perf_counter() - t0) * 1000)  # noqa: E731
    try:
        from .. import studio
        from ..studio.plan import PAINTED
        norm, _ = await asyncio.to_thread(cf.normalize, spec, fmt)
        tokens_src = design_mod.clean_design(spec.get('design'))
        budget = STUDIO_TIME_BUDGET + STUDIO_GRACE
        if deadline is not None:
            budget = max(1.0, min(budget, deadline - time.monotonic()))
        result = await asyncio.wait_for(studio.agent.design(
            norm, fmt, file_id=file_id, brief=brief, request=request or '', tokens_src=tokens_src or None,
            engine=engine, http=http, mode=mode or 'balanced', sandbox=sandbox, deadline=deadline), timeout=budget)
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        drop_workspace(file_id, sandbox)
        return None, fell_back_caveat('it ran out of time'), ms()
    except cf.SpecError:  # the legacy build will name the rule
        drop_workspace(file_id, sandbox)
        return None, None, ms()
    except Exception as e:
        drop_workspace(file_id, sandbox)
        return None, fell_back_caveat(f'{type(e).__name__}: {e}' if str(e) else type(e).__name__), ms()
    if getattr(result, 'fell_back', False) or (fmt in PAINTED and getattr(result, 'painted_bytes', None) is None):
        drop_workspace(file_id, sandbox)
        why = next(iter(getattr(result, 'caveats', None) or []), None) or 'nothing was painted'
        return None, (why if str(why).startswith('The design stage failed') else fell_back_caveat(why)), ms()
    return result, None, ms()


async def build_designed(spec: dict, fmt: str, *, source: str, tokens: int = 0, from_id: str | None = None,
                         extra: list | tuple = (), brief: Brief | None = None, credits: list | tuple = (),
                         role: str | None = None, engine=None, mode: str = 'balanced', http=None, request: str = '',
                         sandbox: str | None = None, deadline: float | None = None, seed_from: str | None = None
                         ) -> tuple[dict, dict, bytes, dict]:
    """build() with the design stage in front when TG_STUDIO designs the format: (CreatedFile, spec, bytes, {usage,
    notes, caveats}). The file id is made first so Studio's workspace is the file's. A conversion (source 'convert')
    is designed keyless; seed_from names a file whose workspace seeds this one (Workspace.copy_to: fonts, images and
    measurements are reused). A design failure, or a painted file that fails a block rule, falls back to the standard
    renderer with one caveat. Raises SpecError like build()."""
    file_id = uuid.uuid4().hex[:12]
    got = {'usage': studio_usage(None), 'notes': [], 'caveats': []}
    designed = None
    if studio_on(fmt):
        if source != 'llm' or not tokens:
            engine = None   # a file made at 0 tokens (an answer, a table, a conversion, a rebuild) is designed keyless
        if seed_from:
            try:
                from ..studio import workspace
                src = workspace.open_workspace(seed_from, sandbox=sandbox)
                if src.exists():
                    await asyncio.to_thread(src.copy_to, file_id)
            except Exception:
                pass
        designed, fell, _ = await design_file(spec, fmt, file_id=file_id, brief=brief, request=request, engine=engine,
                                              http=http, mode=mode, sandbox=sandbox, deadline=deadline)
        if fell:
            got['caveats'].append(fell)
        if designed is not None:
            got['usage'] = studio_usage(designed)
            got['notes'] += [n for n in getattr(designed, 'notes', None) or [] if n]
            got['caveats'] += [c for c in getattr(designed, 'caveats', None) or [] if c]
    tokens = int(tokens) + got['usage']['llm_in'] + got['usage']['llm_out']
    kw = dict(source=source, tokens=tokens, from_id=from_id, extra=extra, brief=brief, credits=credits, role=role,
              file_id=file_id)
    try:
        meta, spec, data = await asyncio.to_thread(build, spec, fmt, studio=designed, **kw)
    except cf.SpecError as e:
        if designed is None or getattr(designed, 'painted_bytes', None) is None:
            raise
        # the painted file failed a block rule: the standard renderer makes it instead (a file is never lost to Studio)
        drop_workspace(file_id, sandbox)
        got['caveats'].append(fell_back_caveat(f'its file failed rule {e.rule_id}: {e.message}'))
        meta, spec, data = await asyncio.to_thread(build, spec, fmt, **kw)
    return meta, spec, data, got


def merged_rules(results) -> list[dict]:
    """RuleResults as dicts, one per rule id in first-seen order: a rule that failed anywhere is failed, with every
    distinct note (normalize's own result and the writer's notes for the same rule become one)."""
    out: dict[str, dict] = {}
    for r in results:
        d = r.to_dict() if hasattr(r, 'to_dict') else dict(r)
        if d['id'] not in out:
            out[d['id']] = d
            continue
        cur = out[d['id']]
        cur['ok'] = bool(cur['ok'] and d['ok'])
        notes = [n for n in (cur.get('note'), d.get('note')) if n]
        cur['note'] = '; '.join(dict.fromkeys(n for part in notes for n in part.split('; ')))
    return list(out.values())


def design_applied(design: dict, theme: dict, choice, fmt: str, requested_font: bool) -> dict:
    """CreatedFile.design: the design, the colours the file uses and the fonts it really uses, with plain notes."""
    notes = []
    if fmt == 'md':
        notes.append('A Markdown file carries no colours or fonts; convert it to PDF, Word or PowerPoint to see the '
                     'design (0 tokens).')
    elif choice.note and not requested_font:
        notes.append(choice.note)
    heading = None if fmt == 'md' else choice.used if fmt == 'pdf' or requested_font else theme.get('heading_font')
    return design_mod.applied(design, theme, {'body': choice.used or None, 'heading': heading or None,
                                              'notes': notes})


def answer_for(meta: dict, notes: list[str], brief: Brief | None = None) -> str:
    """The create step's answer: the headline ("Created **x.pdf**, 12 pages", with the target when the brief set
    one), the notes, then the warn rules that failed."""
    warn = [r for r in meta['rules'] if r['severity'] == 'warn' and not r['ok']]
    head = f'Created **{meta["name"]}**, {shape_of(meta)}'
    if brief is not None and brief.pages and meta.get('pages'):
        head += f' (asked for {span(brief.pages)})'
    elif brief is not None and brief.slides and meta.get('slides'):
        head += f' (asked for {span(brief.slides)})'
    lines = [head]
    if warn:
        lines.append(f'{len(warn)} check{"" if len(warn) == 1 else "s"} to look at: ' +
                     '; '.join(f'{r["id"]} ({r["note"]})' if r['note'] else r['id'] for r in warn) + '.')
    return '\n\n'.join([lines[0], *notes, *lines[1:]])


def caveats_for(meta: dict, brief: Brief | None, spec: dict | None = None, extra: list[str] = (),
                info: list[str] | tuple = ()) -> list[str]:
    """What the file could not honour, in plain words (C8): failed brief checks, the font and asset notes passed in
    `extra`, and notes the writer left in the spec that admit something was not done. `info` are notes that only say
    what was done (Studio's "designed 12 slides in the minimal style"): they stay in the answer, never caveats."""
    out = [c for c in extra if c]
    failed = {r['id']: r for r in meta.get('rules') or [] if not r['ok'] and r['id'] in ('V5', 'V6', 'V7', 'V8', 'V9',
                                                                                             'V10')}
    if 'V5' in failed:
        out.append(failed['V5']['note'])
    if 'V6' in failed and not any('image' in c.lower() for c in out):
        out.append('no images were added to the file')
    if 'V7' in failed:
        out.append(failed['V7']['note'])
    if 'V8' in failed:
        out.append(f'the file is not fully in the theme asked for ({failed["V8"]["note"]})')
    if 'V9' in failed:
        out.append(failed['V9']['note'].replace(' drawn, asked for at least', ' drawn; asked for at least'))
    out += [n for n in (meta.get('design') or {}).get('notes') or [] if n not in info]
    if 'V10' in failed and failed['V10'].get('note'):
        out.append(failed['V10']['note'])
    for sec in (spec or {}).get('sections') or []:
        note = str(sec.get('notes') or '') if isinstance(sec, dict) else ''
        for sentence in re.split(r'(?<=[.!?])\s+|\n+', note):
            if UNDONE.search(sentence) and len(out) < 12:
                out.append(sentence.strip()[:200])
    return list(dict.fromkeys(o.strip() for o in out if o and o.strip()))


# ---------- reading the request ----------


def answered(rec: dict) -> bool:
    """A finished turn with a real answer: some step answered, not only a follow-up question, a block or a failure."""
    return rec.get('status') == 'done' and any(t.get('ok') and t.get('agent') not in ('clarify', 'blocked')
                                               for t in rec.get('tasks') or [])


def only_created(rec: dict) -> bool:
    """A turn whose only answered steps made files: its reply names the file and is no content to build on."""
    done = [t for t in rec.get('tasks') or [] if t.get('ok') and t.get('agent') not in ('clarify', 'blocked')]
    return bool(done) and all(t.get('agent') == 'create' for t in done)


def asks_for_file(text: str) -> bool:
    """A file request ("put that in a PDF") or only a format after one ("now as slides")."""
    return is_file_request(text) or bool(LEAD_FORMAT.match(text))


def refers_back(text: str) -> bool:
    return bool(REFERS.search(text))


def has_topic(text: str) -> bool:
    """True when the request names what the file is about ("slides on solar power"), not only a format."""
    return any(w not in FILLER and len(w) > 1 for w in re.findall(r'[a-z0-9]+', text.lower()))


def theme_of(text: str) -> str | None:
    return next((name for name, rx in THEME.items() if rx.search(text)), None)


def topic_title(text: str) -> str:
    """A title from a question or step: "What is a black hole?" -> "Black hole"."""
    t = re.sub(r'\s+', ' ', str(text or '')).strip().rstrip('?!. ')
    for _ in range(2):
        t = LEAD_ASK.sub('', t).strip()
    t = re.sub(r'^(?:the|a|an)\s+', '', t, flags=re.I)
    t = t[:100].rsplit(' ', 1)[0] if len(t) > 100 else t
    return t[:1].upper() + t[1:] if t else ''


def default_format(text: str, spec: dict) -> str:
    """No format named: slides were caught by detect_format; a report is a PDF, content that is all tables is a
    spreadsheet, anything else Markdown."""
    if re.search(r'\breports?\b', text, re.I):
        return 'pdf'
    blocks = [b for s in spec.get('sections') or [] if isinstance(s, dict) for b in s.get('blocks') or []
              if isinstance(b, dict)]
    if blocks and any(b.get('type') == 'table' for b in blocks) and all(b.get('type') in ('table', 'chart') for b in blocks):
        return 'xlsx'
    return 'md'


def spec_text(spec: dict) -> str:
    """The words of a spec, for Jev's safety check (X4): titles, headings, text, bullets, notes, code, chart labels and
    series names, and every table cell that is text (each distinct one once; numbers carry no words). Call it on the
    normalized spec, so the check reads exactly what the file will hold (entities decoded, markup removed)."""
    out, seen = [str(spec.get('title') or ''), str(spec.get('subtitle') or '')], set()
    for s in spec.get('sections') or []:
        if not isinstance(s, dict):
            continue
        out += [str(s.get('heading') or ''), str(s.get('notes') or '')]
        for b in s.get('blocks') or []:
            if not isinstance(b, dict):
                continue
            out += [str(b.get(k) or '') for k in ('text', 'title', 'by')]
            out += [str(x) for x in b.get('items') or []] + [str(x) for x in b.get('columns') or []]
            out += [str(x) for x in b.get('labels') or []]
            out += [str(x.get('name') or '') for x in b.get('series') or [] if isinstance(x, dict)]
            for r in b.get('rows') or []:
                cells = [v.get('formula', '') if isinstance(v, dict) else v for v in (r if isinstance(r, list) else [r])]
                words = [str(v) for v in cells if isinstance(v, str) and v.strip() and v not in seen]
                seen.update(words)
                if words:
                    out.append(' '.join(words))
    return '\n'.join(x for x in out if x.strip())


def chunks(text: str, size: int = CREATE_SAFETY_CHARS) -> list[str]:
    """The text in pieces of at most `size` characters, cut at line ends where it can be."""
    out, cur = [], ''
    for line in text.split('\n'):
        while len(line) > size:
            if cur:
                out.append(cur)
                cur = ''
            out.append(line[:size])
            line = line[size:]
        if cur and len(cur) + 1 + len(line) > size:
            out.append(cur)
            cur = line
        else:
            cur = f'{cur}\n{line}' if cur else line
    if cur or not out:
        out.append(cur)
    return out


def csv_rows(rows: list[list]) -> str:
    buf = io.StringIO()
    csv.writer(buf, lineterminator='\n').writerows(rows)
    return buf.getvalue().strip()


def context_text(job: Job, budget: int = CREATE_CONTEXT_CHARS) -> str:
    """What the spec call sees besides the request, at most `budget` characters: the earlier steps' answers, attached
    tables as columns plus the first CREATE_TABLE_ROWS rows, attached documents, then earlier turns, newest first."""
    parts = [f'Earlier step "{t[:200]}":\n{a}' for t, a in job.deps]
    parts += [f'Attached table {m["name"]}: {len(rows):,} rows; columns: {", ".join(map(str, cols))}\n'
              f'First {min(CREATE_TABLE_ROWS, len(rows))} rows:\n{csv_rows([cols, *rows[:CREATE_TABLE_ROWS]])}'[:1500]
              for m, cols, rows in job.tables]
    parts += [f'Attached file {m["name"]}:\n{text}' for m, text in job.docs]
    parts += [f'Earlier turn:\nQ: {t["query"]}\nA: {t["answer"] or "(a file was made)"}'
              for t in reversed(job.context[-TURNS:])]
    out, left = [], budget
    for p in parts:
        if left < 200:
            break
        out.append(p if len(p) <= left else p[:left - 3].rstrip() + '...')
        left -= len(out[-1]) + 2
    return '\n\n'.join(out)


# ---------- the agent ----------


THEME_WORDS = {'mono': 'black and white', 'dark': 'a dark theme', 'warm': 'a warm theme'}
RESTYLE = re.compile(r'\bre-?(?:style|theme|colou?r)\b|\bapply\b|\bstyle\s+(?:it|this|that)\b', re.I)


async def make(job: Job, engine=None, jev=None, mode: str = 'balanced') -> Made:
    """The file for one create step (see _make). Never raises: any failure is an honest answer that still reports the
    engine, tokens, phases and checkpoint spent so far (docs/PLAN-files-robust.md 3.1)."""
    from ..create.longdoc import Tally
    if job.tally is None:
        job.tally = Tally()
    # what finish() hands the design stage, wherever it is called from (docs/PLAN-designer.md 9.10)
    token = STUDIO_RUN.set({'engine': engine, 'mode': mode, 'http': job.http, 'request': job.request,
                            'sandbox': job.sandbox, 'deadline': job.deadline})
    try:
        return await _make(job, engine, jev, mode)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        tin, tout = job.tally.tokens
        if isinstance(e, cf.SpecError):
            msg = f'No file was made. Rule {e.rule_id} blocked it: {e.message}'
        else:
            msg = f'No file was made: something went wrong while building it ({type(e).__name__}: {str(e)[:120]}).'
        used = (job.tally.engine or getattr(engine, 'name', None) or 'keyless') if tin or tout else 'keyless'
        return Made(msg, False, used, tin, tout, phases=list(job.tally.phases.values()), checkpoint=job.tally.state)
    finally:
        STUDIO_RUN.reset(token)


def pick_design(job: Job, design_docs: list, req: str, theme: str | None) -> tuple[dict | None, list[str]]:
    """The spec['design'] for this request (4.3): the first design file attached to it, else the newest one from
    earlier turns; notes for a design that could not be read, was mentioned but not attached, or lost to a theme."""
    src = design_docs[0] if design_docs else (job.design_docs[0] if job.design_docs else None)
    if src is None:
        name = design_mod.mentioned_name(req)
        return None, ([design_mod.missing_note(name)] if name else [])
    meta, text = src
    name = str((meta or {}).get('name') or 'design.md')
    tokens = design_mod.parse_design(text, name)
    if tokens is None:
        return None, [design_mod.unreadable_note(name)]
    if theme:  # an explicit theme word in the request wins over the design's colours
        return None, [f"The design's colours were not used because you asked for {THEME_WORDS.get(theme, theme)}."]
    return design_mod.to_spec(tokens), []


async def _make(job: Job, engine=None, jev=None, mode: str = 'balanced') -> Made:
    """The file for one create step. engine None is keyless: only the zero-token paths can make a file. The path order
    is docs/PLAN-accuracy-v2.md C2: the chat's last file in another format, a file an earlier step made, an earlier
    step's answer (copied only when the brief asks for no more), then the chat, tables, previous answer and engine.
    A design file attached to the request is split from the content documents and applied by code (4.3)."""
    if job.resume:
        from ..create import longdoc
        return await longdoc.resume_long(job, engine, jev, job.resume)
    req = job.request.strip()
    brief = job.brief if job.brief is not None else parse_brief(req)
    fmt = cf.detect_format(req) or brief.format
    theme = theme_of(req) or brief.theme
    design_docs, job.docs = design_mod.split_docs(job.docs, req)
    job.design, job.design_notes = pick_design(job, design_docs, req, theme)
    if job.design:
        brief = dataclasses.replace(brief, design=job.design['name'])
    job.brief = brief
    last = job.last_file
    last_turn_file = bool(last) and last[2]
    attached = bool(job.tables or job.docs)  # content files only: a design file is not content
    # the file is about the whole chat ("notes of our conversation"): every earlier answer, not only the last one
    conversation = bool(CONVERSATION.search(req)) and any(t.get('answer', '').strip() for t in job.context)
    # the chat's last file in another format: right after it was made ("now as slides", "put that in Word", "I also
    # want an Excel spreadsheet"), or when the request names it as what to convert ("turn the pdf into slides"). New
    # material wins: an earlier step of this run (its answer is what the file is for) or an attached file.
    names_file = FILE_REF.search(req) and (CONVERT.search(req) or re.search(r'\b(?:into|to|as)\b', req, re.I))
    other_format = bool(last) and fmt is not None and fmt != last[0]['format'] and not has_topic(req)
    restyle = bool(job.design) and bool(last) and (refers_back(req) or FILE_REF.search(req) or RESTYLE.search(req))
    wants_file = bool(last) and not job.deps and not job.dep_files and not attached and not conversation and (
        (last_turn_file and (LEAD_FORMAT.match(req) or CONVERT.search(req) or refers_back(req) or other_format))
        or names_file or restyle)
    if wants_file:
        return await convert_last(last, fmt, theme, brief=brief, role=job.role, design=job.design,
                                  design_notes=job.design_notes)
    if job.dep_files:  # C2.1: a file an earlier step of this run made is the starting point, never its reply line
        return await from_dep_files(job, engine, jev, fmt, theme, brief, mode)
    # the latest earlier turn that answered something (a turn that only made a file has no answer of its own here)
    prev = next((t for t in reversed(job.context) if (t.get('answer') or '').strip()), None)
    notes, source, spec, short_from = [], None, None, None
    if job.deps and job.tables and fmt == 'xlsx':
        # "turn this data into an Excel sheet with a chart": the table is the content; an earlier analysis step's
        # answer rides along as notes rather than replacing the data
        spec, source = from_tables(job.tables), 'table'
        if (extra := from_answers(job.deps)) is not None:
            spec['sections'] += extra['sections']
    elif job.deps:
        spec, source = from_answers(job.deps), 'answer'
        if asks_more(brief):
            if engine is not None:  # C2.3: the earlier answers are notes for a longer file the engine writes
                return await write_long(job, engine, jev, fmt, brief, mode)
            if spec is None:  # C2.4, nothing to render: an honest failure, never a file of a status line
                return Made(f'No file was made: {wanted(brief, fmt)} needs an LLM engine to write, and the step it '
                            f'was to be made from has no answer to put in it. Choose an engine in Settings.', False)
            short_from = 'the earlier answer'
        elif spec is None:
            return Made('No file was made: the step it was to be made from has no answer to put in it.', False)
    elif conversation and not attached:
        spec, source = conversation_spec(job.context), 'answer'
    elif job.tables and (engine is None or not GENERATIVE.search(req)):
        spec, source = from_tables(job.tables), 'table'
        if engine is None and GENERATIVE.search(req):
            notes.append('Keyless mode made it straight from the table; writing an analysis of it needs an engine.')
    elif not attached and prev is not None and (refers_back(req) or not has_topic(req)):
        spec, source = answer_spec(prev['answer'], topic_title(prev['query'])), 'answer'
    elif refers_back(req) and not attached and not has_topic(req):
        return Made('There is no earlier answer in this chat to put in a file. Ask the question first, or say what the '
                    'file should contain.', False)
    elif engine is None and asks_more(brief):
        return Made(f'No file was made: {wanted(brief, fmt)} needs an LLM engine to write, and there is no earlier '
                    f'answer or attached table here to make it from. Choose an engine in Settings, or ask the question '
                    f'first and then say "put that in a PDF".', False)
    elif engine is None:
        return Made('I can make a PDF, Word, PowerPoint, Excel or Markdown file from an earlier answer in this chat or '
                    'from an attached table with no model at all, but there is nothing like that to use here, and '
                    'writing new content needs an LLM engine. Ask a question first and then say "put that in a PDF", '
                    'attach a CSV or JSON table, or choose an engine.', False)
    if spec is None:
        if asks_more(brief):  # C2.5: a long, illustrated or diagrammed file
            return await write_long(job, engine, jev, fmt, brief, mode)
        return await from_engine(job, engine, jev, fmt, theme, mode, brief=brief)
    if fmt is None:
        fmt = default_format(req, spec)
        notes.append(no_format_note(fmt))
    return await finish(spec, fmt, jev, source=source, notes=notes, brief=brief, theme=theme, role=job.role,
                        short_from=short_from, design=job.design, design_notes=job.design_notes)


def no_format_note(fmt: str) -> str:
    return (f'No format was named, so this is {LABELS[fmt]}. Convert it to '
            f'{", ".join(LABELS[f] for f in cf.FORMATS if f != fmt)} from the file card at no cost.')


def wanted(brief: Brief, fmt: str | None) -> str:
    """What the brief asks for, in words: "a 12-13 page PDF", "a 20 slide deck", "a file with diagrams"."""
    label = LABELS.get(fmt or brief.format or '', 'file')
    label = 'deck' if label == 'PowerPoint' else f'{label} file' if label in ('Word', 'Excel', 'Markdown') else label
    if brief.pages:
        return f'a {span(brief.pages)} page {label}'
    if brief.slides:
        return f'a {span(brief.slides)} slide {label}'
    if brief.words:
        return f'a {brief.words:,} word {label}'
    extras = [w for w, on in (('images', brief.images), ('diagrams', brief.diagrams)) if on]
    return f'a {label} with {" and ".join(extras)}' if extras else f'a {label}'


def short_caveat(brief: Brief, meta: dict, source: str) -> str:
    """C2.4: a keyless file made from what there was, when the brief asked for more."""
    if brief.pages:
        made = f'{meta["pages"]}' if meta.get('pages') else 'a shorter file'
        return f'asked for {span(brief.pages)} pages; made {made} from {source}; writing more needs an engine.'
    if brief.slides:
        made = f'{meta["slides"]}' if meta.get('slides') else 'fewer'
        return f'asked for {span(brief.slides)} slides; made {made} from {source}; writing more needs an engine.'
    extras = [w for w, on in (('images', brief.images), ('diagrams', brief.diagrams),
                              (f'{brief.words or 0:,} words', bool(brief.words))) if on]
    return f'asked for {" and ".join(extras)}; made the file from {source}; adding them needs an engine.'


def held(spec: dict, fmt: str, brief: Brief) -> dict:
    """What a stored spec holds for this format: pages (PDF, rendered when the brief sets a page count), slides (PPTX),
    words, diagrams and images. Blocking."""
    norm, _ = cf.normalize(spec, fmt)
    blocks = [b for s in norm['sections'] for b in s['blocks']]
    text = spec_text(norm)
    out = {'pages': None, 'slides': None, 'words': len(text.split()),
           'diagrams': sum(b['type'] in cf.DIAGRAMS for b in blocks), 'images': sum(b['type'] == 'image' for b in blocks)}
    if fmt == 'pdf' and brief.pages:
        from pypdf import PdfReader
        out['pages'] = len(PdfReader(io.BytesIO(cf.render(spec, fmt))).pages)
    if fmt == 'pptx':
        out['slides'] = 1 + len(norm['sections'])
    return out


def enough(have: dict, brief: Brief) -> bool:
    """True when a stored spec already holds what the brief asks for (C2.1): no page or slide target above what it
    makes + 1, and no images, diagrams or words it lacks."""
    if brief.pages and have['pages'] is not None and brief.pages[0] > have['pages'] + 1:
        return False
    if brief.pages and have['pages'] is None and brief.pages[0] > have['words'] / brief_mod.WORDS_PER_PAGE + 1:
        return False
    if brief.slides and have['slides'] is not None and brief.slides[0] > have['slides'] + 1:
        return False
    if brief.images and not have['images']:
        return False
    if brief.diagrams and have['diagrams'] < diagrams_min(brief):
        return False
    return not (brief.words and have['words'] < 0.8 * brief.words)


async def from_dep_files(job: Job, engine, jev, fmt: str | None, theme: str | None, brief: Brief, mode: str) -> Made:
    """C2.1: the file a dependency step made. Converted or re-rendered at 0 tokens when it holds enough; otherwise it
    seeds the long-document writer, or (keyless) is rendered as it is with a caveat."""
    meta, stored = job.dep_files[-1]
    fmt = fmt or meta['format']
    try:
        have = await asyncio.to_thread(held, stored, fmt, brief)
    except cf.SpecError as e:
        return Made(f'No file was made. Rule {e.rule_id} blocked it: {e.message}', False)
    if not enough(have, brief) and engine is not None:
        return await write_long(job, engine, jev, fmt, brief, mode, seed=stored)
    note = (f'Made from **{meta["name"]}** with no model tokens.' if fmt != meta['format'] else
            f'Re-rendered **{meta["name"]}** with no model tokens.')
    return await finish(dict(stored), fmt, None, source='convert', from_id=meta.get('id'), extra=carried(meta),
                        notes=[note], brief=brief, theme=theme, role=job.role,
                        short_from=None if enough(have, brief) else 'the earlier file', design=job.design,
                        design_notes=job.design_notes)


def image_seeds(job: Job) -> list[tuple[str, str]]:
    """B5: `IMAGE: <search query> | <caption>` lines in the earlier steps' notes, as figure ideas."""
    out = []
    for _, answer in job.deps:
        out += image_ideas(answer or '')  # the notes' own format, backticked or not (agents/llm.py IMAGE_LINE)
    return out[:8]


async def with_images(spec: dict, job: Job, brief: Brief, theme: str | None) -> tuple[dict, list, list[str], dict]:
    """C5: figure blocks (or the notes' image ideas) resolved to credited images; (spec, credits, caveats, phase)."""
    from ..create import assets
    t0 = time.perf_counter()
    has_figures = any(isinstance(b, dict) and b.get('type') == 'figure' for sec in spec.get('sections') or []
                      if isinstance(sec, dict) for b in sec.get('blocks') or [])
    if not brief.images:  # figures nobody asked for are left out, with a note on how to add them
        if has_figures:
            spec, _, _ = await assets.resolve_figures(spec, None, mono=False)
            return spec, [], [PICTURES_NOTE], {}
        return spec, [], [], {}
    spec, credits, caveats = await assets.resolve_figures(spec, job.http, mono=(theme or spec.get('theme')) == 'mono',
                                                          seeds=image_seeds(job))
    phase = {'phase': 'assets', 'calls': 0, 'llm_in': 0, 'llm_out': 0, 'ms': round((time.perf_counter() - t0) * 1000)}
    return spec, credits, caveats, phase


PICTURES_NOTE = 'Pictures were left out because the request did not ask for images. Say "with images" to add them.'


def from_answers(deps: list[tuple[str, str]]) -> dict | None:
    """The spec of the earlier steps' answers (zero tokens). With several, each answer is a section under its step. A
    create step's reply ("Created **x.md**, 12 KB" and its notes) is no content: it counts as empty (B3)."""
    deps = [(t, clean_answer(a)) for t, a in deps if a.strip() and not CREATED_ONLY.match(a.strip().split('\n', 1)[0])]
    deps = [(t, a) for t, a in deps if a.strip()]
    if not deps:
        return None
    if len(deps) == 1:
        return answer_spec(deps[0][1], topic_title(deps[0][0]))
    return answer_spec('\n\n'.join(f'## {topic_title(t) or t}\n\n{a}' for t, a in deps), 'Results')


def conversation_spec(context: list[dict]) -> dict:
    """The spec of every answered turn of the chat (zero tokens): one section per turn, headed by its question."""
    turns = [(t['query'], t['answer']) for t in context if (t.get('answer') or '').strip()]
    if len(turns) == 1:
        return answer_spec(turns[0][1], topic_title(turns[0][0]))
    return answer_spec('\n\n'.join(f'## {topic_title(q) or q}\n\n{a}' for q, a in turns), 'Conversation notes')


H1 = re.compile(r'^\s{0,3}#\s+\S', re.M)


def clean_answer(answer: str) -> str:
    """B3: an answer without the chat's own lines: "**weather**: " labels at line starts, a create step's
    "Created **x**, ..." reply and its "No format was named" note."""
    text = AGENT_LABEL.sub('', str(answer or ''))
    text = REPLY_LINE.sub('', text)
    return re.sub(r'\n{3,}', '\n\n', text).strip()


def answer_spec(answer: str, title: str) -> dict:
    """An answer's spec (zero tokens), titled by its one "# " heading when it has exactly one, else by `title` (from
    the question it answered), so a first "## Work" heading doesn't become the file's title."""
    answer = clean_answer(answer)
    if len(H1.findall(answer)) == 1:
        spec = cf.from_markdown(answer)
    else:
        spec = cf.from_markdown(answer, title=title or None)
    spec['title'] = spec.get('title') or title or 'Document'
    return spec


def from_tables(tables: list[tuple[dict, list, list]]) -> dict:
    """The spec of the attached tables (zero tokens): one table section each, with a chart where one fits."""
    specs = [cf.from_table(m, rows, cols) for m, cols, rows in tables]
    spec = specs[0]
    for s in specs[1:]:
        spec['sections'] += s['sections']
    if len(specs) > 1:
        spec['title'], spec['subtitle'] = 'Attached tables', ''
    return spec


async def convert_last(last, fmt: str | None, theme: str | None, *, brief: Brief | None = None,
                       role: str | None = None, design: dict | None = None, design_notes: list[str] = ()) -> Made:
    """The chat's last file in another format, theme, font or design (0 tokens: "restyle it with this design.md")."""
    meta, spec, _ = last
    if fmt is None and design:
        fmt = meta['format']
    if fmt is None:
        return Made(f'Which format should **{meta["name"]}** become: PDF, Word, PowerPoint, Excel or Markdown?', False)
    font = brief.font if brief is not None else None
    if fmt == meta['format'] and (not theme or theme == spec.get('theme')) and (not font or font == spec.get('font')) \
            and not design:
        return Made(f'**{meta["name"]}** is already a {LABELS[fmt]} file.', True, file=meta)
    note = (f'Restyled **{meta["name"]}** with {design["name"]} and no model tokens.' if design and fmt == meta['format']
            else f'Converted from **{meta["name"]}** with no model tokens.')
    # The stored spec passed the safety check when it was made; converting it asks no one (0 tokens, rule L5).
    return await finish(dict(spec), fmt, None, source='convert', from_id=meta['id'], extra=carried(meta),
                        notes=[note], brief=brief, theme=theme, role=role, design=design, design_notes=design_notes)


def carried(meta: dict) -> list:
    """The source file's X4 result, which a conversion keeps: the content it checked is the same."""
    return [cf.RuleResult(**{k: r[k] for k in ('id', 'severity', 'ok', 'note')}) for r in meta.get('rules') or []
            if r.get('id') == 'X4']


# ---------- rung 1 of the repair ladder: read and repair a reply in code (docs/PLAN-files-robust.md 3.1) ----------

PROSE_NOTE = 'The model replied in plain text, so its text was laid out as the file.'


LOOKS_JSON = re.compile(r'^\s*(?:```\w*\s*)?[\[{]')


def parse_reply(text: str):
    """cf.parse_spec (tolerant JSON; a top-level list becomes {'sections': list}); None when no object can be read."""
    return cf.parse_spec(text)


def read_reply(text: str, prose: bool = True) -> tuple[dict | None, list[str]]:
    """(the reply as a spec, notes). A reply that is no JSON but has prose is laid out as Markdown, with a note. A reply
    too large to read (L6) is None with its reason."""
    text = str(text or '')
    try:
        spec = parse_reply(text)
    except cf.SpecError as e:
        return None, [f'The model\'s reply could not be read as a file ({e.message}).']
    if isinstance(spec, dict):
        spec = cf.strip_internal(spec)
        for alias in ('slides', 'pages', 'parts'):
            secs = spec.get('sections')
            if not (isinstance(secs, list) and secs) and isinstance(spec.get(alias), list) and spec[alias]:
                spec = {**spec, 'sections': spec[alias]}
        return spec, []
    if prose and len(text.strip()) >= 40 and not LOOKS_JSON.match(text) and re.search(r'[^\W\d_]{3}', text):
        md = cf.from_markdown(text)
        if md.get('sections'):
            return md, [PROSE_NOTE]
    return None, []


def repair_spec(spec) -> tuple[dict, list]:
    """cf.repair: format independent, never raises."""
    return cf.repair(spec)


def has_text(spec) -> bool:
    """The S2 test: something in the spec can be shown."""
    return bool(cf.has_text(spec))


SECTION_N = re.compile(r'\bsections? 1\b')


def repair_section(raw, n: int) -> tuple[dict, list]:
    """One section of a reply repaired alone, its notes naming it as section n of the file."""
    fixed, results = repair_spec({'title': '', 'subtitle': '', 'sections': [raw]})
    secs = fixed.get('sections') or []
    sec = dict(secs[0]) if secs else {'heading': '', 'level': 1, 'blocks': [], 'notes': ''}
    for more in secs[1:]:  # a section split in two (30+ blocks, or a heading block) stays one part
        sec['blocks'] = list(sec.get('blocks') or []) + list(more.get('blocks') or [])
    out = [cf.RuleResult(r.id, r.severity, r.ok, SECTION_N.sub(f'section {n}', r.note or '')) for r in results]
    return sec, out


def usable(sec) -> bool:
    """A section with at least one block that shows something (a page break alone does not count)."""
    return isinstance(sec, dict) and any(isinstance(b, dict) and b.get('type') != 'page_break'
                                         for b in sec.get('blocks') or [])


def _key(h) -> str:
    return re.sub(r'[^a-z0-9]', '', str(h or '').lower())


def place(secs: list, parts: list) -> list:
    """The reply's sections on the parts they were asked for: in order when the counts match, else by heading, then
    in order; sections left over are kept inside the last placed one (paid content is never thrown away)."""
    secs = [s for s in secs if s is not None]
    if len(secs) == len(parts):
        return list(secs)
    out, used = [None] * len(parts), set()
    for j, p in enumerate(parts):
        for k, sec in enumerate(secs):
            h = sec.get('heading') or sec.get('title') if isinstance(sec, dict) else sec
            if k not in used and _key(p.heading) and _key(h) == _key(p.heading):
                out[j] = sec
                used.add(k)
                break
    rest = [sec for k, sec in enumerate(secs) if k not in used]
    for j in range(len(parts)):
        if out[j] is None and rest:
            out[j] = rest.pop(0)
    if rest:
        target = next((j for j in range(len(parts) - 1, -1, -1) if isinstance(out[j], dict)), None)
        if target is not None:
            merged = dict(out[target])
            blocks = list(merged.get('blocks') or []) if isinstance(merged.get('blocks'), list) else []
            for sec in rest:
                if isinstance(sec, dict) and isinstance(sec.get('blocks'), list):
                    blocks += sec['blocks']
                elif isinstance(sec, str):
                    blocks.append({'type': 'paragraph', 'text': sec})
            merged['blocks'] = blocks
            out[target] = merged
    return out


async def from_engine(job: Job, engine, jev, fmt: str | None, theme: str | None, mode: str, *,
                      brief: Brief | None = None) -> Made:
    """One engine call for the spec, with the trimmed context and the format's token cap. The reply is repaired in
    code; when it has nothing usable at all, one retry call (while time allows); the reply is kept in a checkpoint so
    a later fix can rebuild it at 0 tokens."""
    from ..create import longdoc
    req = job.request.strip()
    brief = brief if brief is not None else parse_brief(req)
    shape = SHAPES.get(fmt, SHAPES['default'])
    effort = 'high' if DEEP.search(req) and mode != 'quick' else 'low'
    ctx = context_text(job)
    prompt = f'Request: {req}\nFormat: {LABELS.get(fmt, "not named")}' + (f'\n\nContext:\n{ctx}' if ctx else '')
    if job.tally is None:
        job.tally = longdoc.Tally()
    tally = longdoc.Tally()
    state = {'v': 1, 'kind': 'single', 'format': fmt, 'request': req, 'brief': brief_mod.to_dict(brief), 'theme': theme,
             'font': brief.font, 'design': job.design, 'reply': None, 'engine': engine.name, 'tokens_in': 0,
             'tokens_out': 0, 'calls': 0, 'at': time.time(), 'phase': 'sections', 'file_id': None, 'role': job.role}

    async def call(text: str):
        t0 = time.perf_counter()
        try:
            r = await engine.stream(system=SYSTEM.format(shape=shape), prompt=text, effort=effort,
                                    max_tokens=CREATE_MAX_TOKENS[fmt or 'md'], schema=cf.DOCSPEC_SCHEMA)
        except (EngineError, EngineRefusal) as e:
            ms = (time.perf_counter() - t0) * 1000
            got = getattr(e, 'reply', None)
            tally.add('sections', got, ms)
            job.tally.add('sections', got, ms)
            raise
        ms = (time.perf_counter() - t0) * 1000
        tally.add('sections', r, ms)
        job.tally.add('sections', r, ms)
        state['calls'] += 1
        state['tokens_in'] += r.input_tokens
        state['tokens_out'] += r.output_tokens
        return r

    def spent() -> dict:
        tin, tout = tally.tokens
        return {'engine': tally.engine or engine.name, 'llm_in': tin, 'llm_out': tout, 'effort': effort,
                'phases': list(tally.phases.values())}
    try:
        reply = await call(prompt)
    except EngineRefusal:
        s = spent()
        return Made(f'{engine.label} declined to write this file.', False, s['engine'], s['llm_in'], s['llm_out'],
                    effort=effort, phases=s['phases'])
    except EngineError as e:
        s = spent()
        return Made(f'The create agent needs {engine.label} to write new content, which failed ({e.why}).', False,
                    s['engine'], s['llm_in'], s['llm_out'], effort=effort, phases=s['phases'])
    raw = reply.text or ''
    spec, notes = read_reply(raw)
    repairs = None
    if (spec is None or not has_text(spec)) and longdoc.time_left(job) >= longdoc.REPAIR_MIN_SECONDS:
        retry = (f'{prompt}\n\nYour last reply had no usable content. Reply with the whole file as JSON matching the '
                 f'schema, with at least one section that has a block.')
        try:
            r2 = await call(retry)
            repairs = {'calls': 1, 'llm_in': r2.input_tokens, 'llm_out': r2.output_tokens, 'sections': []}
            spec2, notes2 = read_reply(r2.text or '')
            if spec2 is not None and has_text(spec2):
                spec, notes, raw = spec2, notes2, r2.text or ''
        except (EngineError, EngineRefusal):
            pass
    state['reply'] = raw[:200_000]
    longdoc.sink(job, state)
    s = spent()
    if spec is None or not has_text(spec):
        if isinstance(spec, dict):
            msg = ('No file was made. Rule S2 blocked it: the model\'s reply had nothing that could be shown in a file. '
                   'Resume can try again.')
        else:
            why = notes[0] if notes else 'the model\'s reply had nothing that could be laid out as a file.'
            msg = f'No file was made: {why[:1].lower() + why[1:]} Resume can try again.'
        return Made(msg, False, s['engine'],
                    s['llm_in'], s['llm_out'], effort=effort, phases=s['phases'], repairs=repairs,
                    checkpoint=state, reply=raw[:200_000])
    spec, fixed = repair_spec(spec)  # rung 1: images and the build see the repaired spec only
    if fmt is None:
        fmt = default_format(req, spec)
        notes.append(no_format_note(fmt))
        state['format'] = fmt
    spec, credits, caveats, phase = await with_images(spec, job, brief, theme)
    out = await finish(spec, fmt, jev, source='llm', tokens=s['llm_in'] + s['llm_out'], notes=notes, brief=brief,
                       theme=theme, role=job.role, credits=credits, caveats=caveats, design=job.design,
                       design_notes=job.design_notes, extra=fixed)
    # the design stage's own calls (art direction) are finish()'s llm_in/llm_out: added, not replaced
    out.engine, out.llm_in, out.llm_out, out.effort = s['engine'], s['llm_in'] + out.llm_in, s['llm_out'] + out.llm_out, effort
    out.phases = [*s['phases'], *([phase] if phase else []), *out.phases]
    out.repairs, out.reply = repairs, raw[:200_000]
    if out.file is not None:
        out.file['phases'] = out.phases
        if repairs:
            out.file['repairs'] = repairs
        state['file_id'], state['phase'] = out.file.get('id'), 'done'
    longdoc.sink(job, state)
    out.checkpoint = state
    return out


async def rebuild_single(job: Job, jev, state: dict, fmt: str, brief: Brief) -> Made:
    """A `single` checkpoint rebuilt from its stored reply with no model call (rung 1 and finish)."""
    spec, notes = read_reply(str(state.get('reply') or ''))
    job.design = job.design or state.get('design')
    if spec is None or not has_text(spec):
        return Made('No file was made: the stored reply still has nothing that could be laid out as a file. Ask again '
                    'to write it anew.', False, checkpoint=state)
    theme = state.get('theme')
    spec, fixed = repair_spec(spec)
    spec, credits, caveats, phase = await with_images(spec, job, brief, theme)
    out = await finish(spec, fmt, jev, source='llm', tokens=0, notes=notes, brief=brief, theme=theme,
                       role=state.get('role') or job.role, credits=credits, caveats=caveats, design=job.design,
                       extra=fixed)
    out.phases = [*([phase] if phase else []), *out.phases]
    if out.file is not None:
        out.file['phases'] = out.phases
        state['file_id'], state['phase'] = out.file.get('id'), 'done'
    out.checkpoint = state
    return out


async def write_long(job: Job, engine, jev, fmt: str | None, brief: Brief, mode: str, *,
                     seed: dict | None = None) -> Made:
    """C3: the long-document writer, with the format settled first (a page count means a PDF, a slide count a deck)."""
    from ..create import longdoc
    notes = []
    if fmt is None:
        fmt = 'pptx' if brief.slides else 'pdf'
        notes.append(no_format_note(fmt))
    return await longdoc.write_long(job, engine, jev, fmt, brief, mode, seed=seed, notes=notes)


async def finish(spec: dict, fmt: str, jev, *, source: str, tokens: int = 0, from_id: str | None = None,
                 notes: list[str] = (), extra: list = (), brief: Brief | None = None, theme: str | None = None,
                 role: str | None = None, credits: list = (), caveats: list[str] = (),
                 short_from: str | None = None, design: dict | None = None, design_notes: list[str] = (),
                 engine=_UNSET, mode: str | None = None, http=_UNSET, request: str | None = None,
                 sandbox=_UNSET, deadline=_UNSET) -> Made:
    """X4 (unless this is a conversion of a spec that passed it: jev None), then normalize -> render -> verify. The
    brief's theme and font and the design go into the spec, the brief checks run, and what could not be honoured
    becomes caveats. An explicit theme word wins over a design the stored spec carries.

    When TG_STUDIO designs the format, the design stage runs after X4 (docs/PLAN-designer.md 9.10): the file id is
    made first, studio.agent.design lays out and paints the file, and build() verifies the painted bytes. Any failure
    falls back to the standard renderer with a caveat. engine, mode, http, request, sandbox and deadline default to
    what make() set for the run; a conversion (source 'convert') is designed keyless, at 0 tokens."""
    extra, jev_tokens, notes, caveats = list(extra), 0, list(notes), [*caveats, *(design_notes or [])]
    if design:
        spec['design'] = design
    if theme and spec.get('design') and not design:
        spec.pop('design', None)
        caveats.append(f"The design's colours were not used because you asked for {THEME_WORDS.get(theme, theme)}.")
    if theme:
        spec['theme'] = theme
    if brief is not None and brief.font:
        spec['font'] = brief.font
    if jev is not None:
        # X4 reads the text the file will hold: the normalized spec, where "&#115;..." is already "s...", all of it
        try:
            norm, _ = cf.normalize(spec, fmt)
        except cf.SpecError as e:
            return Made(f'No file was made. Rule {e.rule_id} blocked it: {e.message}', False)
        pieces = chunks(spec_text(norm)) or ['(empty)']
        if len(pieces) > CREATE_SAFETY_CHUNKS:
            return Made(f'No file was made. Rule X4 blocked it: the file would hold more text than the safety check '
                        f'reads ({len(pieces) * CREATE_SAFETY_CHARS // 1000:,}K characters; at most '
                        f'{CREATE_SAFETY_CHUNKS * CREATE_SAFETY_CHARS // 1000:,}K).', False)
        gate = asyncio.Semaphore(8)

        async def check(piece):
            async with gate:
                return await unsafe_score(jev, piece or '(empty)')
        try:
            scored = await asyncio.gather(*(check(p) for p in pieces))
        except Exception as e:
            return Made(f'No file was made: the safety check (rule X4) could not run ({str(e)[:120]}).', False)
        score, jev_tokens = max(sc for sc, _ in scored), sum(n for _, n in scored)
        if score >= BLOCK_AT:
            return Made(f'No file was made. Rule X4 blocked it: Jev flagged the content as unsafe ({score:.0%}).', False,
                        jev_tokens=jev_tokens)
        extra.append(cf.RuleResult('X4', 'block', True, f'Jev unsafe score {score:.0%}' +
                                   (f' (highest of {len(pieces)} parts)' if len(pieces) > 1 else '')))
    run = STUDIO_RUN.get() or {}
    pick = lambda v, k: run.get(k) if v is _UNSET else v  # noqa: E731
    t0 = time.perf_counter()
    try:
        meta, spec, data, got = await build_designed(
            spec, fmt, source=source, tokens=tokens, from_id=from_id, extra=extra, brief=brief, credits=credits,
            role=role, engine=pick(engine, 'engine'), mode=mode or run.get('mode') or 'balanced', http=pick(http, 'http'),
            request=request if request is not None else run.get('request') or '', sandbox=pick(sandbox, 'sandbox'),
            deadline=pick(deadline, 'deadline'))
    except cf.SpecError as e:
        return Made(f'No file was made. Rule {e.rule_id} blocked it: {e.message}', False, jev_tokens=jev_tokens)
    render_ms = round((time.perf_counter() - t0) * 1000)
    usage = got['usage']
    notes += got['notes']
    caveats += got['caveats']
    if short_from and brief is not None:
        caveats.insert(0, short_caveat(brief, meta, short_from))
    if credits:
        n = len(credits)
        notes.append(f'{n} image{"" if n == 1 else "s"} from Wikimedia Commons, each credited under it and under '
                     f'"Image credits".' if fmt in ('pdf', 'docx', 'pptx') else
                     f'{n} image{"" if n == 1 else "s"} found, but a {LABELS[fmt]} file can\'t embed pictures, so '
                     f'only the captions and credits are in it.')
        if fmt not in ('pdf', 'docx', 'pptx'):
            caveats.append(f'images aren\'t embedded in a {LABELS[fmt]} file; convert it to PDF, Word or PowerPoint '
                           f'to include them (0 tokens)')
    if brief is not None and brief.font:
        choice = fonts.resolve(brief.font, fmt, theme=meta.get('theme'))
        if choice.note:
            notes.append(choice.note)
            if fmt in ('pdf', 'md'):
                caveats.append(choice.note)
    if short_from:  # the keyless caveat already says the file is shorter than asked
        meta_rules = [r for r in meta['rules'] if r['id'] != 'V5']
    else:
        meta_rules = meta['rules']
    found = caveats_for({**meta, 'rules': meta_rules}, brief, spec, caveats, info=got['notes'])
    made = Made(answer_for(meta, notes, brief), True, jev_tokens=jev_tokens, file=meta, spec=spec, data=data,
                caveats=found, llm_in=usage['llm_in'], llm_out=usage['llm_out'])
    if usage['calls'] or usage['llm_in'] or usage['llm_out']:
        made.engine = getattr(pick(engine, 'engine'), 'name', None) or 'keyless'
    # Studio's calls, tokens and time are the render entry's (FilePhase keeps its names; DesignReport.phases has
    # the breakdown)
    made.phases = [{'phase': 'render', 'calls': usage['calls'], 'llm_in': usage['llm_in'], 'llm_out': usage['llm_out'],
                    'ms': render_ms}]
    meta['phases'] = made.phases
    return made

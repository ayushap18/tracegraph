"""The long-document writer (docs/PLAN-accuracy-v2.md C3): an outline call, then section calls in batches at word
targets from the brief, then a fit loop that renders and trims or tops up until the page or slide count is in range.

Sizes come from word targets in the prompts and from code, never from max_tokens alone: CLI engines ignore it
(engines/cli.py). A 12-page PDF takes an outline call, about three section calls and at most one top-up call; the fit
loop renders at most three times before the final build renders the file once more (four renders in all).
"""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import math
import re
import time
from typing import TYPE_CHECKING

from .brief import WORDS_PER_PAGE, describe, diagrams_min

if TYPE_CHECKING:
    from ..agents.create import Job, Made
    from .brief import Brief

BLOCK_HINTS = ['paragraph', 'bullets', 'table', 'chart', 'timeline', 'tree', 'flow', 'figure']

# Call 1: {title, subtitle, sections: [{heading, level, words, blocks_hint}]}
OUTLINE_SCHEMA = {
    'type': 'object',
    'properties': {
        'title': {'type': 'string'},
        'subtitle': {'type': 'string'},
        'sections': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'heading': {'type': 'string'},
                    'level': {'type': 'integer'},
                    'words': {'type': 'integer'},
                    'blocks_hint': {'type': 'array', 'items': {'type': 'string', 'enum': BLOCK_HINTS}},
                },
                'required': ['heading', 'level', 'words', 'blocks_hint'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['title', 'subtitle', 'sections'],
    'additionalProperties': False,
}

LONG_CONTEXT_CHARS = 12_000   # CREATE_CONTEXT_CHARS raised for the long writer only (C2.3)
BATCH_WORDS = 1500            # target words per section call
MAX_SECTION_WORDS = 900
MIN_SECTION_WORDS = 60
DIAGRAM_PAGE, IMAGE_PAGE = 0.4, 0.35   # the page share a diagram or an image takes from the text
PLANNED_IMAGES = 5                     # "4-6 figures"
SLIDE_WORDS = 45
MAX_FIT_RENDERS = 3                    # plus the final build: at most 4 renders
MAX_PARTS = 38                         # sections, leaving room for "Image credits" under the 40-section limit
# docs/PLAN-files-robust.md 3.1 to 3.3: the repair ladder and checkpoints
MAX_REPAIR_CALLS = 2                   # calls that rewrite only the sections that came back unusable
REPAIR_MIN_SECONDS = 60                # ... and only while this much of the run's time is left
REPAIR_GROUP = 10                      # missing sections per repair call
CHECKPOINT_MAX_BYTES = 400_000

OUTLINE_SYSTEM = ('Do not use tools. You plan one long downloadable file as JSON matching the schema: a title, a '
                  'subtitle (may be empty) and its sections in reading order, each with a heading, a level (1 to 3), a '
                  'word target and the kinds of blocks it will hold. Plan content only: no styling, fonts, colours or '
                  'layout. Use only facts from the request and the notes given here.')
LONG_SHAPES = {
    'pptx': 'It becomes slides: one section per slide, each a heading and at most 5 bullets of at most 12 words, or one '
            'table, chart, diagram or figure; put anything longer in notes.',
    'default': 'It becomes a long document: write each section you are asked for at its word target, in paragraphs of '
               '60 to 150 words, with bullets, tables and charts where the content calls for them.',
}
KIND_TEXT = {'timeline': 'a timeline block (4 to 12 dated events)', 'tree': 'a tree block (one root, 5 to 20 nodes)',
             'flow': 'a flow block (3 to 8 steps joined by edges)',
             # the Studio kinds (create/diagram.NEW_KINDS), written as a diagram block with this kind
             'cycle': 'a diagram block of kind cycle (3 to 8 steps that repeat)',
             'venn': 'a diagram block of kind venn (2 or 3 sets with items, and what they share)',
             'pyramid': 'a diagram block of kind pyramid (3 to 6 levels, top first)',
             'matrix': 'a diagram block of kind matrix (4 labelled quadrants with items, and the two axes)',
             'mindmap': 'a diagram block of kind mindmap (one centre idea, 2 to 30 nodes)',
             'process': 'a diagram block of kind process (2 to 7 steps, each a label and a short detail)',
             'comparison': 'a diagram block of kind comparison (2 or 3 columns with items)',
             'stat-cards': 'a diagram block of kind stat-cards (2 to 4 short numbers, each with a label)',
             'scatter': 'a diagram block of kind scatter (2 to 200 points with x and y, and the axis labels)',
             'any': 'a timeline, tree or flow block, a diagram block or a chart'}
KIND_WORDS = {'timeline': re.compile(r'histor|origin|timeline|milestone|evolution|begin|began|era|develop', re.I),
              'tree': re.compile(r'type|kind|field|branch|categor|taxonom|structure|subfield|famil|class', re.I),
              'flow': re.compile(r'how|process|work|step|pipeline|cycle|method|train|lifecycle', re.I),
              'cycle': re.compile(r'cycle|loop|repeat|circular|lifecycle', re.I),
              'venn': re.compile(r'compar|similar|differ|overlap|versus|\bvs\b|both', re.I),
              'pyramid': re.compile(r'hierarch|level|pyramid|needs|priorit|food chain|tier', re.I),
              'matrix': re.compile(r'swot|strength|weakness|priorit|quadrant|matrix|risk|trade', re.I),
              'mindmap': re.compile(r'overview|concept|idea|theme|topic|introduc|summary', re.I),
              'process': re.compile(r'how|process|step|method|procedure|stage|guide', re.I),
              'comparison': re.compile(r'compar|versus|\bvs\b|pros|cons|differ|advantage|option', re.I),
              'stat-cards': re.compile(r'fact|figure|number|statistic|key|impact|scale|data|result', re.I),
              'scatter': re.compile(r'correlat|relationship|data|trend|measure|result|experiment', re.I)}


@dataclasses.dataclass
class Part:
    heading: str
    level: int
    words: int
    hints: list[str]
    diagrams: list[str] = dataclasses.field(default_factory=list)
    figures: int = 0


class Tally:
    """Calls and tokens per phase, for Made.phases and the CreatedFile meta (C3 accounting)."""

    def __init__(self):
        self.phases: dict[str, dict] = {}
        self.engine = None
        self.state: dict | None = None   # the latest checkpoint (docs/PLAN-files-robust.md 3.3)

    def add(self, phase: str, reply=None, ms: float = 0.0, calls: int = 1):
        p = self.phases.setdefault(phase, {'phase': phase, 'calls': 0, 'llm_in': 0, 'llm_out': 0, 'ms': 0})
        p['calls'] += calls
        p['ms'] += round(ms)
        if reply is not None:
            p['llm_in'] += reply.input_tokens
            p['llm_out'] += reply.output_tokens
            self.engine = reply.engine or self.engine

    @property
    def calls(self) -> int:
        return sum(p['calls'] for p in self.phases.values())

    @property
    def tokens(self) -> tuple[int, int]:
        return (sum(p['llm_in'] for p in self.phases.values()), sum(p['llm_out'] for p in self.phases.values()))


def mean(pair) -> float:
    return (pair[0] + pair[1]) / 2


def budget(fmt: str, brief: Brief) -> tuple[int, int]:
    """(target words, target content sections) for the file."""
    n_diag = diagrams_min(brief)
    n_img = PLANNED_IMAGES if brief.images else 0
    if fmt == 'pptx':
        sections = max(1, round(mean(brief.slides)) - 1) if brief.slides else 9
        return sections * SLIDE_WORDS, sections
    if brief.pages:
        words = round(mean(brief.pages) * WORDS_PER_PAGE - (DIAGRAM_PAGE * n_diag + IMAGE_PAGE * n_img) * WORDS_PER_PAGE)
        words = max(words, 250)
    elif brief.words:
        words = brief.words
    else:
        words = 1500
    return words, max(3, min(MAX_PARTS, round(words / 450)))


def words_of(text: str) -> int:
    return len(str(text).split())


def block_words(b: dict) -> int:
    t = b.get('type')
    if t in ('paragraph', 'quote'):
        return words_of(b.get('text', ''))
    if t == 'bullets':
        return sum(words_of(i) for i in b.get('items') or [])
    if t == 'table':
        return sum(words_of(str(c)) for r in b.get('rows') or [] for c in (r if isinstance(r, list) else [r]))
    return 0


def section_words(sec: dict) -> int:
    return sum(block_words(b) for b in sec.get('blocks') or [] if isinstance(b, dict))


def spec_words(spec: dict) -> int:
    return sum(section_words(s) for s in spec.get('sections') or [] if isinstance(s, dict))


# ---------- the plan ----------


def parts_from(outline: dict, target_words: int, n_sections: int, slides: bool) -> list[Part]:
    parts = []
    for s in outline.get('sections') or []:
        if not isinstance(s, dict) or not str(s.get('heading') or '').strip():
            continue
        try:
            level = min(max(int(s.get('level') or 1), 1), 3)
            words = max(int(s.get('words') or 0), 1)
        except (TypeError, ValueError):
            level, words = 1, 1
        hints = [h for h in s.get('blocks_hint') or [] if h in BLOCK_HINTS]
        parts.append(Part(str(s['heading']).strip()[:200], level, words, hints))
    parts = parts[:MAX_PARTS]
    if slides:
        for p in parts:
            p.words, p.level = SLIDE_WORDS, 1
        return parts[:n_sections] if len(parts) > n_sections else parts
    rescale(parts, target_words)
    return parts


def rescale(parts: list[Part], target_words: int) -> None:
    """Word targets that sum to target_words, each between MIN_SECTION_WORDS and MAX_SECTION_WORDS."""
    total = sum(p.words for p in parts) or 1
    for p in parts:
        p.words = min(MAX_SECTION_WORDS, max(MIN_SECTION_WORDS, round(p.words * target_words / total)))


def assign(parts: list[Part], brief: Brief) -> None:
    """Puts each asked-for diagram and figure in the section that fits it best, so the section calls ask for them."""
    if not parts:
        return
    start = 1 if len(parts) > 1 else 0
    kinds = list(brief.diagram_kinds) or (['any'] if brief.diagrams else [])
    for kind in kinds:
        taken = [p for p in parts if p.diagrams]
        pick = (next((p for p in parts if kind in p.hints and p not in taken), None) or
                next((p for p in parts[start:] if kind in KIND_WORDS and KIND_WORDS[kind].search(p.heading)
                      and p not in taken), None) or
                next((p for p in parts[start:] if p not in taken), None) or parts[-1])
        pick.diagrams.append(kind)
    if brief.images:
        order = [p for p in parts if 'figure' in p.hints] + [p for p in parts[start:] if 'figure' not in p.hints]
        order = order or parts
        per = 2 if len(order) < PLANNED_IMAGES else 1
        left = PLANNED_IMAGES
        for p in order * per:
            if left <= 0:
                break
            p.figures += 1
            left -= 1


def batches(parts: list[Part], n: int) -> list[list[Part]]:
    """The parts in n contiguous groups of about equal words."""
    n = max(1, min(n, len(parts)))
    total = sum(p.words for p in parts)
    out, cur, acc = [], [], 0
    for i, p in enumerate(parts):
        cur.append(p)
        acc += p.words
        left_parts, left_groups = len(parts) - i - 1, n - len(out) - 1
        if left_groups > 0 and (acc >= total * (len(out) + 1) / n or left_parts == left_groups):
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def asks(p: Part, slides: bool) -> str:
    what = [f'- "{p.heading}" (level {p.level}): ' + ('one slide' if slides else f'about {p.words} words')]
    for kind in p.diagrams:
        what.append(f'Include {KIND_TEXT.get(kind, KIND_TEXT["any"])}.')
    if p.figures:
        what.append(f'Include {p.figures} figure block{"s" if p.figures > 1 else ""} (a Wikimedia Commons search query '
                    f'and a caption).')
    return ' '.join(what)


# ---------- the call plan (shared with jevrouter/estimate.py, so the two cannot drift) ----------


@dataclasses.dataclass
class CallPlan:
    sections: int          # budget()[1]
    words: int             # budget()[0]
    batches: int           # n_batches
    max_calls: int         # outline + batches + top-up + MAX_REPAIR_CALLS
    token_budget: int


def plan_calls(fmt: str, brief: Brief) -> CallPlan:
    """How many calls the writer makes for a file: the outline, the section batches, at most one top-up and at most
    MAX_REPAIR_CALLS repair calls."""
    words, sections = budget(fmt, brief)
    slides = fmt == 'pptx'
    n_batches = max(1, math.ceil((sections / 10) if slides else words / BATCH_WORDS))
    token_budget = min(32000, sections * 350) if slides else min(32000, int(1.6 * words * 1.4))
    return CallPlan(sections, words, n_batches, 1 + n_batches + 1 + MAX_REPAIR_CALLS, token_budget)


# ---------- checkpoints (docs/PLAN-files-robust.md 3.3) ----------


def _json_size(obj) -> int:
    try:
        return len(json.dumps(obj, ensure_ascii=False, default=str).encode('utf-8', 'replace'))
    except (TypeError, ValueError, RecursionError):
        return CHECKPOINT_MAX_BYTES + 1


def cap_state(state: dict) -> dict:
    """The state within CHECKPOINT_MAX_BYTES: `ctx` is cut first, then the oldest written sections are dropped (their
    parts become missing again, so a resume writes them)."""
    if _json_size(state) <= CHECKPOINT_MAX_BYTES:
        return state
    state = {**state, 'written': dict(state.get('written') or {})}
    for size in (4000, 1000, 0):
        state['ctx'] = str(state.get('ctx') or '')[:size]
        if _json_size(state) <= CHECKPOINT_MAX_BYTES:
            return state
    for k in sorted(state['written'], key=lambda x: int(x) if str(x).isdigit() else 0):
        del state['written'][k]
        if _json_size(state) <= CHECKPOINT_MAX_BYTES:
            break
    if _json_size(state) > CHECKPOINT_MAX_BYTES:
        state['reply'] = str(state.get('reply') or '')[:CHECKPOINT_MAX_BYTES // 2]
    return state


def info(state: dict, qid: int, tid: str) -> dict:
    """The CheckpointInfo summary of a stored state (web/src/protocol.ts); the full state never leaves the server."""
    state = state if isinstance(state, dict) else {}
    kind = 'single' if state.get('kind') == 'single' else 'longdoc'
    file_id = state.get('file_id')
    phase = state.get('phase') if state.get('phase') in ('outline', 'sections', 'topup', 'render', 'done') else 'sections'
    if kind == 'single':
        planned, written, missing = 1, 1 if file_id else 0, []
        resumable = not file_id and bool(str(state.get('reply') or '').strip())
    else:
        parts = [p for p in state.get('parts') or [] if isinstance(p, dict)]
        done = {str(k) for k in (state.get('written') or {})}
        missing = [str(p.get('heading') or f'Section {i + 1}') for i, p in enumerate(parts) if str(i) not in done]
        planned, written = len(parts), len(parts) - len(missing)
        # every part written but no file built (a timeout or cancel during pictures, the fit loop or the render, or a
        # render that failed) is resumable too: the resume goes straight to the build at 0 section calls
        resumable = bool(missing) or not file_id
    by = state.get('resumed_by') if isinstance(state.get('resumed_by'), dict) else None
    if by and by.get('file_id'):  # an earlier Resume already made the file
        resumable = False
    out = {'qid': qid, 'tid': str(tid), 'kind': kind, 'format': state.get('format') or 'pdf', 'phase': phase,
           'planned': planned, 'written': written, 'missing': missing,
           'tokens_in': int(state.get('tokens_in') or 0), 'tokens_out': int(state.get('tokens_out') or 0),
           'at': float(state.get('at') or 0.0), 'resumable': resumable, 'file_id': file_id}
    if by and type(by.get('qid')) is int:
        out['resumed_by'] = by['qid']
    return out


def sink(job, state: dict) -> None:
    """Hands a checkpoint to the pipeline's sink (never fails the file) and keeps it on the job's tally."""
    state = cap_state(state)
    tally = getattr(job, 'tally', None)
    if tally is not None:
        tally.state = state
    fn = getattr(job, 'checkpoint', None)
    if fn is None:
        return
    try:
        fn(copy.deepcopy(state))
    except Exception:
        pass


def time_left(job) -> float:
    deadline = getattr(job, 'deadline', None)
    return float('inf') if deadline is None else deadline - time.monotonic()


def partial_caveat(missing: list[str], planned: int, slides: bool) -> str:
    heads = ', '.join(missing[:5]) + (f' and {len(missing) - 5} more' if len(missing) > 5 else '')
    return (f'{len(missing)} of {planned} planned {"slides" if slides else "sections"} could not be written ({heads}). '
            f'Use Resume on the file card to write them; it reuses everything already written.')


# ---------- the writer ----------


class Writer:
    """One long file: outline, section batches, the repair ladder (docs/PLAN-files-robust.md 3.1), images, the fit
    loop and the final build. Its state is the checkpoint, sent to the job's sink after every paid call."""

    def __init__(self, job: Job, engine, jev, fmt: str, brief: Brief, mode: str, *, seed: dict | None = None,
                 notes=(), state: dict | None = None):
        from ..agents import create as ca
        from . import brief as brief_mod
        self.ca, self.job, self.engine, self.jev, self.fmt, self.brief, self.mode = ca, job, engine, jev, fmt, brief, mode
        self.seed, self.notes = seed, list(notes)
        self.req = job.request.strip()
        self.slides = fmt == 'pptx'
        self.label = ca.LABELS.get(fmt, fmt)
        self.theme = ca.theme_of(self.req) or brief.theme
        plan = plan_calls(fmt, brief)
        self.target_words, self.n_sections, self.n_batches = plan.words, plan.sections, plan.batches
        self.max_calls, self.token_budget = plan.max_calls, plan.token_budget
        if getattr(job, 'tally', None) is None:
            job.tally = Tally()
        self.tally = Tally()   # this writer's calls; job.tally sees the same adds
        self.brief_lines = describe(brief, fmt)
        self.system = ca.SYSTEM.format(shape=LONG_SHAPES['pptx' if self.slides else 'default'])
        self.results: list = []     # RuleResults for the file (S1 notes by section, V12)
        self.caveats: list[str] = []
        self.repairs = {'calls': 0, 'llm_in': 0, 'llm_out': 0, 'sections': []}
        self.bad: set[int] = set()   # parts whose reply came back with nothing usable
        design = getattr(job, 'design', None)
        self.state = state if state is not None else {
            'v': 1, 'kind': 'longdoc', 'format': fmt, 'request': self.req, 'brief': brief_mod.to_dict(brief),
            'theme': self.theme, 'font': brief.font, 'design': design, 'ctx': None, 'title': '', 'subtitle': '',
            'parts': [], 'written': {}, 'failed': [], 'phase': 'outline', 'engine': getattr(engine, 'name', None),
            'tokens_in': 0, 'tokens_out': 0, 'calls': 0, 'at': time.time(), 'file_id': None, 'role': job.role,
            'mode': mode, 'repairs': 0}
        if self.state.get('ctx') is None:
            self.state['ctx'] = context(job, seed, ca)
        self.ctx = str(self.state.get('ctx') or '')

    # ----- calls -----

    async def call(self, phase: str, **kw):
        from ..engines import EngineError, EngineRefusal
        t0 = time.perf_counter()
        try:
            reply = await self.engine.stream(**kw)
        except (EngineError, EngineRefusal):
            ms = (time.perf_counter() - t0) * 1000
            self.tally.add(phase, None, ms)
            self.job.tally.add(phase, None, ms)
            self.state['calls'] = self.state.get('calls', 0) + 1
            raise
        ms = (time.perf_counter() - t0) * 1000
        self.tally.add(phase, reply, ms)
        self.job.tally.add(phase, reply, ms)
        self.state['calls'] = self.state.get('calls', 0) + 1
        self.state['tokens_in'] = self.state.get('tokens_in', 0) + reply.input_tokens
        self.state['tokens_out'] = self.state.get('tokens_out', 0) + reply.output_tokens
        self.state['engine'] = reply.engine or self.state.get('engine')
        return reply

    def save(self, phase: str | None = None):
        if phase:
            self.state['phase'] = phase
        self.state['at'] = time.time()
        sink(self.job, self.state)

    def failed(self, msg: str) -> Made:
        tin, tout = self.tally.tokens
        self.save()
        return self.ca.Made(msg, False, self.tally.engine or self.engine.name, tin, tout,
                            phases=list(self.tally.phases.values()), checkpoint=self.state)

    # ----- the plan -----

    @property
    def parts(self) -> list[Part]:
        return [Part(**{k: p[k] for k in ('heading', 'level', 'words', 'hints', 'diagrams', 'figures') if k in p})
                for p in self.state.get('parts') or [] if isinstance(p, dict)]

    def plan_text(self, parts: list[Part]) -> str:
        return '\n'.join(f'{i}. {p.heading}' + ('' if self.slides else f' (about {p.words} words)')
                         for i, p in enumerate(parts, 1))

    async def outline(self) -> Made | None:
        from ..engines import EngineError, EngineRefusal
        ca, seed = self.ca, self.seed
        seed_heads = [s.get('heading') for s in (seed or {}).get('sections') or []
                      if isinstance(s, dict) and s.get('heading')]
        lines = [f'Request: {self.req}', f'Format: {self.label}', *self.brief_lines,
                 (f'Plan {self.n_sections} sections, one per slide.' if self.slides else
                  f'Plan about {self.target_words:,} words in all, in {max(3, round(self.target_words / 500))} to '
                  f'{max(4, round(self.target_words / 300))} sections of at most {MAX_SECTION_WORDS} words each.')]
        if seed_heads:
            lines.append('Build on this earlier outline, keeping what fits: ' + '; '.join(seed_heads[:30]))
        prompt = '\n'.join(lines) + (f'\n\nNotes and context:\n{self.ctx}' if self.ctx else '')
        try:
            reply = await self.call('outline', system=OUTLINE_SYSTEM, prompt=prompt, effort='medium', max_tokens=2000,
                                    schema=OUTLINE_SCHEMA)
            outline, _ = ca.read_reply(reply.text, prose=False)
            if not isinstance(outline, dict) or not outline.get('sections'):
                raise ValueError('not an outline')
        except EngineRefusal:
            return self.failed(f'{self.engine.label} declined to write this file.')
        except EngineError as e:
            return self.failed(f'The create agent needs {self.engine.label} to write new content, which failed '
                               f'({e.why}).')
        except ValueError:
            outline = {'title': (seed or {}).get('title') or ca.topic_title(self.req) or 'Document', 'subtitle': '',
                       'sections': [{'heading': h, 'level': 1, 'words': 1, 'blocks_hint': []} for h in seed_heads]}
        parts = parts_from(outline, self.target_words, self.n_sections, self.slides)
        if not parts:
            return self.failed('No file was made: the model\'s outline had no sections. Resume can try again.')
        assign(parts, self.brief)
        self.state['parts'] = [dataclasses.asdict(p) for p in parts]
        self.state['title'] = str(outline.get('title') or (seed or {}).get('title') or ca.topic_title(self.req)
                                  or 'Document')[:120]
        self.state['subtitle'] = str(outline.get('subtitle') or '')[:300]
        self.save('sections')
        return None

    # ----- sections -----

    def missing(self) -> list[int]:
        done = {str(k) for k in self.state['written']}
        return [i for i in range(len(self.state['parts'])) if str(i) not in done]

    def take(self, idx: list[int], secs: list[dict], notes: list[str]):
        """Places the sections of one reply on their parts (rung 1: every section repaired in code); a part whose
        section kept no block stays missing."""
        parts = self.parts
        placed = self.ca.place(secs, [parts[i] for i in idx])
        for i, raw in zip(idx, placed):
            if raw is None:
                self.bad.add(i)
                if i not in self.state['failed']:
                    self.state['failed'].append(i)
                continue
            sec, results = self.ca.repair_section(raw, i + 1)
            sec['heading'], sec['level'] = parts[i].heading, parts[i].level
            self.results += results
            if self.ca.usable(sec):
                self.state['written'][str(i)] = sec
                if i in self.state['failed']:
                    self.state['failed'].remove(i)
            else:
                self.bad.add(i)
                if i not in self.state['failed']:
                    self.state['failed'].append(i)
        for n in notes:
            if n not in self.notes:
                self.notes.append(n)

    async def write(self, idx: list[int], gate: asyncio.Semaphore, *, repair: bool = False) -> bool:
        """One section call for the parts `idx`; True when the call returned (usable or not)."""
        from .. import create as cf
        from ..engines import EngineError, EngineRefusal
        parts = self.parts
        group = [parts[i] for i in idx]
        words = sum(p.words for p in group)
        cap = max(1500, int(self.token_budget * words / max(self.target_words, 1)))
        lines = [f'Request: {self.req}', f'Format: {self.label}', f'Title: {self.state["title"]}', 'Outline:',
                 self.plan_text(parts)]
        if repair:
            lines += ['The last reply for these sections had no usable content. Write them again, in this order, as '
                      'the sections array, each with at least one block (the title and subtitle may be empty):']
        else:
            lines += ['Write only these sections, in this order, as the sections array (the title and subtitle may be '
                      'empty):']
        lines += [*(asks(p, self.slides) for p in group), *[ln for ln in self.brief_lines if ln.startswith('Black and white')]]
        prompt = '\n'.join(lines) + (f'\n\nNotes and context:\n{self.ctx}' if self.ctx else '')
        async with gate:
            try:
                r = await self.call('sections', system=self.system, prompt=prompt, effort='low', max_tokens=cap,
                                    schema=cf.DOCSPEC_SCHEMA)
            except (EngineError, EngineRefusal):
                for i in idx:
                    if i not in self.state['failed']:
                        self.state['failed'].append(i)
                self.save()
                return False
            if repair:
                self.repairs['calls'] += 1
                self.repairs['llm_in'] += r.input_tokens
                self.repairs['llm_out'] += r.output_tokens
                self.state['repairs'] = self.state.get('repairs', 0) + 1
            got, notes = self.ca.read_reply(r.text)
            secs = [s for s in ((got or {}).get('sections') or []) if isinstance(s, dict)]
            self.take(idx, secs, notes)
            self.save()
            return True

    async def sections(self):
        todo = self.missing()
        if not todo:
            return
        parts = self.parts
        n = max(1, math.ceil(len(todo) / 10) if self.slides else
                math.ceil(sum(parts[i].words for i in todo) / BATCH_WORDS))
        if not self.state.get('written') and len(todo) == len(parts):
            n = self.n_batches
        sub = [parts[i] for i in todo]
        pos = {id(p): i for p, i in zip(sub, todo)}
        idx_groups = [[pos[id(p)] for p in g] for g in batches(sub, n)]
        gate = asyncio.Semaphore(2 if getattr(self.engine, 'billing', 'api') == 'api' else 1)
        await asyncio.gather(*(self.write(g, gate) for g in idx_groups))

    async def repair(self):
        """Rung 2: at most MAX_REPAIR_CALLS calls that rewrite only the missing sections, while time allows."""
        gate = asyncio.Semaphore(1)
        while self.missing() and self.repairs['calls'] < MAX_REPAIR_CALLS and time_left(self.job) >= REPAIR_MIN_SECONDS:
            todo = self.missing()[:REPAIR_GROUP]
            heads = [self.parts[i].heading for i in todo]
            before = self.repairs['calls']
            ok = await self.write(todo, gate, repair=True)
            if not ok or self.repairs['calls'] == before:
                break  # the engine is failing: no more calls
            fixed = [h for i, h in zip(todo, heads) if str(i) in self.state['written']]
            self.repairs['sections'] += [h for h in fixed if h not in self.repairs['sections']]

    # ----- the file -----

    async def run(self) -> Made:
        from .. import create as cf
        ca, brief, fmt = self.ca, self.brief, self.fmt
        if not self.state.get('parts'):
            if (out := await self.outline()) is not None:
                return out
        await self.sections()
        if self.missing():
            await self.repair()
        parts = self.parts
        written = self.state['written']
        missing = self.missing()
        sections = [copy.deepcopy(written[str(i)]) for i in range(len(parts)) if str(i) in written]
        # S1 notes for sections that came back unusable, by their place in the file
        for i in sorted(self.bad):
            if i < len(parts):
                again = ' and was written again' if str(i) in written else ''
                self.results.append(cf.RuleResult('S1', 'fix', False, f'the reply for section {i + 1} '
                                                  f'("{parts[i].heading}") had no usable content{again}'))
        if not sections:
            self.save()
            return self.failed(f'The create agent needs {self.engine.label} to write new content, which failed (no '
                               f'section came back).')
        partial = None
        if missing:
            heads = [parts[i].heading for i in missing]
            partial = {'planned': len(parts), 'written': len(parts) - len(missing), 'missing': heads, 'resume': None}
            unit = 'slides' if self.slides else 'sections'
            self.results.append(cf.RuleResult('V12', 'warn', False, f'{partial["written"]} of {len(parts)} planned '
                                              f'{unit} written; missing: {", ".join(heads[:5])}'
                                              + (f' and {len(heads) - 5} more' if len(heads) > 5 else '')))
            self.caveats.append(partial_caveat(heads, len(parts), self.slides))
        spec = {'title': self.state['title'] or 'Document', 'subtitle': self.state.get('subtitle') or '',
                'sections': sections}
        # images before the fit loop, so it measures the file as it will be
        spec, credits, image_caveats, phase = await ca.with_images(spec, self.job, brief, self.theme)
        self.caveats += image_caveats
        if phase:
            self.tally.phases['assets'] = phase
        if self.theme:
            spec['theme'] = self.theme
        if brief.font:
            spec['font'] = brief.font
        render_ms = await self.fit(spec, parts, bool(missing))
        tin, tout = self.tally.tokens
        self.save('render')
        out = await ca.finish(spec, fmt, self.jev, source='llm', tokens=tin + tout, notes=list(self.notes),
                              brief=brief, theme=self.theme, role=self.job.role, credits=credits,
                              caveats=self.caveats, extra=self.results, design=getattr(self.job, 'design', None),
                              design_notes=getattr(self.job, 'design_notes', None))
        out.engine = self.tally.engine or self.engine.name
        # finish()'s own calls (Studio's art direction, critic and freeform pages) add to the writer's, never replace
        studio_in, studio_out = out.llm_in, out.llm_out
        out.llm_in, out.llm_out, out.effort = tin + studio_in, tout + studio_out, 'low'
        final = next((p for p in out.phases if p['phase'] == 'render'), None)
        self.tally.add('render', None, render_ms + (final['ms'] if final else 0), calls=final['calls'] if final else 0)
        if final:
            self.tally.phases['render']['llm_in'] += final['llm_in']
            self.tally.phases['render']['llm_out'] += final['llm_out']
        out.phases = [self.tally.phases[k] for k in ('outline', 'sections', 'topup', 'assets', 'render')
                      if k in self.tally.phases]
        out.repairs = dict(self.repairs) if self.repairs['calls'] else None
        out.partial = partial if out.file is not None else None
        if out.file is not None:
            out.file['phases'] = out.phases
            out.file['tokens'] = tin + tout + studio_in + studio_out
            if out.repairs:
                out.file['repairs'] = out.repairs
            if partial:
                out.file['partial'] = partial
            self.state['file_id'] = out.file.get('id')
            self.save('done' if not missing else 'render')
        else:
            self.save()
        out.checkpoint = self.state
        return out

    async def fit(self, spec: dict, parts: list[Part], has_missing: bool) -> float:
        """The fit loop: render and trim or top up until the page or slide count is in range. A rule that fails while
        measuring is noted and the loop stops; the final build still renders what there is."""
        from .. import create as cf
        brief, fmt, slides = self.brief, self.fmt, self.slides
        target = brief.slides if slides else brief.pages
        render_ms = 0.0
        if not target:
            return render_ms
        topped = False
        for _ in range(MAX_FIT_RENDERS):
            t0 = time.perf_counter()
            try:
                count, per_page = await asyncio.to_thread(measure, spec, fmt, brief=brief, request=self.req,
                                                          design=getattr(self.job, 'design', None))
            except cf.SpecError as e:
                self.caveats.append(f'The length could not be checked before the final build ({e.rule_id}: '
                                    f'{e.message}), so the file is as written.')
                break
            except Exception as e:  # a renderer failure while measuring: the final build has its own ladder
                self.caveats.append(f'The length could not be checked before the final build '
                                    f'({type(e).__name__}), so the file is as written.')
                break
            render_ms += (time.perf_counter() - t0) * 1000
            lo, hi = target
            if lo <= count <= hi:
                break
            if count < lo:
                # a partial file is finished by Resume, not by new sections that would take the missing ones' place
                if topped or has_missing or self.tally.calls >= self.max_calls:
                    break
                topped = True
                self.save('topup')
                await top_up(spec, parts, fmt, lo, hi, count, per_page, self.call, self.system, self.req, self.label,
                             self.state['title'], self.plan_text(parts), self.ctx, self.token_budget, self.target_words)
                self.save()
                continue
            trim(spec, fmt, count, lo, hi, per_page)
        return render_ms


async def write_long(job: Job, engine, jev, fmt: str, brief: Brief, mode: str, *, seed: dict | None = None,
                     notes: list[str] | tuple = ()) -> Made:
    """C3 with the repair ladder: outline, section batches, repair calls for what came back unusable, then a partial
    file with a Resume caveat rather than no file."""
    return await Writer(job, engine, jev, fmt, brief, mode, seed=seed, notes=notes).run()


async def resume_long(job: Job, engine, jev, state: dict) -> Made:
    """Continues a checkpoint: a `longdoc` state writes only the parts not yet written (then images, the fit loop and
    the final build, as write_long); a `single` state rebuilds its stored reply with no call. The new file's meta
    carries `resumed_from` and only the new calls' tokens."""
    from ..agents import create as ca
    from . import brief as brief_mod
    ref = state if isinstance(state, dict) else {}
    if isinstance(ref.get('state'), dict):  # {'qid', 'tid', 'state'} as the resume request carries it
        state = ref['state']
    state = copy.deepcopy(state or {})
    fmt = state.get('format') if state.get('format') in ca.LABELS else 'pdf'
    brief = brief_mod.from_dict(state.get('brief')) or brief_mod.parse_brief(state.get('request') or job.request)
    if state.get('design') and getattr(job, 'design', None) is None:
        job.design = state['design']
    resumed_from = {'qid': ref.get('qid', state.get('qid')), 'tid': ref.get('tid', state.get('tid')),
                    'file_id': state.get('file_id')}
    for k in ('tokens_in', 'tokens_out', 'calls'):
        state[k] = 0
    state['file_id'] = None
    state.pop('resumed_by', None)  # the source's own record of who resumed it, not this run's
    if state.get('kind') == 'single':
        out = await ca.rebuild_single(job, jev, state, fmt, brief)
    else:
        if not job.request.strip():
            job.request = state.get('request') or ''
        if getattr(job, 'role', None) is None:
            job.role = state.get('role') or 'primary'
        if engine is None:
            return ca.Made('No file was made: writing the missing sections needs an LLM engine. Choose an engine in '
                           'Settings and use Resume again.', False, checkpoint=state)
        out = await Writer(job, engine, jev, fmt, brief, state.get('mode') or 'balanced', state=state).run()
    if out.file is not None:
        out.file['resumed_from'] = resumed_from
    return out


def context(job: Job, seed: dict | None, ca) -> str:
    """The notes the writer sees, at most LONG_CONTEXT_CHARS: the seed file's text first (when there is one), then the
    earlier steps' answers without their reply lines (at most 60%), attached tables, attached content documents (at
    least 25% when there are any) and earlier turns. Design files are never here (they are applied by code)."""
    budget_chars = LONG_CONTEXT_CHARS
    head = []
    if seed:
        try:
            from . import normalize
            text = ca.spec_text(normalize(seed, 'md')[0])
        except Exception:
            text = ''
        if text.strip():
            head.append(f'Earlier file "{seed.get("title") or "draft"}":\n{text}'[:8000])
    left = budget_chars - sum(len(p) + 2 for p in head)
    deps = [(t, ca.clean_answer(a)) for t, a in job.deps]
    dep_parts = [f'Earlier step "{t[:200]}":\n{a}' for t, a in deps if a.strip()]
    doc_parts = [f'Attached file {m.get("name", "file")}:\n{text}' for m, text in job.docs if str(text or '').strip()]
    docs_len = sum(len(p) + 2 for p in doc_parts)
    docs_floor = min(docs_len, int(0.25 * budget_chars)) if doc_parts else 0
    deps_room = max(0, min(int(0.6 * budget_chars), left - docs_floor))
    dep_text = _fill(dep_parts, deps_room)
    left -= len(dep_text) + (2 if dep_text else 0)
    rest_job = dataclasses.replace(job, deps=[], docs=[])
    tables_text = ca.context_text(dataclasses.replace(rest_job, context=[]), max(0, left - docs_floor)) \
        if job.tables else ''
    left -= len(tables_text) + (2 if tables_text else 0)
    doc_text = _fill(doc_parts, max(0, left))
    left -= len(doc_text) + (2 if doc_text else 0)
    turns_text = ca.context_text(dataclasses.replace(rest_job, tables=[]), left) if left >= 200 and job.context else ''
    return '\n\n'.join(p for p in [*head, dep_text, tables_text, doc_text, turns_text] if p)[:budget_chars]


def _fill(parts: list[str], room: int) -> str:
    out, left = [], room
    for p in parts:
        if left < 200:
            break
        out.append(p if len(p) <= left else p[:left - 3].rstrip() + '...')
        left -= len(out[-1]) + 2
    return '\n\n'.join(out)


def studio_count(spec: dict, fmt: str, *, brief=None, request: str = '', design: dict | None = None) -> int | None:
    """The slides or pages Studio will make of this spec when TG_STUDIO designs the format (studio.agent.count_pages,
    keyless, 0 tokens), or None when it doesn't or can't say (the standard renderer's count is used then)."""
    from ..agents.create import studio_on
    if fmt not in ('pptx', 'pdf') or not studio_on(fmt):
        return None
    from .. import create as cf
    from . import design as design_mod
    try:
        from ..studio import agent as studio_agent
        norm, _ = cf.normalize(spec, fmt)
        return studio_agent.count_pages(norm, fmt, brief=brief, request=request or '',
                                        tokens_src=design_mod.clean_design(design) or None)
    except cf.SpecError:
        raise
    except Exception:
        return None


def measure(spec: dict, fmt: str, *, brief=None, request: str = '', design: dict | None = None) -> tuple[int, float]:
    """(pages or slides the spec makes, words per text page). PDF renders; slides come from normalize; Word and
    Markdown are estimated from words. With Studio designing the format, the count is Studio's layout (its pages
    start sections on a new page and set type larger, so the standard renderer's count would be wrong). Blocking."""
    from .. import create as cf
    designed = studio_count(spec, fmt, brief=brief, request=request, design=design)
    if fmt == 'pptx':
        if designed is not None:
            return designed, float(SLIDE_WORDS)
        norm, _ = cf.normalize(spec, fmt)
        return 1 + len(norm['sections']), float(SLIDE_WORDS)
    words = spec_words(spec)
    if fmt != 'pdf':
        return max(1, round(words / WORDS_PER_PAGE)), float(WORDS_PER_PAGE)
    if designed is not None:
        pages = designed
    else:
        from io import BytesIO

        from pypdf import PdfReader
        pages = len(PdfReader(BytesIO(cf.render(spec, fmt))).pages)
    blocks = [b for s in spec.get('sections') or [] for b in s.get('blocks') or [] if isinstance(b, dict)]
    visual = DIAGRAM_PAGE * sum(b.get('type') in cf.DIAGRAMS for b in blocks) + \
        IMAGE_PAGE * sum(b.get('type') == 'image' for b in blocks) + \
        0.3 * sum(b.get('type') == 'chart' for b in blocks)
    text_pages = max(1.0, pages - 0.5 - visual)
    return pages, min(750.0, max(250.0, words / text_pages))


def aim(lo: int, hi: int) -> float:
    """Where the last page should end: half way into the range's last page."""
    return hi - 0.5 if hi > lo else lo - 0.35


async def top_up(spec, parts, fmt, lo, hi, count, per_page, call, system, req, label, title, plan_text, ctx,
                 token_budget, target_words) -> dict:
    """One extra call for the words (or slides) the file is short of, split across its thinnest sections."""
    from .. import create as cf
    from ..agents import create as ca
    from ..engines import EngineError, EngineRefusal
    body = [s for s in spec['sections'] if s.get('heading') != 'Image credits']
    if fmt == 'pptx':
        need = max(1, round(aim(lo, hi)) - count)
        ask = [f'The deck is {need} slide{"" if need == 1 else "s"} short. Write {need} more sections (one per slide) '
               f'that continue the outline without repeating it, as the sections array.']
        words = need * SLIDE_WORDS
    else:
        need_words = max(120, round((aim(lo, hi) - (count - 0.5)) * per_page))
        by_share = sorted(range(len(body)), key=lambda i: section_words(body[i]) /
                          max(1, next((p.words for p in parts if p.heading == body[i].get('heading')), 1)))
        chosen = sorted(by_share[:min(3, len(body))])
        each = max(60, round(need_words / max(1, len(chosen))))
        ask = ['The document is too short. Write more for these sections, in this order, as the sections array: new '
               'paragraphs (and bullets or a table where useful) that continue each section without repeating it.']
        ask += [f'- "{body[i].get("heading")}": add about {each} words' for i in chosen]
        words = each * len(chosen)
    prompt = '\n'.join([f'Request: {req}', f'Format: {label}', f'Title: {title}', 'Outline:', plan_text, *ask])
    prompt += f'\n\nNotes and context:\n{ctx}' if ctx else ''
    cap = max(1200, int(token_budget * words / max(target_words, 1)))
    try:
        r = await call('topup', system=system, prompt=prompt, effort='low', max_tokens=cap, schema=cf.DOCSPEC_SCHEMA)
    except (EngineError, EngineRefusal):
        return spec
    got, _ = ca.read_reply(r.text)
    more = []
    for n, raw in enumerate((got or {}).get('sections') or [], 1):
        if isinstance(raw, dict):
            sec, _ = ca.repair_section(raw, n)
            if ca.usable(sec):
                more.append(sec)
    if fmt == 'pptx':
        credits = [s for s in spec['sections'] if s.get('heading') == 'Image credits']
        spec['sections'] = (body + more)[:MAX_PARTS] + credits
        return spec
    for i, extra in zip(chosen, more):
        blocks = [b for b in extra.get('blocks') or [] if isinstance(b, dict) and b.get('type') != 'figure']
        body[i]['blocks'] = (list(body[i].get('blocks') or []) + blocks)[:30]
    return spec


def trim(spec: dict, fmt: str, count: int, lo: int, hi: int, per_page: float) -> None:
    """Code shortens a file that came out too long: trailing paragraphs of the longest sections first, then page
    breaks (slides: the last content slides)."""
    body = [s for s in spec['sections'] if s.get('heading') != 'Image credits']
    if fmt == 'pptx':
        over = count - hi
        keep = body[:max(1, len(body) - over)]
        spec['sections'] = keep + [s for s in spec['sections'] if s.get('heading') == 'Image credits']
        return
    cut = max(((count - 0.5) - aim(lo, hi)) * per_page, per_page * 0.5)
    while cut > 0:
        paras = [(section_words(s), s) for s in body
                 if sum(1 for b in s.get('blocks') or [] if b.get('type') == 'paragraph') >= 2]
        if not paras:
            break
        _, sec = max(paras, key=lambda x: x[0])
        idx = max(i for i, b in enumerate(sec['blocks']) if b.get('type') == 'paragraph')
        cut -= block_words(sec['blocks'][idx])
        del sec['blocks'][idx]
    if cut > 0:
        for s in body:
            s['blocks'] = [b for b in s.get('blocks') or [] if b.get('type') != 'page_break']

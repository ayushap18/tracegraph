"""The long-document writer (docs/PLAN-accuracy-v2.md C3): an outline call, then section calls in batches at word
targets from the brief, then a fit loop that renders and trims or tops up until the page or slide count is in range.

Sizes come from word targets in the prompts and from code, never from max_tokens alone: CLI engines ignore it
(engines/cli.py). A 12-page PDF takes an outline call, about three section calls and at most one top-up call; the fit
loop renders at most three times before the final build renders the file once more (four renders in all).
"""
from __future__ import annotations

import asyncio
import dataclasses
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
             'flow': 'a flow block (3 to 8 steps joined by edges)', 'any': 'a timeline, tree or flow block or a chart'}
KIND_WORDS = {'timeline': re.compile(r'histor|origin|timeline|milestone|evolution|begin|began|era|develop', re.I),
              'tree': re.compile(r'type|kind|field|branch|categor|taxonom|structure|subfield|famil|class', re.I),
              'flow': re.compile(r'how|process|work|step|pipeline|cycle|method|train|lifecycle', re.I)}


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
        what.append(f'Include {KIND_TEXT[kind]}.')
    if p.figures:
        what.append(f'Include {p.figures} figure block{"s" if p.figures > 1 else ""} (a Wikimedia Commons search query '
                    f'and a caption).')
    return ' '.join(what)


# ---------- the writer ----------


async def write_long(job: Job, engine, jev, fmt: str, brief: Brief, mode: str, *, seed: dict | None = None,
                     notes: list[str] | tuple = ()) -> Made:
    from .. import create as cf
    from ..agents import create as ca
    from ..engines import EngineError, EngineRefusal, parse_json

    req = job.request.strip()
    slides = fmt == 'pptx'
    label = ca.LABELS.get(fmt, fmt)
    theme = ca.theme_of(req) or brief.theme
    target_words, n_sections = budget(fmt, brief)
    n_batches = max(1, math.ceil((n_sections / 10) if slides else target_words / BATCH_WORDS))
    max_calls = 2 + n_batches + 1
    token_budget = min(32000, int(1.6 * target_words * 1.4)) if not slides else min(32000, n_sections * 350)
    tally = Tally()
    brief_lines = describe(brief, fmt)
    ctx = context(job, seed, ca)
    system = ca.SYSTEM.format(shape=LONG_SHAPES['pptx' if slides else 'default'])

    def failed(msg: str) -> Made:
        tin, tout = tally.tokens
        return ca.Made(msg, False, tally.engine or engine.name, tin, tout, phases=list(tally.phases.values()))

    async def call(phase: str, **kw):
        t0 = time.perf_counter()
        try:
            reply = await engine.stream(**kw)
        except (EngineError, EngineRefusal):
            tally.add(phase, None, (time.perf_counter() - t0) * 1000)
            raise
        tally.add(phase, reply, (time.perf_counter() - t0) * 1000)
        return reply

    # call 1: the outline
    seed_heads = [s.get('heading') for s in (seed or {}).get('sections') or [] if isinstance(s, dict) and s.get('heading')]
    lines = [f'Request: {req}', f'Format: {label}', *brief_lines,
             (f'Plan {n_sections} sections, one per slide.' if slides else
              f'Plan about {target_words:,} words in all, in {max(3, round(target_words / 500))} to '
              f'{max(4, round(target_words / 300))} sections of at most {MAX_SECTION_WORDS} words each.')]
    if seed_heads:
        lines.append('Build on this earlier outline, keeping what fits: ' + '; '.join(seed_heads[:30]))
    prompt = '\n'.join(lines) + (f'\n\nNotes and context:\n{ctx}' if ctx else '')
    try:
        reply = await call('outline', system=OUTLINE_SYSTEM, prompt=prompt, effort='medium', max_tokens=2000,
                           schema=OUTLINE_SCHEMA)
        outline = parse_json(reply.text)
        if not isinstance(outline, dict):
            raise ValueError('not an object')
    except EngineRefusal:
        return failed(f'{engine.label} declined to write this file.')
    except EngineError as e:
        return failed(f'The create agent needs {engine.label} to write new content, which failed ({e.why}).')
    except ValueError:
        outline = {'title': (seed or {}).get('title') or ca.topic_title(req) or 'Document', 'subtitle': '',
                   'sections': [{'heading': h, 'level': 1, 'words': 1, 'blocks_hint': []} for h in seed_heads]}
    parts = parts_from(outline, target_words, n_sections, slides)
    if not parts:
        return failed('No file was made. Rule S1 blocked it: the model\'s outline had no sections.')
    assign(parts, brief)
    title = str(outline.get('title') or (seed or {}).get('title') or ca.topic_title(req) or 'Document')[:120]
    plan_text = '\n'.join(f'{i}. {p.heading}' + ('' if slides else f' (about {p.words} words)')
                          for i, p in enumerate(parts, 1))

    # calls 2..n: the sections, in batches
    groups = batches(parts, n_batches)
    gate = asyncio.Semaphore(2 if getattr(engine, 'billing', 'api') == 'api' else 1)

    async def write(group: list[Part]) -> list[dict] | None:
        words = sum(p.words for p in group)
        cap = max(1500, int(token_budget * words / max(target_words, 1)))
        prompt = '\n'.join([f'Request: {req}', f'Format: {label}', f'Title: {title}', 'Outline:', plan_text,
                            'Write only these sections, in this order, as the sections array (the title and subtitle '
                            'may be empty):', *(asks(p, slides) for p in group),
                            *[ln for ln in brief_lines if ln.startswith('Black and white')]])
        prompt += f'\n\nNotes and context:\n{ctx}' if ctx else ''
        async with gate:
            try:
                r = await call('sections', system=system, prompt=prompt, effort='low', max_tokens=cap,
                               schema=cf.DOCSPEC_SCHEMA)
                got = cf.strip_internal(parse_json(r.text))
            except (EngineError, EngineRefusal, ValueError):
                return None
        secs = [s for s in (got.get('sections') if isinstance(got, dict) else None) or [] if isinstance(s, dict)]
        if len(secs) == len(group):  # the outline's headings and levels keep the file consistent
            for s, p in zip(secs, group):
                s['heading'], s['level'] = p.heading, p.level
        return secs
    written = await asyncio.gather(*(write(g) for g in groups))
    sections = [s for secs in written if secs for s in secs][:MAX_PARTS]
    caveats = []
    if not sections:
        return failed(f'The create agent needs {engine.label} to write new content, which failed (no section came '
                      f'back).')
    if any(secs is None for secs in written):
        missing = sum(len(g) for g, secs in zip(groups, written) if secs is None)
        caveats.append(f'{missing} planned section{"" if missing == 1 else "s"} could not be written')
    spec = {'title': title, 'subtitle': str(outline.get('subtitle') or '')[:300], 'sections': sections}

    # images before the fit loop, so it measures the file as it will be
    spec, credits, image_caveats, phase = await ca.with_images(spec, job, brief, theme)
    caveats += image_caveats
    if phase:
        tally.phases['assets'] = phase
    if theme:
        spec['theme'] = theme
    if brief.font:
        spec['font'] = brief.font

    # the fit loop
    target = brief.slides if slides else brief.pages
    render_ms = 0.0
    if target:
        topped = False
        for _ in range(MAX_FIT_RENDERS):
            t0 = time.perf_counter()
            try:
                count, per_page = await asyncio.to_thread(measure, spec, fmt)
            except cf.SpecError:
                break
            render_ms += (time.perf_counter() - t0) * 1000
            lo, hi = target
            if lo <= count <= hi:
                break
            if count < lo:
                if topped or tally.calls >= max_calls:
                    break
                topped = True
                spec = await top_up(spec, parts, fmt, lo, hi, count, per_page, call, system, req, label, title,
                                    plan_text, ctx, token_budget, target_words)
                continue
            trim(spec, fmt, count, lo, hi, per_page)
    tin, tout = tally.tokens
    out = await ca.finish(spec, fmt, jev, source='llm', tokens=tin + tout, notes=list(notes), brief=brief, theme=theme,
                          role=job.role, credits=credits, caveats=caveats)
    out.engine = tally.engine or engine.name
    out.llm_in, out.llm_out, out.effort = tin, tout, 'low'
    final = next((p for p in out.phases if p['phase'] == 'render'), None)
    tally.add('render', None, render_ms + (final['ms'] if final else 0), calls=0)
    out.phases = [tally.phases[k] for k in ('outline', 'sections', 'topup', 'assets', 'render') if k in tally.phases]
    if out.file is not None:
        out.file['phases'] = out.phases
        out.file['tokens'] = tin + tout
    return out


def context(job: Job, seed: dict | None, ca) -> str:
    """The notes the writer sees: the seed file's text first (when there is one), then the earlier steps' answers
    without their reply lines, attached files and earlier turns, at most LONG_CONTEXT_CHARS."""
    parts = []
    if seed:
        try:
            from . import normalize
            text = ca.spec_text(normalize(seed, 'md')[0])
        except Exception:
            text = ''
        if text.strip():
            parts.append(f'Earlier file "{seed.get("title") or "draft"}":\n{text}'[:8000])
    deps = [(t, ca.clean_answer(a)) for t, a in job.deps]
    deps = [(t, a) for t, a in deps if a.strip()]
    rest = ca.context_text(dataclasses.replace(job, deps=deps), LONG_CONTEXT_CHARS - sum(len(p) for p in parts))
    if rest:
        parts.append(rest)
    return '\n\n'.join(parts)[:LONG_CONTEXT_CHARS]


def measure(spec: dict, fmt: str) -> tuple[int, float]:
    """(pages or slides the spec makes, words per text page). PDF renders; slides come from normalize; Word and
    Markdown are estimated from words. Blocking."""
    from .. import create as cf
    if fmt == 'pptx':
        norm, _ = cf.normalize(spec, fmt)
        return 1 + len(norm['sections']), float(SLIDE_WORDS)
    words = spec_words(spec)
    if fmt != 'pdf':
        return max(1, round(words / WORDS_PER_PAGE)), float(WORDS_PER_PAGE)
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
    from ..engines import EngineError, EngineRefusal, parse_json
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
        got = cf.strip_internal(parse_json(r.text))
    except (EngineError, EngineRefusal, ValueError):
        return spec
    more = [s for s in (got.get('sections') if isinstance(got, dict) else None) or [] if isinstance(s, dict)]
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

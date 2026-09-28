"""Cost preflight (docs/PLAN-files-robust.md section 5): what a draft request would cost in model calls, tokens and
time, before anything runs. Pure: no network, no engine call and no store read. The steps are guessed with the same
keyless code the run uses (the brief, the planner's own tests, the create agent's zero-token paths and the long
writer's call plan), and priced with per-engine constants fitted to recorded runs (evals/fixtures/estimate_runs.jsonl).

A call costs `turns * (FIXED_IN + prompt + context + schema)` input tokens. `turns` is the engine's turns for a
structured (schema) call, 1 for a plain call and the planner's own count for the planner; a call that reads an attached
document takes ATTACH_TURNS more on the CLIs that open it with their own tools. Output is the call's base size, times
OUT_MULT for a structured call. Seconds are PER_CALL_S (scaled by the call's turns against a structured call's) plus
PER_KOUT_S per 1,000 output tokens.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .config import (COST_CONFIRM_CALLS, COST_CONFIRM_SECONDS, COST_CONFIRM_TOKENS, COST_DEADLINE_SHARE,
                     CREATE_CONTEXT_CHARS, KEYLESS)

VERSION = 1
CHARS_PER_TOKEN = 4


@dataclass
class EngineView:
    """What the estimate needs to know of an engine: its name and label, how it bills, whether it can search the web,
    and its health (a cooling or failing engine is never offered as the cheaper one)."""
    name: str
    label: str
    billing: str
    web: bool
    healthy: bool = True
    p50_ms: float | None = None


@dataclass
class Draft:
    query: str
    mode: str = 'balanced'
    agent: str | None = None
    files: list[dict] = field(default_factory=list)   # FileInfo metas (name, kind, chars, columns); design files cost 0
    has_answer: bool = False        # the chat has an earlier answer
    last_file: dict | None = None   # the chat's newest CreatedFile meta
    source: str = 'chat'


@dataclass(frozen=True)
class EngineCost:
    fixed_in: int        # tokens of its own every turn (tool definitions, system prompt)
    turns: int           # turns of a structured (schema) call
    out_mult: float      # output of a structured call, against the content it writes
    per_call_s: float    # seconds of a structured call before its output
    per_kout_s: float    # seconds per 1,000 output tokens
    planner_turns: int   # turns of the planner call
    attach_turns: int    # extra turns of a call that reads an attached document


# Fitted to the recorded runs of docs/PLAN-files-robust.md 7.3 (median agy single create: 41,386 in for a 1K-3K prompt).
ENGINES = {
    'agy': EngineCost(12_400, 3, 4.0, 20, 3, 2, 1),
    'codex': EngineCost(15_200, 2, 2.0, 15, 6, 2, 1),
    'claude-code': EngineCost(1_500, 1, 1.5, 8, 10, 1, 0),
    'anthropic': EngineCost(300, 1, 1.0, 3, 12, 1, 0),
    'default': EngineCost(2_000, 1, 1.5, 10, 10, 1, 0),
}
RESEARCH = {'claude-code': (50_000, 3_000, 90), 'default': (40_000, 3_000, 90)}   # (in, out, seconds) of a web search
SINGLE_OUT = {'pptx': 550, 'pdf': 500, 'docx': 500, 'md': 500, 'xlsx': 300}
OUTLINE_OUT, TOPUP_OUT, PLANNER_OUT, MERGE_OUT, ANSWER_OUT = 1_200, 1_000, 500, 800, 700
DOCSPEC_SCHEMA_TOKENS, OUTLINE_SCHEMA_TOKENS = 1_200, 300
PLANNER_EXTRA = 1_150   # the planner's own system prompt and schema
REPLY_OUT = 60          # a create step's answer line, as the merge reads it
TURN_CHARS = 2_000      # what the earlier turns of a chat add to a call's context
IMAGES_S = 15
LONG_CONTEXT_CHARS = 12_000
BATCH_WORDS = 1_500
MAX_REPAIR_CALLS = 2
# Range: the low end keeps the calls that will run, the high end adds the ones that may.
LOW_IN, LOW_OUT, LOW_S = 0.75, 1 / 3, 0.7
HIGH_IN, HIGH_OUT, HIGH_S = 1.4, 2.0, 1.5

RESEARCHY = re.compile(r'\b(?:research|latest|news|current(?:ly)?|today|recent(?:ly)?|up[- ]to[- ]date|this\s+'
                       r'(?:week|month|year))\b', re.I)
FILE_WORD = re.compile(r'\b(?:make|put|turn|create|export|save|convert|generate|build|write|produce|prepare|draft|'
                       r'compile|format)\b.*\b(?:file|document|doc)s?\b', re.I | re.S)
DESIGN_NAME = re.compile(r'design|style|brand|theme|tokens', re.I)
DESIGN_ASK = re.compile(r'\bdesign(?:\.md|\s+file|\s+system)?\b|\bstyle\s*guide\b|\bbrand\s+guide\b', re.I)
WHAT = {'pptx': 'deck', 'pdf': 'PDF', 'docx': 'document', 'xlsx': 'spreadsheet', 'md': 'file'}
FORMAT_LABEL = {'pdf': 'PDF', 'docx': 'Word', 'pptx': 'PowerPoint', 'xlsx': 'Excel', 'md': 'Markdown'}


@dataclass
class Call:
    """`calls` model calls of one phase on one engine; tokens and seconds are the mid values of all of them together."""
    phase: str
    engine: EngineView
    calls: int
    tokens_in: float
    tokens_out: float
    seconds: float
    optional: bool = False
    in_low: bool = True   # the planner may not run: it is left out of the low end

    def to_dict(self) -> dict:
        return {'phase': self.phase, 'engine': self.engine.name, 'calls': self.calls, 'tokens_in': round(self.tokens_in),
                'tokens_out': round(self.tokens_out), 'seconds': round(self.seconds, 1), 'optional': self.optional}


@dataclass
class Estimate:
    calls: int
    tokens_in: int
    tokens_out: int
    seconds: int
    range: dict
    engine: str | None
    engine_label: str | None
    billing: str | None
    dollars: list | None
    keyless: bool
    long_file: bool
    needs_confirmation: bool
    reasons: list[dict]
    breakdown: list[dict]
    deadline_s: float
    cheaper: list[dict]
    summary: str
    version: int = VERSION

    def to_dict(self) -> dict:
        """Exactly the TS Estimate (web/src/protocol.ts)."""
        return {'version': self.version, 'calls': self.calls, 'tokens_in': self.tokens_in, 'tokens_out': self.tokens_out,
                'seconds': self.seconds, 'range': {k: list(v) for k, v in self.range.items()}, 'engine': self.engine,
                'engine_label': self.engine_label, 'billing': self.billing,
                'dollars': list(self.dollars) if self.dollars is not None else None, 'keyless': self.keyless,
                'long_file': self.long_file, 'needs_confirmation': self.needs_confirmation,
                'reasons': [dict(r) for r in self.reasons], 'breakdown': [dict(b) for b in self.breakdown],
                'deadline_s': self.deadline_s, 'cheaper': [dict(c) for c in self.cheaper], 'summary': self.summary}


# ---------- pricing one call ----------


def cost_of(engine: EngineView | None) -> EngineCost:
    return ENGINES.get(engine.name if engine else '', ENGINES['default'])


def per_call_s(engine: EngineView) -> float:
    c = cost_of(engine)
    if engine.p50_ms:
        return max(c.per_call_s / 2, engine.p50_ms / 1000)
    return c.per_call_s


def price(phase: str, engine: EngineView, *, prompt: float, ctx: float = 0.0, schema: float = 0.0,
          base_out: float = ANSWER_OUT, structured: bool = True, planner: bool = False, attached: bool = False,
          calls: int = 1, parallel: int = 1, optional: bool = False, in_low: bool = True) -> Call:
    """`calls` calls of one kind. `parallel` of them run side by side (section batches on an API engine)."""
    c = cost_of(engine)
    turns = c.planner_turns if planner else c.turns if structured else 1
    turns += c.attach_turns if attached else 0
    tin = turns * (c.fixed_in + prompt + ctx + schema)
    tout = base_out * (c.out_mult if structured else 1.0)
    one_s = per_call_s(engine) * turns / max(c.turns, 1) + tout / 1000 * c.per_kout_s
    waves = math.ceil(calls / max(1, parallel))
    return Call(phase, engine, calls, tin * calls, tout * calls, one_s * waves, optional, in_low)


def research_call(engine: EngineView) -> Call:
    tin, tout, s = RESEARCH.get(engine.name, RESEARCH['default'])
    return Call('research', engine, 1, tin, tout, s)


# ---------- reading the draft (the same keyless code the run uses) ----------


def is_design_file(meta: dict, request: str) -> bool:
    """A design system the request asks to apply (create/design.split_docs): priced at 0 tokens, since it is applied by
    code and never sent to a model."""
    try:
        from .create import design
        split = design.split_docs
    except (ImportError, AttributeError):
        return bool(DESIGN_NAME.search(str(meta.get('name') or '')) and DESIGN_ASK.search(request))
    try:
        return bool(split([(meta, str(meta.get('text') or ''))], request)[0])
    except Exception:
        return False


def _brief(text: str):
    from .create.brief import parse_brief
    try:
        return parse_brief(text)
    except Exception:
        from .create.brief import Brief
        return Brief()


def _asks_more(brief) -> bool:
    from .create.brief import asks_more
    return asks_more(brief)


def plan_calls(fmt: str, brief):
    """The long writer's own call plan (longdoc.plan_calls), so the estimate cannot drift from what it will do. Until
    that helper exists, the same arithmetic as write_long."""
    from .create import longdoc
    fn = getattr(longdoc, 'plan_calls', None)
    if fn is not None:
        return fn(fmt, brief)
    words, sections = longdoc.budget(fmt, brief)
    slides = fmt == 'pptx'
    batches = max(1, math.ceil((sections / 10) if slides else words / longdoc.BATCH_WORDS))
    token_budget = min(32000, int(1.6 * words * 1.4)) if not slides else min(32000, sections * 350)

    @dataclass
    class _Plan:
        sections: int
        words: int
        batches: int
        max_calls: int
        token_budget: int
    return _Plan(sections, words, batches, 2 + batches + 1 + repair_calls(), token_budget)


def repair_calls() -> int:
    from .create import longdoc
    return int(getattr(longdoc, 'MAX_REPAIR_CALLS', MAX_REPAIR_CALLS))


def long_context_chars() -> int:
    from .create import longdoc
    return int(getattr(longdoc, 'LONG_CONTEXT_CHARS', LONG_CONTEXT_CHARS))


def wants_file(draft: Draft, docs: list, tables: list) -> bool:
    from .agents.create import asks_for_file, has_topic
    from .create.spec import detect_format
    q = draft.query
    return (draft.agent == 'create' or asks_for_file(q) or (detect_format(q) is not None and has_topic(q)) or
            bool(FILE_WORD.search(q) and (docs or tables or has_topic(q) or draft.has_answer)))


def create_path(draft: Draft, brief, fmt: str | None, deps: bool, docs: list, tables: list) -> str:
    """'zero' (a file made from what the chat already has, 0 tokens), 'long' (the long writer) or 'single' (one spec
    call), in the order agents/create.make() takes them."""
    from .agents.create import CONVERT, FILE_REF, GENERATIVE, LEAD_FORMAT, has_topic, refers_back
    q = draft.query.strip()
    last = draft.last_file
    if last and not deps and not docs and not tables:
        other = fmt is not None and fmt != last.get('format') and not has_topic(q)
        names = FILE_REF.search(q) and (CONVERT.search(q) or re.search(r'\b(?:into|to|as)\b', q, re.I))
        if LEAD_FORMAT.match(q) or CONVERT.search(q) or refers_back(q) or other or names:
            return 'zero'
    if deps:
        return 'long' if _asks_more(brief) else 'zero'
    if tables and not GENERATIVE.search(q):
        return 'zero'
    if draft.has_answer and not docs and not tables and (refers_back(q) or not has_topic(q)):
        return 'zero'
    return 'long' if _asks_more(brief) else 'single'


def planner_runs(draft: Draft, engine: EngineView | None, context: bool) -> bool:
    """Whether the LLM planner will be asked (planner.plan): never keyless or quick; research and deep plan with it
    whenever the query may hold several requests; otherwise only when the heuristic plan can't settle it."""
    from .planner import needs_llm_plan, worth_llm_plan
    if engine is None or draft.mode == 'quick':
        return False
    ctx = [{'query': '', 'answer': ''}] if context else None
    names = [str(f.get('name') or '') for f in draft.files] or None
    try:
        worth = worth_llm_plan(draft.query, ctx)
        needs = needs_llm_plan(draft.query, ctx, names)
    except Exception:
        return True
    if draft.mode == 'research':
        return True
    return worth and (draft.mode == 'deep' or needs)


def keyless_part(text: str) -> bool:
    from .gate import confirmed
    for agent in KEYLESS:
        try:
            if confirmed(agent, text):
                return True
        except Exception:
            continue
    return False


# ---------- the estimate ----------


def estimate(draft: Draft, engine: EngineView | None, web_engine: EngineView | None, *, deadline_s: float,
             alternatives: list[EngineView] = (), prices: dict | None = None,
             lean: list[EngineView] | None = None) -> Estimate:
    """The draft's cost on `engine` (the lead of Auto, or the pinned engine), with any web search on `web_engine`.
    `lean` is Auto's chain when the run would send a long file to its cheapest healthy backend (H8,
    Router.lean_engine): the file's calls are then priced on the engine `cheapest` picks from it, as the run does.
    Starts nothing and calls no one."""
    q = draft.query.strip()
    design = [f for f in draft.files if is_design_file(f, q)]
    content = [f for f in draft.files if f not in design]
    tables = [f for f in content if f.get('columns')]
    docs = [f for f in content if not f.get('columns')]
    reasons: list[dict] = []
    if engine is None:
        return finish([], draft, None, deadline_s, reasons + [reason('keyless')], False, prices, design=bool(design))
    web = web_engine if web_engine is not None and web_engine.web else (engine if engine.web else None)
    brief = _brief(q)
    from .create.spec import detect_format
    fmt = detect_format(q) or brief.format
    prompt = 300 + len(q) / CHARS_PER_TOKEN
    doc_chars = sum(int(f.get('chars') or 0) for f in docs) + 1500 * len(tables)
    turn_chars = TURN_CHARS if draft.has_answer else 0
    attached = bool(docs or tables)
    context = draft.has_answer or draft.last_file is not None
    calls: list[Call] = []
    planner = planner_runs(draft, engine, context)
    if planner:
        calls.append(price('planner', engine, prompt=prompt + PLANNER_EXTRA, ctx=turn_chars / CHARS_PER_TOKEN,
                           base_out=PLANNER_OUT, planner=True, in_low=False))
        reasons.append(reason('planner'))
    steps: list[str] = []   # 'research' | 'answer' | 'create' | 'keyless'
    long_file, file_fmt = False, None
    if wants_file(draft, docs, tables):
        path = create_path(draft, brief, fmt, False, docs, tables)
        topic = None
        if draft.mode == 'research' and web is not None:
            topic = 'research'
        elif planner and path != 'zero':
            topic = 'research' if web is not None and RESEARCHY.search(q) else 'answer'
        dep_chars = 0
        if topic == 'research':
            calls.append(research_call(web))
            dep_chars = RESEARCH.get(web.name, RESEARCH['default'])[1] * CHARS_PER_TOKEN
            reasons.append(reason('research', label=web.label))
        elif topic == 'answer':
            calls.append(price('answer', engine, prompt=prompt, ctx=min(doc_chars, LONG_CONTEXT_CHARS) / CHARS_PER_TOKEN,
                               base_out=ANSWER_OUT, structured=False, attached=attached))
            dep_chars = ANSWER_OUT * CHARS_PER_TOKEN
        if topic:
            steps.append(topic)
            path = create_path(draft, brief, fmt, True, docs, tables)
        steps.append('create')
        file_fmt = fmt or ('pptx' if brief.slides else 'pdf' if path == 'long' else 'md')
        ctx_chars = dep_chars + doc_chars + turn_chars
        if path == 'long':
            long_file = True
            writer = engine
            if lean:
                pick = cheapest(lean, q, brief, fmt=fmt or brief.format, ctx_chars=ctx_chars, attached=attached)
                writer = next((v for v in lean if v is not None and v.name == pick), engine)
            calls += long_calls(writer, file_fmt, brief, prompt, min(ctx_chars, long_context_chars()), attached)
            plan = plan_calls(file_fmt, brief)
            reasons.append(reason('long_file', batches=plan.batches, what=WHAT.get(file_fmt, 'file')))
            reasons.append(reason('repair', n=repair_calls()))
        elif path == 'single':
            one = dict(prompt=prompt, ctx=min(ctx_chars, CREATE_CONTEXT_CHARS) / CHARS_PER_TOKEN,
                       schema=DOCSPEC_SCHEMA_TOKENS, base_out=SINGLE_OUT.get(file_fmt, 500), attached=attached)
            calls.append(price('sections', engine, **one))
            calls.append(price('repair', engine, **one, optional=True))
        if path != 'zero' and brief.images:  # pictures are searched for and fetched by code: time, no tokens
            first = next(c for c in calls if c.phase == 'sections' and not c.optional)
            first.seconds += IMAGES_S
            reasons.append(reason('images'))
    else:
        from .planner import candidate_split
        if draft.mode == 'research' and web is not None:
            calls.append(research_call(web))
            steps.append('research')
            reasons.append(reason('research', label=web.label))
        else:
            parts = candidate_split(q) if planner else [q]
            for part in parts:
                if draft.agent is None and keyless_part(part):
                    steps.append('keyless')
                    continue
                calls.append(price('answer', engine, prompt=300 + len(part) / CHARS_PER_TOKEN,
                                   ctx=(min(doc_chars, LONG_CONTEXT_CHARS) + turn_chars) / CHARS_PER_TOKEN,
                                   base_out=ANSWER_OUT, structured=False, attached=attached))
                steps.append('answer')
    if len(steps) >= 2 and any(s in ('research', 'answer') for s in steps):
        answers = sum(RESEARCH['default'][1] if s == 'research' else ANSWER_OUT if s == 'answer' else REPLY_OUT
                      for s in steps)
        calls.append(price('merge', engine, prompt=prompt, ctx=answers, base_out=MERGE_OUT, structured=False))
        reasons.append(reason('merge'))
    if attached and cost_of(engine).attach_turns and any(c.calls for c in calls):
        reasons.append(reason('attachments', label=engine.label))
    if design:
        reasons.append(reason('design', name=design[0].get('name') or 'the design file'))
    if cost_of(engine).fixed_in >= 10_000 and any(c.calls and c.engine is engine for c in calls):
        c = cost_of(engine)
        reasons.append(reason('engine_overhead', label=engine.label, fixed=c.fixed_in, turns=c.turns))
    est = finish(calls, draft, engine, deadline_s, reasons, long_file, prices, what=file_fmt, design=bool(design))
    if alternatives and est.calls:
        est.cheaper = cheaper(draft, est, engine, alternatives, deadline_s, research='research' in steps)
    return est


def long_calls(engine: EngineView, fmt: str, brief, prompt: float, ctx_chars: float, attached: bool) -> list[Call]:
    """The long writer's calls: the outline, the section batches (one at a time on a subscription, two at a time on an
    API engine), then the ones that may run: a top-up and the repair calls."""
    plan = plan_calls(fmt, brief)
    ctx = ctx_chars / CHARS_PER_TOKEN
    batches = max(1, int(plan.batches))
    per_words = max(1, int(plan.words)) / batches
    section_out = per_words * 1.4 * 1.3 + 600
    parallel = 2 if engine.billing == 'api' else 1
    kw = dict(prompt=prompt, ctx=ctx, attached=attached)
    return [price('outline', engine, **kw, schema=OUTLINE_SCHEMA_TOKENS, base_out=OUTLINE_OUT),
            price('sections', engine, **kw, schema=DOCSPEC_SCHEMA_TOKENS, base_out=section_out, calls=batches,
                  parallel=parallel),
            price('topup', engine, **kw, schema=DOCSPEC_SCHEMA_TOKENS, base_out=TOPUP_OUT, optional=True),
            price('repair', engine, **kw, schema=DOCSPEC_SCHEMA_TOKENS, base_out=section_out, calls=repair_calls(),
                  optional=True)]


def finish(calls: list[Call], draft: Draft | None, engine: EngineView | None, deadline_s: float, reasons: list[dict],
           long_file: bool, prices: dict | None, *, what: str | None = None, design: bool = False,
           kind: str | None = None) -> Estimate:
    """Totals, range, reasons and the summary of priced calls."""
    sure = [c for c in calls if not c.optional]
    low = [c for c in sure if c.in_low]
    mid_calls = sum(c.calls for c in sure)
    tin, tout, secs = (sum(c.tokens_in for c in sure), sum(c.tokens_out for c in sure), sum(c.seconds for c in sure))
    maybe = [c for c in calls if c.optional]

    def high(key: str, mult: float) -> int:
        # the planned calls widened; a call that may run (a top-up or repair runs at most once, on a shortfall) at its
        # own mid value
        return round(sum(getattr(c, key) for c in sure) * mult + sum(getattr(c, key) for c in maybe))
    rng = {'calls': (sum(c.calls for c in low), sum(c.calls for c in calls)),
           'tokens_in': (round(sum(c.tokens_in for c in low) * LOW_IN), high('tokens_in', HIGH_IN)),
           'tokens_out': (round(sum(c.tokens_out for c in low) * LOW_OUT), high('tokens_out', HIGH_OUT)),
           'seconds': (round(sum(c.seconds for c in low) * LOW_S), high('seconds', HIGH_S))}
    keyless = mid_calls == 0 and not any(c.calls for c in calls)
    by_engine: dict[str, int] = {}
    views: dict[str, EngineView] = {}
    for c in calls:
        if c.calls:
            by_engine[c.engine.name] = by_engine.get(c.engine.name, 0) + c.calls
            views[c.engine.name] = c.engine
    main = views[max(by_engine, key=by_engine.get)] if by_engine else engine
    billing = main.billing if main is not None and not keyless else None
    dollars = None
    if billing == 'api':
        p = prices or {}
        cin, cout = float(p.get('claude_in', 5.0)), float(p.get('claude_out', 25.0))
        dollars = [round(rng['tokens_in'][0] * cin / 1e6 + rng['tokens_out'][0] * cout / 1e6, 4),
                   round(rng['tokens_in'][1] * cin / 1e6 + rng['tokens_out'][1] * cout / 1e6, 4)]
    if keyless and not any(r['code'] == 'keyless' for r in reasons):
        reasons = [*reasons, reason('keyless')]
    est = Estimate(calls=mid_calls, tokens_in=round(tin), tokens_out=round(tout), seconds=round(secs), range=rng,
                   engine=main.name if main is not None and not keyless else None,
                   engine_label=main.label if main is not None and not keyless else None, billing=billing,
                   dollars=dollars, keyless=keyless, long_file=long_file, needs_confirmation=False, reasons=[],
                   breakdown=[c.to_dict() for c in calls], deadline_s=float(deadline_s), cheaper=[], summary='')
    if not keyless and deadline_s and rng['seconds'][1] >= COST_DEADLINE_SHARE * deadline_s:
        reasons = [*reasons, reason('deadline', minutes=round(deadline_s / 60))]
    est.reasons = list({r['code']: r for r in reasons}.values())
    est.needs_confirmation = needs_confirmation(est)
    est.summary = summary(est, [views[n].label for n in by_engine], what, kind)
    return est


def needs_confirmation(est: Estimate) -> bool:
    """5.3: not keyless, and any of: high tokens at or above the engine's threshold, mid calls at or above
    COST_CONFIRM_CALLS, mid seconds at or above COST_CONFIRM_SECONDS, high seconds at or above COST_DEADLINE_SHARE of the
    deadline."""
    if est.keyless or not est.calls:
        return False
    limit = COST_CONFIRM_TOKENS.get(est.engine or 'default', COST_CONFIRM_TOKENS['default'])
    high_tokens = est.range['tokens_in'][1] + est.range['tokens_out'][1]
    return (high_tokens >= limit or est.calls >= COST_CONFIRM_CALLS or est.seconds >= COST_CONFIRM_SECONDS or
            bool(est.deadline_s and est.range['seconds'][1] >= COST_DEADLINE_SHARE * est.deadline_s))


def cheaper(draft: Draft, est: Estimate, engine: EngineView, alternatives, deadline_s: float, *,
            research: bool) -> list[dict]:
    """Healthy engines that can take every step (web search for research) and would use at most 0.6 of the tokens,
    best first, at most 2."""
    mine = est.tokens_in + est.tokens_out
    out = []
    for alt in alternatives:
        if alt.name == engine.name or not alt.healthy or (research and not alt.web):
            continue
        other = estimate(draft, alt, alt if alt.web else None, deadline_s=deadline_s)
        theirs = other.tokens_in + other.tokens_out
        if mine and other.calls and theirs <= 0.6 * mine:
            out.append({'engine': alt.name, 'label': alt.label, 'calls': other.calls, 'tokens_in': other.tokens_in,
                        'tokens_out': other.tokens_out, 'seconds': other.seconds,
                        'saves': round(1 - theirs / mine, 3)})
    return sorted(out, key=lambda c: c['tokens_in'] + c['tokens_out'])[:2]


def create_tokens(engine: EngineView, request: str, brief, fmt: str | None = None, ctx_chars: float = 0,
                  attached: bool = False) -> float:
    """Mid tokens (in plus out) of one create step on this engine: the long writer's planned calls, or one spec call."""
    prompt = 300 + len(request) / CHARS_PER_TOKEN
    fmt = fmt or ('pptx' if getattr(brief, 'slides', None) else 'pdf')
    if _asks_more(brief):
        calls = long_calls(engine, fmt, brief, prompt, min(ctx_chars, long_context_chars()), attached)
    else:
        calls = [price('sections', engine, prompt=prompt, ctx=min(ctx_chars, CREATE_CONTEXT_CHARS) / CHARS_PER_TOKEN,
                       schema=DOCSPEC_SCHEMA_TOKENS, base_out=SINGLE_OUT.get(fmt, 500), attached=attached)]
    return sum(c.tokens_in + c.tokens_out for c in calls if not c.optional)


def cheapest(views: list[EngineView], request: str, brief, *, fmt: str | None = None, ctx_chars: float = 0,
             attached: bool = False) -> str | None:
    """H8: of these engines (in Auto's order), the healthy one whose create step costs the fewest tokens; ties keep
    Auto's order."""
    best, best_tokens = None, None
    for v in views:
        if v is None or not v.healthy:
            continue
        tokens = create_tokens(v, request, brief, fmt, ctx_chars, attached)
        if best_tokens is None or tokens < best_tokens:
            best, best_tokens = v.name, tokens
    return best


# ---------- resume, several engines, what was used ----------


def _state_brief(state: dict):
    from .create import brief as brief_mod
    try:
        got = brief_mod.from_dict(state.get('brief'))
    except Exception:
        got = None
    return got if got is not None else _brief(str(state.get('request') or ''))


def for_resume(state: dict, engine: EngineView | None, *, deadline_s: float, lean: list[EngineView] | None = None,
               prices: dict | None = None) -> Estimate:
    """What writing only the missing parts of a checkpoint costs. A single-call state with its reply kept is rebuilt
    at 0 tokens; a long-document state whose outline never came back is a whole write (outline, every batch, the
    repairs). `lean` is Auto's chain: the long writer then runs on the cheapest healthy engine, as the resume run does
    (Router.lean_engine), and is priced there."""
    state = state if isinstance(state, dict) else {}
    fmt = str(state.get('format') or 'pdf')
    reasons = [reason('resume')]
    if engine is None:
        return finish([], None, None, deadline_s, reasons + [reason('keyless')], False, prices, what=fmt,
                      kind='resume')
    request = str(state.get('request') or '')
    prompt = 300 + len(request) / CHARS_PER_TOKEN
    ctx = len(str(state.get('ctx') or '')) / CHARS_PER_TOKEN
    calls: list[Call] = []
    if state.get('kind') == 'single':
        if not state.get('reply'):
            one = dict(prompt=prompt, ctx=ctx, schema=DOCSPEC_SCHEMA_TOKENS, base_out=SINGLE_OUT.get(fmt, 500))
            calls += [price('sections', engine, **one), price('repair', engine, **one, optional=True)]
        return finish(calls, None, engine, deadline_s, reasons, False, prices, what=fmt, kind='resume')
    brief = _state_brief(state)
    if lean and _asks_more(brief):  # as Router.lean_engine: only a long file goes to the cheapest engine
        pick = cheapest(lean, request, brief, fmt=fmt, ctx_chars=ctx * CHARS_PER_TOKEN)
        engine = next((v for v in lean if v is not None and v.name == pick), engine)
    parts = [p for p in state.get('parts') or [] if isinstance(p, dict)]
    if not parts and not state.get('file_id'):
        # the outline failed: resuming writes the whole file
        calls += long_calls(engine, fmt, brief, prompt, min(ctx * CHARS_PER_TOKEN, long_context_chars()), False)
        plan = plan_calls(fmt, brief)
        reasons += [reason('long_file', batches=plan.batches, what=WHAT.get(fmt, 'file')),
                    reason('repair', n=repair_calls())]
        return finish(calls, None, engine, deadline_s, reasons, True, prices, what=fmt, kind='resume')
    written = {str(k) for k in (state.get('written') or {})}
    failed = {int(i) for i in state.get('failed') or [] if isinstance(i, (int, str)) and str(i).lstrip('-').isdigit()}
    missing = [p for i, p in enumerate(parts) if str(i) not in written or i in failed]
    if missing:
        words = sum(int(p.get('words') or 0) for p in missing)
        batches = math.ceil(len(missing) / 10) if fmt == 'pptx' else max(1, math.ceil(words / BATCH_WORDS))
        section_out = max(1, words) / batches * 1.4 * 1.3 + 600
        kw = dict(prompt=prompt, ctx=ctx, schema=DOCSPEC_SCHEMA_TOKENS, base_out=section_out)
        parallel = 2 if engine.billing == 'api' else 1
        calls += [price('sections', engine, **kw, calls=batches, parallel=parallel),
                  price('repair', engine, **kw, calls=repair_calls(), optional=True)]
        reasons.append(reason('repair', n=repair_calls()))
    return finish(calls, None, engine, deadline_s, reasons, True, prices, what=fmt, kind='resume')


FILE_PHASES = ('outline', 'sections', 'topup', 'repair')


def file_share(est: dict | None) -> dict | None:
    """The part of a run's estimate (Estimate.to_dict) that one created file's own calls stand for: the outline,
    section, top-up and repair calls, with totals and range worked out as finish() does. A file card then compares
    like with like (the planner, research and merge calls belong to the run). The estimate itself when it has no
    file calls."""
    if not isinstance(est, dict) or not isinstance(est.get('breakdown'), list):
        return est
    rows = [r for r in est['breakdown'] if isinstance(r, dict) and r.get('phase') in FILE_PHASES]
    if not rows or len(rows) == len(est['breakdown']):
        return est
    sure = [r for r in rows if not r.get('optional')]
    maybe = [r for r in rows if r.get('optional')]
    tot = lambda rs, k: sum(float(r.get(k) or 0) for r in rs)  # noqa: E731
    rng = {'calls': (int(tot(sure, 'calls')), int(tot(rows, 'calls'))),
           'tokens_in': (round(tot(sure, 'tokens_in') * LOW_IN), round(tot(sure, 'tokens_in') * HIGH_IN +
                                                                        tot(maybe, 'tokens_in'))),
           'tokens_out': (round(tot(sure, 'tokens_out') * LOW_OUT), round(tot(sure, 'tokens_out') * HIGH_OUT +
                                                                          tot(maybe, 'tokens_out'))),
           'seconds': (round(tot(sure, 'seconds') * LOW_S), round(tot(sure, 'seconds') * HIGH_S + tot(maybe, 'seconds')))}
    out = {**est, 'calls': int(tot(sure, 'calls')), 'tokens_in': round(tot(sure, 'tokens_in')),
           'tokens_out': round(tot(sure, 'tokens_out')), 'seconds': round(tot(sure, 'seconds')), 'range': rng,
           'breakdown': rows, 'cheaper': [], 'scope': 'file'}
    whole = (est.get('tokens_in') or 0) + (est.get('tokens_out') or 0)
    if est.get('dollars') and whole:
        share = (out['tokens_in'] + out['tokens_out']) / whole
        out['dollars'] = [round(float(d) * share, 4) for d in est['dollars']]
    calls = out['calls']
    out['summary'] = (f'The file itself was estimated at about {calls} model call{"" if calls == 1 else "s"}: roughly '
                      f'{two_figures(out["tokens_in"] + out["tokens_out"]):,} tokens.')
    return out


def combine(estimates: list[Estimate]) -> Estimate:
    """Several engines answering one ask side by side: calls and tokens add up, time is the slowest run's."""
    if not estimates:
        raise ValueError('nothing to combine')
    if len(estimates) == 1:
        return estimates[0]
    add = lambda k: sum(getattr(e, k) for e in estimates)
    rng = {k: (sum(e.range[k][0] for e in estimates), sum(e.range[k][1] for e in estimates))
           for k in ('calls', 'tokens_in', 'tokens_out')}
    rng['seconds'] = (max(e.range['seconds'][0] for e in estimates), max(e.range['seconds'][1] for e in estimates))
    engines = {e.engine for e in estimates if e.engine}
    labels = list(dict.fromkeys(e.engine_label for e in estimates if e.engine_label))
    billing = {e.billing for e in estimates if e.billing}
    dollars = None
    if any(e.dollars for e in estimates):
        dollars = [round(sum((e.dollars or [0, 0])[0] for e in estimates), 4),
                   round(sum((e.dollars or [0, 0])[1] for e in estimates), 4)]
    reasons = list({r['code']: r for e in estimates for r in e.reasons}.values())
    reasons.append(reason('several_engines', n=len(estimates)))
    one = len(engines) == 1
    out = Estimate(calls=add('calls'), tokens_in=add('tokens_in'), tokens_out=add('tokens_out'),
                   seconds=max(e.seconds for e in estimates), range=rng, engine=engines.pop() if one else None,
                   engine_label=labels[0] if one and labels else None,
                   billing=billing.pop() if len(billing) == 1 else None, dollars=dollars,
                   keyless=all(e.keyless for e in estimates), long_file=any(e.long_file for e in estimates),
                   needs_confirmation=False, reasons=reasons, breakdown=[b for e in estimates for b in e.breakdown],
                   deadline_s=max(e.deadline_s for e in estimates), cheaper=[], summary='')
    out.needs_confirmation = any(e.needs_confirmation for e in estimates) or (
        not out.keyless and out.calls >= COST_CONFIRM_CALLS)
    out.summary = summary(out, labels, None, 'several', n=len(estimates))
    return out


def actual_of(record: dict) -> dict:
    """CostActual of a finished run: calls from each create step's phases (1 for a create call without them), 1 per
    other LLM step, 1 for an LLM planner and 1 for an LLM merge; tokens from the run's tokens; seconds from total_ms."""
    calls = 0
    for t in record.get('tasks') or []:
        engine = t.get('engine')
        if t.get('agent') == 'create':
            phases = t.get('phases') or next((f.get('phases') for f in t.get('created_files') or [] if f.get('phases')),
                                            None)
            if phases:
                calls += sum(int(p.get('calls') or 0) for p in phases if isinstance(p, dict))
            elif engine not in (None, 'keyless') and 'answer' in t:
                calls += 1
        elif engine not in (None, 'keyless') and 'answer' in t and t.get('agent') not in ('blocked',):
            calls += 1
    timings = record.get('timings') or {}
    calls += (timings.get('planner') == 'llm') + (timings.get('merger') == 'llm')
    tokens = record.get('tokens') or {}
    return {'calls': int(calls), 'tokens_in': int(tokens.get('llm_in') or 0), 'tokens_out': int(tokens.get('llm_out') or 0),
            'seconds': round((record.get('total_ms') or 0) / 1000, 1)}


# ---------- words ----------

REASONS = {
    'keyless': 'No model is called: the answer comes from code.',
    'planner': 'A planning call splits the request into steps first.',
    'research': 'Research searches the web on {label} before anything is written.',
    'long_file': 'The {what} is written in parts: an outline, then {batches} batch{es} of sections.',
    'repair': 'If a part comes back unusable, up to {n} more call{s} rewrite only that part.',
    'merge': 'A final call joins the answers into one reply.',
    'engine_overhead': '{label} adds about {fixed:,} tokens of its own to every turn, and a structured reply takes '
                       'about {turns} turns.',
    'attachments': '{label} reads the attached files in an extra turn.',
    'images': 'Pictures are searched for and added, which takes about 15 more seconds.',
    'design': 'The design file {name} is applied by code and costs no tokens.',
    'deadline': 'This may come close to the {minutes} minute limit.',
    'several_engines': '{n} engines each answer the same question.',
    'resume': 'Only the missing parts are written; everything already written is reused.',
}


def reason(code: str, **kw) -> dict:
    kw.setdefault('es', 'es' if kw.get('batches', 1) != 1 else '')
    kw.setdefault('s', 's' if kw.get('n', 1) != 1 else '')
    try:
        text = REASONS[code].format(**kw)
    except (KeyError, IndexError, ValueError):
        text = REASONS.get(code, code)
    return {'code': code, 'text': text}


def two_figures(n: float) -> int:
    """Rounded to 2 significant figures: 243,700 -> 240,000."""
    n = round(n)
    if n < 100:
        return n
    digits = int(math.floor(math.log10(abs(n)))) - 1
    return int(round(n, -digits))


def duration(seconds: float) -> str:
    if seconds < 120:
        return f'about {max(1, round(seconds))} seconds'
    lo = max(1, math.floor(seconds / 60))
    hi = max(lo + 1, math.ceil(seconds * 1.5 / 60))
    return f'about {lo} to {hi} minutes'


def and_list(items: list[str]) -> str:
    items = [i for i in items if i]
    return items[0] if len(items) == 1 else ', '.join(items[:-1]) + ' and ' + items[-1] if items else ''


def summary(est: Estimate, labels: list[str], what: str | None, kind: str | None = None, n: int = 0) -> str:
    """One plain sentence for the dialog: "This deck needs about 6 model calls on Antigravity and Claude Code: roughly
    260,000 tokens and about 4 to 7 minutes." """
    if est.keyless:
        return 'This needs no model calls: it is made by code at 0 tokens.'
    subject = ('Resuming this file' if kind == 'resume' else f'These {n} answers' if kind == 'several' else
               f'This {WHAT[what]}' if what in WHAT else 'This run')
    verb = 'need' if kind == 'several' else 'needs'
    calls = f'{est.calls} model call{"" if est.calls == 1 else "s"}'
    on = f' on {and_list(labels)}' if labels else ''
    tokens = f'{two_figures(est.tokens_in + est.tokens_out):,}'
    return f'{subject} {verb} about {calls}{on}: roughly {tokens} tokens and {duration(est.seconds)}.'

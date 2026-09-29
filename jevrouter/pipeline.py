"""handle(query): plan -> (route -> run) per step, each as soon as its dependencies answered -> merge, emitting the SSE
protocol in PLAN.md and docs/PLAN-v4.md, with the stage timings, caches and chat modes of docs/PLAN-speed-evals-chat.md."""
import asyncio
import copy
import hashlib
import itertools
import os
import random
import re
import time
import uuid
from collections import deque
from pathlib import Path

from . import agents as agent_registry
from . import cache as cache_mod
from . import gate
from . import policy
from . import suspects as suspects_mod
from . import verify as verify_mod
from .agents import create as create_agent
from .agents.llm import COMMON
from .agents.tools import sql_agent, sql_in
from .config import (AGENTS, BLOCK_AT, DEEP_MIN_OK, DEP_CONTEXT_CHARS, EASY_AT, FORCED_MIN, GUARDS, HARD_AT, HISTORY,
                     KEYLESS, LONG_RUN_TIMEOUT, MAX_QUERY_CHARS, MAX_SUBTASKS, PRICES, REPORT, RESEARCH, RUN, RUN_TIMEOUT, SAMPLES, SQL_AGENT,
                     STRONGEST, env_flag)
from .create.brief import Brief, merge as brief_merge, parse_brief
from .engines.auto import Steered
from .engines.health import Health, instrument_all, pct
from .events import Broadcaster
from .files import FILE_AGENTS, file_agents, search, to_table
from .jev import clean, criteria, route_one, unsafe_score
from .merger import STYLES, StepOut, compose
from .planner import (SCHEMA as PLAN_SCHEMA, elliptical, expand_described, fill_from_frame, is_file_request,
                      kind as plan_kind, parse_steps, plan, resolve_step, system_for as plan_system)
from .sandbox import DEFAULT_TTL, MAX_SANDBOXES, TURNS as SANDBOX_TURNS, Sandboxes, SandboxError
from .store import Store

ROUTED_KEYS = ('agent', 'pick', 'reason', 'probabilities', 'confidence', 'urgency', 'unsafe', 'clear', 'jev_ms', 'model',
               'examples')
USE_ACTIVE = object()  # a run's engine argument: the engine active at submit time (None means keyless)
ROUTE_CHARS = 2000  # Jev reads at most this much of a step (B4); the planner and the agents get all of it
RESOLVE_CHARS = 1500  # of each earlier answer, for the dependent-step rewrite (it needs facts, not whole answers)
ATTACHED_CONTEXT_CHARS = 2000  # of an attached file, for a step that builds on a document or data step (B1)
FAILED_CONTEXT_CHARS = 200
MEANINGS_TIMEOUT = 3.0  # seconds the lone-term lookup (jevrouter/gate.py) may add after Jev has routed
# Sandbox runs are never stored. Their qids come from a separate range so they can't collide with
# (or leave gaps in) the persisted run numbers, and they're forgotten when they finish.
SANDBOX_QID0 = 1_000_000_000
SANDBOX_SESSIONS = MAX_SANDBOXES  # most sandbox conversations kept in memory (jevrouter/sandbox.py)
LABELS_CACHED = 5000  # newest labels kept in memory for route examples (each agent uses only its latest few)
EFFORT = {'quick': 'low', 'deep': 'high'}  # the effort every LLM agent call uses in these chat modes


async def warm_up(engine):
    """Starts the CLI processes a run is most likely to need, so the first query doesn't wait for a cold start."""
    if engine is None:
        return
    planner = dict(system=plan_system(getattr(engine, 'supports_web', False)), effort='low', schema=PLAN_SCHEMA)
    for call in [planner, *(dict(system=s, effort=e) for s, e in COMMON)]:
        try:
            await engine.prewarm(**call)
        except Exception:
            pass  # warming is only an optimisation


def ms_since(t: float) -> int:
    return round((time.perf_counter() - t) * 1000)


def pinned_plan(pinned: dict) -> dict:
    """A plan given with the run ({subtasks: [str], deps: [[int]]}, D3), replayed instead of calling the planner. It is
    checked like an LLM plan: blank steps dropped, at most MAX_SUBTASKS, dependencies only on earlier steps."""
    texts = pinned.get('subtasks') or []
    deps = pinned.get('deps') or [[] for _ in texts]
    raw = [{'text': t, 'depends_on': d} for t, d in zip(texts, [*deps, *([[]] * max(0, len(texts) - len(deps)))])]
    subtasks, deps = parse_steps(raw)
    if not subtasks:
        raise ValueError('the pinned plan has no steps')
    return {'planner': 'pinned', 'subtasks': subtasks, 'deps': deps, 'multi': None, 'jev_tokens': 0, 'claude_in': 0,
            'claude_out': 0}


def expand_steps(texts: list[str], deps: list[list[int]]) -> tuple[list[str], list[list[int]]]:
    """A5 with an engine: a step whose place or currency is named only by a description the static tables can't
    settle becomes a lookup step plus the step itself (planner.expand_described), while the plan has room."""
    out_texts, out_deps, where = [], [], {}
    for i, (text, dep) in enumerate(zip(texts, deps)):
        pieces = expand_described(text)
        if len(pieces) > 1 and len(out_texts) + len(pieces) + (len(texts) - i - 1) > MAX_SUBTASKS:
            pieces = [(text, [])]
        if len(pieces) > 1 and any(texts[d] == pieces[0][0] for d in dep if d < len(texts)):
            pieces = [(text, [])]  # already expanded (a plan pinned from a run): its lookup is one of its dependencies
        base = len(out_texts)
        for j, (piece, own) in enumerate(pieces):
            out_texts.append(piece)
            out_deps.append(sorted({base + k for k in own} | ({where[d] for d in dep} if j == len(pieces) - 1 else set())))
        where[i] = len(out_texts) - 1
    return out_texts, out_deps


ROUTE_DEP_CHARS = 500  # of each earlier answer Jev sees when it routes a dependent step (the agent gets DEP_CONTEXT_CHARS)


def route_context(ctx: str) -> str:
    """The dependency context as Jev routes on it: each earlier answer cut to ROUTE_DEP_CHARS."""
    if not ctx:
        return ''
    lines = ctx.split('\n')
    return '\n'.join(ln if len(ln) <= ROUTE_DEP_CHARS else ln[:ROUTE_DEP_CHARS].rstrip() + ' [...]' for ln in lines)


# the ambiguous_term trace note of a route-only run, which never looks a lone term's meanings up (evals.check reads it)
ROUTE_TERM = 'a lone term: its meanings are looked up only when the run answers'
# the lookup step expand_described writes before a request that names its place or currency by description
LOOKUP_STEP = re.compile(r'What is .+\? Give only the (?:place name|currency code)\.')


def allot(lengths: dict, budget: int) -> dict:
    """Shares of `budget` for texts of these lengths: each gets an equal share, and a short one's unused share goes to
    the longer ones."""
    shares, left = {}, max(0, budget)
    for n, (key, size) in enumerate(sorted(lengths.items(), key=lambda kv: kv[1])):
        give = min(size, left // (len(lengths) - n))
        shares[key], left = give, left - give
    return shares


def cut(text: str, limit: int) -> str:
    """The text within `limit` characters, cut at the last blank line or line end before it, and marked."""
    if len(text) <= limit:
        return text
    at = text.rfind('\n\n', 0, limit)
    at = at if at > limit // 2 else text.rfind('\n', 0, limit)
    at = at if at > limit // 2 else limit
    return text[:at].rstrip() + f'\n[truncated {len(text) - at:,} chars]'


def body_font() -> str | None:
    """The family of TRACEGRAPH_BODY_FONT (the font file created documents embed), or None when it isn't set."""
    path = os.environ.get('TRACEGRAPH_BODY_FONT', '').strip()
    if not path:
        return None
    try:
        from .create import fonts
        return fonts.env_family() or Path(path).stem
    except Exception:
        return Path(path).stem


CHECKPOINT_PHASES = ('outline', 'sections', 'topup', 'render', 'done')
CHECKPOINT_SAVE_S = 3.0  # a running file step's checkpoint is saved with the run at most this often (and per phase)


def engine_info(e) -> dict:
    """EngineInfo for hello and config: the engine's own info plus `vision`, whether it can read images (Studio's
    Polish and design critic, docs/PLAN-designer.md 9.7); False when the engine does not say."""
    return {**e.info(), 'vision': bool(getattr(e, 'supports_vision', False))}


def drop_design_workspaces(sid: str):
    """A forgotten sandbox takes its Studio design workspaces with it (docs/PLAN-designer.md 3.6). Never raises."""
    try:
        from .studio import workspace
        workspace.drop_sandbox(sid)
    except Exception:
        pass


def checkpoint_info(state: dict, qid: int, tid: str) -> dict:
    """The CheckpointInfo summary of a create step's saved state (web/src/protocol.ts): longdoc.info when the long writer
    provides it, else the same summary read from the state's documented shape (docs/PLAN-files-robust.md 3.3)."""
    try:
        from .create import longdoc
        info = getattr(longdoc, 'info', None)
        if info is not None:
            return info(state, qid, tid)
    except Exception:
        pass
    state = state if isinstance(state, dict) else {}
    kind = 'single' if state.get('kind') == 'single' else 'longdoc'
    file_id = state.get('file_id') or None
    if kind == 'single':
        planned, written, missing = 1, 1 if file_id else 0, []
        resumable = not file_id
    else:
        parts = [p for p in state.get('parts') or [] if isinstance(p, dict)]
        done = state.get('written') if isinstance(state.get('written'), dict) else {}
        failed = {str(i) for i in state.get('failed') or []}
        usable = {k for k, s in done.items() if k not in failed and isinstance(s, dict) and s.get('blocks')}
        missing = [str(p.get('heading') or f'Section {i + 1}') for i, p in enumerate(parts) if str(i) not in usable]
        planned, written = len(parts), len(parts) - len(missing)
        resumable = bool(missing) or bool(failed) or not file_id
    by = state.get('resumed_by') if isinstance(state.get('resumed_by'), dict) else None
    if by and by.get('file_id'):
        resumable = False
    phase = state.get('phase') if state.get('phase') in CHECKPOINT_PHASES else 'sections'
    out = {'qid': qid, 'tid': tid, 'kind': kind, 'format': state.get('format') or 'pdf', 'phase': phase,
           'planned': planned, 'written': written, 'missing': missing, 'tokens_in': int(state.get('tokens_in') or 0),
           'tokens_out': int(state.get('tokens_out') or 0), 'at': float(state.get('at') or 0), 'resumable': resumable,
           'file_id': file_id}
    if by and type(by.get('qid')) is int:
        out['resumed_by'] = by['qid']
    return out


def public_run(rec: dict | None) -> dict | None:
    """A run as the API and the event stream show it: the full checkpoint state (H2) stays on the server, and only its
    summaries (`checkpoints`) go out."""
    if not isinstance(rec, dict) or 'checkpoints_state' not in rec:
        return rec
    out = {k: v for k, v in rec.items() if k != 'checkpoints_state'}
    out['checkpoints'] = [checkpoint_info(s, rec.get('qid'), tid) for tid, s in (rec['checkpoints_state'] or {}).items()]
    return out


def table_files(attached: list[dict], engine, query: str) -> list[dict]:
    """The attached files the sql agent can query: tables, when an engine can write the SQL or (keyless) the user
    wrote a SELECT themselves."""
    return [f for f in attached if f.get('columns')] if engine is not None or sql_in(query) else []


class Router:
    def __init__(self, jev, http=None, engine=None, bus: Broadcaster | None = None, registry: dict | None = None,
                 engines: dict | None = None, store: Store | None = None):
        self.jev, self.http = jev, http
        self.bus = bus or Broadcaster()
        self.store = store or Store()
        self.engines = engines or ({engine.name: engine} if engine is not None else {})
        self.health = Health()  # per-engine counters since start (GET /api/engines/health)
        instrument_all(self.engines, self.health)
        if engine is not None and engine.name not in self.engines:
            instrument_all({engine.name: engine}, self.health)
        self.fixed_registry = registry  # tests pin agents; real runs rebuild the registry when the engine changes
        self.route_cache = cache_mod.RouteCache()  # Jev decisions per (subtask text, route criteria), jevrouter/cache.py
        self.multi_cache = cache_mod.TTLCache(cache_mod.ROUTE_MAX, cache_mod.ROUTE_TTL)  # Jev's multi score per query
        self.customs = self.store.agents()
        self.use_engine(engine)
        # qids keep counting across restarts, and the last finished runs replay in `hello`
        self.ids = itertools.count(self.store.max_qid() + 1)
        self.history: deque = deque(self.store.recent_finished(HISTORY), maxlen=HISTORY)
        self.inflight: dict[int, dict] = {}  # qid -> partial record, so a browser joining mid-run can replay it
        self.running: dict[int, asyncio.Task] = {}  # qid -> task, for cancel
        self.state = {'autopilot': False, 'interval': 3.0}
        self.stats = {'queries': 0, 'subtasks': 0, 'errors': 0, 'jev_input_tokens': 0, 'claude_input_tokens': 0,
                      'claude_output_tokens': 0,
                      'by_agent': {a: 0 for a in [*AGENTS, *RESEARCH, *REPORT, *RUN, *FILE_AGENTS, *SQL_AGENT, *GUARDS]}}
        self.tasks: set[asyncio.Task] = set()
        self.evals: dict[str, asyncio.Task] = {}  # eval_id -> running eval (jevrouter/evals.py)
        self.run_timeout = float(os.environ.get('TG_RUN_TIMEOUT', RUN_TIMEOUT))
        # A long document (a 12-page PDF: research, an outline, section batches on a CLI that runs two at a time) takes
        # minutes by design; it gets its own deadline so the plain-question one stays tight.
        self.long_run_timeout = float(os.environ.get('TG_LONG_RUN_TIMEOUT', LONG_RUN_TIMEOUT))
        # Sandbox: qid -> sandbox id for runs in flight, their scratch stats, and each sandbox's memory (never on disk).
        self.sandbox: dict[int, str] = {}
        self.sandbox_stats: dict[int, dict] = {}
        self.sandboxes = Sandboxes()
        self.sandbox_ids = itertools.count(SANDBOX_QID0)
        self.sandbox_ttl = float(os.environ.get('TG_SANDBOX_TTL', DEFAULT_TTL))
        # Route examples (docs/PLAN-learning.md): the global switch, and the labels Jev's criteria are built from.
        self.route_examples = env_flag('TG_ROUTE_EXAMPLES')
        self.labels: list[dict] = []
        self.refresh_labels()

    def refresh_labels(self):
        """Reloads the labels cache after a label is saved or deleted, so the next route sees the change."""
        self.labels = self.store.list_labels(limit=LABELS_CACHED)
        self.route_cache.clear()

    def set_route_examples(self, on: bool):
        self.route_examples = on
        self.route_cache.clear()

    def criteria(self, agents: dict) -> dict:
        """The route criteria with examples from the cached labels, memoised until the labels or the agents change."""
        labels, key = self.labels, tuple(agents.items())
        cached = getattr(self, '_criteria', None)
        if cached is None or cached[0] is not labels or cached[1] != key:  # refresh_labels swaps in a new list
            cached = self._criteria = (labels, key, criteria(agents, labels))
        return cached[2]

    def criteria_without(self, agents: dict, texts) -> dict:
        """criteria() minus every label whose text is one of `texts` (compared as Jev sees examples). An eval holds out
        its own case this way, so examples on measures whether routing generalises, not recall of the answer."""
        drop = {clean(t).casefold() for t in texts if t}
        return criteria(agents, [l for l in self.labels if clean(l.get('text')).casefold() not in drop])

    # ---------- agents and engines ----------

    def offered(self, engine, with_files: bool = False, tables: bool = False) -> dict:
        """The route Choice's criteria for a run on this engine: engine-only agents appear only when they can run, and
        the sql agent only when an attached file is a table."""
        a = dict(AGENTS)
        if engine is not None:
            a.update(RESEARCH if engine.supports_web else {})
            a.update(REPORT)
            a.update(RUN if getattr(engine, 'supports_exec', False) else {})
            a.update({c['name']: c['description'] for c in self.customs})
        return {**a, **(FILE_AGENTS if with_files else {}), **(SQL_AGENT if with_files and tables else {})}

    def registry_for(self, engine) -> dict:
        if self.fixed_registry is not None:
            return {**agent_registry.extras(self.http, engine, self.customs), **self.fixed_registry}
        return agent_registry.build(self.http, engine, self.customs)

    def run_registry(self, tuned, prefer_keyless: bool = False) -> dict:
        """One run's agents: LLM agents on `tuned` (the engine at the run's effort and style, or None for keyless).
        prefer_keyless (quick mode): agents that have a keyless version use it. Agents pinned by tests stay as they are."""
        if self.fixed_registry is not None:
            return {**agent_registry.extras(self.http, tuned, self.customs), **self.fixed_registry}
        return agent_registry.build(self.http, tuned, self.customs, prefer_keyless=prefer_keyless)

    def use_engine(self, engine):
        """Switch the LLM backend (or None for keyless). Runs already in flight keep the engine they started with."""
        self.engine = engine
        self.agents = self.offered(engine)
        # Wrapped with no effort or style of its own, so the effort each agent call used is still recorded.
        self.registry = self.registry_for(agent_registry.Tuned(engine) if engine is not None else None)
        self.route_cache.clear()  # the agents offered to Jev may have changed

    def healthy(self, e, now: float | None = None) -> bool:
        """Available, not cooling down after a failure (in any Auto) or blocked, and most of its recent calls succeeded."""
        now = time.monotonic() if now is None else now
        if e is None or not e.available()[0] or (getattr(e, '_blocked', None) or (0, ''))[0] > now:
            return False
        if any((getattr(a, 'cooling', None) or {}).get(e.name, (0, ''))[0] > now for a in self.engines.values()):
            return False
        h = self.health.stats.get(e.name)
        return not (h and h['calls'] >= 4 and h['ok'] / h['calls'] < DEEP_MIN_OK)

    def backends(self) -> list:
        """The real engines (not Auto), in Auto's order when there is an Auto."""
        auto = self.engines.get('auto')
        names = list(getattr(auto, 'order', None) or []) + [n for n in self.engines if n != 'auto']
        return [self.engines[n] for n in dict.fromkeys(names) if n in self.engines and n != 'auto']

    def deep_engine(self):
        """Deep mode's engine when none is named: the engine the user selected, strictly; only when that is Auto (or
        keyless) the strongest healthy one (config.STRONGEST), else the active one."""
        if self.engine is not None and self.engine.name != 'auto':
            return self.engine
        now = time.monotonic()
        return next((self.engines[n] for n in STRONGEST if n in self.engines and self.healthy(self.engines[n], now)),
                    self.engine)

    def fastest(self, exclude=()):
        """The healthy engine with the lowest p50 so far (the engines' order when none has latencies yet), or None."""
        now = time.monotonic()
        ok = [e for e in self.backends() if e.name not in exclude and self.healthy(e, now)]
        p50 = {e.name: pct(sorted(h['ms']), 0.5) for e in ok if (h := self.health.stats.get(e.name)) and len(h['ms']) >= 3}
        timed = [e for e in ok if e.name in p50]
        return min(timed, key=lambda e: p50[e.name]) if timed else (ok[0] if ok else None)

    def step_engine(self, run_engine, hard: float | None, agent: str):
        """(engine, effort) for one LLM step by Jev's hard score (A5), or None to keep the run's engine as it is. An easy
        step goes to the fastest healthy engine at effort low, a hard one to the strongest at effort high. The choice
        goes through Auto when there is one, so a failure still falls through to the next engine."""
        if hard is None or EASY_AT < hard < HARD_AT:
            return None
        easy = hard <= EASY_AT
        needs = {'web': agent == 'research', 'exec': agent == 'run'}
        auto = self.engines.get('auto')
        pick = None
        if hasattr(auto, 'fastest'):
            pick = auto.fastest(**needs) if easy else auto.strongest(STRONGEST, **needs)
        if pick is None:
            pick = self.fastest() if easy else self.deep_engine()
        if pick is None:
            return None
        engine = Steered(auto, pick.name) if hasattr(auto, 'fastest') and pick.name in auto.engines else pick
        return engine, 'low' if easy else 'high'

    def engine_view(self, e):
        """What the cost estimate knows of an engine (estimate.EngineView): its billing, web search, health and p50."""
        from .estimate import EngineView
        if e is None:
            return None
        h = self.health.stats.get(e.name)
        ms = sorted(h['ms']) if h else []
        return EngineView(e.name, getattr(e, 'label', e.name), getattr(e, 'billing', 'api') or 'api',
                          bool(getattr(e, 'supports_web', False)), self.healthy(e), pct(ms, 0.5) if len(ms) >= 3 else None)

    def lean_engine(self, engine, brief, request: str, attached: list[dict], deps: list) -> str | None:
        """H8 (docs/PLAN-files-robust.md 5.5): on Auto, a long file goes first to the healthy backend whose estimate for
        it is lowest (Antigravity adds about 12,400 tokens of its own to every turn). None keeps Auto's own order: the
        switch is off (TG_LEAN_LONG_FILES=0), the engine is not Auto, or the file is not long."""
        from .config import lean_long_files_on
        if not lean_long_files_on() or getattr(engine, 'name', None) != 'auto' or not hasattr(engine, 'chain'):
            return None
        try:
            from .create.brief import asks_more
            if not asks_more(brief):
                return None
            from . import estimate as est
            from .create.spec import detect_format
            views = [self.engine_view(e) for e in engine.chain()]
            ctx = sum(len(a or '') for _, a in deps) + sum(int(f.get('chars') or 0) for f in attached)
            return est.cheapest(views, request, brief, fmt=detect_format(request) or brief.format, ctx_chars=ctx,
                                attached=bool(attached))
        except Exception:
            return None

    def design_history(self, rec: dict, sandbox: str | None, attached: list[dict], texts: dict, query: str,
                       qid: int) -> list[tuple[dict, str]]:
        """H5: the request asks to use a design file ("use the design.md") but none is attached to this message: the
        design-like files attached to earlier turns of the same chat, newest first, at most 2."""
        try:
            from .create import design
            mention, is_design = design.MENTION, design.is_design
        except (ImportError, AttributeError):
            return []
        try:
            if not mention.search(query) or any(is_design(f, texts.get(f['id']) or '', query) >= 0.5 for f in attached):
                return []
            seen, out = {f['id'] for f in attached}, []
            if sandbox:
                mem = self.sandboxes.peek(sandbox)
                held = list((mem.files if mem else {}).values())[::-1]
                candidates = [(m, t) for m, t in held if m.get('id') not in seen]
            else:
                sid = rec.get('session_id')
                recs = self.store.session_records(sid, qid, create_agent.LOOKBACK) if sid else []
                ids = [f for r in reversed(recs) for f in r.get('files') or [] if f not in seen]
                metas = {m['id']: m for m in self.store.list_files(list(dict.fromkeys(ids)))} if ids else {}
                candidates = [(metas[i], self.store.file_text(i)) for i in dict.fromkeys(ids) if i in metas]
            for meta, text in candidates:
                if is_design(meta, text or '', query) >= 0.5:
                    out.append((meta, text or ''))
                if len(out) == 2:
                    break
            return out
        except Exception:
            return []

    def web_engine(self):
        """Research mode's engine when none is named: the active one if it can search the web, else the first
        available engine that can, else None."""
        if self.engine is not None and self.engine.supports_web:
            return self.engine
        return next((e for e in self.engines.values() if e.supports_web and e.available()[0]), None)

    def reload_customs(self):
        self.customs = self.store.agents()
        self.use_engine(self.engine)

    def config(self) -> dict:
        e = self.engine
        # Subscription engines cost nothing per call here; the plan's own limits apply instead.
        prices = PRICES if e is None or e.billing == 'api' else {**PRICES, 'claude_in': 0.0, 'claude_out': 0.0}
        from .evals import CASSETTE
        features = {'files': True, 'compare': True, 'evals': True, 'custom_agents': True,
                    'exec': bool(getattr(e, 'supports_exec', False)), 'cassette': CASSETTE.exists()}
        return {'agents': {**self.agents, **FILE_AGENTS, **SQL_AGENT}, 'guards': GUARDS, 'claude': e is not None, 'state': self.state,
                'stats': self.stats, 'samples': SAMPLES, 'prices': prices,
                'engine': engine_info(e) if e is not None else None,
                'engines': [engine_info(x) for x in self.engines.values()],
                'features': features, 'route_examples': self.route_examples,
                'limits': {'query_chars': MAX_QUERY_CHARS}, 'fonts': {'body': body_font()}}

    def hello(self) -> dict:
        # history fills in completion order; replay wants qid order, with in-flight runs included
        records = sorted([*self.history, *(r for q, r in self.inflight.items() if q not in self.sandbox)], key=lambda r: r['qid'])
        return {'type': 'hello', **self.config(), 'history': [public_run(r) for r in records[-HISTORY:]]}

    def deadline(self, query: str, engine) -> float:
        """Seconds a run may take: the long-document deadline when an engine will write a long or illustrated file."""
        if engine is None:
            return self.run_timeout
        from .create.brief import asks_more, parse_brief
        try:
            long_doc = asks_more(parse_brief(query))
        except Exception:
            long_doc = False
        return max(self.run_timeout, self.long_run_timeout) if long_doc else self.run_timeout

    def stats_for(self, qid: int) -> dict:
        """The stats a run counts towards: the global ones, or a throwaway copy for a sandbox run."""
        return self.sandbox_stats.get(qid, self.stats)

    def usage(self, rec: dict | None, d: dict):
        """Adds planner/agent/merger token counts to the global stats and to this run's own `tokens`."""
        jev, llm_in, llm_out = d.get('jev_tokens', 0), d.get('claude_in', 0), d.get('claude_out', 0)
        stats = self.stats_for(rec['qid']) if rec is not None else self.stats
        stats['jev_input_tokens'] += jev
        stats['claude_input_tokens'] += llm_in
        stats['claude_output_tokens'] += llm_out
        if rec is not None:
            t = rec['tokens']
            t['jev_in'] += jev
            t['llm_in'] += llm_in
            t['llm_out'] += llm_out

    def claude_usage(self, d):  # kept for callers from v2
        self.usage(None, d)

    # ---------- runs ----------

    def get_run(self, qid: int) -> dict | None:
        return self.inflight.get(qid) or self.store.get_run(qid)

    def runs_where(self, col: str, value: str) -> list[dict]:
        """Session or compare runs in qid order, with in-flight ones taken from memory (the DB copy is the start state)."""
        return [self.inflight.get(r['qid'], r) for r in self.store.runs_where(col, value)]

    def submit(self, query: str, source: str, *, session_id=None, engine=USE_ACTIVE, files=(), compare_id=None,
               sandbox: str | None = None, extras: dict | None = None, examples: bool | None = None, **chat) -> int:
        """Allocates the qid now (so POST /ask can return it) and runs the query in the background.
        With `sandbox` (a sandbox id) the run is ephemeral: nothing is stored, it doesn't count towards stats or
        history, and its events only reach that sandbox's event stream. A sandbox run's `extras` (all optional):
        `draft` (an unsaved custom agent dict), `files` ([(meta, text)] from sandbox memory), `replaces` (qid of the
        turn this edits) and `remember` (default True: read and write the sandbox's follow-up memory).
        `examples` forces route examples on or off for this run only (evals); None follows the global switch.
        An eval run's `extras` may carry `holdout` (the case query): with examples on, labels whose text is that query or
        the subtask being routed are left out of that route's criteria.
        `chat` (docs/PLAN-speed-evals-chat.md, see handle()): mode, style, agent, group_id, chosen, context_before."""
        if sandbox:
            self.sandboxes.get(sandbox, create=True)  # marks it used, so the sweeper leaves it alone
            qid = next(self.sandbox_ids)
            self.sandbox[qid] = sandbox
            self.sandbox_stats[qid] = {'queries': 0, 'subtasks': 0, 'errors': 0, 'jev_input_tokens': 0,
                                       'claude_input_tokens': 0, 'claude_output_tokens': 0, 'by_agent': {}}
        else:
            qid = next(self.ids)
        task = asyncio.create_task(self.handle(query, source, qid, session_id=session_id, engine=engine, files=files,
                                               compare_id=compare_id, extras=extras, examples=examples, **chat))
        self.tasks.add(task)
        self.running[qid] = task
        task.add_done_callback(lambda t: (self.tasks.discard(t), self.running.pop(qid, None)))
        return qid

    # ---------- answer groups (several answers to one question) ----------

    def run_records(self, group_id: str) -> list[dict]:
        """A group's runs in qid order, in-flight ones from memory."""
        return [self.inflight.get(r['qid'], r) for r in self.store.group_runs(group_id)]

    def set_chosen(self, rec: dict, value: bool, group_id: str | None = None, picked: bool | None = None):
        """Updates a run's group fields everywhere it lives: the in-flight record, history and the store. `picked`
        marks the user's own choice (POST /api/runs/{qid}/choose), which settle_group() never overrides."""
        for r in [self.inflight.get(rec['qid']), *(h for h in self.history if h['qid'] == rec['qid']), rec]:
            if r is not None:
                r['chosen'] = value
                if group_id:
                    r['group_id'] = group_id
                if picked is not None:
                    r['picked'] = picked
        self.store.save_run(self.inflight.get(rec['qid'], rec))

    def settle_group(self, group_id: str):
        """After a group's run ends: if the chosen answer (the default, not one the user picked) failed or was
        cancelled, the first sibling that finished well becomes chosen, so follow-ups don't build on an error."""
        runs = self.run_records(group_id)
        if any(r.get('picked') for r in runs):
            return
        chosen = next((r for r in runs if r.get('chosen')), None)
        if chosen is not None and chosen.get('status') in ('running', 'done'):
            return
        ok = next((r for r in runs if r.get('status') == 'done'), None)
        if ok is not None:
            for r in runs:
                self.set_chosen(r, r['qid'] == ok['qid'])

    def choose(self, qid: int) -> tuple[str, str | None]:
        """Makes a run the chosen answer of its group: ('ok', group_id), ('unknown', None) or ('no group', None)."""
        rec = self.get_run(qid)
        if rec is None or qid in self.sandbox:
            return 'unknown', None
        gid = rec.get('group_id')
        if not gid:
            return 'no group', None
        for r in self.run_records(gid):
            self.set_chosen(r, r['qid'] == qid, picked=r['qid'] == qid)
        return 'ok', gid

    def join_group(self, qid: int) -> tuple[str, int]:
        """(group_id, first qid) for another answer to run `qid`'s question; a run that had no group starts one, as its
        chosen answer."""
        rec = self.get_run(qid)
        gid = rec.get('group_id')
        if not gid:
            gid = uuid.uuid4().hex[:10]
            self.set_chosen(rec, True, gid)
        return gid, min([qid, *(r['qid'] for r in self.run_records(gid))])

    def cancel(self, qid: int) -> str:
        """'ok' if the run was cancelled, 'finished' if it had already ended, 'unknown' if there's no such run."""
        task = self.running.get(qid)
        if task is not None and not task.done():
            task.cancel()
            return 'ok'
        return 'finished' if self.get_run(qid) else 'unknown'

    async def handle(self, query: str, source: str, qid: int | None = None, *, session_id=None, engine=USE_ACTIVE,
                     files=(), compare_id=None, extras: dict | None = None, examples: bool | None = None,
                     mode: str | None = None, style: str | None = None, agent: str | None = None,
                     group_id: str | None = None, chosen: bool | None = None, context_before: int | None = None):
        """Chat variety (docs/PLAN-speed-evals-chat.md): `mode` quick/balanced/deep/research, `style` an answer style,
        `agent` a forced agent (@agent, validated by the caller), `group_id` the answer group this run belongs to and
        `chosen` whether it is the group's chosen answer, `context_before` the qid whose earlier turns are this run's
        follow-up context (a group's first run, so another answer to a question never sees later turns)."""
        qid = qid or next(self.ids)
        sandbox = self.sandbox.get(qid)
        mode, style = mode or 'balanced', style or 'default'
        extras = {**(extras or {}), 'examples': self.route_examples if examples is None else bool(examples),
                  'context_before': context_before,
                  'clock': {'plan': None, 'route': [], 'agents': [], 'merge': None, 'first': None, 'planner': 'single',
                            'merger': None, 'hits': 0}}
        # Engine per step (A5): on Auto, or on deep mode's own pick of engine; an engine the user named stays pinned,
        # even when it is the one deep mode would have picked (callers pass USE_ACTIVE when none is named).
        named = engine is not USE_ACTIVE
        if engine is USE_ACTIVE:
            engine = self.deep_engine() if mode == 'deep' else self.engine
        # Steps are spread over engines only when the user chose Auto; a selected engine is used for every step.
        extras['steer'] = engine is not None and engine.name == 'auto'
        t0 = time.perf_counter()
        rec = self.inflight[qid] = {
            'qid': qid, 'text': query, 'source': source, 'at': time.time(), 'plan': None, 'tasks': [], 'merged': None,
            'total_ms': None, 'error': None, 'status': 'running', 'engine': engine.name if engine else None,
            'session_id': session_id, 'compare_id': compare_id, 'files': list(files),
            'tokens': {'jev_in': 0, 'llm_in': 0, 'llm_out': 0},
            'mode': mode, 'style': style, 'agent': agent, 'group_id': group_id,
            'chosen': True if group_id is None else bool(chosen)}
        if extras.get('dry_run') == 'route':
            rec['dry_run'] = 'route'
        if isinstance(extras.get('estimate'), dict):  # 5.4: estimated vs used, filled in at the end
            rec['cost'] = {'estimate': extras['estimate'], 'actual': None}
        if not sandbox:
            if session_id:
                self.store.touch_session(session_id, query)
            self.store.save_run(rec)
        stats = self.stats_for(qid)
        emit = self.bus.emit
        deadline = self.deadline(query, engine)
        extras['deadline_at'] = time.monotonic() + deadline  # H2: the create agent's repair ladder reads what is left
        try:
            async with asyncio.timeout(deadline):
                status = await self._handle(query, source, qid, t0, rec, engine, extras)
        except asyncio.CancelledError:
            # Cancelling the task already cancelled every agent under it (and killed any CLI child); report and finish.
            status = 'cancelled'
            self.unfinished(qid, rec, 'cancelled')
            emit('cancelled', qid=qid)
        except TimeoutError:
            status = 'timeout'
            stats['errors'] += 1
            self.unfinished(qid, rec, 'timed out')
            rec['error'] = f'The run timed out after {deadline:.0f}s.'
            emit('error', qid=qid, tid=None, message=rec['error'])
        except Exception as e:
            # Keep the browser's run from hanging: whatever broke, the query still ends with done.
            status = 'error'
            stats['errors'] += 1
            self.unfinished(qid, rec, 'failed')
            rec['error'] = str(e)[:200]
            emit('error', qid=qid, tid=None, message=rec['error'])
        rec['status'], rec['total_ms'] = status, ms_since(t0)
        rec['timings'] = timings(extras['clock'], t0)
        try:  # D6: signs this run went wrong, so real traffic can become eval cases
            made = [f['id'] for t in rec.get('tasks') or [] for f in t.get('created_files') or [] if f.get('id')]
            specs = {fid: s for fid in made if (s := self.created_spec(fid, sandbox)) is not None}
            rec['suspects'] = [] if rec.get('dry_run') else suspects_mod.suspects(rec, specs)
        except Exception:
            rec['suspects'] = []
        after = {}
        if rec.get('cost') is not None:
            try:
                from .estimate import actual_of
                rec['cost']['actual'] = actual_of(rec)
            except Exception:
                pass
            after['cost'] = rec['cost']
        if rec.get('file_failed'):
            after['file_failed'] = True
        if rec.get('checkpoints_state'):  # a timed-out or cancelled file step still offers Resume in live views
            after['checkpoints'] = (public_run(rec) or {}).get('checkpoints') or []
        emit('done', qid=qid, total_ms=rec['total_ms'], stats=stats, status=status, tokens=rec['tokens'],
             timings=rec['timings'], **after)
        self.inflight.pop(qid, None)
        if sandbox:
            self.remember_sandbox_turn(sandbox, rec, extras, status)
            self.sandbox.pop(qid, None)
            self.sandbox_stats.pop(qid, None)
        else:
            self.history.append(rec)
            self.store.save_run(rec)
            if rec.get('group_id'):
                self.settle_group(rec['group_id'])

    def remember_sandbox_turn(self, sid: str, rec: dict, extras: dict | None = None, status: str = 'done'):
        """Adds a finished sandbox run to its thread (in place of the turn it replaces, if any). Cancelled runs (unless
        a file step left a resumable checkpoint) and `remember: false` runs leave the memory alone, and a sandbox
        forgotten meanwhile isn't brought back."""
        extras = extras or {}
        mem = self.sandboxes.get(sid) if sid in self.sandboxes else None
        if mem is None or not extras.get('remember', True):
            return
        if status == 'cancelled':
            # a cancelled file step that paid for parts keeps its checkpoint, so Resume can still continue it
            infos = (public_run(rec) or {}).get('checkpoints') or []
            if not any(i.get('resumable') for i in infos):
                return
        mem.record(rec, extras.get('replaces'))

    def clear_sandbox(self, sid: str) -> int:
        """Forgets a sandbox: cancels its running queries and drops its thread and files. Returns runs cancelled."""
        n = 0
        for qid, owner in list(self.sandbox.items()):
            if owner == sid and self.cancel(qid) == 'ok':
                n += 1
        self.sandboxes.drop(sid)
        drop_design_workspaces(sid)
        return n

    def sweep_sandboxes(self, now: float | None = None) -> list[str]:
        """Forgets sandboxes idle longer than the TTL that have no running queries. Returns the ids forgotten."""
        gone = self.sandboxes.sweep(time.time() if now is None else now, self.sandbox_ttl, set(self.sandbox.values()))
        for sid in gone:
            drop_design_workspaces(sid)
        return gone

    async def sweeper(self, every: float = 60.0):
        while True:
            await asyncio.sleep(every)
            self.sweep_sandboxes()

    def keep_sandbox(self, sid: str, qids: list[int] | None = None) -> tuple[str, list[int]]:
        """Copies finished sandbox turns into a new saved chat (the sandbox is unchanged): new qids from the normal
        counter, tids rewritten to match, source 'chat', a new session, and no file references (sandbox files are never
        saved). `qids` defaults to the whole thread; turns keep their thread order. Returns (session_id, new qids)."""
        mem = self.sandboxes.get(sid) if sid in self.sandboxes else None
        thread = mem.thread if mem else []
        if not thread:
            raise SandboxError('this sandbox has no finished turns to keep')
        known = {t['qid'] for t in thread}
        if qids is not None:
            if missing := [q for q in qids if q not in known]:
                raise SandboxError(f'{missing[0]} is not a finished turn in this sandbox')
            if not qids:
                raise SandboxError('qids must list at least one turn')
        wanted = set(known if qids is None else qids)
        session_id = uuid.uuid4().hex[:12]
        new_qids = []
        for turn in [t for t in thread if t['qid'] in wanted]:
            rec = renumber(copy.deepcopy(turn['record']), next(self.ids))
            rec.update(source='chat', session_id=session_id, compare_id=None, files=[], group_id=None, chosen=True)
            self.keep_created(mem, rec)
            if not new_qids:
                self.store.touch_session(session_id, rec['text'])
            self.store.save_run(rec)
            self.history.append(rec)
            new_qids.append(rec['qid'])
        return session_id, new_qids

    def keep_created(self, mem, rec: dict):
        """Keeping a sandbox turn keeps the files it made: the ones still in the sandbox's memory are saved with the
        run (by id, under data/created); a file the sandbox has already let go of drops off the task."""
        for t in rec.get('tasks') or []:
            kept = []
            for f in t.get('created_files') or []:
                held = mem.created.get(f.get('id')) if mem else None
                if held is not None:
                    meta, spec, data = held
                    meta = {**meta, 'qid': rec['qid'], 'sandbox': None}
                    if isinstance(meta.get('partial'), dict) and isinstance(meta['partial'].get('resume'), dict):
                        # Resume on the kept file continues in the saved chat, from the kept run's checkpoint
                        meta['partial'] = {**meta['partial'], 'resume': {'qid': rec['qid'], 'tid': t.get('tid'),
                                                                         'sandbox': None}}
                    if self.store.get_created(meta['id']) is None:
                        self.store.add_created(meta, spec, data)
                    kept.append(meta)
            if 'created_files' in t:
                t['created_files'] = kept

    def save_created(self, meta: dict, spec: dict, data: bytes, sandbox: str | None) -> dict:
        """Stores a file a run made: in the sandbox's memory for a sandbox run (never on disk), else in the store."""
        if sandbox:
            meta = {**meta, 'sandbox': sandbox}
            mem = self.sandboxes.peek(sandbox)
            if mem is not None:
                mem.add_created(meta, spec, data)
        else:
            self.store.add_created(meta, spec, data)
        return meta

    def chat_files(self, rec: dict, sandbox: str | None, before: int,
                   remember: bool = True) -> tuple[list[dict], tuple | None]:
        """What the create step may build on from earlier in the chat: its recent turns that answered, oldest first
        ([{query, answer}]), and the newest file those turns made as (CreatedFile, spec, made by the latest of them).
        A turn that only asked a follow-up question, was blocked or failed is skipped, so "now as slides" after a
        clarify still means the file made before it."""
        if sandbox:
            mem = self.sandboxes.peek(sandbox) if remember else None
            recs = [t['record'] for t in mem.thread[-create_agent.LOOKBACK:]] if mem else []
        else:
            mem, sid = None, rec.get('session_id')
            recs = self.store.session_records(sid, before, create_agent.LOOKBACK) if sid else []
        turns = [r for r in recs if create_agent.answered(r)]
        last = None
        for i in range(len(turns) - 1, -1, -1):
            files = [f for t in turns[i].get('tasks') or [] for f in t.get('created_files') or []]
            if files:
                held = mem.created.get(files[-1]['id']) if mem else None
                spec = held[1] if held else None if sandbox else self.store.created_spec(files[-1]['id'])
                if spec is not None:
                    last = (files[-1], spec, i == len(turns) - 1)
                    break
        # A turn that only made a file keeps its question but not its reply ("Created **x.pdf**, 1 page"), which is
        # not content: "I also want a spreadsheet" must not become a file of that sentence.
        context = [{'query': r['text'], 'answer': '' if create_agent.only_created(r) else (r.get('merged') or {}).get('answer') or ''}
                   for r in turns]
        return context, last

    def unfinished(self, qid: int, rec: dict, why: str):
        """Subtasks that never answered get an error, so no card is left spinning."""
        for t in rec['tasks']:
            if 'answer' not in t and 'error' not in t:
                t['error'] = f'{why} before it answered'
                self.bus.emit('error', qid=qid, tid=t['tid'], message=t['error'])

    def previous_turn(self, rec: dict, sandbox: str | None, extras: dict, qid: int) -> tuple[str, str, dict] | None:
        """(question, answer, frame) of the chat's latest finished turn when it left a frame (A4), else None. Only the
        latest turn counts: a follow-up refers to what was just said."""
        if sandbox:
            mem = self.sandboxes.peek(sandbox) if extras.get('remember', True) else None
            recs = [t['record'] for t in (mem.thread if mem else []) if t['qid'] != extras.get('replaces')][-1:]
        elif rec.get('session_id'):
            recs = self.store.session_records(rec['session_id'], extras.get('context_before') or qid, 1)
        else:
            recs = []
        for r in recs[-1:]:
            frames = [t['frame'] for t in r.get('tasks') or [] if isinstance(t.get('frame'), dict)]
            if frames:
                return r.get('text') or '', (r.get('merged') or {}).get('answer') or '', frames[-1]
        return None

    def mark_resumed(self, rec: dict, tid: str, by_qid: int, sandbox: str | None, file_id: str | None = None):
        """Records on a run's checkpoint that run `by_qid` resumes it (and, once it has one, the file it made), so the
        same checkpoint is never written twice. A stored run is saved; a sandbox run's record lives in memory."""
        state = (rec.get('checkpoints_state') or {}).get(tid)
        if not isinstance(state, dict):
            return
        state['resumed_by'] = {'qid': by_qid, 'file_id': file_id}
        if file_id:  # the source's partial file no longer offers Resume: it names the run that finished it
            for t in rec.get('tasks') or []:
                for i, f in enumerate(t.get('created_files') or []):
                    ref = (f.get('partial') or {}).get('resume') if isinstance(f.get('partial'), dict) else None
                    if isinstance(ref, dict) and ref.get('tid') == tid:
                        partial = {**f['partial'], 'resume': None, 'resumed_by': by_qid}
                        t['created_files'][i] = {**f, 'partial': partial}
                        try:
                            if sandbox:
                                mem = self.sandboxes.peek(sandbox)
                                held = mem.created.get(f.get('id')) if mem else None
                                if held is not None:
                                    mem.created[f['id']] = ({**held[0], 'partial': partial}, held[1], held[2])
                            else:
                                self.store.update_created(f['id'], partial=partial)
                        except Exception:
                            pass
        if not sandbox and rec.get('status') != 'running':
            try:
                self.store.save_run(rec)
            except Exception:
                pass

    def resume_done(self, resume: dict, by_qid: int, file_id: str, sandbox: str | None):
        """A resume run made its file: the source run's checkpoint says so."""
        src_qid, src_tid = resume.get('qid'), resume.get('tid')
        if type(src_qid) is not int or not isinstance(src_tid, str):
            return
        if sandbox:
            mem = self.sandboxes.peek(sandbox)
            src = next((t['record'] for t in (mem.thread if mem else []) if t['qid'] == src_qid), None)
        else:
            src = self.inflight.get(src_qid) or self.store.get_run(src_qid)
        if isinstance(src, dict):
            self.mark_resumed(src, src_tid, by_qid, sandbox, file_id)

    def created_spec(self, file_id: str, sandbox: str | None) -> dict | None:
        """The stored spec of a file this run (or its sandbox) made, so a dependent create step can start from it."""
        if sandbox:
            mem = self.sandboxes.peek(sandbox)
            held = mem.created.get(file_id) if mem else None
            return held[1] if held else None
        return self.store.created_spec(file_id)

    async def _handle(self, query, source, qid, t0, rec, engine, extras) -> str:
        emit = self.bus.emit
        emit('query', qid=qid, text=query, source=source, session_id=rec['session_id'], compare_id=rec['compare_id'],
             engine=rec['engine'], files=rec['files'])
        stats = self.stats_for(qid)
        stats['queries'] += 1
        mode, style, forced = rec['mode'], rec['style'], rec['agent']
        clock = extras['clock']  # stage intervals and counters for RunTimings (see timings())
        # D3 (docs/PLAN-accuracy-v2.md): an eval may route with its own Jev (a recorded one, jevrouter/cassette.py) and
        # a pinned plan, and a route-only run stops before any agent, merger, engine or HTTP call.
        jev = extras.get('jev') or self.jev
        dry = extras.get('dry_run') == 'route'
        use_cache = source != 'eval' and jev is self.jev  # evals measure routing itself: every case asks Jev
        web = bool(engine is not None and engine.supports_web)
        show_trace = env_flag('TG_POLICY_TRACE', True)

        def text_out(tid, chunk):
            """A delta of answer text; the first one of the run is its time to first token."""
            if chunk:
                if clock['first'] is None:
                    clock['first'] = time.perf_counter()
                emit('delta', qid=qid, tid=tid, text=chunk)

        sandbox = self.sandbox.get(qid)
        if sandbox:  # sandbox files come from memory (snapshotted at submit); nothing is read from the store
            attached = [meta for meta, _ in extras.get('files', ())]
            texts = {meta['id']: text for meta, text in extras.get('files', ())}
        else:
            attached = self.store.list_files(rec['files']) if rec['files'] else []
            texts = {f['id']: self.store.file_text(f['id']) for f in attached}
        # SQL over attached tables: an engine writes the query; keyless, only a SELECT the user wrote can run.
        tables = table_files(attached, engine, query)
        agents = self.offered(engine, with_files=bool(attached), tables=bool(tables))
        draft = extras.get('draft')
        if draft and engine is not None:  # an unsaved custom agent: offered to Jev and runnable for this run only
            agents = {**agents, draft['name']: draft['description']}
        if sandbox:
            mem = self.sandboxes.peek(sandbox) if extras.get('remember', True) else None
            context = (mem.context(extras.get('replaces')) if mem else None) or None
        else:
            # Of an answer group only the chosen run is context, and a group's runs see the turns before its question.
            context = self.store.turns(rec['session_id'], extras.get('context_before') or qid, 3,
                                       rec.get('group_id')) if rec['session_id'] else None

        crit = self.criteria(agents) if extras['examples'] else agents  # one set of criteria for every subtask
        holdout = extras.get('holdout') if extras['examples'] else None  # an eval case's query: never its own example
        # Jev only sees text: without the file names, "the attached CSV" reads as unclear and gets gated to clarify.
        files_note = f"\n\n(Attached files: {', '.join(f['name'] for f in attached)})" if attached else ''

        # A4: a keyless follow-up ("make it 500", "and in GBP?") is completed from the previous turn's frame; when it
        # can't be, Jev sees the previous turn next to it. With an LLM planner, the planner resolves follow-ups instead.
        keyless_plan = engine is None or mode == 'quick'
        prev = self.previous_turn(rec, sandbox, extras, qid) if keyless_plan and context else None
        frame = prev[2] if prev else None
        filled = fill_from_frame(query, frame) if frame else None
        plan_query = filled or query
        prev_block = (f'\n\nPrevious turn: {prev[0][:200]} -> {prev[1][:200]}'
                      if prev and not filled and elliptical(query) else '')
        prev_hash = hashlib.sha1(prev_block.encode()).hexdigest()[:12] if prev_block else None

        def criteria_for(text, ctx, st_text):
            return self.criteria_without(agents, (holdout, text + ctx, st_text)) if holdout else crit

        def jev_text(text: str, ctx: str) -> tuple[str, str, str | None]:
            """(the text a step is routed on, what Jev also sees after it, the route cache's extra key part). Jev routes
            on a short context (ROUTE_DEP_CHARS of each earlier answer), not the one the agent gets."""
            block = prev_block if prev_block and elliptical(text) else ''
            return text + route_context(ctx), block, prev_hash if block else None

        async def jev_route(text: str, c: dict, extra: str | None = None, block: str = '') -> dict:
            """Jev's decision for this text, from the route cache when the same text met the same criteria before. Jev
            reads at most ROUTE_CHARS of it (B4); the planner and the agents still get all of it. The attached-files
            note and the previous-turn block always reach Jev: what is cut is the end of the step's text."""
            key = self.route_cache.key(text + files_note, c, extra) if use_cache else None
            d = self.route_cache.get(key) if key else None
            if d is not None:
                clock['hits'] += 1
                return {**d, 'cached': True, 'jev_ms': 0, 'input_tokens': 0}
            tail = files_note + block
            room = max(0, ROUTE_CHARS - len(tail))
            d = await route_one(jev, text[:room] + tail, c)
            if len(text) > room:
                d['cut'] = len(text) + len(tail)
            if key:
                self.route_cache.put(key, d)
            return d

        def route_args(st, text, ctx):
            sent, block, extra = jev_text(text, ctx)
            return sent, criteria_for(text, ctx, st.get('text')), extra, block

        # Speculative routing (A2): while the LLM planner works, Jev routes the whole query; when the plan comes back as
        # that same single step, its route is already done.
        spec: dict = {}

        def speculate():
            if not forced:
                sent, block, extra = jev_text(plan_query, '')
                spec['task'] = asyncio.create_task(jev_route(sent, criteria_for(plan_query, '', plan_query), extra, block))

        tp0 = time.perf_counter()
        # H6: a resume run writes the missing parts of one file step: no planner and no routing, one create step
        resume = extras.get('resume') if isinstance(extras.get('resume'), dict) else None
        if resume is not None:
            state = resume.get('state') if isinstance(resume.get('state'), dict) else {}
            p = {'planner': 'pinned', 'subtasks': [str(state.get('request') or query)], 'deps': [[]], 'multi': None,
                 'jev_tokens': 0, 'claude_in': 0, 'claude_out': 0}
        elif extras.get('plan'):
            p = pinned_plan(extras['plan'])
        else:
            try:
                p = await plan(plan_query, jev, None if dry else engine, context, [f['name'] for f in attached], mode=mode,
                               on_llm=speculate, scores=self.multi_cache if use_cache else None, forced=forced, web=web)
            except BaseException:
                if spec.get('task'):
                    spec['task'].cancel()
                raise
        clock['plan'] = (tp0, time.perf_counter())
        clock['planner'], clock['hits'] = plan_kind(p), clock['hits'] + bool(p.get('cached'))
        self.usage(rec, p)
        step_texts, step_deps = p['subtasks'], p['deps']
        if engine is not None and resume is None:  # A5: a place named only by description is looked up first
            step_texts, step_deps = expand_steps(step_texts, step_deps)
        lookups = {i for i, t in enumerate(step_texts) if LOOKUP_STEP.fullmatch(t)}  # never the @agent's step
        subtasks = [{'tid': f'{qid}.{n}', 'text': s, 'depends_on': [f'{qid}.{d + 1}' for d in deps]}
                    for n, (s, deps) in enumerate(zip(step_texts, step_deps), 1)]
        by_tid = {st['tid']: dict(st) for st in subtasks}
        rec.update(plan={'planner': p['planner'], 'subtasks': subtasks}, tasks=list(by_tid.values()))
        emit('plan', qid=qid, planner=p['planner'], subtasks=subtasks, multi=p['multi'], ms=ms_since(t0))
        if spec.get('task'):
            one = subtasks[0] if len(subtasks) == 1 else None
            if one is None or cache_mod.normalize(one['text']) != cache_mod.normalize(plan_query):
                self.drop_speculation(rec, spec.pop('task'))
            else:
                spec['tid'] = one['tid']

        # The agents this run can call: its engine at the mode's effort, with the answer style for a lone LLM agent
        # (several answers get the style from the merger instead), plus file agents and a sandbox draft.
        style_note = STYLES.get(style, '') if len(subtasks) == 1 else ''
        def registry_on(tuned, reuse: bool = False) -> dict:
            registry = self.registry if reuse else self.run_registry(tuned, prefer_keyless=mode == 'quick')
            if attached:  # the file agents exist only for this run, bound to its files
                registry = {**file_agents(attached, texts, tuned), **registry}
                if tables:
                    registry['sql'] = sql_agent(tables, texts, tuned)
            if draft and tuned is not None:
                registry = {**registry, draft['name']: agent_registry.extras(self.http, tuned, [draft])[draft['name']]}
            return registry

        tuned = agent_registry.Tuned(engine, EFFORT.get(mode), style_note) if engine is not None else None
        # the active engine's agents, as they are, when nothing about this run changes them
        registry = registry_on(tuned, engine is self.engine and not EFFORT.get(mode) and not style_note and mode != 'quick')

        def agent_for(agent: str, hard):
            """The runner for an LLM step: on the engine its difficulty calls for when this run may choose one per step
            (A5), else on the run's own. The mode's effort (quick low, deep high) still wins over the difficulty's."""
            pick = self.step_engine(engine, hard, agent) if extras['steer'] and agent in registry else None
            if pick is None:
                return registry.get(agent)
            step_engine, effort = pick
            return registry_on(agent_registry.Tuned(step_engine, EFFORT.get(mode) or effort, style_note)).get(agent)

        llm_helpers = engine is not None and mode != 'quick' and not dry  # quick: no LLM rewrite of steps, no LLM clarify

        # A1: the @agent binds to exactly one step. A plan of one step is that step; @create binds the last step that
        # asks for a file (or the last step, which then makes the file the whole query asks for); any other agent binds
        # the independent step Jev gives it the highest probability, routed here once and reused by route_jev.
        bound_tid, whole_query, bind_note = None, False, None
        pre_routed: dict[str, tuple[str, dict]] = {}  # tid -> (the text Jev saw, its decision)
        if forced:
            if len(subtasks) == 1:
                bound_tid = subtasks[0]['tid']
            elif forced == 'create':
                asks = [st for st in subtasks if is_file_request(st['text']) or create_agent.asks_for_file(st['text'])]
                bound_tid, whole_query = (asks or subtasks)[-1]['tid'], not asks
            else:
                t1 = time.perf_counter()
                # every step the plan made for the request, dependent ones too: the planner puts gathering first and the
                # @agent's own step after it ("What currency does Japan use?" then "Convert 100 USD into that currency")
                steps = [st for n, st in enumerate(subtasks) if n not in lookups]
                routes = await asyncio.gather(*(jev_route(*route_args(st, st['text'], '')) for st in steps),
                                              return_exceptions=True)
                clock['route'].append((t1, time.perf_counter()))
                best, best_p = None, -1.0
                for st, d in zip(steps, routes):
                    if isinstance(d, BaseException):
                        continue
                    pre_routed[st['tid']] = (st['text'], d)
                    if (pr := (d.get('probabilities') or {}).get(forced, 0.0)) > best_p:
                        best, best_p = st, pr
                if best is None or best_p < FORCED_MIN:
                    best, bind_note = steps[0], 'forced agent fits no step well'
                bound_tid = best['tid']

        def is_file_step(st) -> bool:
            return st['tid'] == bound_tid and forced == 'create' or is_file_request(st['text'])

        def primary_file_step(st) -> bool:
            """The run's primary file step (C1): the bound step when @create is forced, else the last create step."""
            if forced == 'create':
                return st['tid'] == bound_tid
            at = next(i for i, s in enumerate(subtasks) if s['tid'] == st['tid'])
            return not any(is_file_request(s['text']) or create_agent.asks_for_file(s['text']) for s in subtasks[at + 1:])

        def brief_for(st) -> Brief:
            text = by_tid[st['tid']].get('input') or st['text']
            return brief_merge(parse_brief(query), parse_brief(text)) if primary_file_step(st) else parse_brief(text)

        def feeds_long_file(st) -> bool:
            """B5: a step a create step builds a long file from (pages >= 3, slides >= 8, images or diagrams)."""
            for s in subtasks:
                if st['tid'] in s['depends_on'] and is_file_step(s):
                    try:
                        b = brief_for(s)
                    except Exception:
                        continue
                    if (b.pages and b.pages[1] >= 3) or (b.slides and b.slides[1] >= 8) or b.images or b.diagrams:
                        return True
            return False

        def dep_context(st) -> str:
            """B1: the answers a step builds on, within DEP_CONTEXT_CHARS shared across them (a short answer leaves its
            share to the longer ones), each cut at a line end and marked; a failed one keeps 200 chars. A dependency
            that read an attached document or table also brings up to 2,000 chars of that file."""
            deps = st['depends_on']
            good = [d for d in deps if results[d].get('ok')]
            shares = allot({d: len(results[d]['answer']) for d in good}, DEP_CONTEXT_CHARS - FAILED_CONTEXT_CHARS * (len(deps) - len(good)))
            lines = []
            for d in deps:
                r = results[d]
                lines.append(f"- {by_tid[d]['text']}: " + (cut(r['answer'], shares[d]) if d in shares else
                                                            f"(this step failed: {r['answer'][:FAILED_CONTEXT_CHARS]})"))
                if r.get('agent') in ('document', 'data') and attached:
                    lines.append(f'  (from the attached file: {attachment_passage(r, st)})')
            return '\n\nContext from earlier steps:\n' + '\n'.join(lines)

        def attachment_passage(r: dict, st) -> str:
            names = {n.strip() for n in (r.get('source') or '').split(',')}
            named = [(f['name'], texts.get(f['id']) or '') for f in attached if f['name'] in names] or \
                    [(f['name'], texts.get(f['id']) or '') for f in attached]
            try:
                hits = search(named, st['text'], 3)
            except Exception:
                hits = []
            passage = '\n'.join(c for _, _, c in hits) or (named[0][1] if named else '')
            return passage[:ATTACHED_CONTEXT_CHARS]

        async def prepare(st) -> tuple[str, str]:
            """(the subtask's own text, possibly made self-contained; the context from its dependencies, or '')."""
            text = st['text']
            # A5: "the capital of Switzerland", "the currency of Brazil" from the static tables, at no cost
            if resolved := gate.resolve_described(text):
                text = by_tid[st['tid']]['input'] = resolved
            if not st['depends_on']:
                return text, ''
            earlier = [by_tid[d].get('input') or by_tid[d]['text'] for d in st['depends_on']]
            if dry:  # route mode: no dependency has answered, and nothing may call the engine
                ctx = '\n\nContext from earlier steps:\n' + '\n'.join(
                    f"- {by_tid[d]['text']}: (not run: route mode)" for d in st['depends_on'])
                if (there := gate.resolve_there(text, earlier)) is not None:
                    text = by_tid[st['tid']]['input'] = there
                return text, ctx
            ctx = dep_context(st)
            rewritten = None
            # A file request is built from the earlier answers themselves (docs/PLAN-files.md): rewriting it costs tokens
            # and would only paste those answers into the step's text.
            if llm_helpers and not is_file_request(text):
                upstream = [results[d]['answer'] for d in st['depends_on'] if results[d].get('ok')]
                facts = '\n\nContext from earlier steps:\n' + '\n'.join(
                    f"- {by_tid[d]['text']}: " + (results[d]['answer'][:RESOLVE_CHARS] if results[d].get('ok') else
                                                  f"(this step failed: {results[d]['answer'][:200]})")
                    for d in st['depends_on'])
                try:
                    rewritten, tin, tout = await resolve_step(engine, text + facts, step=text, upstream=upstream)
                    self.usage(rec, {'claude_in': tin, 'claude_out': tout})
                except Exception:
                    rewritten = None
            # Keyless (or when the rewrite left it as it was): "the time there" names the place the earlier steps were
            # about, a place or the country of the currency converted to, so the time or weather agent can parse it;
            # "convert the result to EUR" names the number the earlier step worked out.
            if rewritten is None or gate.THERE.search(rewritten):
                rewritten = gate.resolve_there(rewritten or text, earlier) or rewritten
            if rewritten is None and not llm_helpers:
                pairs = [(by_tid[d]['text'], results[d]['answer']) for d in st['depends_on'] if results[d].get('ok')]
                rewritten = gate.resolve_result(text, pairs)
            if rewritten is None or rewritten == text:
                return text, ctx
            by_tid[st['tid']]['input'] = rewritten
            return rewritten, ctx

        notes: dict[str, agent_registry.AgentResult] = {}  # tid -> the answer the policy decided (jevrouter/policy.py)
        caveats: dict[str, list[str]] = {}  # tid -> limits the answer must state (B6)
        hardness: dict[str, float | None] = {}  # tid -> Jev's hard score

        async def route(st, text, ctx):
            t1 = time.perf_counter()
            if resume is not None:
                clock['route'].append((t1, time.perf_counter()))
                return route_resume(st)
            try:
                return await (route_forced(st, text, ctx) if st['tid'] == bound_tid else route_jev(st, text, ctx))
            finally:
                clock['route'].append((t1, time.perf_counter()))

        def step_ctx(st, text, ctx, bound: bool = False) -> policy.StepCtx:
            return policy.StepCtx(text=text, ctx=ctx, tid=st['tid'], offered=agents, runnable=set(registry),
                                  forced=forced if bound else None, mode=mode, has_engine=engine is not None, web=web,
                                  attached=bool(attached), has_context=bool(context), depends_on=st['depends_on'],
                                  frame=frame if keyless_plan else None)

        async def route_jev(st, text, ctx):
            term = None if ctx else gate.bare_term(text)  # pure: route mode sees it too, only the lookup is skipped
            lookup = (asyncio.create_task(gate.meanings(self.http, term))
                      if term and not dry and self.http is not None else None)
            try:
                d = None
                held = pre_routed.pop(st['tid'], None)
                if held is not None and held[0] == text and not ctx:
                    d = held[1]
                elif spec.get('tid') == st['tid']:
                    try:
                        d = await spec.pop('task')
                    except Exception:
                        d = None  # the speculative call failed: ask again below, as if there had been none
                if d is None:
                    d = await jev_route(*route_args(st, text, ctx))
            except Exception as e:
                if lookup:
                    lookup.cancel()
                return route_failed(st, e)
            try:
                dec = policy.apply(d, step_ctx(st, text, ctx))
                # 15. ambiguous_term: a lone term with several meanings ("Mercury") asks which one
                if (lookup and dec.agent not in (*GUARDS, 'chat', 'create') and dec.note is None
                        and (options := await meanings_of(lookup))):
                    dec.agent, dec.reason = 'clarify', f'ambiguous term ({len(options)} meanings)'
                    dec.note, dec.note_ok = gate.meanings_text(text.strip().rstrip('.!?'), options), False
                    dec.trace.append({'rule': 'ambiguous_term', 'agent': 'clarify', 'why': dec.reason})
                elif dry and term and dec.agent not in (*GUARDS, 'chat', 'create') and dec.note is None:
                    # route mode: a run would look the term's meanings up and ask which one when there are several
                    dec.trace.append({'rule': 'ambiguous_term', 'agent': dec.agent, 'why': ROUTE_TERM})
            finally:
                if lookup and not lookup.done():
                    lookup.cancel()
            return routed(st, d, dec)

        async def route_forced(st, text, ctx):
            """@agent on the bound step: no route question, but Jev's safety check still runs, so an unsafe step is
            blocked anyway (a step routed for binding already has it)."""
            t1 = time.perf_counter()
            held = pre_routed.pop(st['tid'], None)
            if held is not None:
                unsafe, tokens, signals = held[1].get('unsafe', 0.0), held[1].get('input_tokens', 0), held[1].get('signals')
            else:
                try:
                    unsafe, tokens = await unsafe_score(jev, text + ctx + files_note)
                except Exception as e:
                    return route_failed(st, e)
                signals = None
            d = {'agent': 'blocked' if unsafe >= BLOCK_AT else forced, 'pick': forced,
                 'reason': f'Jev flagged it as unsafe ({unsafe:.0%})' if unsafe >= BLOCK_AT else f'you picked @{forced}',
                 'probabilities': {forced: 1.0}, 'confidence': 1.0, 'urgency': 0.0, 'unsafe': unsafe, 'clear': 1.0,
                 'jev_ms': ms_since(t1), 'model': '', 'examples': False, 'input_tokens': tokens, 'forced': True,
                 'bound': True}
            if signals:
                d['signals'] = signals
            dec = policy.apply(d, step_ctx(st, text, ctx, bound=True))
            if bind_note:
                dec.trace.insert(0, {'rule': 'forced', 'agent': forced, 'why': bind_note})
            return routed(st, d, dec)

        def route_resume(st):
            """H6: the resume step goes to create with no route question (its content passed the checks when it was
            first written, and the file it makes still goes through X4)."""
            why = 'resume a partial file'
            d = {'agent': 'create', 'pick': 'create', 'reason': why, 'probabilities': {'create': 1.0}, 'confidence': 1.0,
                 'urgency': 0.0, 'unsafe': 0.0, 'clear': 1.0, 'jev_ms': 0, 'model': '', 'examples': False,
                 'input_tokens': 0, 'forced': True, 'bound': True,
                 'trace': [{'rule': 'resume', 'agent': 'create', 'why': why}]}
            return routed(st, d)

        def route_failed(st, e):
            stats['errors'] += 1
            by_tid[st['tid']]['error'] = f'Jev: {str(e)[:200]}'
            emit('error', qid=qid, tid=st['tid'], message=by_tid[st['tid']]['error'])
            return None

        def routed(st, d, dec: policy.Decision | None = None):
            tid = st['tid']
            if dec is not None:
                d['agent'], d['pick'], d['reason'] = dec.agent, dec.pick, dec.reason
                if dec.note is not None:
                    notes[tid] = agent_registry.AgentResult(dec.note, dec.note_ok)
                if dec.caveats:
                    caveats[tid] = list(dec.caveats)
                trace = list(dec.trace)
                if filled:
                    trace.insert(0, {'rule': 'frame', 'agent': d['pick'], 'why': f'completed from the previous turn as "{filled}"'})
                if d.get('cut'):
                    trace.append({'rule': trace[-1]['rule'] if trace else 'missing_slot', 'agent': dec.agent,
                                  'why': f"Jev read the first {ROUTE_CHARS:,} of {d['cut']:,} characters"})
                d['trace'] = trace
            hardness[tid] = d.get('hard')  # Jev's hard score picks the step's engine (A5); not a routed field
            self.usage(rec, {'jev_tokens': d['input_tokens']})
            stats['subtasks'] += 1
            stats['by_agent'][d['agent']] = stats['by_agent'].get(d['agent'], 0) + 1
            fields = {k: d[k] for k in ROUTED_KEYS}
            fields.update({k: True for k in ('cached', 'forced', 'bound') if d.get(k)})  # only when true: older clients ignore them
            if d.get('trace') and show_trace:
                fields['trace'] = d['trace']
            if d.get('signals'):
                fields['signals'] = d['signals']
            if dec is not None and dec.assumption:
                fields['assumption'] = dec.assumption
            if filled and frame:
                fields['frame_used'] = frame
            by_tid[tid].update(fields)
            if dry:  # route mode: the stored task keeps what the harness scores
                by_tid[tid].update({'trace': d.get('trace') or [], 'hard': d.get('hard')})
            extra = {'input': by_tid[tid]['input']} if 'input' in by_tid[tid] else {}
            emit('routed', qid=qid, tid=tid, **fields, **extra)
            return fields

        async def meanings_of(lookup) -> list[str] | None:
            """The lone term's meanings, or None: a slow or failed lookup never fails or stalls the route."""
            try:
                return await asyncio.wait_for(lookup, MEANINGS_TIMEOUT)
            except Exception:  # includes the timeout; wait_for has cancelled the lookup
                return None

        async def ask(text, probabilities) -> agent_registry.AgentResult:
            """A clarify message: the engine writes the question when there is one, from a plain-English template."""
            msg = gate.clarify_text(probabilities, text, agents, earlier=bool(context or attached))
            if not llm_helpers:
                return agent_registry.AgentResult(msg, False)
            msg, tin, tout = await gate.clarify_llm(engine, text, msg)
            return agent_registry.AgentResult(msg, False, None, engine.name if tin or tout else 'keyless', tin, tout)

        async def make_file(st) -> tuple[agent_registry.AgentResult, list[dict], list[str], dict]:
            """The create step (docs/PLAN-files.md): its file from the earlier steps' answers and files, the chat, the
            attached files or the engine, stored by id (or in the sandbox's memory). Returns (the answer, [the
            CreatedFile], the caveats: what the file could not honour, the fields `answered` gains for a create step:
            phases, tokens and the checkpoint summary, on success and on failure; docs/PLAN-files-robust.md H3)."""
            t_make = time.perf_counter()
            deps = [(by_tid[d].get('input') or by_tid[d]['text'], results[d]['answer'] if results[d].get('ok') else '')
                    for d in st['depends_on']]
            dep_files = [(f, s) for d in st['depends_on'] for f in results[d].get('created_files') or []
                         if (s := self.created_spec(f['id'], sandbox)) is not None]
            tables = [(f, *table) for f in attached if f.get('columns') and (table := to_table(f['kind'], texts[f['id']]))]
            docs = [(f, texts[f['id']]) for f in attached if not f.get('columns')]
            turns, last = self.chat_files(rec, sandbox, extras.get('context_before') or qid, extras.get('remember', True))
            # A plan of one step is the user's own request: the LLM planner rewrites follow-ups into self-contained
            # steps ("put that in a PDF" -> "put the information about Ada Lovelace in a PDF"), and the rewrite hides
            # the words that say to reuse the answer or file already in the chat (at no token cost). @create bound to a
            # step that asks for no file makes the file the whole query asks for (A1).
            whole = len(subtasks) == 1 or (whole_query and st['tid'] == bound_tid)
            request = query if whole else by_tid[st['tid']].get('input') or st['text']
            primary = primary_file_step(st)
            brief = brief_for(st)
            tid = st['tid']

            saved = {'phase': None, 'at': 0.0}

            def sink(state):  # H2: the long writer's state after every paid call, kept with the run
                if isinstance(state, dict):
                    rec.setdefault('checkpoints_state', {})[tid] = state
                    # and saved with it (at most every few seconds, and on each phase change), so a server restart
                    # mid-run keeps what was paid for: startup marks the run failed and Resume continues from here
                    now = time.monotonic()
                    if not sandbox and (state.get('phase') != saved['phase'] or now - saved['at'] >= CHECKPOINT_SAVE_S):
                        saved.update(phase=state.get('phase'), at=now)
                        try:
                            self.store.save_run(rec)
                        except Exception:
                            pass

            wanted = {'checkpoint': sink, 'deadline': extras.get('deadline_at'),
                      'resume': resume,  # {qid, tid, state}: the new file's resumed_from names the source run and step
                      'design_docs': self.design_history(rec, sandbox, attached, texts, query, qid) if primary else [],
                      'sandbox': sandbox}  # a sandbox file's design workspace lives in the sandbox's temp dir
            fields = getattr(create_agent.Job, '__dataclass_fields__', {})
            if 'tally' in fields:
                try:
                    from .create.longdoc import Tally
                    wanted['tally'] = Tally()
                except Exception:
                    pass
            job = create_agent.Job(request, deps, turns, tables, docs, last, brief=brief, dep_files=dep_files,
                                   role='primary' if primary else 'working', http=self.http,
                                   **{k: v for k, v in wanted.items() if k in fields and v is not None})
            tuned = None
            if engine is not None:
                prefer = self.lean_engine(engine, brief, request, attached, deps) if primary else None
                tuned = agent_registry.Tuned(engine, prefer=prefer)
            try:
                made = await create_agent.make(job, tuned, jev, mode)
            except Exception as e:  # make() keeps what was spent itself; this keeps it even if it did not
                tally = getattr(job, 'tally', None)
                tin, tout = getattr(tally, 'tokens', (0, 0)) if tally is not None else (0, 0)
                why = str(e)[:160] or type(e).__name__
                made = create_agent.Made(f'No file was made: {why}', False, getattr(tally, 'engine', None) or
                                         (engine.name if engine is not None else 'keyless'), tin, tout,
                                         phases=list(getattr(tally, 'phases', {}).values()) if tally is not None else [])
            self.usage(rec, {'jev_tokens': made.jev_tokens})
            files = []
            state = (rec.get('checkpoints_state') or {}).get(tid)
            if made.file is not None:
                meta = made.file
                if made.data is not None:  # a new file (not one the chat already had in that format)
                    meta = {**meta, 'qid': qid}
                    partial = meta.get('partial') or getattr(made, 'partial', None)
                    if isinstance(partial, dict):  # H7: the Resume button on the file card knows where to go
                        meta['partial'] = {**partial, 'resume': {'qid': qid, 'tid': tid, 'sandbox': sandbox}}
                    if rec.get('cost') is not None:  # 5.4: the file's own share of the estimate, and its own use
                        from .estimate import file_share
                        meta['cost'] = {'estimate': file_share(rec['cost'].get('estimate')), 'actual': {
                            'calls': sum(int(p.get('calls') or 0) for p in made.phases or [] if isinstance(p, dict)),
                            'tokens_in': int(made.llm_in or 0), 'tokens_out': int(made.llm_out or 0),
                            'seconds': round(time.perf_counter() - t_make, 1)}}
                    meta = self.save_created(meta, made.spec, made.data, sandbox)
                    if isinstance(state, dict) and not state.get('file_id'):
                        state['file_id'] = meta['id']
                    if isinstance(resume, dict):  # the source checkpoint is taken: its Resume is not offered again
                        self.resume_done(resume, qid, meta['id'], sandbox)
                files.append(meta)
            elif primary and not made.ok:  # H4: the run's file step made no file
                rec['file_failed'] = True
            out = agent_registry.AgentResult(made.answer, made.ok, None, made.engine, made.llm_in, made.llm_out)
            extra = {'phases': list(made.phases or []), 'llm_in': int(made.llm_in or 0), 'llm_out': int(made.llm_out or 0)}
            if isinstance(state, dict):
                extra['checkpoint'] = checkpoint_info(state, qid, tid)
            return out, files, list(getattr(made, 'caveats', None) or []), extra

        async def run(st, r, text):
            tid, agent = st['tid'], r['agent']
            t1 = time.perf_counter()
            delta = lambda chunk: text_out(tid, chunk)
            efforts: list[str] = []
            made: list[dict] = []  # files a create step made
            create_fields: dict = {}  # H3: phases, tokens and checkpoint of a create step
            limits: list[str] = list(caveats.get(tid, ()))
            token = agent_registry.EFFORTS.set(efforts)
            feeds = agent_registry.FEEDS_FILE.set(agent != 'create' and feeds_long_file(st))
            try:
                with cache_mod.track() as hits:
                    if agent == 'blocked':
                        out = agent_registry.BLOCKED
                        delta(out.answer)
                    elif tid in notes:
                        out = notes[tid]
                        delta(out.answer)
                    elif agent == 'clarify':
                        out = await ask(by_tid[tid].get('input') or st['text'], r['probabilities'])
                        delta(out.answer)
                    elif agent == 'create':
                        out, made, file_caveats, create_fields = await make_file(st)
                        limits += file_caveats
                        delta(out.answer)
                    else:
                        runner = registry.get(agent) if agent in KEYLESS else agent_for(agent, hardness.get(tid))
                        if runner is None:
                            raise KeyError(agent)
                        out = await runner(text, delta)
            except Exception as e:
                why = str(e)[:160] or ('it took too long' if isinstance(e, TimeoutError) else type(e).__name__)
                out = agent_registry.AgentResult(f'{agent} agent failed: {why}', False)
                delta(out.answer)
            finally:
                agent_registry.FEEDS_FILE.reset(feeds)
                agent_registry.EFFORTS.reset(token)
                clock['agents'].append((t1, time.perf_counter()))
            self.usage(rec, {'claude_in': out.claude_in, 'claude_out': out.claude_out})
            answered = {'agent': agent, 'agent_ms': ms_since(t1), 'answer': out.answer, 'ok': out.ok,
                        'source': out.source, 'engine': out.engine}
            if made:
                answered['created_files'] = made
            if agent == 'create':
                answered.update(create_fields)
            if not out.ok:  # a dead end has no figure to hedge (B6)
                limits = [c for c in limits if c != gate.TIME_SENSITIVE_CAVEAT]
            if limits:
                answered['caveats'] = list(dict.fromkeys(limits))
            if out.ok and agent not in GUARDS and agent != 'create':  # A4: what the next keyless turn can build on
                try:
                    answered['frame'] = {'agent': agent, 'slots': gate.frame_slots(agent, by_tid[tid].get('input') or st['text'])}
                except Exception:
                    pass
            elif agent == 'clarify' and (partial := gate.partial_frame(by_tid[tid].get('pick'),
                                                                       by_tid[tid].get('input') or st['text'])):
                answered['frame'] = partial  # the question's answer, next turn, completes what this one parsed
            checks = {}
            if hits[0]:
                clock['hits'] += hits[0]
                checks['cached'] = True
            llm_answer = agent not in GUARDS and tid not in notes and out.engine not in (None, 'keyless')
            if llm_answer and efforts:
                checks['effort'] = efforts[-1]
            if llm_answer and out.ok and agent != 'create':  # A6: keyless recompute of numbers; grounding in deep mode
                deep = mode == 'deep'
                checker = self.fastest(exclude={out.engine}) if deep and agent in verify_mod.GROUNDED else None
                try:
                    v, tin, tout = await verify_mod.verify(by_tid[tid].get('input') or st['text'], out.answer, agent=agent,
                                                           http=self.http, deep=deep, checker=checker, source=out.source)
                except Exception:  # a check must never cost the answer
                    v, tin, tout = (verify_mod.skipped('The check could not run.') if deep else {}), 0, 0
                self.usage(rec, {'claude_in': tin, 'claude_out': tout})
                checks.update(v)
            # A5: a keyless math or currency answer uses every number and code the question gave, the right way round
            coverage = getattr(verify_mod, 'coverage', None)
            if coverage and agent in ('math', 'currency') and out.ok and out.engine == 'keyless' and tid not in notes:
                try:
                    if v := coverage(agent, by_tid[tid].get('input') or st['text'], out.answer):
                        checks.update(v)
                except Exception:
                    pass
            if checks:  # only when there is something to say: older clients ignore the key
                answered['checks'] = checks
            # a create step's answer is a line code wrote ("Created **x.pdf**, 4 pages"): nothing for an LLM to merge
            if agent in GUARDS or tid in notes or (agent in KEYLESS and out.engine == 'keyless') or agent == 'create':
                exact.add(tid)
            by_tid[tid].update(answered)
            emit('answered', qid=qid, tid=tid, **answered)
            return answered

        def blocked_by_dependency(st) -> dict:
            tid = st['tid']
            fields = {'agent': 'blocked', 'pick': 'blocked', 'reason': 'depends on a blocked step', 'probabilities': {},
                      'confidence': 0.0, 'urgency': 0.0, 'unsafe': 1.0, 'clear': 0.0, 'jev_ms': 0, 'model': '',
                      'examples': False}
            trace = [{'rule': 'blocked_dependency', 'agent': 'blocked', 'why': 'depends on a blocked step'}]
            if show_trace:
                fields['trace'] = trace
            stats['subtasks'] += 1
            stats['by_agent']['blocked'] = stats['by_agent'].get('blocked', 0) + 1
            by_tid[tid].update(fields)
            emit('routed', qid=qid, tid=tid, **fields)
            if dry:
                by_tid[tid].update({'trace': trace, 'hard': None})
                return {'agent': 'blocked', 'ok': False, 'answer': ''}
            out = agent_registry.BLOCKED
            text_out(tid, out.answer)
            answered = {'agent': 'blocked', 'agent_ms': 0, 'answer': out.answer, 'ok': out.ok, 'source': out.source,
                        'engine': out.engine}
            exact.add(tid)
            by_tid[tid].update(answered)
            emit('answered', qid=qid, tid=tid, **answered)
            return answered

        async def step(st):
            """One subtask from start to answer: it waits only for its own dependencies, then routes and runs at once,
            so a fast step's answer streams while slower ones are still routing or running (A3)."""
            if st['depends_on']:
                await asyncio.gather(*(steps[d] for d in st['depends_on']))
                # A step that builds on a blocked one is blocked too: its context would carry the harmful request on to
                # the rewrite, Jev and the agent ("How do I make a pipe bomb, then list its components").
                if any(results[d].get('agent') == 'blocked' for d in st['depends_on']):
                    results[st['tid']] = blocked_by_dependency(st)
                    return
            text, ctx = await prepare(st)
            r = await route(st, text, ctx)
            if r is None:
                results[st['tid']] = {'ok': False, 'answer': by_tid[st['tid']]['error']}
                return
            if dry:  # route mode stops here: no agent runs (docs/PLAN-accuracy-v2.md D3)
                results[st['tid']] = {'agent': r['agent'], 'ok': r['agent'] not in GUARDS, 'answer': ''}
                task = by_tid[st['tid']]
                if r['agent'] not in GUARDS and r['agent'] != 'create':  # A4: the frame a follow-up turn is routed from
                    try:
                        task['frame'] = {'agent': r['agent'], 'slots': gate.frame_slots(r['agent'], task.get('input') or st['text'])}
                    except Exception:
                        pass
                elif r['agent'] == 'clarify' and (partial := gate.partial_frame(r.get('pick'), task.get('input') or st['text'])):
                    task['frame'] = partial
                return
            # keyless agents parse places and amounts from plain text: context would only be misread as the step's own
            # numbers ("convert that amount to EUR" + "100 USD = 8,400 INR" is not 100 USD to EUR). With no engine every
            # agent is a keyless lookup (knowledge searches for its whole input), so none of them gets it either.
            results[st['tid']] = await run(st, r, text if r['agent'] in KEYLESS or engine is None else text + ctx)

        # Dependencies only point backwards, so every step's dependencies have a task before it does.
        results: dict[str, dict] = {}
        exact: set[str] = set()  # tids whose answer is exact (keyless or a guard): the template merger is enough
        steps: dict[str, asyncio.Task] = {}
        for st in subtasks:
            steps[st['tid']] = asyncio.create_task(step(st))
        try:
            await asyncio.gather(*steps.values())
        finally:
            for t in steps.values():
                t.cancel()  # no-op for finished steps; stops the rest if one failed or the run was cancelled
            if spec.get('task'):
                self.drop_speculation(rec, spec.pop('task'))

        if dry:
            return 'done'
        answers = [(st, results[st['tid']]) for st in subtasks if 'agent' in results[st['tid']]]
        if not answers:
            rec['error'] = 'Every subtask failed to route; see the errors above.'
            emit('error', qid=qid, tid=None, message=rec['error'])
            return 'error'
        # A2: an LLM merger adds nothing when every answer is exact; quick mode always joins by template, deep never does.
        all_exact = all(st['tid'] in exact for st, _ in answers)
        # H4: a run whose file step made no file says so from the template, never with an LLM call to wrap a failure
        template = engine is None or mode == 'quick' or (mode != 'deep' and all_exact) or bool(rec.get('file_failed'))
        t2 = time.perf_counter()
        steps_out: list[StepOut] = [
            {'tid': st['tid'], 'text': st['text'], 'agent': a['agent'], 'answer': a['answer'], 'ok': a['ok'],
             'files': a.get('created_files') or [], 'caveats': a.get('caveats') or [],
             'assumption': by_tid[st['tid']].get('assumption'), 'feeds_file': a['agent'] != 'create' and feeds_long_file(st),
             'probabilities': by_tid[st['tid']].get('probabilities') or {},
             'asked': a['agent'] == 'clarify' and st['tid'] in notes} for st, a in answers]
        m = await compose(query, steps_out, lambda text: text_out('merge', text), None if template else engine,
                          style=style, exact=all_exact)
        clock['merger'] = m['kind']
        if m['kind'] == 'llm':
            clock['merge'] = (t2, time.perf_counter())
        # Merging keeps the file list: a merged answer that left out a created file still names it.
        if m['kind'] != 'single' and (lost := [f for _, a in answers for f in a.get('created_files') or []
                                              if f['name'] not in m['answer']]):
            note = '\n\n' + '\n'.join(f'Created **{f["name"]}**' for f in lost)
            text_out('merge', note)
            m['answer'] += note
        # A6: a check that disagreed with an answer shows as a short warning, never a silent wrong answer.
        if warn := verify_mod.warning([(st['text'], a.get('checks') or {}) for st, a in answers]):
            if m['kind'] != 'single':  # a lone answer streamed as its step; the merged text replaces it when it arrives
                text_out('merge', warn)
            m['answer'] += warn
        # Research on an engine without web search: it answered from its own knowledge, and the answer says so
        if mode == 'research' and engine is not None and not getattr(engine, 'supports_web', False):
            note = (f"{engine.label} can't search the web, so this answer comes from its own knowledge, not live "
                    'sources. Select Claude Code, Codex or Auto for web research.')
            m['caveats'] = [*(m.get('caveats') or []), note]
            text_out('merge', '\n\n' + note)
            m['answer'] += '\n\n' + note
        self.usage(rec, m)
        rec['merged'] = {'answer': m['answer'], 'engine': m['engine']}
        # B2: what the run couldn't do and the file the answer leads with; only when there is something to say
        if m.get('caveats'):
            rec['merged']['caveats'] = list(m['caveats'])
        if m.get('primary_file'):
            rec['merged']['primary_file'] = m['primary_file']
        emit('merged', qid=qid, **rec['merged'], ms=ms_since(t2))
        return 'done'

    def drop_speculation(self, rec: dict, task: asyncio.Task):
        """A speculative route the plan didn't need: stopped if still running, its Jev tokens counted if it finished."""
        if not task.done():
            task.cancel()
        elif not task.cancelled() and task.exception() is None and not task.result().get('cached'):
            self.usage(rec, {'jev_tokens': task.result()['input_tokens']})


    async def autopilot(self):
        while True:
            await asyncio.sleep(self.state['interval'])
            if self.state['autopilot'] and self.bus.subscribers:
                self.submit(random.choice(SAMPLES), 'autopilot')


def union_ms(spans, start: float | None = None) -> int:
    """Wall-clock milliseconds covered by any of the (start, end) spans, each clipped to begin no earlier than `start`.
    Steps route and run side by side, so a stage's time is the union of its spans, not their sum."""
    clipped = sorted((a if start is None else max(a, start), b) for a, b in spans)
    total, cur = 0.0, None
    for a, b in (x for x in clipped if x[1] > x[0]):
        if cur and a <= cur[1]:
            cur[1] = max(cur[1], b)
            continue
        if cur:
            total += cur[1] - cur[0]
        cur = [a, b]
    return round((total + (cur[1] - cur[0] if cur else 0)) * 1000)


def timings(clock: dict, t0: float) -> dict:
    """RunTimings (web/src/protocol.ts) from a run's clock. A stage that didn't run is null: plan when no planner call
    was made, merge unless the LLM merger ran. Routing counts only after planning ends, so a speculative route the plan
    reused costs the time it still took then (often none)."""
    plan_end = clock['plan'][1] if clock['plan'] else None
    ms = lambda span: round((span[1] - span[0]) * 1000) if span else None
    return {'plan_ms': ms(clock['plan']) if clock['planner'] != 'single' else None,
            'route_ms': union_ms(clock['route'], plan_end) if clock['route'] else None,
            'agents_ms': union_ms(clock['agents']) if clock['agents'] else None,
            'merge_ms': ms(clock['merge']),
            'first_token_ms': round((clock['first'] - t0) * 1000) if clock['first'] else None,
            'planner': clock['planner'], 'merger': clock['merger'] or 'single', 'cache_hits': clock['hits']}


def renumber(rec: dict, qid: int) -> dict:
    """Moves a run record to a new qid: its tids (`<qid>.<n>`, also in depends_on and the plan) follow."""
    old = f"{rec['qid']}."
    tid = lambda t: f'{qid}.{t[len(old):]}' if isinstance(t, str) and t.startswith(old) else t
    for t in [*(rec.get('tasks') or []), *((rec.get('plan') or {}).get('subtasks') or [])]:
        if 'tid' in t:
            t['tid'] = tid(t['tid'])
        if 'depends_on' in t:
            t['depends_on'] = [tid(d) for d in t['depends_on']]
        if isinstance(t.get('checkpoint'), dict):  # a failed file step's Resume points at the run's new number
            t['checkpoint'] = {**t['checkpoint'], 'qid': qid, 'tid': tid(t['checkpoint'].get('tid'))}
    if isinstance(rec.get('checkpoints_state'), dict):
        rec['checkpoints_state'] = {tid(k): v for k, v in rec['checkpoints_state'].items()}
    rec['qid'] = qid
    return rec

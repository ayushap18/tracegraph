"""handle(query): plan -> (route -> run) per step, each as soon as its dependencies answered -> merge, emitting the SSE
protocol in PLAN.md and docs/PLAN-v4.md, with the stage timings, caches and chat modes of docs/PLAN-speed-evals-chat.md."""
import asyncio
import copy
import itertools
import os
import random
import time
import uuid
from collections import deque

from . import agents as agent_registry
from . import cache as cache_mod
from . import gate
from . import verify as verify_mod
from .agents.llm import COMMON
from .agents.tools import dates_question, sql_agent, sql_in, units_question
from .config import (AGENTS, BLOCK_AT, CONFIRM_AT, DEEP_MIN_OK, EASY_AT, GUARDS, HARD_AT, HISTORY, KEYLESS, PRICES, REPORT,
                     RESEARCH, RUN, RUN_TIMEOUT, SAMPLES, SQL_AGENT, STRONGEST, env_flag)
from .engines.auto import Steered
from .engines.health import Health, instrument_all, pct
from .events import Broadcaster
from .files import FILE_AGENTS, file_agents
from .jev import clean, criteria, route_one, unsafe_score
from .merger import STYLES, merge
from .planner import SCHEMA as PLAN_SCHEMA, SYSTEM as PLAN_SYSTEM, kind as plan_kind, plan, resolve_step
from .sandbox import DEFAULT_TTL, MAX_SANDBOXES, TURNS as SANDBOX_TURNS, Sandboxes, SandboxError
from .store import Store

ROUTED_KEYS = ('agent', 'pick', 'reason', 'probabilities', 'confidence', 'urgency', 'unsafe', 'clear', 'jev_ms', 'model',
               'examples')
USE_ACTIVE = object()  # a run's engine argument: the engine active at submit time (None means keyless)
CONTEXT_CHARS = 500
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
    for call in [dict(system=PLAN_SYSTEM, effort='low', schema=PLAN_SCHEMA), *(dict(system=s, effort=e) for s, e in COMMON)]:
        try:
            await engine.prewarm(**call)
        except Exception:
            pass  # warming is only an optimisation


def ms_since(t: float) -> int:
    return round((time.perf_counter() - t) * 1000)


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
        """Deep mode's engine when none is named: the strongest healthy one (config.STRONGEST), else the active one."""
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
        features = {'files': True, 'compare': True, 'evals': True, 'custom_agents': True,
                    'exec': bool(getattr(e, 'supports_exec', False))}
        return {'agents': {**self.agents, **FILE_AGENTS, **SQL_AGENT}, 'guards': GUARDS, 'claude': e is not None, 'state': self.state,
                'stats': self.stats, 'samples': SAMPLES, 'prices': prices,
                'engine': e.info() if e is not None else None, 'engines': [x.info() for x in self.engines.values()],
                'features': features, 'route_examples': self.route_examples}

    def hello(self) -> dict:
        # history fills in completion order; replay wants qid order, with in-flight runs included
        records = sorted([*self.history, *(r for q, r in self.inflight.items() if q not in self.sandbox)], key=lambda r: r['qid'])
        return {'type': 'hello', **self.config(), 'history': records[-HISTORY:]}

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
        extras['steer'] = engine is not None and (engine.name == 'auto' or (mode == 'deep' and not named))
        t0 = time.perf_counter()
        rec = self.inflight[qid] = {
            'qid': qid, 'text': query, 'source': source, 'at': time.time(), 'plan': None, 'tasks': [], 'merged': None,
            'total_ms': None, 'error': None, 'status': 'running', 'engine': engine.name if engine else None,
            'session_id': session_id, 'compare_id': compare_id, 'files': list(files),
            'tokens': {'jev_in': 0, 'llm_in': 0, 'llm_out': 0},
            'mode': mode, 'style': style, 'agent': agent, 'group_id': group_id,
            'chosen': True if group_id is None else bool(chosen)}
        if not sandbox:
            if session_id:
                self.store.touch_session(session_id, query)
            self.store.save_run(rec)
        stats = self.stats_for(qid)
        emit = self.bus.emit
        try:
            async with asyncio.timeout(self.run_timeout):
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
            rec['error'] = f'The run timed out after {self.run_timeout:.0f}s.'
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
        emit('done', qid=qid, total_ms=rec['total_ms'], stats=stats, status=status, tokens=rec['tokens'],
             timings=rec['timings'])
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
        """Adds a finished sandbox run to its thread (in place of the turn it replaces, if any). Cancelled runs and
        `remember: false` runs leave the memory alone, and a sandbox forgotten meanwhile isn't brought back."""
        extras = extras or {}
        mem = self.sandboxes.get(sid) if sid in self.sandboxes else None
        if mem is None or status == 'cancelled' or not extras.get('remember', True):
            return
        mem.record(rec, extras.get('replaces'))

    def clear_sandbox(self, sid: str) -> int:
        """Forgets a sandbox: cancels its running queries and drops its thread and files. Returns runs cancelled."""
        n = 0
        for qid, owner in list(self.sandbox.items()):
            if owner == sid and self.cancel(qid) == 'ok':
                n += 1
        self.sandboxes.drop(sid)
        return n

    def sweep_sandboxes(self, now: float | None = None) -> list[str]:
        """Forgets sandboxes idle longer than the TTL that have no running queries. Returns the ids forgotten."""
        return self.sandboxes.sweep(time.time() if now is None else now, self.sandbox_ttl, set(self.sandbox.values()))

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
            if not new_qids:
                self.store.touch_session(session_id, rec['text'])
            self.store.save_run(rec)
            self.history.append(rec)
            new_qids.append(rec['qid'])
        return session_id, new_qids

    def unfinished(self, qid: int, rec: dict, why: str):
        """Subtasks that never answered get an error, so no card is left spinning."""
        for t in rec['tasks']:
            if 'answer' not in t and 'error' not in t:
                t['error'] = f'{why} before it answered'
                self.bus.emit('error', qid=qid, tid=t['tid'], message=t['error'])

    async def _handle(self, query, source, qid, t0, rec, engine, extras) -> str:
        emit = self.bus.emit
        emit('query', qid=qid, text=query, source=source, session_id=rec['session_id'], compare_id=rec['compare_id'],
             engine=rec['engine'], files=rec['files'])
        stats = self.stats_for(qid)
        stats['queries'] += 1
        mode, style, forced = rec['mode'], rec['style'], rec['agent']
        clock = extras['clock']  # stage intervals and counters for RunTimings (see timings())
        use_cache = source != 'eval'  # evals measure routing itself: every case asks Jev

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

        def criteria_for(text, ctx, st_text):
            return self.criteria_without(agents, (holdout, text + ctx, st_text)) if holdout else crit

        async def jev_route(text: str, c: dict) -> dict:
            """Jev's decision for this text, from the route cache when the same text met the same criteria before."""
            key = self.route_cache.key(text, c) if use_cache else None
            d = self.route_cache.get(key) if key else None
            if d is not None:
                clock['hits'] += 1
                return {**d, 'cached': True, 'jev_ms': 0, 'input_tokens': 0}
            d = await route_one(self.jev, text, c)
            if key:
                self.route_cache.put(key, d)
            return d

        # Speculative routing (A2): while the LLM planner works, Jev routes the whole query; when the plan comes back as
        # that same single step, its route is already done.
        spec: dict = {}

        def speculate():
            if not forced:
                spec['task'] = asyncio.create_task(jev_route(query + files_note, criteria_for(query, '', query)))

        tp0 = time.perf_counter()
        try:
            p = await plan(query, self.jev, engine, context, [f['name'] for f in attached], mode=mode, on_llm=speculate,
                           scores=self.multi_cache if use_cache else None)
        except BaseException:
            if spec.get('task'):
                spec['task'].cancel()
            raise
        clock['plan'] = (tp0, time.perf_counter())
        clock['planner'], clock['hits'] = plan_kind(p), clock['hits'] + bool(p.get('cached'))
        self.usage(rec, p)
        subtasks = [{'tid': f'{qid}.{n}', 'text': s, 'depends_on': [f'{qid}.{d + 1}' for d in deps]}
                    for n, (s, deps) in enumerate(zip(p['subtasks'], p['deps']), 1)]
        by_tid = {st['tid']: dict(st) for st in subtasks}
        rec.update(plan={'planner': p['planner'], 'subtasks': subtasks}, tasks=list(by_tid.values()))
        emit('plan', qid=qid, planner=p['planner'], subtasks=subtasks, multi=p['multi'], ms=ms_since(t0))
        if spec.get('task'):
            one = subtasks[0] if len(subtasks) == 1 else None
            if one is None or cache_mod.normalize(one['text']) != cache_mod.normalize(query):
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

        llm_helpers = engine is not None and mode != 'quick'  # quick mode: no LLM rewrite of steps, no LLM clarify

        async def prepare(st) -> tuple[str, str]:
            """(the subtask's own text, possibly made self-contained; the context from its dependencies, or '')."""
            if not st['depends_on']:
                return st['text'], ''
            lines = []
            for d in st['depends_on']:
                r = results[d]
                lines.append(f"- {by_tid[d]['text']}: " + (r['answer'][:CONTEXT_CHARS] if r['ok'] else
                                                            f"(this step failed: {r['answer'][:200]})"))
            ctx = '\n\nContext from earlier steps:\n' + '\n'.join(lines)
            earlier = [by_tid[d].get('input') or by_tid[d]['text'] for d in st['depends_on']]
            text = None
            if llm_helpers:
                try:
                    text, tin, tout = await resolve_step(engine, st['text'] + ctx)
                    self.usage(rec, {'claude_in': tin, 'claude_out': tout})
                except Exception:
                    text = None
            # Keyless (or when the rewrite left it as it was): "the time there" names the place the earlier steps were
            # about, a place or the country of the currency converted to, so the time or weather agent can parse it.
            if text is None or gate.THERE.search(text):
                text = gate.resolve_there(text or st['text'], earlier) or text
            if text is None:
                return st['text'], ctx
            by_tid[st['tid']]['input'] = text
            return text, ctx

        notes: dict[str, agent_registry.AgentResult] = {}  # tid -> the answer a gate decided (jevrouter/gate.py)
        hardness: dict[str, float | None] = {}  # tid -> Jev's hard score

        async def route(st, text, ctx):
            t1 = time.perf_counter()
            try:
                return await (route_forced(st, text, ctx) if forced else route_jev(st, text, ctx))
            finally:
                clock['route'].append((t1, time.perf_counter()))

        async def route_jev(st, text, ctx):
            term = None if ctx else gate.bare_term(text)
            lookup = asyncio.create_task(gate.meanings(self.http, term)) if term and self.http is not None else None
            try:
                d = None
                if spec.get('tid') == st['tid']:
                    try:
                        d = await spec.pop('task')
                    except Exception:
                        d = None  # the speculative call failed: ask again below, as if there had been none
                if d is None:
                    d = await jev_route(text + ctx + files_note, criteria_for(text, ctx, st.get('text')))
            except Exception as e:
                if lookup:
                    lookup.cancel()
                return route_failed(st, e)
            await review(st['tid'], d, text, lookup)
            if mode == 'research' and d['agent'] == 'knowledge' and 'research' in registry:
                d['agent'], d['reason'] = 'research', f"{d['reason']}; research mode searches the web"
            return routed(st, d)

        async def route_forced(st, text, ctx):
            """@agent: no route question, but Jev's safety check still runs, so an unsafe step is blocked anyway."""
            t1 = time.perf_counter()
            try:
                unsafe, tokens = await unsafe_score(self.jev, text + ctx + files_note)
            except Exception as e:
                return route_failed(st, e)
            blocked = unsafe >= BLOCK_AT
            d = {'agent': 'blocked' if blocked else forced, 'pick': forced,
                 'reason': f'Jev flagged it as unsafe ({unsafe:.0%})' if blocked else f'you picked @{forced}',
                 'probabilities': {forced: 1.0}, 'confidence': 1.0, 'urgency': 0.0, 'unsafe': unsafe, 'clear': 1.0,
                 'jev_ms': ms_since(t1), 'model': '', 'examples': False, 'input_tokens': tokens, 'forced': True}
            return routed(st, d)

        def route_failed(st, e):
            stats['errors'] += 1
            by_tid[st['tid']]['error'] = f'Jev: {str(e)[:200]}'
            emit('error', qid=qid, tid=st['tid'], message=by_tid[st['tid']]['error'])
            return None

        def routed(st, d):
            hardness[st['tid']] = d.get('hard')  # Jev's hard score picks the step's engine (A5); not a routed field
            self.usage(rec, {'jev_tokens': d['input_tokens']})
            stats['subtasks'] += 1
            stats['by_agent'][d['agent']] = stats['by_agent'].get(d['agent'], 0) + 1
            fields = {k: d[k] for k in ROUTED_KEYS}
            fields.update({k: True for k in ('cached', 'forced') if d.get(k)})  # only when true: older clients ignore them
            by_tid[st['tid']].update(fields)
            extra = {'input': by_tid[st['tid']]['input']} if 'input' in by_tid[st['tid']] else {}
            emit('routed', qid=qid, tid=st['tid'], **fields, **extra)
            return fields

        async def review(tid, d, text, lookup):
            """Checks Jev's decision against what the agents can actually do (jevrouter/gate.py). A blocked subtask stays
            blocked; the reason says which check changed the agent. `text` is what a keyless agent will be given (see
            `live` below), so its parser is checked on the same text."""
            try:
                if d['agent'] == 'blocked':
                    return
                # "Remind me at 5pm": an honest "I can't", not the current time
                if cant := gate.cant_do(text, d['pick'], d['probabilities'].get(d['pick'], 0)):
                    d['agent'], d['reason'] = 'chat', f"can't {cant[0]}"
                    notes[tid] = agent_registry.AgentResult(cant[1], True)
                    return
                pick = d['pick']
                if (d['agent'] == 'clarify' and pick in KEYLESS and pick in registry
                        and d['probabilities'].get(pick, 0) >= CONFIRM_AT and gate.confirmed(pick, text)):
                    d['agent'], d['reason'] = pick, f"{d['reason']}, but the {pick} parser found all it needs"
                # "the total in this spreadsheet" reads as vague to Jev, which sees only the text; the attached file is
                # the missing context, so a confident file-agent pick stands.
                if (d['agent'] == 'clarify' and attached and pick in (*FILE_AGENTS, *SQL_AGENT) and pick in agents
                        and d['probabilities'].get(pick, 0) >= CONFIRM_AT and d['unsafe'] < BLOCK_AT):
                    d['agent'], d['reason'] = pick, f"{d['reason']}, but a file is attached"
                try:
                    ask = None if d['agent'] in GUARDS else gate.question(d['agent'], text) or tool_question(d['agent'], text)
                except Exception:  # a parser bug must not fail the whole run; the step's agent reports its own error
                    ask = None
                if ask:
                    d['agent'], d['reason'] = 'clarify', f"missing detail for {d['agent']}"
                    notes[tid] = agent_registry.AgentResult(ask, False)
                if lookup and d['agent'] not in (*GUARDS, 'chat') and (options := await meanings_of(lookup)):
                    d['agent'], d['reason'] = 'clarify', f'ambiguous term ({len(options)} meanings)'
                    notes[tid] = agent_registry.AgentResult(gate.meanings_text(text.strip().rstrip('.!?'), options), False)
            finally:
                if lookup and not lookup.done():
                    lookup.cancel()

        async def meanings_of(lookup) -> list[str] | None:
            """The lone term's meanings, or None: a slow or failed lookup never fails or stalls the route."""
            try:
                return await asyncio.wait_for(lookup, MEANINGS_TIMEOUT)
            except Exception:  # includes the timeout; wait_for has cancelled the lookup
                return None

        async def ask(text, probabilities) -> agent_registry.AgentResult:
            """A clarify message: the engine writes the question when there is one, from a plain-English template."""
            msg = gate.clarify_text(probabilities, text, agents)
            if not llm_helpers:
                return agent_registry.AgentResult(msg, False)
            msg, tin, tout = await gate.clarify_llm(engine, text, msg)
            return agent_registry.AgentResult(msg, False, None, engine.name if tin or tout else 'keyless', tin, tout)

        async def run(st, r, text):
            tid, agent = st['tid'], r['agent']
            t1 = time.perf_counter()
            delta = lambda chunk: text_out(tid, chunk)
            efforts: list[str] = []
            token = agent_registry.EFFORTS.set(efforts)
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
                    else:
                        runner = registry.get(agent) if agent in KEYLESS else agent_for(agent, hardness.get(tid))
                        if runner is None:
                            raise KeyError(agent)
                        out = await runner(text, delta)
            except Exception as e:
                out = agent_registry.AgentResult(f'{agent} agent failed: {str(e)[:160]}', False)
                delta(out.answer)
            finally:
                agent_registry.EFFORTS.reset(token)
                clock['agents'].append((t1, time.perf_counter()))
            self.usage(rec, {'claude_in': out.claude_in, 'claude_out': out.claude_out})
            answered = {'agent': agent, 'agent_ms': ms_since(t1), 'answer': out.answer, 'ok': out.ok,
                        'source': out.source, 'engine': out.engine}
            checks = {}
            if hits[0]:
                clock['hits'] += hits[0]
                checks['cached'] = True
            llm_answer = agent not in GUARDS and tid not in notes and out.engine not in (None, 'keyless')
            if llm_answer and efforts:
                checks['effort'] = efforts[-1]
            if llm_answer and out.ok:  # A6: keyless recompute of numbers always; a grounding check in deep mode
                deep = mode == 'deep'
                checker = self.fastest(exclude={out.engine}) if deep and agent in verify_mod.GROUNDED else None
                try:
                    v, tin, tout = await verify_mod.verify(by_tid[tid].get('input') or st['text'], out.answer, agent=agent,
                                                           http=self.http, deep=deep, checker=checker, source=out.source)
                except Exception:  # a check must never cost the answer
                    v, tin, tout = (verify_mod.skipped('The check could not run.') if deep else {}), 0, 0
                self.usage(rec, {'claude_in': tin, 'claude_out': tout})
                checks.update(v)
            if checks:  # only when there is something to say: older clients ignore the key
                answered['checks'] = checks
            if agent in GUARDS or tid in notes or (agent in KEYLESS and out.engine == 'keyless'):
                exact.add(tid)
            by_tid[tid].update(answered)
            emit('answered', qid=qid, tid=tid, **answered)
            return answered

        def blocked_by_dependency(st) -> dict:
            tid = st['tid']
            fields = {'agent': 'blocked', 'pick': 'blocked', 'reason': 'depends on a blocked step', 'probabilities': {},
                      'confidence': 0.0, 'urgency': 0.0, 'unsafe': 1.0, 'clear': 0.0, 'jev_ms': 0, 'model': '',
                      'examples': False}
            stats['subtasks'] += 1
            stats['by_agent']['blocked'] = stats['by_agent'].get('blocked', 0) + 1
            by_tid[tid].update(fields)
            emit('routed', qid=qid, tid=tid, **fields)
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
            # keyless agents parse places and amounts from plain text: context would only be misread as the step's own
            # numbers ("convert that amount to EUR" + "100 USD = 8,400 INR" is not 100 USD to EUR)
            results[st['tid']] = await run(st, r, text if r['agent'] in KEYLESS else text + ctx)

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

        answers = [(st, results[st['tid']]) for st in subtasks if 'agent' in results[st['tid']]]
        if not answers:
            rec['error'] = 'Every subtask failed to route; see the errors above.'
            emit('error', qid=qid, tid=None, message=rec['error'])
            return 'error'
        # A2: an LLM merger adds nothing when every answer is exact; quick mode always joins by template, deep never does.
        all_exact = all(st['tid'] in exact for st, _ in answers)
        template = engine is None or mode == 'quick' or (mode != 'deep' and all_exact)
        t2 = time.perf_counter()
        m = await merge(query, [(a['agent'], a['answer']) for _, a in answers], lambda text: text_out('merge', text),
                        None if template else engine, style=style, steps=[st['text'] for st, _ in answers],
                        exact=all_exact)
        clock['merger'] = m['kind']
        if m['kind'] == 'llm':
            clock['merge'] = (t2, time.perf_counter())
        # A6: a check that disagreed with an answer shows as a short warning, never a silent wrong answer.
        if warn := verify_mod.warning([(st['text'], a.get('checks') or {}) for st, a in answers]):
            if m['kind'] != 'single':  # a lone answer streamed as its step; the merged text replaces it when it arrives
                text_out('merge', warn)
            m['answer'] += warn
        self.usage(rec, m)
        rec['merged'] = {'answer': m['answer'], 'engine': m['engine']}
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


def tool_question(agent: str, text: str) -> str | None:
    """The unit and date agents' follow-up question when their parser finds nothing to work with (gate.question covers
    the older agents), so "How much is a pound?" asks which meaning instead of failing."""
    return units_question(text) if agent == 'units' else dates_question(text) if agent == 'dates' else None


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
    rec['qid'] = qid
    return rec

"""handle(query): plan -> (route -> run) in dependency waves -> merge, emitting the SSE protocol in PLAN.md and docs/PLAN-v4.md."""
import asyncio
import copy
import itertools
import os
import random
import time
import uuid
from collections import deque

from . import agents as agent_registry
from . import gate
from .agents.llm import COMMON
from .config import AGENTS, CONFIRM_AT, GUARDS, HISTORY, KEYLESS, PRICES, REPORT, RESEARCH, RUN, RUN_TIMEOUT, SAMPLES, env_flag
from .engines.health import Health, instrument_all
from .events import Broadcaster
from .files import FILE_AGENTS, file_agents
from .jev import clean, criteria, route_one
from .merger import merge
from .planner import SCHEMA as PLAN_SCHEMA, SYSTEM as PLAN_SYSTEM, plan, resolve_step
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
                      'by_agent': {a: 0 for a in [*AGENTS, *RESEARCH, *REPORT, *RUN, *FILE_AGENTS, *GUARDS]}}
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

    def offered(self, engine, with_files: bool = False) -> dict:
        """The route Choice's criteria for a run on this engine: engine-only agents appear only when they can run."""
        a = dict(AGENTS)
        if engine is not None:
            a.update(RESEARCH if engine.supports_web else {})
            a.update(REPORT)
            a.update(RUN if getattr(engine, 'supports_exec', False) else {})
            a.update({c['name']: c['description'] for c in self.customs})
        return {**a, **(FILE_AGENTS if with_files else {})}

    def registry_for(self, engine) -> dict:
        if self.fixed_registry is not None:
            return {**agent_registry.extras(self.http, engine, self.customs), **self.fixed_registry}
        return agent_registry.build(self.http, engine, self.customs)

    def use_engine(self, engine):
        """Switch the LLM backend (or None for keyless). Runs already in flight keep the engine they started with."""
        self.engine = engine
        self.agents = self.offered(engine)
        self.registry = self.registry_for(engine)

    def reload_customs(self):
        self.customs = self.store.agents()
        self.use_engine(self.engine)

    def config(self) -> dict:
        e = self.engine
        # Subscription engines cost nothing per call here; the plan's own limits apply instead.
        prices = PRICES if e is None or e.billing == 'api' else {**PRICES, 'claude_in': 0.0, 'claude_out': 0.0}
        features = {'files': True, 'compare': True, 'evals': True, 'custom_agents': True,
                    'exec': bool(getattr(e, 'supports_exec', False))}
        return {'agents': {**self.agents, **FILE_AGENTS}, 'guards': GUARDS, 'claude': e is not None, 'state': self.state,
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
               sandbox: str | None = None, extras: dict | None = None, examples: bool | None = None) -> int:
        """Allocates the qid now (so POST /ask can return it) and runs the query in the background.
        With `sandbox` (a sandbox id) the run is ephemeral: nothing is stored, it doesn't count towards stats or
        history, and its events only reach that sandbox's event stream. A sandbox run's `extras` (all optional):
        `draft` (an unsaved custom agent dict), `files` ([(meta, text)] from sandbox memory), `replaces` (qid of the
        turn this edits) and `remember` (default True: read and write the sandbox's follow-up memory).
        `examples` forces route examples on or off for this run only (evals); None follows the global switch.
        An eval run's `extras` may carry `holdout` (the case query): with examples on, labels whose text is that query or
        the subtask being routed are left out of that route's criteria."""
        if sandbox:
            self.sandboxes.get(sandbox, create=True)  # marks it used, so the sweeper leaves it alone
            qid = next(self.sandbox_ids)
            self.sandbox[qid] = sandbox
            self.sandbox_stats[qid] = {'queries': 0, 'subtasks': 0, 'errors': 0, 'jev_input_tokens': 0,
                                       'claude_input_tokens': 0, 'claude_output_tokens': 0, 'by_agent': {}}
        else:
            qid = next(self.ids)
        task = asyncio.create_task(self.handle(query, source, qid, session_id=session_id, engine=engine, files=files,
                                               compare_id=compare_id, extras=extras, examples=examples))
        self.tasks.add(task)
        self.running[qid] = task
        task.add_done_callback(lambda t: (self.tasks.discard(t), self.running.pop(qid, None)))
        return qid

    def cancel(self, qid: int) -> str:
        """'ok' if the run was cancelled, 'finished' if it had already ended, 'unknown' if there's no such run."""
        task = self.running.get(qid)
        if task is not None and not task.done():
            task.cancel()
            return 'ok'
        return 'finished' if self.get_run(qid) else 'unknown'

    async def handle(self, query: str, source: str, qid: int | None = None, *, session_id=None, engine=USE_ACTIVE,
                     files=(), compare_id=None, extras: dict | None = None, examples: bool | None = None):
        qid = qid or next(self.ids)
        sandbox = self.sandbox.get(qid)
        extras = {**(extras or {}), 'examples': self.route_examples if examples is None else bool(examples)}
        engine = self.engine if engine is USE_ACTIVE else engine
        registry = self.registry if engine is self.engine else self.registry_for(engine)
        t0 = time.perf_counter()
        rec = self.inflight[qid] = {
            'qid': qid, 'text': query, 'source': source, 'at': time.time(), 'plan': None, 'tasks': [], 'merged': None,
            'total_ms': None, 'error': None, 'status': 'running', 'engine': engine.name if engine else None,
            'session_id': session_id, 'compare_id': compare_id, 'files': list(files),
            'tokens': {'jev_in': 0, 'llm_in': 0, 'llm_out': 0}}
        if not sandbox:
            if session_id:
                self.store.touch_session(session_id, query)
            self.store.save_run(rec)
        stats = self.stats_for(qid)
        emit = self.bus.emit
        try:
            async with asyncio.timeout(self.run_timeout):
                status = await self._handle(query, source, qid, t0, rec, engine, registry, extras)
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
        emit('done', qid=qid, total_ms=rec['total_ms'], stats=stats, status=status, tokens=rec['tokens'])
        self.inflight.pop(qid, None)
        if sandbox:
            self.remember_sandbox_turn(sandbox, rec, extras, status)
            self.sandbox.pop(qid, None)
            self.sandbox_stats.pop(qid, None)
        else:
            self.history.append(rec)
            self.store.save_run(rec)

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
            rec.update(source='chat', session_id=session_id, compare_id=None, files=[])
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

    async def _handle(self, query, source, qid, t0, rec, engine, registry, extras) -> str:
        emit = self.bus.emit
        emit('query', qid=qid, text=query, source=source, session_id=rec['session_id'], compare_id=rec['compare_id'],
             engine=rec['engine'], files=rec['files'])
        stats = self.stats_for(qid)
        stats['queries'] += 1

        sandbox = self.sandbox.get(qid)
        if sandbox:  # sandbox files come from memory (snapshotted at submit); nothing is read from the store
            attached = [meta for meta, _ in extras.get('files', ())]
            texts = {meta['id']: text for meta, text in extras.get('files', ())}
        else:
            attached = self.store.list_files(rec['files']) if rec['files'] else []
            texts = {f['id']: self.store.file_text(f['id']) for f in attached}
        agents = self.offered(engine, with_files=bool(attached))
        if attached:  # the file agents exist only for this run, bound to its files
            registry = {**file_agents(attached, texts, engine), **registry}
        draft = extras.get('draft')
        if draft and engine is not None:  # an unsaved custom agent: offered to Jev and runnable for this run only
            agents = {**agents, draft['name']: draft['description']}
            registry = {**registry, draft['name']: agent_registry.extras(self.http, engine, [draft])[draft['name']]}
        if sandbox:
            mem = self.sandboxes.peek(sandbox) if extras.get('remember', True) else None
            context = (mem.context(extras.get('replaces')) if mem else None) or None
        else:
            context = self.store.turns(rec['session_id'], qid, 3) if rec['session_id'] else None
        p = await plan(query, self.jev, engine, context, [f['name'] for f in attached])
        self.usage(rec, p)
        subtasks = [{'tid': f'{qid}.{n}', 'text': s, 'depends_on': [f'{qid}.{d + 1}' for d in deps]}
                    for n, (s, deps) in enumerate(zip(p['subtasks'], p['deps']), 1)]
        by_tid = {st['tid']: dict(st) for st in subtasks}
        rec.update(plan={'planner': p['planner'], 'subtasks': subtasks}, tasks=list(by_tid.values()))
        emit('plan', qid=qid, planner=p['planner'], subtasks=subtasks, multi=p['multi'], ms=ms_since(t0))

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
            if engine is not None:
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

        crit = self.criteria(agents) if extras['examples'] else agents  # one set of criteria for every subtask
        holdout = extras.get('holdout') if extras['examples'] else None  # an eval case's query: never its own example
        # Jev only sees text: without the file names, "the attached CSV" reads as unclear and gets gated to clarify.
        files_note = f"\n\n(Attached files: {', '.join(f['name'] for f in attached)})" if attached else ''

        notes: dict[str, agent_registry.AgentResult] = {}  # tid -> the answer a gate decided (jevrouter/gate.py)

        async def route(st, text, ctx):
            term = None if ctx else gate.bare_term(text)
            lookup = asyncio.create_task(gate.meanings(self.http, term)) if term and self.http is not None else None
            try:
                c = self.criteria_without(agents, (holdout, text + ctx, st.get('text'))) if holdout else crit
                d = await route_one(self.jev, text + ctx + files_note, c)
            except Exception as e:
                if lookup:
                    lookup.cancel()
                stats['errors'] += 1
                by_tid[st['tid']]['error'] = f'Jev: {str(e)[:200]}'
                emit('error', qid=qid, tid=st['tid'], message=by_tid[st['tid']]['error'])
                return None
            await review(st['tid'], d, text, lookup)
            self.usage(rec, {'jev_tokens': d['input_tokens']})
            stats['subtasks'] += 1
            stats['by_agent'][d['agent']] = stats['by_agent'].get(d['agent'], 0) + 1
            fields = {k: d[k] for k in ROUTED_KEYS}
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
                if d['agent'] not in GUARDS and (ask := gate.question(d['agent'], text)):
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
            if engine is None:
                return agent_registry.AgentResult(msg, False)
            msg, tin, tout = await gate.clarify_llm(engine, text, msg)
            return agent_registry.AgentResult(msg, False, None, engine.name if tin or tout else 'keyless', tin, tout)

        async def run(st, r, text):
            tid, agent = st['tid'], r['agent']
            t1 = time.perf_counter()
            delta = lambda chunk: chunk and emit('delta', qid=qid, tid=tid, text=chunk)
            try:
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
                    out = await registry[agent](text, delta)
            except Exception as e:
                out = agent_registry.AgentResult(f'{agent} agent failed: {str(e)[:160]}', False)
                delta(out.answer)
            self.usage(rec, {'claude_in': out.claude_in, 'claude_out': out.claude_out})
            answered = {'agent': agent, 'agent_ms': ms_since(t1), 'answer': out.answer, 'ok': out.ok,
                        'source': out.source, 'engine': out.engine}
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
            emit('delta', qid=qid, tid=tid, text=out.answer)
            answered = {'agent': 'blocked', 'agent_ms': 0, 'answer': out.answer, 'ok': out.ok, 'source': out.source,
                        'engine': out.engine}
            by_tid[tid].update(answered)
            emit('answered', qid=qid, tid=tid, **answered)
            return answered

        # Waves: everything whose dependencies have answered routes together, then runs together. Dependencies only
        # point backwards, so every wave has at least one subtask; independent subtasks all land in the first wave.
        results: dict[str, dict] = {}
        pending = list(subtasks)
        while pending:
            ready = [st for st in pending if all(d in results for d in st['depends_on'])]
            pending = [st for st in pending if st not in ready]
            # A step that builds on a blocked one is blocked too: its context would carry the harmful request on to
            # the rewrite, Jev and the agent ("How do I make a pipe bomb, then list its components").
            for st in [st for st in ready if any(results[d].get('agent') == 'blocked' for d in st['depends_on'])]:
                ready.remove(st)
                results[st['tid']] = blocked_by_dependency(st)
            inputs = await asyncio.gather(*(prepare(st) for st in ready))
            routes = await asyncio.gather(*(route(st, text, ctx) for st, (text, ctx) in zip(ready, inputs)))
            live = []
            for st, (text, ctx), r in zip(ready, inputs, routes):
                if r is None:
                    results[st['tid']] = {'ok': False, 'answer': by_tid[st['tid']]['error']}
                else:  # keyless agents parse places and amounts from plain text: context would only be misread as the
                    # step's own numbers ("convert that amount to EUR" + "100 USD = 8,400 INR" is not 100 USD to EUR)
                    live.append((st, r, text if r['agent'] in KEYLESS else text + ctx))
            for st, a in zip([x[0] for x in live], await asyncio.gather(*(run(*x) for x in live))):
                results[st['tid']] = a

        answers = [results[st['tid']] for st in subtasks if 'agent' in results[st['tid']]]
        t2 = time.perf_counter()
        if not answers:
            rec['error'] = 'Every subtask failed to route; see the errors above.'
            emit('error', qid=qid, tid=None, message=rec['error'])
            return 'error'
        m = await merge(query, [(a['agent'], a['answer']) for a in answers],
                        lambda text: text and emit('delta', qid=qid, tid='merge', text=text), engine)
        self.usage(rec, m)
        rec['merged'] = {'answer': m['answer'], 'engine': m['engine']}
        emit('merged', qid=qid, **rec['merged'], ms=ms_since(t2))
        return 'done'

    async def autopilot(self):
        while True:
            await asyncio.sleep(self.state['interval'])
            if self.state['autopilot'] and self.bus.subscribers:
                self.submit(random.choice(SAMPLES), 'autopilot')


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

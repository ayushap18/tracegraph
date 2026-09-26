"""handle(query): plan -> (route -> run) in dependency waves -> merge, emitting the SSE protocol in PLAN.md and docs/PLAN-v4.md."""
import asyncio
import itertools
import os
import random
import time
from collections import OrderedDict, deque

from . import agents as agent_registry
from .agents.llm import COMMON
from .config import AGENTS, GUARDS, HISTORY, KEYLESS, PRICES, REPORT, RESEARCH, RUN, RUN_TIMEOUT, SAMPLES
from .events import Broadcaster
from .files import FILE_AGENTS, file_agents
from .jev import route_one
from .merger import merge
from .planner import SCHEMA as PLAN_SCHEMA, SYSTEM as PLAN_SYSTEM, plan, resolve_step
from .store import Store

ROUTED_KEYS = ('agent', 'pick', 'reason', 'probabilities', 'confidence', 'urgency', 'unsafe', 'clear', 'jev_ms', 'model')
USE_ACTIVE = object()  # a run's engine argument: the engine active at submit time (None means keyless)
CONTEXT_CHARS = 500
# Sandbox runs are never stored. Their qids come from a separate range so they can't collide with
# (or leave gaps in) the persisted run numbers, and they're forgotten when they finish.
SANDBOX_QID0 = 1_000_000_000
SANDBOX_SESSIONS = 200  # most sandbox conversations kept in memory for follow-up context
SANDBOX_TURNS = 3       # turns of context a sandbox follow-up sees (same as a saved chat)


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
        # Sandbox: qid -> sandbox id for runs in flight, their scratch stats, and recent turns per sandbox (memory only).
        self.sandbox: dict[int, str] = {}
        self.sandbox_stats: dict[int, dict] = {}
        self.sandbox_turns: OrderedDict[str, deque] = OrderedDict()
        self.sandbox_ids = itertools.count(SANDBOX_QID0)

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
                'features': features}

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
               sandbox: str | None = None) -> int:
        """Allocates the qid now (so POST /ask can return it) and runs the query in the background.
        With `sandbox` (a sandbox id) the run is ephemeral: nothing is stored, it doesn't count towards stats or
        history, and its events only reach that sandbox's event stream."""
        if sandbox:
            qid = next(self.sandbox_ids)
            self.sandbox[qid] = sandbox
            self.sandbox_stats[qid] = {'queries': 0, 'subtasks': 0, 'errors': 0, 'jev_input_tokens': 0,
                                       'claude_input_tokens': 0, 'claude_output_tokens': 0, 'by_agent': {}}
        else:
            qid = next(self.ids)
        task = asyncio.create_task(self.handle(query, source, qid, session_id=session_id, engine=engine, files=files,
                                               compare_id=compare_id))
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
                     files=(), compare_id=None):
        qid = qid or next(self.ids)
        sandbox = self.sandbox.get(qid)
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
                status = await self._handle(query, source, qid, t0, rec, engine, registry)
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
            self.remember_sandbox_turn(sandbox, rec)
            self.sandbox.pop(qid, None)
            self.sandbox_stats.pop(qid, None)
        else:
            self.history.append(rec)
            self.store.save_run(rec)

    def remember_sandbox_turn(self, sid: str, rec: dict):
        """Keeps the last few turns of a sandbox conversation in memory, for follow-up context only."""
        turns = self.sandbox_turns.pop(sid, None) or deque(maxlen=SANDBOX_TURNS)
        turns.append({'query': rec['text'], 'answer': (rec.get('merged') or {}).get('answer') or rec.get('error') or ''})
        self.sandbox_turns[sid] = turns  # most recently used last
        while len(self.sandbox_turns) > SANDBOX_SESSIONS:
            self.sandbox_turns.popitem(last=False)

    def clear_sandbox(self, sid: str) -> int:
        """Forgets a sandbox: cancels its running queries and drops its remembered turns. Returns runs cancelled."""
        n = 0
        for qid, owner in list(self.sandbox.items()):
            if owner == sid and self.cancel(qid) == 'ok':
                n += 1
        self.sandbox_turns.pop(sid, None)
        return n

    def unfinished(self, qid: int, rec: dict, why: str):
        """Subtasks that never answered get an error, so no card is left spinning."""
        for t in rec['tasks']:
            if 'answer' not in t and 'error' not in t:
                t['error'] = f'{why} before it answered'
                self.bus.emit('error', qid=qid, tid=t['tid'], message=t['error'])

    async def _handle(self, query, source, qid, t0, rec, engine, registry) -> str:
        emit = self.bus.emit
        emit('query', qid=qid, text=query, source=source, session_id=rec['session_id'], compare_id=rec['compare_id'],
             engine=rec['engine'], files=rec['files'])
        stats = self.stats_for(qid)
        stats['queries'] += 1

        attached = self.store.list_files(rec['files']) if rec['files'] else []
        agents = self.offered(engine, with_files=bool(attached))
        if attached:  # the file agents exist only for this run, bound to its files
            registry = {**file_agents(attached, {f['id']: self.store.file_text(f['id']) for f in attached}, engine), **registry}
        sandbox = self.sandbox.get(qid)
        if sandbox:
            context = list(self.sandbox_turns.get(sandbox, ())) or None
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
            if engine is None:
                return st['text'], ctx
            try:
                text, tin, tout = await resolve_step(engine, st['text'] + ctx)
            except Exception:
                return st['text'], ctx
            self.usage(rec, {'claude_in': tin, 'claude_out': tout})
            by_tid[st['tid']]['input'] = text
            return text, ctx

        # Jev only sees text: without the file names, "the attached CSV" reads as unclear and gets gated to clarify.
        files_note = f"\n\n(Attached files: {', '.join(f['name'] for f in attached)})" if attached else ''

        async def route(st, text):
            try:
                d = await route_one(self.jev, text + files_note, agents)
            except Exception as e:
                stats['errors'] += 1
                by_tid[st['tid']]['error'] = f'Jev: {str(e)[:200]}'
                emit('error', qid=qid, tid=st['tid'], message=by_tid[st['tid']]['error'])
                return None
            self.usage(rec, {'jev_tokens': d['input_tokens']})
            stats['subtasks'] += 1
            stats['by_agent'][d['agent']] = stats['by_agent'].get(d['agent'], 0) + 1
            fields = {k: d[k] for k in ROUTED_KEYS}
            by_tid[st['tid']].update(fields)
            extra = {'input': by_tid[st['tid']]['input']} if 'input' in by_tid[st['tid']] else {}
            emit('routed', qid=qid, tid=st['tid'], **fields, **extra)
            return fields

        async def run(st, r, text):
            tid, agent = st['tid'], r['agent']
            t1 = time.perf_counter()
            delta = lambda chunk: chunk and emit('delta', qid=qid, tid=tid, text=chunk)
            try:
                if agent == 'blocked':
                    out = agent_registry.BLOCKED
                    delta(out.answer)
                elif agent == 'clarify':
                    out = agent_registry.clarify(list(r['probabilities'].items()))
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

        # Waves: everything whose dependencies have answered routes together, then runs together. Dependencies only
        # point backwards, so every wave has at least one subtask; independent subtasks all land in the first wave.
        results: dict[str, dict] = {}
        pending = list(subtasks)
        while pending:
            ready = [st for st in pending if all(d in results for d in st['depends_on'])]
            pending = [st for st in pending if st not in ready]
            inputs = await asyncio.gather(*(prepare(st) for st in ready))
            routes = await asyncio.gather(*(route(st, text + ctx) for st, (text, ctx) in zip(ready, inputs)))
            live = []
            for st, (text, ctx), r in zip(ready, inputs, routes):
                if r is None:
                    results[st['tid']] = {'ok': False, 'answer': by_tid[st['tid']]['error']}
                else:  # keyless agents parse places and amounts from the text, so they get the self-contained text alone
                    live.append((st, r, text if r['agent'] in KEYLESS and 'input' in by_tid[st['tid']] else text + ctx))
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

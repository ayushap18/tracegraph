"""handle(query): plan -> route all (parallel) -> run all (parallel) -> merge, emitting the SSE protocol in PLAN.md."""
import asyncio
import itertools
import random
import time
from collections import deque

from . import agents as agent_registry
from .config import AGENTS, GUARDS, HISTORY, PRICES, RESEARCH, SAMPLES
from .events import Broadcaster
from .jev import route_one
from .merger import merge
from .planner import plan

ROUTED_KEYS = ('agent', 'pick', 'reason', 'probabilities', 'confidence', 'urgency', 'unsafe', 'clear', 'jev_ms', 'model')


def ms_since(t: float) -> int:
    return round((time.perf_counter() - t) * 1000)


class Router:
    def __init__(self, jev, http=None, engine=None, bus: Broadcaster | None = None, registry: dict | None = None,
                 engines: dict | None = None):
        self.jev, self.http = jev, http
        self.bus = bus or Broadcaster()
        self.engines = engines or ({engine.name: engine} if engine is not None else {})
        self.fixed_registry = registry  # tests pin agents; real runs rebuild the registry when the engine changes
        self.use_engine(engine)
        self.ids = itertools.count(1)
        self.history: deque = deque(maxlen=HISTORY)
        self.inflight: dict[int, dict] = {}  # qid -> partial record, so a browser joining mid-run can replay it
        self.state = {'autopilot': False, 'interval': 3.0}
        self.stats = {'queries': 0, 'subtasks': 0, 'errors': 0, 'jev_input_tokens': 0, 'claude_input_tokens': 0,
                      'claude_output_tokens': 0, 'by_agent': {a: 0 for a in [*AGENTS, *RESEARCH, *GUARDS]}}
        self.tasks: set[asyncio.Task] = set()

    def use_engine(self, engine):
        """Switch the LLM backend (or None for keyless). Runs already in flight keep the engine they started with."""
        self.engine = engine
        web = engine is not None and engine.supports_web
        self.agents = {**AGENTS, **(RESEARCH if web else {})}
        self.registry = self.fixed_registry if self.fixed_registry is not None else agent_registry.build(self.http, engine)

    def config(self) -> dict:
        e = self.engine
        # Subscription engines cost nothing per call here; the plan's own limits apply instead.
        prices = PRICES if e is None or e.billing == 'api' else {**PRICES, 'claude_in': 0.0, 'claude_out': 0.0}
        return {'agents': self.agents, 'guards': GUARDS, 'claude': e is not None, 'state': self.state,
                'stats': self.stats, 'samples': SAMPLES, 'prices': prices,
                'engine': e.info() if e is not None else None, 'engines': [x.info() for x in self.engines.values()]}

    def hello(self) -> dict:
        # history fills in completion order; replay wants qid order, with in-flight runs included
        records = sorted([*self.history, *self.inflight.values()], key=lambda r: r['qid'])
        return {'type': 'hello', **self.config(), 'history': records[-HISTORY:]}

    def claude_usage(self, d):
        self.stats['claude_input_tokens'] += d.get('claude_in', 0)
        self.stats['claude_output_tokens'] += d.get('claude_out', 0)

    def submit(self, query: str, source: str) -> int:
        """Allocates the qid now (so POST /ask can return it) and runs the query in the background."""
        qid = next(self.ids)
        task = asyncio.create_task(self.handle(query, source, qid))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return qid

    async def handle(self, query: str, source: str, qid: int | None = None):
        qid = qid or next(self.ids)
        t0 = time.perf_counter()
        rec = self.inflight[qid] = {'qid': qid, 'text': query, 'source': source, 'at': time.time(), 'plan': None,
                                    'tasks': [], 'merged': None, 'total_ms': None, 'error': None}
        try:
            await self._handle(query, source, qid, t0, rec)
        except Exception as e:
            # Keep the browser's run from hanging: whatever broke, the query still ends with done.
            self.stats['errors'] += 1
            rec['error'] = str(e)[:200]
            self.bus.emit('error', qid=qid, tid=None, message=rec['error'])
            rec['total_ms'] = ms_since(t0)
            self.bus.emit('done', qid=qid, total_ms=rec['total_ms'], stats=self.stats)
        finally:
            self.history.append(self.inflight.pop(qid))

    async def _handle(self, query: str, source: str, qid: int, t0: float, rec: dict):
        emit = self.bus.emit
        emit('query', qid=qid, text=query, source=source)
        self.stats['queries'] += 1

        engine, registry = self.engine, self.registry  # pinned for this run even if the user switches mid-way
        p = await plan(query, self.jev, engine)
        self.stats['jev_input_tokens'] += p['jev_tokens']
        self.claude_usage(p)
        subtasks = [{'tid': f'{qid}.{n}', 'text': s} for n, s in enumerate(p['subtasks'], 1)]
        by_tid = {st['tid']: {'tid': st['tid'], 'text': st['text']} for st in subtasks}
        rec.update(plan={'planner': p['planner'], 'subtasks': subtasks}, tasks=list(by_tid.values()))
        emit('plan', qid=qid, planner=p['planner'], subtasks=subtasks, multi=p['multi'], ms=ms_since(t0))

        async def route(st):
            try:
                d = await route_one(self.jev, st['text'], self.agents)
            except Exception as e:
                self.stats['errors'] += 1
                by_tid[st['tid']]['error'] = f'Jev: {str(e)[:200]}'
                emit('error', qid=qid, tid=st['tid'], message=by_tid[st['tid']]['error'])
                return None
            self.stats['jev_input_tokens'] += d['input_tokens']
            self.stats['subtasks'] += 1
            self.stats['by_agent'][d['agent']] = self.stats['by_agent'].get(d['agent'], 0) + 1
            fields = {k: d[k] for k in ROUTED_KEYS}
            by_tid[st['tid']].update(fields)
            emit('routed', qid=qid, tid=st['tid'], **fields)
            return fields

        routes = await asyncio.gather(*(route(st) for st in subtasks))

        async def run(st, r):
            tid, agent = st['tid'], r['agent']
            t1 = time.perf_counter()
            delta = lambda text: text and emit('delta', qid=qid, tid=tid, text=text)
            try:
                if agent == 'blocked':
                    out = agent_registry.BLOCKED
                    delta(out.answer)
                elif agent == 'clarify':
                    out = agent_registry.clarify(list(r['probabilities'].items()))
                    delta(out.answer)
                else:
                    out = await registry[agent](st['text'], delta)
            except Exception as e:
                out = agent_registry.AgentResult(f'{agent} agent failed: {str(e)[:160]}', False)
                delta(out.answer)
            self.claude_usage({'claude_in': out.claude_in, 'claude_out': out.claude_out})
            answered = {'agent': agent, 'agent_ms': ms_since(t1), 'answer': out.answer, 'ok': out.ok,
                        'source': out.source, 'engine': out.engine}
            by_tid[tid].update(answered)
            emit('answered', qid=qid, tid=tid, **answered)
            return answered

        live = [(st, r) for st, r in zip(subtasks, routes) if r]
        answers = await asyncio.gather(*(run(st, r) for st, r in live))

        t2 = time.perf_counter()
        if answers:
            m = await merge(query, [(a['agent'], a['answer']) for a in answers],
                            lambda text: text and emit('delta', qid=qid, tid='merge', text=text), engine)
            self.claude_usage(m)
            rec['merged'] = {'answer': m['answer'], 'engine': m['engine']}
            emit('merged', qid=qid, **rec['merged'], ms=ms_since(t2))
        else:
            rec['error'] = 'Every subtask failed to route; see the errors above.'
            emit('error', qid=qid, tid=None, message=rec['error'])

        rec['total_ms'] = ms_since(t0)
        emit('done', qid=qid, total_ms=rec['total_ms'], stats=self.stats)

    async def autopilot(self):
        while True:
            await asyncio.sleep(self.state['interval'])
            if self.state['autopilot'] and self.bus.subscribers:
                self.submit(random.choice(SAMPLES), 'autopilot')

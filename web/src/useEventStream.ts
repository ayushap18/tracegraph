import { useCallback, useEffect, useReducer, useRef } from 'react'
import {
  MERGE_TID, emptyStats,
  type AnsweredFields, type ControlState, type EngineInfo, type Features, type HistoryRecord, type RunStatus, type MergeEngine, type Planner, type Prices,
  type RoutedFields, type ServerEvent, type Source, type Stats,
} from './protocol'

export const MAX_RUNS = 60

export interface Task {
  tid: string
  text: string
  routed?: RoutedFields
  answered?: AnsweredFields
  stream: string // deltas so far; replaced by the full answer on `answered`
  error?: string // Jev failed to route it; no routed/answered will follow
  depends_on: string[] // tids this step waits for (v4 DAG plans); [] for independent steps
}

export interface Run {
  qid: number
  text: string
  source: Source
  at: number // seconds
  plan?: { planner: Planner; multi: number | null; ms: number | null }
  order: string[]
  tasks: Record<string, Task>
  mergeStream: string
  merged?: { answer: string; engine: MergeEngine; ms: number | null }
  total_ms: number | null
  done: boolean
  error?: string
  status: RunStatus // v4: running until done; cancelled/timeout/error come from the server
  engine: string | null // per-run engine override, when one was given
  session_id: string | null
  compare_id: string | null
  files: string[]
  marks: Marks // client receipt times (performance.now ms); empty for runs loaded from history
}

// When each stage's event reached this browser, for the pipeline waterfall.
export interface Marks {
  query?: number
  plan?: number
  routed: Record<string, number>
  answered: Record<string, number>
  merged?: number
  done?: number
}

export interface Store {
  connected: boolean
  ready: boolean
  agents: Record<string, string>
  guards: string[]
  claude: boolean
  engine: EngineInfo | null
  engines: EngineInfo[]
  state: ControlState
  stats: Stats
  samples: string[]
  prices: Prices
  features: Features
  runs: Run[] // ascending qid, capped at MAX_RUNS
  lastError: string | null
  rev: number // bumps on every non-delta change, so charts can skip per-token redraws
}

type Action =
  | { kind: 'events'; events: Array<{ e: ServerEvent; rx: number }> }
  | { kind: 'conn'; connected: boolean }

const initial: Store = {
  connected: false, ready: false, agents: {}, guards: [], claude: false, engine: null, engines: [],
  state: { autopilot: false, interval: 3 }, stats: emptyStats(), samples: [],
  prices: { jev_in: 0.042, claude_in: 5, claude_out: 25 }, runs: [], lastError: null, rev: 0,
  features: { files: false, compare: false, evals: false, custom_agents: false, exec: false },
}

const newRun = (qid: number, text = '', source: Source = 'you'): Run => ({
  qid, text, source, at: Date.now() / 1000, order: [], tasks: {}, mergeStream: '', total_ms: null, done: false,
  status: 'running', engine: null, session_id: null, compare_id: null, files: [],
  marks: { routed: {}, answered: {} },
})

const newTask = (tid: string, text = ''): Task => ({ tid, text, stream: '', depends_on: [] })

/** Builds a client Run from a history/persisted record (hello.history, /api/runs, sessions, compare). */
export function fromRecord(r: HistoryRecord): Run {
  const run = newRun(r.qid, r.text, r.source)
  run.at = r.at
  if (r.plan) run.plan = { planner: r.plan.planner, multi: null, ms: null }
  for (const s of r.plan?.subtasks ?? []) { run.order.push(s.tid); run.tasks[s.tid] = { ...newTask(s.tid, s.text), depends_on: s.depends_on ?? [] } }
  for (const t of r.tasks ?? []) {
    const task = run.tasks[t.tid] ?? newTask(t.tid, t.text ?? '')
    if (!run.tasks[t.tid]) run.order.push(t.tid)
    if (t.error) task.error = t.error
    if (t.agent !== undefined && t.probabilities) {
      task.routed = {
        agent: t.agent, pick: t.pick ?? t.agent, reason: t.reason ?? '', probabilities: t.probabilities,
        confidence: t.confidence ?? 0, urgency: t.urgency ?? 0, unsafe: t.unsafe ?? 0, clear: t.clear ?? 0,
        jev_ms: t.jev_ms ?? 0, model: t.model ?? '',
      }
    }
    if (t.answer !== undefined) {
      task.answered = {
        agent: t.agent ?? '', agent_ms: t.agent_ms ?? 0, answer: t.answer, ok: t.ok ?? true,
        source: t.source ?? null, engine: t.engine ?? 'keyless',
      }
      task.stream = t.answer
    }
    run.tasks[t.tid] = task
  }
  if (r.merged) run.merged = { ...r.merged, ms: null }
  if (r.error) run.error = r.error
  run.total_ms = r.total_ms
  run.status = r.status ?? (r.total_ms == null ? 'running' : r.error ? 'error' : 'done')
  run.done = r.total_ms != null || (r.status != null && r.status !== 'running') // in-flight record: later live events finish it
  run.engine = r.engine ?? null
  run.session_id = r.session_id ?? null
  run.compare_id = r.compare_id ?? null
  run.files = r.files ?? []
  return run
}

// Runs are copied on write so React sees new identities only for the run that changed.
function withRun(runs: Run[], qid: number, fn: (r: Run) => Run): Run[] {
  const i = runs.findIndex(r => r.qid === qid)
  if (i >= 0) { const out = runs.slice(); out[i] = fn(runs[i]); return out }
  // Unknown qid (joined mid-run, or trimmed): keep the event on a stub run rather than dropping it.
  const out = [...runs, fn(newRun(qid))].sort((a, b) => a.qid - b.qid)
  return out.length > MAX_RUNS ? out.slice(-MAX_RUNS) : out
}

function withTask(run: Run, tid: string, fn: (t: Task) => Task): Run {
  const cur = run.tasks[tid] ?? newTask(tid)
  return { ...run, order: run.tasks[tid] ? run.order : [...run.order, tid], tasks: { ...run.tasks, [tid]: fn(cur) } }
}

function apply(s: Store, e: ServerEvent, rx: number): Store {
  switch (e.type) {
    case 'hello': {
      const runs = (e.history ?? []).map(fromRecord).sort((a, b) => a.qid - b.qid).slice(-MAX_RUNS)
      return {
        ...s, ready: true, agents: e.agents ?? {}, guards: e.guards ?? [], claude: !!e.claude, engine: e.engine ?? null, engines: e.engines ?? [], state: e.state,
        features: e.features ?? s.features,
        stats: e.stats ?? emptyStats(), samples: e.samples ?? [], prices: e.prices ?? s.prices, runs, lastError: null,
      }
    }
    case 'state': return { ...s, state: e.state }
    case 'config':
      return { ...s, agents: e.agents ?? s.agents, guards: e.guards ?? s.guards, claude: !!e.claude, engine: e.engine ?? null,
        engines: e.engines ?? s.engines, prices: e.prices ?? s.prices, samples: e.samples ?? s.samples, features: e.features ?? s.features }
    case 'query':
      return {
        ...s, runs: withRun(s.runs, e.qid, r => ({
          ...r, text: e.text, source: e.source, at: Date.now() / 1000, marks: { ...r.marks, query: rx },
          engine: e.engine ?? null, session_id: e.session_id ?? null, compare_id: e.compare_id ?? null, files: e.files ?? [],
        })),
      }
    case 'plan':
      return {
        ...s, runs: withRun(s.runs, e.qid, r => {
          let out: Run = { ...r, plan: { planner: e.planner, multi: e.multi, ms: e.ms }, marks: { ...r.marks, plan: rx } }
          for (const st of e.subtasks) out = withTask(out, st.tid, t => ({ ...t, text: st.text, depends_on: st.depends_on ?? [] }))
          return out
        }),
      }
    case 'routed': {
      const { type: _t, qid: _q, tid, ...routed } = e
      return { ...s, runs: withRun(s.runs, e.qid, r => withTask({ ...r, marks: { ...r.marks, routed: { ...r.marks.routed, [tid]: rx } } }, tid, t => ({ ...t, routed }))) }
    }
    case 'delta':
      return {
        ...s, runs: withRun(s.runs, e.qid, r => e.tid === MERGE_TID
          ? { ...r, mergeStream: r.mergeStream + e.text }
          : withTask(r, e.tid, t => ({ ...t, stream: t.stream + e.text }))),
      }
    case 'answered': {
      const { type: _t, qid: _q, tid, ...answered } = e
      return { ...s, runs: withRun(s.runs, e.qid, r => withTask({ ...r, marks: { ...r.marks, answered: { ...r.marks.answered, [tid]: rx } } }, tid, t => ({ ...t, answered, stream: answered.answer }))) }
    }
    case 'merged':
      return { ...s, runs: withRun(s.runs, e.qid, r => ({ ...r, merged: { answer: e.answer, engine: e.engine, ms: e.ms }, mergeStream: e.answer, marks: { ...r.marks, merged: rx } })) }
    case 'done':
      return {
        ...s, stats: e.stats ?? s.stats, runs: withRun(s.runs, e.qid, r => ({
          ...r, done: true, total_ms: e.total_ms, marks: { ...r.marks, done: rx },
          // `cancelled` arrives before `done`; keep it if done carries no status (older servers).
          status: e.status ?? (r.status === 'running' ? (r.error ? 'error' : 'done') : r.status),
        })),
      }
    case 'cancelled':
      return { ...s, runs: withRun(s.runs, e.qid, r => ({ ...r, status: 'cancelled' })) }
    case 'error': {
      // A tid error belongs to that task (it never gets routed); only query-level errors mark the run.
      const runs = e.qid == null ? s.runs : withRun(s.runs, e.qid, r => e.tid
        ? withTask(r, e.tid, t => ({ ...t, error: e.message }))
        : { ...r, error: e.message })
      return { ...s, runs, lastError: e.message }
    }
    default: return s
  }
}

function reducer(s: Store, a: Action): Store {
  if (a.kind === 'conn') return { ...s, connected: a.connected }
  let out = s
  let structural = false
  for (const { e, rx } of a.events) { out = apply(out, e, rx); if (e.type !== 'delta') structural = true }
  return structural ? { ...out, rev: s.rev + 1 } : out
}

export type Listener = (e: ServerEvent) => void

// Owns the EventSource. Deltas arrive per token, so events are batched into one dispatch per animation frame.
export function useEventStream(url = '/events') {
  const [store, dispatch] = useReducer(reducer, initial)
  const listeners = useRef(new Set<Listener>())
  const subscribe = useCallback((fn: Listener) => {
    listeners.current.add(fn)
    return () => { listeners.current.delete(fn) }
  }, [])

  useEffect(() => {
    let queue: Array<{ e: ServerEvent; rx: number }> = []
    let raf = 0, timer = 0, retry = 0, backoff = 1000
    let es: EventSource | null = null
    const flush = () => {
      if (raf) cancelAnimationFrame(raf)
      if (timer) clearTimeout(timer)
      raf = timer = 0
      const batch = queue; queue = []
      if (batch.length) dispatch({ kind: 'events', events: batch })
    }
    // rAF is paused in background tabs, so fall back to a timer there rather than buffer without limit.
    const schedule = () => {
      if (raf || timer) return
      if (document.hidden) timer = window.setTimeout(flush, 250)
      else raf = requestAnimationFrame(flush)
    }
    const connect = () => {
      retry = 0
      const src = es = new EventSource(url)
      src.onopen = () => { backoff = 1000; dispatch({ kind: 'conn', connected: true }) }
      src.onerror = () => {
        dispatch({ kind: 'conn', connected: false })
        // An HTTP error or wrong content type closes the stream for good; the browser only retries dropped streams.
        if (src.readyState === EventSource.CLOSED && !retry) {
          src.close()
          retry = window.setTimeout(connect, backoff)
          backoff = Math.min(backoff * 2, 10000)
        }
      }
      src.onmessage = m => {
        let e: ServerEvent
        try { e = JSON.parse(m.data) as ServerEvent } catch { return }
        if (!e || typeof e !== 'object' || !('type' in e)) return
        queue.push({ e, rx: performance.now() })
        for (const fn of listeners.current) { try { fn(e) } catch (err) { console.error(err) } }
        if (e.type === 'hello') flush() // reconnect: reset immediately so later events land on fresh state
        else schedule()
      }
    }
    connect()
    return () => {
      es?.close()
      if (raf) cancelAnimationFrame(raf)
      if (timer) clearTimeout(timer)
      if (retry) clearTimeout(retry)
    }
  }, [url])

  return { store, subscribe }
}

export async function post<T = unknown>(url: string, body: unknown): Promise<T | null> {
  try {
    const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
    return r.ok ? (await r.json()) as T : null
  } catch { return null }
}

import { useCallback, useEffect, useReducer, useRef } from 'react'
import {
  MERGE_TID, emptyStats,
  type AnsweredFields, type ControlState, type HistoryRecord, type MergeEngine, type Planner, type Prices,
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
}

export interface Store {
  connected: boolean
  ready: boolean
  agents: Record<string, string>
  guards: string[]
  claude: boolean
  state: ControlState
  stats: Stats
  samples: string[]
  prices: Prices
  runs: Run[] // ascending qid, capped at MAX_RUNS
  lastError: string | null
  rev: number // bumps on every non-delta change, so charts can skip per-token redraws
}

type Action =
  | { kind: 'events'; events: ServerEvent[] }
  | { kind: 'conn'; connected: boolean }

const initial: Store = {
  connected: false, ready: false, agents: {}, guards: [], claude: false,
  state: { autopilot: false, interval: 3 }, stats: emptyStats(), samples: [],
  prices: { jev_in: 0.042, claude_in: 5, claude_out: 25 }, runs: [], lastError: null, rev: 0,
}

const newRun = (qid: number, text = '', source: Source = 'you'): Run => ({
  qid, text, source, at: Date.now() / 1000, order: [], tasks: {}, mergeStream: '', total_ms: null, done: false,
})

function fromRecord(r: HistoryRecord): Run {
  const run = newRun(r.qid, r.text, r.source)
  run.at = r.at
  if (r.plan) run.plan = { planner: r.plan.planner, multi: null, ms: null }
  for (const s of r.plan?.subtasks ?? []) { run.order.push(s.tid); run.tasks[s.tid] = { tid: s.tid, text: s.text, stream: '' } }
  for (const t of r.tasks ?? []) {
    const task = run.tasks[t.tid] ?? { tid: t.tid, text: t.text ?? '', stream: '' }
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
  run.done = r.total_ms != null // in-flight record: later live events finish it
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
  const cur = run.tasks[tid] ?? { tid, text: '', stream: '' }
  return { ...run, order: run.tasks[tid] ? run.order : [...run.order, tid], tasks: { ...run.tasks, [tid]: fn(cur) } }
}

function apply(s: Store, e: ServerEvent): Store {
  switch (e.type) {
    case 'hello': {
      const runs = (e.history ?? []).map(fromRecord).sort((a, b) => a.qid - b.qid).slice(-MAX_RUNS)
      return {
        ...s, ready: true, agents: e.agents ?? {}, guards: e.guards ?? [], claude: !!e.claude, state: e.state,
        stats: e.stats ?? emptyStats(), samples: e.samples ?? [], prices: e.prices ?? s.prices, runs, lastError: null,
      }
    }
    case 'state': return { ...s, state: e.state }
    case 'query':
      return { ...s, runs: withRun(s.runs, e.qid, r => ({ ...r, text: e.text, source: e.source })) }
    case 'plan':
      return {
        ...s, runs: withRun(s.runs, e.qid, r => {
          let out: Run = { ...r, plan: { planner: e.planner, multi: e.multi, ms: e.ms } }
          for (const st of e.subtasks) out = withTask(out, st.tid, t => ({ ...t, text: st.text }))
          return out
        }),
      }
    case 'routed': {
      const { type: _t, qid: _q, tid, ...routed } = e
      return { ...s, runs: withRun(s.runs, e.qid, r => withTask(r, tid, t => ({ ...t, routed }))) }
    }
    case 'delta':
      return {
        ...s, runs: withRun(s.runs, e.qid, r => e.tid === MERGE_TID
          ? { ...r, mergeStream: r.mergeStream + e.text }
          : withTask(r, e.tid, t => ({ ...t, stream: t.stream + e.text }))),
      }
    case 'answered': {
      const { type: _t, qid: _q, tid, ...answered } = e
      return { ...s, runs: withRun(s.runs, e.qid, r => withTask(r, tid, t => ({ ...t, answered, stream: answered.answer }))) }
    }
    case 'merged':
      return { ...s, runs: withRun(s.runs, e.qid, r => ({ ...r, merged: { answer: e.answer, engine: e.engine, ms: e.ms }, mergeStream: e.answer })) }
    case 'done':
      return { ...s, stats: e.stats ?? s.stats, runs: withRun(s.runs, e.qid, r => ({ ...r, done: true, total_ms: e.total_ms })) }
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
  for (const e of a.events) { out = apply(out, e); if (e.type !== 'delta') structural = true }
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
    let queue: ServerEvent[] = []
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
        queue.push(e)
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

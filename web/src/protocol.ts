// Mirrors the SSE protocol table in PLAN.md. Server -> browser, `data: <json>\n\n` on GET /events.

export type Source = 'you' | 'autopilot' | 'chat' | 'compare' | 'eval'
// Planner, answer and merge engines carry the LLM engine's name (claude-code, codex, agy, anthropic) or a built-in mode.
export type Planner = string // engine name | 'heuristic'
export type Engine = string // engine name | 'keyless'
export type MergeEngine = string // engine name | 'concat' | 'single'

export interface EngineInfo {
  name: string; label: string; billing: 'api' | 'subscription'; web: boolean; available: boolean; why: string
  // Auto only: the order it tries engines in, the one it will try first, and engines skipped after a recent failure.
  order?: string[]; lead?: string | null; cooling?: Record<string, string>
}

export interface ControlState { autopilot: boolean; interval: number }

export interface Stats {
  queries: number
  subtasks: number
  errors: number
  jev_input_tokens: number
  claude_input_tokens: number
  claude_output_tokens: number
  by_agent: Record<string, number> // counts subtasks, guards included
}

export interface Prices { jev_in: number; claude_in: number; claude_out: number }

export interface Subtask { tid: string; text: string; depends_on?: string[] } // depends_on: tids this step waits for (v4)

export interface RoutedFields {
  agent: string
  pick: string
  reason: string
  probabilities: Record<string, number> // sorted desc
  confidence: number
  urgency: number
  unsafe: number
  clear: number
  jev_ms: number
  model: string
}

export interface AnsweredFields {
  agent: string
  agent_ms: number
  answer: string
  ok: boolean
  source: string | null
  engine: Engine
}

// One entry of `hello.history`, ascending qid, max 60; queries still in flight have total_ms null.
export interface HistoryRecord {
  qid: number
  text: string
  source: Source
  at: number
  plan: { planner: Planner; subtasks: Subtask[] } | null
  tasks: Array<{ tid: string; text?: string; error?: string } & Partial<RoutedFields> & Partial<AnsweredFields>>
  merged: { answer: string; engine: MergeEngine } | null
  total_ms: number | null
  error?: string | null
  // v4 (additive): absent on records from older servers.
  status?: RunStatus
  engine?: string | null
  session_id?: string | null
  compare_id?: string | null
  files?: string[]
}

export type RunStatus = 'running' | 'done' | 'cancelled' | 'timeout' | 'error'

// A persisted run as returned by /api/runs, /api/sessions/:id and /api/compare/:id: the history record plus v4 fields.
export interface RunRecord extends HistoryRecord {
  status: RunStatus
  engine: string | null
  session_id: string | null
  compare_id: string | null
  files: string[]
}

export interface Features { files: boolean; compare: boolean; evals: boolean; custom_agents: boolean; exec: boolean }

export interface HelloEvent {
  type: 'hello'
  agents: Record<string, string> // only the agents active now
  guards: string[]
  claude: boolean // an LLM engine is active (kept for older clients)
  engine?: EngineInfo | null
  engines?: EngineInfo[]
  state: ControlState
  stats: Stats
  history: HistoryRecord[]
  samples: string[]
  prices: Prices
  features?: Features // v4
}
export interface StateEvent { type: 'state'; state: ControlState }
export interface ConfigEvent extends Omit<HelloEvent, 'type' | 'history'> { type: 'config' } // engine switched
export interface QueryEvent {
  type: 'query'; qid: number; text: string; source: Source
  session_id?: string | null; compare_id?: string | null; engine?: string | null; files?: string[] // v4
}
export interface PlanEvent { type: 'plan'; qid: number; planner: Planner; subtasks: Subtask[]; multi: number | null; ms: number }
export interface RoutedEvent extends RoutedFields { type: 'routed'; qid: number; tid: string }
export interface DeltaEvent { type: 'delta'; qid: number; tid: string; text: string } // tid may be "merge"
export interface AnsweredEvent extends AnsweredFields { type: 'answered'; qid: number; tid: string }
export interface MergedEvent { type: 'merged'; qid: number; answer: string; engine: MergeEngine; ms: number }
export interface DoneEvent { type: 'done'; qid: number; total_ms: number; stats: Stats; status?: RunStatus }
export interface CancelledEvent { type: 'cancelled'; qid: number }
export interface EvalProgressEvent { type: 'eval_progress'; eval_id: string; done: number; total: number; passed: number }
export interface EvalDoneEvent { type: 'eval_done'; eval_id: string; passed: number; total: number; accuracy: number; status?: 'done' | 'cancelled' | 'error' }
export interface ErrorEvent { type: 'error'; qid: number | null; tid: string | null; message: string }

export type ServerEvent =
  | HelloEvent | StateEvent | ConfigEvent | QueryEvent | PlanEvent | RoutedEvent
  | DeltaEvent | AnsweredEvent | MergedEvent | DoneEvent | ErrorEvent
  | CancelledEvent | EvalProgressEvent | EvalDoneEvent

export type ConfigBody = Omit<HelloEvent, 'type' | 'history'>

// ---------- REST payloads (§1) ----------

export interface AskBody { query: string; session_id?: string; engine?: string; files?: string[]; source?: 'you' | 'chat' | 'compare' | 'eval' }
export interface AskResponse { ok: true; qid: number; session_id: string | null }

export interface SessionSummary { id: string; title: string; created: number; updated: number; turns: number }
export interface SessionDetail { id: string; title: string; runs: RunRecord[] }

export type AgentKind = 'builtin' | 'custom' | 'guard'
export interface AgentInfo { name: string; description: string; kind: AgentKind; engine_required: boolean; available: boolean; prompt?: string; web?: boolean }
export interface NewAgent { name: string; description: string; prompt: string; web?: boolean }

export type FileKind = 'text' | 'csv' | 'pdf' | 'json'
export interface FileInfo { id: string; name: string; size: number; kind: FileKind; chars: number; rows?: number; columns?: string[] }

export interface CompareResponse { compare_id: string; runs: Array<{ engine: string; qid: number }> }
export interface CompareDetail { compare_id: string; query: string; runs: RunRecord[] }

export type EvalStatus = 'running' | 'done' | 'cancelled' | 'error'
export interface EvalSummary { eval_id: string; at: number; engine: string | null; status: EvalStatus; passed: number; total: number; accuracy: number; silent_wrong: number; done?: number }
export interface EvalCase { id: string; query: string; tags: string[]; pass: boolean; reasons: string[]; agents: string[]; answer: string; ms: number; qid: number | null; expect_agents?: string[] | null; expect_outcome?: string | null }
export interface EvalDetail extends EvalSummary { cases: EvalCase[] }

export interface EngineTestResult { ok: boolean; ms: number; text?: string; error?: string }
export interface ControlBody { autopilot?: boolean; interval?: number; engine?: string; engine_order?: string[] }
export interface ControlResponse extends ControlState { engine: string | null }

export const MERGE_TID = 'merge'
export const MIN_CONFIDENCE = 0.45

export const COLORS: Record<string, string> = {
  math: '#4f7cff', weather: '#17a9bd', time: '#9466ff', currency: '#23a864', knowledge: '#d99a06',
  code: '#e8622f', chat: '#d4549f', research: '#0ea5a4', clarify: '#8a8f9c', blocked: '#e0443a',
  document: '#6d7cf5', data: '#2f9e8f', report: '#b0762a', run: '#c2410c',
}
const FALLBACK = ['#7a6cf0', '#3d9970', '#c0587e', '#b8860b', '#5a8fa8']
export function colorOf(agent: string | undefined): string {
  if (!agent) return '#8a8f9c'
  if (COLORS[agent]) return COLORS[agent]
  let h = 0
  for (const c of agent) h = (h * 31 + c.charCodeAt(0)) >>> 0
  return FALLBACK[h % FALLBACK.length]
}

export const emptyStats = (): Stats => ({
  queries: 0, subtasks: 0, errors: 0, jev_input_tokens: 0, claude_input_tokens: 0, claude_output_tokens: 0, by_agent: {},
})

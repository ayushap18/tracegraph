// Mirrors the SSE protocol table in PLAN.md. Server -> browser, `data: <json>\n\n` on GET /events.

export type Source = 'you' | 'autopilot'
// Planner, answer and merge engines carry the LLM engine's name (claude-code, codex, agy, anthropic) or a built-in mode.
export type Planner = string // engine name | 'heuristic'
export type Engine = string // engine name | 'keyless'
export type MergeEngine = string // engine name | 'concat' | 'single'

export interface EngineInfo { name: string; label: string; billing: 'api' | 'subscription'; web: boolean; available: boolean; why: string }

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

export interface Subtask { tid: string; text: string }

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
}

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
}
export interface StateEvent { type: 'state'; state: ControlState }
export interface ConfigEvent extends Omit<HelloEvent, 'type' | 'history'> { type: 'config' } // engine switched
export interface QueryEvent { type: 'query'; qid: number; text: string; source: Source }
export interface PlanEvent { type: 'plan'; qid: number; planner: Planner; subtasks: Subtask[]; multi: number | null; ms: number }
export interface RoutedEvent extends RoutedFields { type: 'routed'; qid: number; tid: string }
export interface DeltaEvent { type: 'delta'; qid: number; tid: string; text: string } // tid may be "merge"
export interface AnsweredEvent extends AnsweredFields { type: 'answered'; qid: number; tid: string }
export interface MergedEvent { type: 'merged'; qid: number; answer: string; engine: MergeEngine; ms: number }
export interface DoneEvent { type: 'done'; qid: number; total_ms: number; stats: Stats }
export interface ErrorEvent { type: 'error'; qid: number | null; tid: string | null; message: string }

export type ServerEvent =
  | HelloEvent | StateEvent | ConfigEvent | QueryEvent | PlanEvent | RoutedEvent
  | DeltaEvent | AnsweredEvent | MergedEvent | DoneEvent | ErrorEvent

export type ConfigBody = Omit<HelloEvent, 'type' | 'history'>

export const MERGE_TID = 'merge'
export const MIN_CONFIDENCE = 0.45

export const COLORS: Record<string, string> = {
  math: '#4f7cff', weather: '#17a9bd', time: '#9466ff', currency: '#23a864', knowledge: '#d99a06',
  code: '#e8622f', chat: '#d4549f', research: '#0ea5a4', clarify: '#8a8f9c', blocked: '#e0443a',
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

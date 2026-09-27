// Mirrors the SSE protocol table in PLAN.md. Server -> browser, `data: <json>\n\n` on GET /events.

export type Source = 'you' | 'autopilot' | 'chat' | 'compare' | 'eval' | 'sandbox'
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
  examples?: boolean // learning plan: route examples were in Jev's criteria for this decision
  cached?: boolean // speed plan: this decision came from the routing cache
  forced?: boolean // chat: the user picked the agent with @agent
}

export interface AnsweredFields {
  agent: string
  agent_ms: number
  answer: string
  ok: boolean
  source: string | null
  engine: Engine
  checks?: AnswerChecks // speed plan: verify result, cache hit, effort used
  created_files?: CreatedFile[] // files plan: files the create agent made in this step
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
  mode?: ChatMode; style?: AnswerStyle; agent?: string | null // agent: forced with @agent
  group_id?: string | null; chosen?: boolean // several answers: only the chosen run of a group feeds follow-ups
  timings?: RunTimings
}

export interface Features { files: boolean; compare: boolean; evals: boolean; custom_agents: boolean; exec: boolean }

export interface HelloEvent {
  type: 'hello'
  agents: Record<string, string> // only the agents active now
  guards: string[]
  claude: boolean // an LLM engine is active (kept for older clients)
  engine?: EngineInfo | null
  engines?: EngineInfo[]
  route_examples?: boolean // learning plan: corrections are fed to Jev as examples
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
export interface RunTokens { jev_in: number; llm_in: number; llm_out: number }
export interface DoneEvent { type: 'done'; qid: number; total_ms: number; stats: Stats; status?: RunStatus; tokens?: RunTokens; timings?: RunTimings }
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

export interface DraftAgent { name: string; description: string; prompt: string; web?: boolean }
export interface AskBody {
  query: string; session_id?: string; engine?: string; files?: string[]; source?: 'you' | 'chat' | 'compare' | 'eval' | 'sandbox'
  // sandbox only (docs/PLAN-sandbox.md)
  sandbox_id?: string; draft_agent?: DraftAgent; replaces?: number; remember?: boolean
  // chat variety (docs/PLAN-speed-evals-chat.md)
  mode?: ChatMode; style?: AnswerStyle
  agent?: string       // "@agent": skip routing, send every step to this offered agent
  engines?: string[]   // 2-3 engines: one run per engine in a new answer group (response carries qids + group_id)
  retry_of?: number    // another answer for that run's question, added to its group (use with `engine`)
}
export interface AskResponse { ok: true; qid: number; session_id: string | null; sandbox_id?: string; qids?: number[]; group_id?: string }

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
export interface EvalSummary extends EvalSummaryExtra { eval_id: string; at: number; engine: string | null; status: EvalStatus; passed: number; total: number; accuracy: number; silent_wrong: number; done?: number; examples?: boolean }
export interface EvalCase extends EvalCaseExtra { id: string; query: string; tags: string[]; pass: boolean; reasons: string[]; agents: string[]; answer: string; ms: number; qid: number | null; expect_agents?: string[] | null; expect_outcome?: string | null }
export interface EvalDetail extends EvalSummary { cases: EvalCase[] }

export interface EngineTestResult { ok: boolean; ms: number; text?: string; error?: string }
export interface ControlBody { autopilot?: boolean; interval?: number; engine?: string; engine_order?: string[]; route_examples?: boolean }
export interface ControlResponse extends ControlState { engine: string | null }

export const MERGE_TID = 'merge'
export const MIN_CONFIDENCE = 0.45

// Agent colours are theme tokens (--agent-<name> in theme.css, lighter in dark mode).
// colorOf() returns a CSS value, usable in style props, SVG fill/stroke and color-mix();
// the hex after the comma is only a fallback. Use colorHex() when JS needs the real value.
export const COLORS: Record<string, string> = {
  math: '#4f7cff', weather: '#1597a9', time: '#8b5cf6', currency: '#1f9d5a', knowledge: '#c98a06',
  code: '#e0582a', chat: '#cf4b97', research: '#0e9594', clarify: '#80858f', blocked: '#d93a30',
  document: '#6371f0', data: '#2a8f82', report: '#a86d24', run: '#c2410c',
}
const FALLBACK = ['#7a6cf0', '#3d9970', '#c0587e', '#b8860b', '#5a8fa8']
export function colorOf(agent: string | undefined): string {
  if (!agent) return 'var(--agent-clarify, #80858f)'
  if (COLORS[agent]) return `var(--agent-${agent}, ${COLORS[agent]})`
  let h = 0
  for (const c of agent) h = (h * 31 + c.charCodeAt(0)) >>> 0
  const i = h % FALLBACK.length
  return `var(--agent-fallback-${i}, ${FALLBACK[i]})`
}
/** The resolved colour for the current theme, for code that has to compute with it. */
export function colorHex(agent: string | undefined): string {
  const v = colorOf(agent)
  const m = /var\((--[\w-]+), ([^)]+)\)/.exec(v)
  if (!m || typeof document === 'undefined') return v
  return getComputedStyle(document.documentElement).getPropertyValue(m[1]).trim() || m[2]
}

export const emptyStats = (): Stats => ({
  queries: 0, subtasks: 0, errors: 0, jev_input_tokens: 0, claude_input_tokens: 0, claude_output_tokens: 0, by_agent: {},
})

// ---------- routing that learns (docs/PLAN-learning.md) ----------
export type Verdict = 'right' | 'wrong'
/** One user judgement of one routing decision (one per run subtask). `correct` = picked when verdict is 'right'. */
export interface Label {
  id: string; qid: number; tid: string; text: string; picked: string; correct: string; verdict: Verdict
  confidence: number; margin: number; note: string | null; at: number; promoted: string | null
}
export interface NewLabel { qid: number; tid: string; verdict: Verdict; correct?: string; note?: string }
export type ShakyReason = 'low confidence' | 'low margin' | 'clarify' | 'agent failed' | 're-asked'
/** A saved, unlabelled subtask whose routing looked doubtful. */
export interface ReviewItem {
  qid: number; tid: string; text: string; picked: string; confidence: number; margin: number
  runner_up: string | null; probabilities: Record<string, number>; reasons: ShakyReason[]; at: number
}
/** Examples Jev sees for one agent when route examples are on. */
export interface AgentExamples { agent: string; examples: string[]; not: string[] }
export interface EvalCompareCase { id: string; query: string; a_pass: boolean | null; b_pass: boolean | null }
export interface EvalCompareSide { eval_id: string; engine: string | null; examples: boolean; accuracy: number; passed: number; total: number; silent_wrong: number; mean_jev_tokens: number }
export interface EvalCompare { a: EvalCompareSide; b: EvalCompareSide; cases: EvalCompareCase[] }
export interface EngineHealth {
  name: string; label: string; calls: number; ok: number; fallbacks_from: number; fallbacks_to: number
  p50_ms: number | null; p95_ms: number | null; last_error: string | null; cooling_until: number | null
}

// ---------- speed, harder evals, chat variety (docs/PLAN-speed-evals-chat.md) ----------
export type ChatMode = 'quick' | 'balanced' | 'deep' | 'research'
export type AnswerStyle = 'default' | 'concise' | 'detailed' | 'bullets' | 'steps' | 'simple' | 'table'
/** Milliseconds per stage of one run. null = the stage did not run (e.g. no LLM planner, template merge). */
export interface RunTimings {
  plan_ms: number | null; route_ms: number | null; agents_ms: number | null; merge_ms: number | null
  first_token_ms: number | null // query start -> first answer text shown
  planner: 'llm' | 'heuristic' | 'single'; merger: 'llm' | 'template' | 'single'
  cache_hits: number
}
/** Per-subtask extras on `answered` events and stored tasks. */
export interface AnswerChecks { verified?: 'ok' | 'mismatch' | 'skipped'; verify_note?: string | null; cached?: boolean; effort?: 'low' | 'medium' | 'high' }
export interface StageStats { stage: 'plan' | 'route' | 'agents' | 'merge' | 'first_token' | 'total'; p50: number | null; p90: number | null; n: number }
/** GET /api/timings?engine=&limit= : per-stage percentiles over recent saved runs. */
export interface TimingsSummary { engine: string | null; runs: number; stages: StageStats[] }

export type EvalSplit = 'dev' | 'holdout' | 'all'
export interface RunEvalBody { engine?: string; examples?: boolean; split?: EvalSplit; tags?: string[]; repeat?: number; judge?: string | null }
export interface TagScore { passed: number; total: number }
/** Extra fields on eval summaries (all optional so older stored evals still parse). */
export interface EvalSummaryExtra {
  split?: EvalSplit; repeat?: number; judge?: string | null; tags?: string[] | null
  by_tag?: Record<string, TagScore>; p50_ms?: number | null; p95_ms?: number | null; flaky?: number; judge_mean?: number | null
  judge_errors?: string[]  // "case id: why" for every rubric case the judge failed to score (those cases fail)
}
export interface EvalTurnResult { query: string; pass: boolean; reasons: string[]; answer: string; agents: string[]; ms: number; qid: number | null }
export interface JudgeScore { correct: number; complete: number; grounded: number; concise: number; mean: number; note: string; engine: string }
/** Extra fields on scored eval cases. */
export interface EvalCaseExtra {
  kind?: 'single' | 'multi_turn' | 'file' | 'judge'; split?: 'dev' | 'holdout'
  turns?: EvalTurnResult[] // multi-turn cases: one per turn
  attempts?: boolean[]     // repeat > 1: pass/fail per attempt (every phrasing, run after run)
  flaky?: boolean          // the repeats of one phrasing disagree (a paraphrase that always fails is not flaky)
  judge?: JudgeScore | null; judge_error?: string // judge_error: the judge failed, so the rubric wasn't checked
  max_ms?: number | null; over_budget?: boolean
}

// ---------- created files (docs/PLAN-files.md, docs/RULES-files.md) ----------
export type FileFormat = 'pdf' | 'docx' | 'pptx' | 'xlsx' | 'md'
export type RuleSeverity = 'block' | 'fix' | 'warn'
/** One ruleset check on one file; ok false with severity fix means it was corrected. */
export interface RuleResult { id: string; severity: RuleSeverity; ok: boolean; note: string }
export interface RuleInfo { id: string; group: string; text: string; severity: RuleSeverity | null; enforced: boolean }
export interface CreatedFile {
  id: string; name: string; format: FileFormat; size: number; created: number; qid: number | null
  title: string
  pages?: number | null; slides?: number | null; sheets?: string[] | null
  tokens: number          // LLM tokens the spec cost; 0 for zero-token paths and conversions
  source: 'llm' | 'answer' | 'table' | 'convert'  // where the content came from
  from_id?: string | null // convert: the file it was converted from
  rules: RuleResult[]     // every check run; warnings shown on the card
  sandbox?: string | null // sandbox id when the file lives only in that sandbox's memory
}
export type FilePreview =
  | { kind: 'markdown'; text: string }
  | { kind: 'outline'; items: Array<{ level: number; text: string }>; pages?: number | null; slides?: number | null }
  | { kind: 'sheets'; sheets: Array<{ name: string; columns: string[]; rows: Array<Array<string | number | null>>; total_rows: number }> }

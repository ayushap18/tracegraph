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
  merged: MergedRecord | null
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

export interface Features {
  files: boolean; compare: boolean; evals: boolean; custom_agents: boolean; exec: boolean
  cassette?: boolean // a Jev cassette is recorded, so a routing-only eval can replay it (older servers leave it out)
}

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
export interface EvalCase extends EvalCaseExtra { id: string; query: string; tags: string[]; pass: boolean | null; reasons: string[]; agents: string[]; answer: string; ms: number; qid: number | null; expect_agents?: string[] | null; expect_outcome?: string | null }
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
  document: '#6371f0', data: '#2a8f82', report: '#a86d24', run: '#c2410c', unsupported: '#9a8558',
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

// ---------- accuracy v2 (docs/PLAN-accuracy-v2.md) ----------
export type GuardOutcome = 'clarify' | 'blocked' | 'unsupported'
export type PolicyRule =
  | 'blocked' | 'blocked_dependency' | 'unsupported' | 'cant_do' | 'forced' | 'create_demote' | 'file_intent'
  | 'keyless_contract' | 'confirmed' | 'attached_file' | 'refers_back_file' | 'advice' | 'time_sensitive'
  | 'clarify_policy' | 'missing_slot' | 'mode_research' | 'ambiguous_term' | 'frame'
/** One rule the decision policy applied to a step, in order. `agent` is the agent after this rule. */
export interface PolicyStep { rule: PolicyRule; agent: string; why: string }
/** Jev's extra scores from the same route call (0..1). */
export interface RouteSignals { live?: number; action?: number; personal?: number; described?: number }
/** What a finished step is about, carried to the next turn (keyless follow-ups). */
export interface TurnFrame { agent: string; slots: Record<string, string | number> }

export interface RoutedFields {
  trace?: PolicyStep[]
  signals?: RouteSignals
  bound?: boolean          // this step took the run's @agent (only one step per run)
  assumption?: string      // stated before the answer instead of asking a clarifying question
  frame_used?: TurnFrame   // the previous turn's frame this step was completed from
}
export interface AnsweredFields {
  caveats?: string[]       // what this step could not do, in plain words
  frame?: TurnFrame
}
export interface MergedEvent { caveats?: string[]; primary_file?: string | null } // primary_file: CreatedFile id
/** The stored merged answer. Phase 0 also changes the one existing line in HistoryRecord from
 *  `merged: { answer: string; engine: MergeEngine } | null` to `merged: MergedRecord | null`
 *  (the only edit above this block: TS can't redeclare a merged property with a different type). */
export interface MergedRecord { answer: string; engine: MergeEngine; caveats?: string[]; primary_file?: string | null }

export type SuspectCode =
  | 'pages_short' | 'forced_non_file' | 'reply_template_body' | 'extra_format' | 'unfulfilled' | 'dup_clarify'
  | 'agent_label_leak' | 'dead_end' | 'cut_off'
export interface SuspectCheck { code: SuspectCode; note: string }
export interface RunRecord { suspects?: SuspectCheck[]; dry_run?: 'route' | null }
export interface PromoteRunResponse { case_id: string; created: boolean; path: string }

export interface Limits { query_chars: number }
export interface FontsInfo { body: string | null } // TRACEGRAPH_BODY_FONT family, or null
export interface HelloEvent { limits?: Limits; fonts?: FontsInfo }

// Created files: the request's brief and how the file met it.
export type ThemeName = 'clean' | 'dark' | 'warm' | 'mono'
export type DiagramKind = 'timeline' | 'tree' | 'flow'
  // Studio kinds the writer can be asked for (create/brief.WRITABLE_KINDS; 'labelled' is made by code only)
  | 'cycle' | 'venn' | 'pyramid' | 'matrix' | 'mindmap' | 'process' | 'comparison' | 'stat-cards' | 'scatter'
export interface FileBrief {
  format: FileFormat | null
  pages: [number, number] | null
  slides: [number, number] | null
  theme: ThemeName | null
  font: string | null
  images: boolean
  image_source: 'web' | null
  diagrams: boolean
  diagram_kinds: DiagramKind[]
  words: number | null
  capped: boolean
}
export interface ImageCredit {
  asset: string; title: string; author: string; license: string; license_url: string | null
  source_url: string; caption: string
}
export interface FilePhase { phase: 'outline' | 'sections' | 'topup' | 'assets' | 'render'; calls: number; llm_in: number; llm_out: number; ms: number }
export interface CreatedFile {
  brief?: FileBrief | null
  role?: 'primary' | 'working'
  theme?: ThemeName
  font_used?: string | null
  diagrams?: number
  images?: number
  credits?: ImageCredit[]
  phases?: FilePhase[]
}

// Evals
export interface ChatOpts { mode?: ChatMode; agent?: string; style?: AnswerStyle }
export type EvalMode = 'full' | 'route'
export type JevSource = 'live' | 'replay' | 'record'
export type FailStage =
  | 'plan_text' | 'plan_shape' | 'jev_pick' | 'clarity' | 'confidence' | 'gate_missing_detail' | 'gate_cant'
  | 'gate_wants_file' | 'unsupported' | 'forced' | 'frame' | 'agent_answer' | 'file' | 'judge' | 'budget'
export type ReasonCodeName =
  | 'wrong_agent' | 'extra_agent' | 'missing_agent' | 'forbidden_agent' | 'wrong_outcome' | 'plan_shape' | 'wrong_deps'
  | 'answer_regex' | 'forbidden_regex' | 'file' | 'judge' | 'budget' | 'unjudged' | 'run_status' | 'unrecorded'
export interface ReasonCode { code: ReasonCodeName; step?: number; want?: string; got?: string; stage?: FailStage }
export interface StepRoute {
  tid: string; agent: string; pick: string; confidence: number; clear: number
  expected?: string | null; stage?: FailStage | null; trace?: PolicyStep[]
}
export interface FileScore {
  format: FileFormat; pages: number | null; slides: number | null; words: number; headings: number
  images: number; diagrams: number; fonts: string[]; grayscale: boolean | null
  source: CreatedFile['source']; files: number
}
export interface AgentPR {
  agent: string; tp: number; fp: number; fn: number
  precision: number | null; recall: number | null; f1: number | null; support: number
}
export interface CalibrationBin { lo: number; hi: number; n: number; accuracy: number | null; mean: number }
export interface Calibration { signal: 'confidence' | 'clear'; bins: CalibrationBin[]; ece: number | null }
export interface TagGate {
  tag: string; min: number; passed: number; total: number; rate: number; lower: number
  baseline: number | null; ok: boolean
}
export interface EvalCaseExtra {
  route_pass?: boolean
  answer_pass?: boolean | null
  unjudged?: boolean
  codes?: ReasonCode[]
  steps?: StepRoute[]
  file?: FileScore | null
  tokens?: RunTokens
  chat?: ChatOpts
}
export interface EvalSummaryExtra {
  mode?: EvalMode
  jev?: JevSource
  suite_sha?: string
  route_pass?: TagScore
  answer_pass?: TagScore
  by_tag_route?: Record<string, TagScore>
  unjudged?: number
  unrecorded?: number
  confusion?: Record<string, Record<string, number>> // expected -> got -> count
  per_agent?: AgentPR[]
  stages?: Partial<Record<FailStage, number>>
  calibration?: Calibration[]
  gates?: TagGate[]
  tokens?: RunTokens
}
export interface RunEvalBody { mode?: EvalMode; jev?: JevSource }

// ---------- files that always build, design files, cost preflight (docs/PLAN-files-robust.md) ----------
// Additive only: every field below is optional on older records, and no line above this block changes.

/** A2: the stage one estimated model call belongs to. */
export type EstimatePhase = 'planner' | 'research' | 'answer' | 'outline' | 'sections' | 'topup' | 'repair' | 'merge'
  | 'critic'   // Studio: one design critic call on the contact sheets (Polish, docs/PLAN-designer.md 9.9)
/** One line of the estimate's breakdown: `calls` calls of one phase on one engine (mid values). */
export interface EstimateCall {
  phase: EstimatePhase
  engine: string          // engine name, e.g. 'agy', 'claude-code'
  calls: number
  tokens_in: number
  tokens_out: number
  seconds: number
  optional: boolean       // counted only in the high end of the range (top-up, repair)
}
/** [low, high] of each total. The mid values are the Estimate's own fields. */
export interface EstimateRange {
  calls: [number, number]
  tokens_in: [number, number]
  tokens_out: [number, number]
  seconds: [number, number]
}
export type EstimateReasonCode =
  | 'long_file' | 'research' | 'planner' | 'merge' | 'engine_overhead' | 'attachments' | 'images' | 'design'
  | 'deadline' | 'several_engines' | 'resume' | 'keyless' | 'repair'
/** Why the run costs what it does, one plain sentence each (shown in the dialog). */
export interface EstimateReason { code: EstimateReasonCode; text: string }
/** A healthy engine that would do the same run for fewer tokens. */
export interface CheaperEngine {
  engine: string; label: string
  calls: number; tokens_in: number; tokens_out: number; seconds: number
  saves: number           // share of tokens saved, 0..1
}
/** POST /api/estimate, and the `estimate` of a 409 from /ask or /api/created/resume. No model is called to make it. */
export interface Estimate {
  version: 1
  calls: number
  tokens_in: number
  tokens_out: number
  seconds: number
  range: EstimateRange
  engine: string | null        // the engine most calls go to; null when keyless or several engines
  engine_label: string | null
  billing: 'api' | 'subscription' | null
  dollars: [number, number] | null  // api billing only, from `prices`
  keyless: boolean             // no model call at all (keyless runs are never asked to confirm)
  long_file: boolean           // the long-document writer will run
  needs_confirmation: boolean
  reasons: EstimateReason[]
  breakdown: EstimateCall[]
  deadline_s: number           // the run's deadline (TG_RUN_TIMEOUT or TG_LONG_RUN_TIMEOUT)
  cheaper: CheaperEngine[]     // best first; empty when none is healthy and cheaper
  summary: string              // one plain sentence for the dialog
}
/** The same fields /ask takes; nothing is started. */
export type EstimateBody = Omit<AskBody, 'confirm_cost' | 'retry_of' | 'replaces' | 'remember' | 'draft_agent'>
export interface AskBody {
  confirm_cost?: boolean // the user saw the estimate and chose Continue; without it a costly run returns 409
}
/** The body of a 409 from /ask or /api/created/resume when the run needs confirming. */
export interface NeedsConfirmation { error: string; needs_confirmation: true; estimate: Estimate }
/** What a run really used, for "estimated vs used". */
export interface CostActual { calls: number; tokens_in: number; tokens_out: number; seconds: number }
export interface RunCost { estimate: Estimate | null; actual: CostActual | null }

/** A3: how far a long file got. Full state stays on the server; this is the summary. */
export type CheckpointPhase = 'outline' | 'sections' | 'topup' | 'render' | 'done'
export interface CheckpointInfo {
  qid: number
  tid: string
  kind: 'longdoc' | 'single'
  format: FileFormat
  phase: CheckpointPhase
  planned: number              // sections (slides) planned
  written: number              // sections written and usable
  missing: string[]            // headings still to write
  tokens_in: number            // spent so far
  tokens_out: number
  at: number
  resumable: boolean
  file_id: string | null       // the partial file, when one was delivered
  resumed_by?: number          // the run that resumed this checkpoint (resumable is false once it made the file)
}
export interface ResumeRef { qid: number; tid: string; sandbox?: string | null }
/** A3: a file delivered with some planned sections missing. */
export interface PartialInfo { planned: number; written: number; missing: string[]; resume: ResumeRef | null; resumed_by?: number | null }
/** Model calls spent re-writing sections that came back unusable. */
export interface RepairInfo { calls: number; llm_in: number; llm_out: number; sections: string[] }
export interface ResumeBody { qid: number; tid: string; engine?: string; confirm_cost?: boolean; sandbox_id?: string }
export interface ResumeResponse { ok: true; qid: number; session_id: string | null; estimate: Estimate }

/** A4: roles a design file can set. Colours are RRGGBB without '#'. */
export type DesignRole =
  | 'bg' | 'surface' | 'text' | 'heading' | 'muted' | 'accent' | 'border' | 'header_bg' | 'header_text' | 'stripe'
  | 'code_bg'
export interface DesignApplied {
  name: string                         // the design file's name, e.g. 'DESIGN-lovable.md'
  colors: Partial<Record<DesignRole, string>>
  palette: string[]
  heading_font: string | null          // what the design asked for
  body_font: string | null
  fonts_used: { heading: string | null; body: string | null }  // what the file really uses
  nudged: string[]                     // roles whose colour was adjusted for contrast
  notes: string[]                      // plain sentences, also in the caveats
  confidence: number                   // 0..1, how sure the parser was
}
export interface FileBrief {
  design?: string | null // name of the design file the request asked to use
}
export interface CreatedFile {
  partial?: PartialInfo | null
  repairs?: RepairInfo | null
  cost?: RunCost | null
  design?: DesignApplied | null
  resumed_from?: { qid: number; tid: string; file_id: string | null } | null
}
export interface AnsweredFields {
  phases?: FilePhase[]         // create steps keep these even when no file was made
  llm_in?: number
  llm_out?: number
  checkpoint?: CheckpointInfo | null
}
export interface RunRecord {
  cost?: RunCost | null
  file_failed?: boolean        // the run's primary file step made no file
  checkpoints?: CheckpointInfo[]
}
export interface DoneEvent { cost?: RunCost | null; file_failed?: boolean; checkpoints?: CheckpointInfo[] }

// ---------- Studio, the design stage (docs/PLAN-designer.md section 9) ----------
export type DesignPresetId =
  | 'bold-dark' | 'editorial' | 'minimal' | 'vibrant' | 'pastel' | 'academic' | 'mono' | 'high-legibility'
export type DesignTemplateId =
  | 'class-presentation' | 'lab-report' | 'research-poster' | 'revision-notes' | 'infographic' | 'book-report'
  | 'science-fair'
export type SlideLayoutId =
  | 'cover-hero' | 'cover-type' | 'section-divider' | 'title-bullets' | 'image-left-text' | 'image-right-text'
  | 'full-bleed-image-caption' | 'big-number' | 'stat-cards' | 'quote' | 'two-column' | 'comparison'
  | 'full-width-diagram' | 'chart-focus' | 'timeline-strip' | 'closing'
export type PageTemplateId =
  | 'cover' | 'chapter-opener' | 'text-side-figure' | 'two-column-text' | 'full-figure' | 'pull-quote' | 'key-points'
  | 'references'
export type DesignLayoutId = SlideLayoutId | PageTemplateId | 'freeform'
export type DesignCheckId = 'D1' | 'D2' | 'D3' | 'D4' | 'D5' | 'D6' | 'D7' | 'D8'
export type DesignStop = 'pass' | 'rounds' | 'budget' | 'deadline' | 'error' | 'keyless'
export type FontRole = 'display' | 'heading' | 'body' | 'caption' | 'mono'
export type OpenFontLicence = 'OFL-1.1' | 'Apache-2.0' | 'UFL-1.0'

/** GET /api/design/presets: one preset. Colours are RRGGBB without '#'. */
export interface DesignPresetInfo {
  id: DesignPresetId
  name: string                 // 'Bold dark'
  description: string          // who it is for
  dark: boolean
  families: Partial<Record<FontRole, string>>
  colors: { bg: string; text: string; accent: string; accent2: string }
  thumb: string                // '/api/design/presets/<id>/thumb' (PNG)
}
export interface DesignTemplateInfo {
  id: DesignTemplateId
  name: string
  description: string
  format: 'pptx' | 'pdf' | 'docx'
  preset: DesignPresetId
  paper: 'a4' | 'letter' | 'a3' | 'a2' | null
  sequence: DesignLayoutId[]
  tone: string[]
}
/** GET /api/fonts/search: an open-licensed family (never anything else). */
export interface FontInfo {
  family: string
  category: 'sans' | 'serif' | 'mono' | 'display' | 'handwriting'
  licence: OpenFontLicence
  source: 'cache' | 'system' | 'fontsource' | 'google-fonts' | 'user' | 'builtin'
  styles: string[]             // 'regular' | 'bold' | 'italic' | 'bolditalic'
  installed: boolean           // already in the font cache or on this machine
  preview: string              // '/api/fonts/preview?family=<family>' (PNG)
}
/** A font a designed file uses. */
export interface DesignFontUse {
  family: string
  role: FontRole
  source: string
  licence: string              // an OpenFontLicence, 'system', 'user' or 'builtin'
  embedded: boolean            // PDF subsets only
  fallback?: string | null     // what PowerPoint/Word shows when the family isn't installed
  requested?: string | null
  note?: string | null         // e.g. "Anthropic Sans isn't openly licensed, so this uses Inter, the closest open match"
}
/** One visual QA finding (D1-D8). page is 0-based; shown to people as page + 1. */
export interface DesignQaResult {
  id: DesignCheckId
  ok: boolean
  note: string
  page: number | null
  box: string | null
  value: number | null
  threshold: number | null
  fixed: boolean
}
export interface DesignCheckSummary { id: DesignCheckId; name: string; ok: boolean; failures: number; note: string }
/** GET /api/created/{id}/design: the design report stored with the file. */
export interface DesignReport {
  version: 1
  score: number                // 0..100
  preset: DesignPresetId | 'custom'
  format: FileFormat
  fonts: DesignFontUse[]
  rounds: number
  stop: DesignStop
  tokens: Record<string, number> // direct_in, direct_out, critic_in, critic_out, freeform_in, freeform_out
  checks: DesignCheckSummary[]   // all eight, D1..D8
  results: DesignQaResult[]      // at most 50 failures
  layouts: Partial<Record<DesignLayoutId, number>>
  fallbacks: Array<{ page: number; from: string; to: string; why: string }>
  critic: { ran: boolean; why: string; edits?: Array<{ page: number; action: string; arg: string | number | null }>; rolled_back?: Array<{ page: number; action: string; arg: string | number | null }> }
  thumbs: number
  phases?: DesignPhase[]
  notes: string[]
}
/** Studio's own phase breakdown (CreatedFile.phases keeps FilePhase; Studio's totals are in its 'render' entry). */
export interface DesignPhase {
  phase: 'direct' | 'assets' | 'layout' | 'thumbs' | 'qa' | 'fix' | 'critic' | 'freeform' | 'paint'
  calls: number; llm_in: number; llm_out: number; ms: number
}
/** The DesignPlan summary served next to the report (the full plan stays on the server). */
export interface DesignPlanSummary {
  preset: DesignPresetId | 'custom'
  format: FileFormat
  pages: Array<{ index: number; layout: DesignLayoutId; variant: 'default' | 'compact'; freeform: boolean; section: number | null }>
  fonts: DesignFontUse[]
  assets: number
  score: number | null
  rounds: number
  stop: DesignStop | null
  tokens: Record<string, number>
}
/** GET /api/created/{id}/thumbs: one page/slide thumbnail. page is 1-based. */
export interface Thumb { page: number; url: string; w: number; h: number; layout: DesignLayoutId }
/** POST /api/created/{id}/restyle: 0 tokens, a new CreatedFile (source 'convert', from_id the original). layouts
 *  keys are 0-based page indexes as strings. */
export interface RestyleBody {
  preset?: DesignPresetId
  fonts?: Partial<Record<'display' | 'heading' | 'body', string>>
  dark?: boolean
  template?: DesignTemplateId
  layouts?: Record<string, DesignLayoutId>
  print?: boolean
}
/** POST /api/created/{id}/polish: one critic round (vision engines only); goes through the cost estimate. */
export interface PolishBody { engine?: string; confirm_cost?: boolean }
/** CreatedFile.design, Studio fields (absent on files made without Studio). When no design file was used, name is
 *  'preset:<id>', colors are the preset's and confidence is 1. */
export interface DesignApplied {
  studio?: boolean
  preset?: DesignPresetId | 'custom' | null
  fonts?: DesignFontUse[]
  score?: number | null
  thumbs?: number              // how many thumbnails GET /api/created/{id}/thumbs serves
}
/** Studio (builder W): whether the engine can read images (Python `Engine.supports_vision`), so the Design panel can
 *  disable Polish with a reason. Absent on older servers; the client then assumes claude-code and anthropic can. */
export interface EngineInfo {
  vision?: boolean
}

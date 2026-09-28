// Typed helpers for every REST endpoint in PLAN-v4 §1. Each returns parsed JSON or throws ApiError.
import type {
  AgentInfo, AskBody, AskResponse, CompareDetail, CompareResponse, ControlBody, ControlResponse, EngineTestResult,
  EvalDetail, EvalSummary, FileInfo, NewAgent, RunRecord, SessionDetail, SessionSummary,
  AgentExamples, EngineHealth, EvalCompare, Label, NewLabel, ReviewItem, ShakyReason, Verdict,
  RunEvalBody, TimingsSummary, PromoteRunResponse,
  CreatedFile, FileFormat, FilePreview, RuleInfo,
} from './protocol'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, headers: { Accept: 'application/json' } }
  if (body instanceof FormData) init.body = body
  else if (body !== undefined) {
    init.body = JSON.stringify(body)
    init.headers = { ...init.headers, 'Content-Type': 'application/json' }
  }
  let res: Response
  try {
    res = await fetch(url, init)
  } catch {
    throw new ApiError(0, 'Network error: the server is unreachable')
  }
  const text = await res.text()
  let data: unknown = null
  if (text) { try { data = JSON.parse(text) } catch { data = null } }
  if (!res.ok) {
    const msg = data && typeof data === 'object' && 'error' in data && typeof (data as { error: unknown }).error === 'string'
      ? (data as { error: string }).error
      : `${res.status} ${res.statusText || 'request failed'}`
    throw new ApiError(res.status, msg)
  }
  return data as T
}

const get = <T>(url: string) => request<T>('GET', url)
const post = <T>(url: string, body?: unknown) => request<T>('POST', url, body ?? {})
const del = <T>(url: string) => request<T>('DELETE', url)
const enc = encodeURIComponent

// ---------- runs ----------
export const ask = (body: AskBody) => post<AskResponse>('/ask', body)
export const cancelRun = (qid: number) => post<{ ok: true }>(`/api/runs/${qid}/cancel`)
/** Forget a sandbox on the server (cancel its runs, drop its follow-up context). keepalive so it also works while
 *  the page is unloading; failures are ignored because the server forgets idle sandboxes on its own. */
export const clearSandbox = (id: string) =>
  fetch(`/api/sandbox/${encodeURIComponent(id)}`, { method: 'DELETE', keepalive: true }).then(() => undefined, () => undefined)

export interface ListRunsParams {
  limit?: number; before?: number; q?: string; source?: string; status?: string; engine?: string
  suspect?: boolean // only runs whose checks flagged them as likely wrong (suspect=1)
}
/** GET /api/runs → newest first. `before` is a qid cursor for "Load more". */
export function listRuns(p: ListRunsParams = {}) {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(p)) {
    if (v === undefined || v === null || v === '' || v === false) continue
    qs.set(k, v === true ? '1' : String(v))
  }
  const s = qs.toString()
  return get<{ runs: RunRecord[] }>('/api/runs' + (s ? '?' + s : ''))
}
export const getRun = (qid: number) => get<RunRecord>(`/api/runs/${qid}`)
/** Draft an eval case from a saved run (evals/cases.local.jsonl). 409 when the case exists, 404 for an unknown run. */
export const promoteRun = (qid: number) => post<PromoteRunResponse>(`/api/runs/${qid}/promote`)
/** Several answers: make this run the chosen answer of its group (only chosen runs feed follow-ups). */
export const chooseRun = (qid: number) => post<{ ok: true; group_id: string; chosen: number }>(`/api/runs/${qid}/choose`)
/** Per-stage p50/p90 over recent saved runs, optionally for one engine ('none' = keyless). */
export const getTimings = (engine?: string, limit = 200) =>
  get<TimingsSummary>(`/api/timings?limit=${limit}` + (engine ? `&engine=${enc(engine)}` : ''))

// ---------- chat sessions ----------
export const listSessions = (limit = 30) => get<{ sessions: SessionSummary[] }>(`/api/sessions?limit=${limit}`)
export const getSession = (id: string) => get<SessionDetail>(`/api/sessions/${enc(id)}`)
export const deleteSession = (id: string) => del<{ ok: true }>(`/api/sessions/${enc(id)}`)

// ---------- agents ----------
export const listAgents = () => get<{ agents: AgentInfo[] }>('/api/agents')
export const createAgent = (a: NewAgent) => post<AgentInfo>('/api/agents', a)
export const deleteAgent = (name: string) => del<{ ok: true }>(`/api/agents/${enc(name)}`)

// ---------- files ----------
export function uploadFile(file: File) {
  const fd = new FormData()
  fd.append('file', file, file.name)
  return request<FileInfo>('POST', '/api/files', fd)
}
export const listFiles = () => get<{ files: FileInfo[] }>('/api/files')

// ---------- sandbox (docs/PLAN-sandbox.md): files live in server memory only ----------
export function uploadSandboxFile(sandboxId: string, file: File) {
  const fd = new FormData()
  fd.append('file', file, file.name)
  return request<FileInfo>('POST', `/api/sandbox/${enc(sandboxId)}/files`, fd)
}
export const deleteSandboxFile = (sandboxId: string, fid: string) => del<{ ok: true }>(`/api/sandbox/${enc(sandboxId)}/files/${enc(fid)}`)
/** Save sandbox turns as a normal chat (on request only). Default: the whole sandbox thread. */
export const keepSandbox = (sandboxId: string, qids?: number[]) =>
  post<{ session_id: string; qids: number[] }>(`/api/sandbox/${enc(sandboxId)}/keep`, qids ? { qids } : {})
export const deleteFile = (id: string) => del<{ ok: true }>(`/api/files/${enc(id)}`)

// ---------- compare ----------
export const compare = (query: string, engines: string[]) => post<CompareResponse>('/api/compare', { query, engines })
export const getCompare = (id: string) => get<CompareDetail>(`/api/compare/${enc(id)}`)

// ---------- evals ----------
/** examples: run with route examples on (true) or off (false); omitted = the server's current setting. */
export const runEval = (engine?: string, examples?: boolean) =>
  post<{ eval_id: string }>('/api/evals/run', { ...(engine ? { engine } : {}), ...(examples === undefined ? {} : { examples }) })
/** Full options: split (dev/holdout/all), tag filter, repeat count for flakiness, judge engine for open-ended cases
 *  ('auto' picks a healthy engine other than the one under test), mode ('route' plans and routes only, no agents run)
 *  and where Jev's answers come from ('replay' reads the recorded cassette). */
export const runEvalWith = (body: RunEvalBody) => post<{ eval_id: string }>('/api/evals/run', body)
export const compareEvals = (a: string, b: string) => get<EvalCompare>(`/api/evals/compare?a=${enc(a)}&b=${enc(b)}`)
export const cancelEval = (id: string) => post<{ ok: true }>(`/api/evals/${enc(id)}/cancel`)
export const listEvals = () => get<{ evals: EvalSummary[] }>('/api/evals')
export const getEval = (id: string) => get<EvalDetail>(`/api/evals/${enc(id)}`)

// ---------- engines and control ----------
export const testEngine = (name: string) => post<EngineTestResult>(`/api/engines/${enc(name)}/test`)
export const control = (body: ControlBody) => post<ControlResponse>('/control', body)
/** Switch the active engine; 'none' means keyless. */
export const setEngine = (name: string) => control({ engine: name })
/** The order Auto tries engines in; engines left out keep their place after these. */
export const setEngineOrder = (order: string[]) => control({ engine_order: order })

/** Turn route examples on or off for every new run. */
export const setRouteExamples = (on: boolean) => control({ route_examples: on })
export const engineHealth = () => get<{ engines: EngineHealth[] }>('/api/engines/health')

// ---------- labels and review (docs/PLAN-learning.md) ----------
export const createLabel = (l: NewLabel) => post<Label>('/api/labels', l)
export const deleteLabel = (id: string) => del<{ ok: true }>(`/api/labels/${enc(id)}`)
export interface ListLabelsParams { agent?: string; verdict?: Verdict; qid?: number; limit?: number }
export function listLabels(p: ListLabelsParams = {}) {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  const s = qs.toString()
  return get<{ labels: Label[] }>('/api/labels' + (s ? '?' + s : ''))
}
export const promoteLabel = (id: string) => post<{ case_id: string; created: boolean }>(`/api/labels/${enc(id)}/promote`)
export const listReview = (limit = 50, reason?: ShakyReason) =>
  get<{ items: ReviewItem[]; scanned: number }>(`/api/review?limit=${limit}` + (reason ? `&reason=${enc(reason)}` : ''))
export const agentExamples = () => get<{ enabled: boolean; agents: AgentExamples[] }>('/api/agents/examples')

// ---------- created files (docs/PLAN-files.md) ----------
export const listCreated = (limit = 50, before?: number) =>
  get<{ files: CreatedFile[] }>(`/api/created?limit=${limit}` + (before ? `&before=${before}` : ''))
export const getCreated = (id: string) => get<CreatedFile>(`/api/created/${enc(id)}`)
export const deleteCreated = (id: string) => del<{ ok: true }>(`/api/created/${enc(id)}`)
/** Re-render from the stored spec in another format; costs no LLM tokens. */
export const convertCreated = (id: string, format: FileFormat) => post<CreatedFile>(`/api/created/${enc(id)}/convert`, { format })
/** Download and preview URLs; sandbox files are served from the sandbox's memory. */
export const createdUrl = (f: Pick<CreatedFile, 'id' | 'sandbox'>, what: 'download' | 'preview') =>
  f.sandbox ? `/api/sandbox/${enc(f.sandbox)}/created/${enc(f.id)}/${what}` : `/api/created/${enc(f.id)}/${what}`
export const previewCreated = (f: Pick<CreatedFile, 'id' | 'sandbox'>) => get<FilePreview>(createdUrl(f, 'preview'))
export const listRules = () => get<{ rules: RuleInfo[] }>('/api/rules')

/** Human-readable message for any thrown value. */
export const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e))

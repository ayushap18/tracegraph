// Typed helpers for every REST endpoint in PLAN-v4 §1. Each returns parsed JSON or throws ApiError.
import type {
  AgentInfo, AskBody, AskResponse, CompareDetail, CompareResponse, ControlBody, ControlResponse, EngineTestResult,
  EvalDetail, EvalSummary, FileInfo, NewAgent, RunRecord, SessionDetail, SessionSummary,
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

export interface ListRunsParams { limit?: number; before?: number; q?: string; source?: string; status?: string; engine?: string }
/** GET /api/runs → newest first. `before` is a qid cursor for "Load more". */
export function listRuns(p: ListRunsParams = {}) {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(p)) if (v !== undefined && v !== null && v !== '') qs.set(k, String(v))
  const s = qs.toString()
  return get<{ runs: RunRecord[] }>('/api/runs' + (s ? '?' + s : ''))
}
export const getRun = (qid: number) => get<RunRecord>(`/api/runs/${qid}`)

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
export const runEval = (engine?: string) => post<{ eval_id: string }>('/api/evals/run', engine ? { engine } : {})
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

/** Human-readable message for any thrown value. */
export const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e))

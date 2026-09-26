import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { cancelRun, compare, errorText as errText, getCompare } from '../api'
import { Badge, Button, EmptyState, IconButton, Skeleton, StatusBadge, copyText, navigate, timeAgo, useNow, useToast } from '../ui'
import { useStore } from '../store'
import { EngineIcon, Icon } from '../icons'
import type { RunRecord } from '../protocol'
import { fromRecord, type Run } from '../useEventStream'
import { Chip } from '../components/Panels'
import Markdown from '../components/Markdown'
import { ms } from '../lib'

// Side-by-side engine answers. Live runs stream from the store (by qid); anything the store no longer holds comes
// from GET /api/compare/:id. Past comparisons in this browser live in localStorage.

const HISTORY_KEY = 'tg-compare-history'
const HISTORY_MAX = 20
const MIN_ENGINES = 2
const MAX_ENGINES = 4

interface HistoryEntry { id: string; query: string; at: number; runs: Array<{ engine: string; qid: number }> }

function readHistory(): HistoryEntry[] {
  try {
    const raw = localStorage.getItem(HISTORY_KEY)
    const list = raw ? (JSON.parse(raw) as unknown) : []
    return Array.isArray(list) ? (list as HistoryEntry[]).filter(h => h && typeof h.id === 'string' && typeof h.query === 'string' && Number.isFinite(h.at) && Array.isArray(h.runs) && h.runs.every(r => r && typeof r.engine === 'string' && Number.isFinite(r.qid))) : []
  } catch { return [] }
}
function writeHistory(list: HistoryEntry[]) {
  try { localStorage.setItem(HISTORY_KEY, JSON.stringify(list.slice(0, HISTORY_MAX))) } catch { /* storage off: history is this visit only */ }
}

// One engine column, normalised from either a live store Run or a stored record.
interface Column {
  engine: string
  qid: number
  status: string // running | done | cancelled | timeout | error
  answer: string
  streaming: boolean
  agents: string[]
  plan: string[]
  total_ms: number | null
  error?: string
}

function fromRun(engine: string, run: Run): Column {
  const tasks = run.order.map(t => run.tasks[t]).filter(Boolean)
  const status = run.status ?? (!run.done ? 'running' : run.error ? 'error' : 'done')
  const answer = run.merged?.answer || run.mergeStream || tasks.map(t => t.answered?.answer ?? t.stream).filter(Boolean).join('\n\n')
  return {
    engine, qid: run.qid, status, answer, streaming: !run.done && !run.merged,
    agents: tasks.map(t => t.routed?.agent).filter((a): a is string => !!a),
    plan: tasks.map(t => t.text).filter(Boolean), total_ms: run.total_ms, error: run.error,
  }
}

const approxTokens = (s: string) => Math.round(s.length / 4)

export default function Compare({ params }: { params: Record<string, string> }) {
  const id = params.id
  const [history, setHistory] = useState<HistoryEntry[]>(readHistory)
  const remember = useCallback((h: HistoryEntry) => {
    setHistory(prev => { const next = [h, ...prev.filter(p => p.id !== h.id)].slice(0, HISTORY_MAX); writeHistory(next); return next })
  }, [])
  const clearHistory = () => { setHistory([]); writeHistory([]) }

  return (
    <div className="cmp-page">
      <NewComparison onStarted={remember} compact={!!id} />
      {id && <ComparisonView key={id} id={id} history={history} remember={remember} />}
      <HistoryList history={history} current={id} onClear={clearHistory} />
    </div>
  )
}

function NewComparison({ onStarted, compact }: { onStarted: (h: HistoryEntry) => void; compact: boolean }) {
  const { store } = useStore()
  const toast = useToast()
  const [query, setQuery] = useState('')
  const [picked, setPicked] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const available = store.engines.filter(e => e.available)

  // Preselect the first available engines once the store knows them.
  useEffect(() => {
    if (picked.length || available.length < MIN_ENGINES) return
    setPicked(available.slice(0, Math.min(3, available.length)).map(e => e.name))
  }, [available.length]) // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (name: string) =>
    setPicked(p => (p.includes(name) ? p.filter(n => n !== name) : p.length >= MAX_ENGINES ? p : [...p, name]))

  const q = query.trim()
  const valid = q.length > 0 && picked.length >= MIN_ENGINES && picked.length <= MAX_ENGINES && picked.every(name => available.some(e => e.name === name))
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (!valid || busy) return
    setBusy(true)
    try {
      const res = await compare(q, picked)
      onStarted({ id: res.compare_id, query: q, at: Date.now() / 1000, runs: res.runs })
      navigate(`/compare/${res.compare_id}`)
    } catch (err) {
      toast.error(`Could not start the comparison: ${errText(err)}`)
    } finally { setBusy(false) }
  }

  return (
    <form className={'panel cmp-new' + (compact ? ' compact' : '')} onSubmit={submit} aria-label="New comparison">
      <div className="cmp-head">
        <h2 className="cmp-title"><Icon name="compare" size={16} /> Compare engines</h2>
        {!compact && <p className="muted small">Ask one question to several engines at once and read their answers side by side.</p>}
      </div>
      <div className="cmp-ask">
        <label className="sr-only" htmlFor="cmp-q">Question</label>
        <input id="cmp-q" className="cmp-input" value={query} maxLength={500} placeholder="Ask something to compare…"
          onChange={e => setQuery(e.target.value)} autoComplete="off" />
        <Button type="submit" icon="play" disabled={!valid || busy}>{busy ? 'Starting…' : 'Run'}</Button>
      </div>
      <fieldset className="cmp-engines">
        <legend className="muted small">Engines ({picked.length}/{MAX_ENGINES}, pick {MIN_ENGINES}–{MAX_ENGINES})</legend>
        {!store.ready ? <Skeleton height={34} />
          : store.engines.length === 0 ? <p className="muted small">No engines reported by the server.</p>
          : store.engines.map(e => {
            const on = picked.includes(e.name)
            const full = !on && picked.length >= MAX_ENGINES
            return (
              <label key={e.name} className={'cmp-engine' + (on ? ' on' : '') + (!e.available ? ' off' : '')}
                title={!e.available ? e.why : full ? `At most ${MAX_ENGINES} engines` : e.label}>
                <input type="checkbox" checked={on} disabled={!e.available || full} onChange={() => toggle(e.name)} />
                <EngineIcon name={e.name} size={15} />
                <span>{e.label}</span>
                {!e.available && <span className="muted small">unavailable</span>}
              </label>
            )
          })}
      </fieldset>
      {store.ready && available.length < MIN_ENGINES && (
        <p className="muted small cmp-warn">At least {MIN_ENGINES} available engines are needed to compare. Set them up in <a href="#/settings">Settings</a>.</p>
      )}
    </form>
  )
}

function ComparisonView({ id, history, remember }: { id: string; history: HistoryEntry[]; remember: (h: HistoryEntry) => void }) {
  const { store } = useStore()
  const toast = useToast()
  const local = history.find(h => h.id === id)
  const [data, setData] = useState<{ query: string; runs: RunRecord[] } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [copied, setCopied] = useState(false)

  const load = useCallback(async () => {
    try {
      const res = await getCompare(id)
      setData({ query: res.query, runs: res.runs })
      setError(null)
      return res
    } catch (err) {
      setError(errText(err))
      return null
    } finally { setLoading(false) }
  }, [id])

  // Record a comparison opened from a shared link, so it shows in this browser's history too.
  useEffect(() => {
    void load().then(res => {
      if (!res || local) return
      remember({ id, query: res.query, at: res.runs[0]?.at ?? Date.now() / 1000,
        runs: res.runs.map(r => ({ engine: r.engine ?? '?', qid: r.qid })) })
    })
  }, [load]) // eslint-disable-line react-hooks/exhaustive-deps

  const pairs = useMemo(() => {
    if (data) return data.runs.map(r => ({ engine: r.engine ?? '?', qid: r.qid, record: r as RunRecord | undefined }))
    return (local?.runs ?? []).map(r => ({ ...r, record: undefined as RunRecord | undefined }))
  }, [data, local])

  const columns: Column[] = pairs.map(p => {
    const live = store.runs.find(r => r.qid === p.qid)
    if (live && (live.text || live.order.length)) return fromRun(p.engine, live)
    if (p.record) return { ...fromRun(p.engine, fromRecord(p.record)), streaming: false }
    return { engine: p.engine, qid: p.qid, status: 'running', answer: '', streaming: true, agents: [], plan: [], total_ms: null }
  })

  // Runs the store no longer tracks (joined late, or trimmed) are refreshed from the API until they finish.
  const stale = columns.some(c => c.status === 'running' && !store.runs.some(r => r.qid === c.qid))
  useEffect(() => {
    if (!stale) return
    const t = window.setInterval(() => void load(), 3000)
    return () => clearInterval(t)
  }, [stale, load])

  const finished = columns.filter(c => c.status === 'done' && c.total_ms != null)
  const fastest = finished.length > 1 ? finished.reduce((a, b) => ((a.total_ms ?? 0) <= (b.total_ms ?? 0) ? a : b)).qid : null
  const query = data?.query ?? local?.query ?? ''

  const share = async () => {
    if (await copyText(location.href)) { setCopied(true); window.setTimeout(() => setCopied(false), 1500) }
    else toast.error('Could not copy the link')
  }
  const stop = async (qid: number) => {
    try { await cancelRun(qid) } catch (err) { toast.error(`Could not stop run #${qid}: ${errText(err)}`) }
  }

  if (loading && !local) {
    return (
      <section className="cmp-cols" aria-busy="true" aria-label="Loading comparison">
        {[0, 1, 2].map(i => <div className="panel cmp-col" key={i}><Skeleton height={20} /><Skeleton height={120} /></div>)}
      </section>
    )
  }
  if (!pairs.length) {
    return (
      <div className="panel">
        <EmptyState icon="compare" title="Comparison not found" text={error ?? 'This comparison has no runs.'}
          action={<Button variant="ghost" onClick={() => navigate('/compare')}>New comparison</Button>} />
      </div>
    )
  }

  return (
    <section className="cmp-result" aria-label="Comparison result">
      <div className="cmp-result-head">
        <div className="cmp-q">
          <span className="muted small">Question</span>
          <p>{query || '…'}</p>
        </div>
        <IconButton icon="link" label={copied ? 'Link copied' : 'Copy link to this comparison'} onClick={() => void share()} />
      </div>
      <div className="cmp-cols" style={{ ['--n' as string]: String(columns.length) }}>
        {columns.map(c => (
          <article key={c.qid} className={'panel cmp-col st-' + c.status} aria-label={`${c.engine} answer`}>
            <header className="cmp-col-head">
              <span className="cmp-engine-name"><EngineIcon name={c.engine} size={16} /> {engineLabel(store.engines, c.engine)}</span>
              <span className="cmp-badges">
                {c.qid === fastest && <Badge tone="ok" icon="bolt">fastest</Badge>}
                <StatusBadge status={c.status} />
                {c.status === 'running' && <IconButton icon="stop" label={`Stop ${c.engine} run`} onClick={() => void stop(c.qid)} />}
              </span>
            </header>
            <dl className="cmp-stats">
              <div><dt>Time</dt><dd className="num">{c.status === 'running' ? '…' : ms(c.total_ms)}</dd></div>
              <div><dt>Tokens</dt><dd className="num" title="Approximate output tokens (answer length ÷ 4)">≈{approxTokens(c.answer).toLocaleString()}</dd></div>
              <div><dt>Steps</dt><dd className="num">{c.plan.length || '–'}</dd></div>
            </dl>
            {c.agents.length > 0 && <div className="cmp-agents">{c.agents.map((a, i) => <Chip key={i} agent={a} />)}</div>}
            {c.plan.length > 1 && (
              <details className="cmp-plan">
                <summary>Plan ({c.plan.length} steps)</summary>
                <ol>{c.plan.map((p, i) => <li key={i}>{p}</li>)}</ol>
              </details>
            )}
            <div className={'answer cmp-answer md' + (c.streaming && c.status === 'running' ? ' streaming' : '')}>
              {c.answer ? <Markdown text={c.answer} />
                : c.status === 'running' ? <Skeleton lines={3} />
                : <span className="muted">{c.error ?? 'No answer.'}</span>}
            </div>
            <a className="cmp-trace small" href={`#/runs/${c.qid}`}>View trace <Icon name="external" size={12} /></a>
          </article>
        ))}
      </div>
    </section>
  )
}

function engineLabel(engines: Array<{ name: string; label: string }>, name: string) {
  return engines.find(e => e.name === name)?.label ?? name
}

function HistoryList({ history, current, onClear }: { history: HistoryEntry[]; current?: string; onClear: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const now = useNow()
  return (
    <section className="panel cmp-history" aria-label="Past comparisons">
      <h2>
        <span className="h-title"><Icon name="history" size={14} /> Past comparisons</span>
        {history.length > 0 && (confirming
          ? <span className="cmp-confirm">
              <span className="small">Clear all?</span>
              <Button size="sm" variant="danger" onClick={() => { onClear(); setConfirming(false) }}>Clear</Button>
              <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
            </span>
          : <Button size="sm" variant="ghost" onClick={() => setConfirming(true)}>Clear history</Button>)}
      </h2>
      {history.length === 0
        ? <EmptyState icon="history" title="No comparisons yet" text="Comparisons you run in this browser show up here." />
        : (
          <ul className="cmp-hist-list">
            {history.map(h => (
              <li key={h.id} className={h.id === current ? 'on' : ''}>
                <a href={`#/compare/${h.id}`} aria-current={h.id === current ? 'page' : undefined}>
                  <span className="cmp-hist-q">{h.query}</span>
                  <span className="cmp-hist-meta">
                    {h.runs.map(r => <EngineIcon key={r.qid} name={r.engine} size={13} />)}
                    <span className="muted small">{timeAgo(h.at, now)}</span>
                  </span>
                </a>
              </li>
            ))}
          </ul>
        )}
    </section>
  )
}

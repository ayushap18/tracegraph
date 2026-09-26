import { useCallback, useEffect, useId, useMemo, useState, type FormEvent } from 'react'
import { cancelRun, compare, errorText as errText, getCompare } from '../api'
import { Badge, Button, Card, EmptyState, IconButton, Skeleton, StatusBadge, copyText, navigate, timeAgo, useNow, useToast } from '../ui'
import { useStore } from '../store'
import { EngineIcon, Icon } from '../icons'
import type { RunRecord } from '../protocol'
import { fromRecord, type Run } from '../useEventStream'
import { AgentBadge, PageBody } from '../components/app'
import { Input } from '@/components/ui/input'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'
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

// Result columns follow the width of the results area (not the viewport), so the history column on xl never
// squeezes four answers into slivers: one column when narrow, two from ~672px, then as many as there are engines.
const COLS: Record<number, string> = {
  1: '',
  2: '@2xl:grid-cols-2',
  3: '@2xl:grid-cols-2 @4xl:grid-cols-3',
  4: '@2xl:grid-cols-2 @5xl:grid-cols-4',
}

export default function Compare({ params }: { params: Record<string, string> }) {
  const id = params.id
  const [history, setHistory] = useState<HistoryEntry[]>(readHistory)
  const remember = useCallback((h: HistoryEntry) => {
    setHistory(prev => { const next = [h, ...prev.filter(p => p.id !== h.id)].slice(0, HISTORY_MAX); writeHistory(next); return next })
  }, [])
  const clearHistory = () => { setHistory([]); writeHistory([]) }

  return (
    <PageBody width="wide">
      <div className="grid min-w-0 grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_320px] xl:items-start">
        <div className="flex min-w-0 flex-col gap-6">
          <NewComparison onStarted={remember} compact={!!id} />
          {id ? <ComparisonView key={id} id={id} history={history} remember={remember} /> : (
            <div className="rounded-lg border border-dashed border-border">
              <EmptyState icon="compare" title="Answers appear here, side by side"
                text="Ask a question above and pick two to four engines. Each column shows the answer, the time it took, the agents it used and a link to its trace." />
            </div>
          )}
        </div>
        <HistoryList history={history} current={id} onClear={clearHistory} />
      </div>
    </PageBody>
  )
}

function NewComparison({ onStarted, compact }: { onStarted: (h: HistoryEntry) => void; compact: boolean }) {
  const { store } = useStore()
  const toast = useToast()
  const legendId = useId()
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
  // The toggle group reports the whole next selection; route the one engine that changed through `toggle`
  // so the 2 to 4 rule stays in one place.
  const onGroupChange = (next: string[]) => {
    const changed = next.find(n => !picked.includes(n)) ?? picked.find(n => !next.includes(n))
    if (changed) toggle(changed)
  }

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
    <form onSubmit={submit} aria-label="New comparison"
      className={cn('flex min-w-0 flex-col rounded-lg border border-border bg-surface', compact ? 'gap-3 p-3 sm:p-4' : 'gap-4 p-4 sm:p-5')}>
      {!compact && (
        <p className="m-0 max-w-[70ch] text-[13px] leading-relaxed text-muted-foreground">
          Ask one question to several engines at once and read their answers side by side.
        </p>
      )}
      <div className="flex flex-col gap-2 sm:flex-row">
        <label className="sr-only" htmlFor="cmp-q">Question</label>
        <Input id="cmp-q" value={query} maxLength={500} placeholder="Ask something to compare…" autoComplete="off"
          onChange={e => setQuery(e.target.value)} className="h-11 flex-1 bg-background text-[15px] md:text-[15px]" />
        <Button type="submit" icon="play" size="lg" disabled={!valid || busy} className="h-11 sm:px-5">{busy ? 'Starting…' : 'Run'}</Button>
      </div>
      <div className="flex min-w-0 flex-col gap-2">
        <span id={legendId} className="text-[13px] tabular-nums text-muted-foreground">
          Engines, {picked.length} of {MAX_ENGINES}, pick {MIN_ENGINES}-{MAX_ENGINES}
        </span>
        {!store.ready ? <Skeleton height={36} width={320} radius={8} />
          : store.engines.length === 0 ? <p className="m-0 text-[13px] text-muted-foreground">No engines reported by the server.</p>
          : (
            <ToggleGroup type="multiple" value={picked} onValueChange={onGroupChange} aria-labelledby={legendId} spacing={2}
              className="w-full flex-wrap justify-start">
              {store.engines.map(e => {
                const on = picked.includes(e.name)
                const full = !on && picked.length >= MAX_ENGINES
                return (
                  <span key={e.name} className="inline-flex" title={!e.available ? e.why : full ? `At most ${MAX_ENGINES} engines` : e.label}>
                    <ToggleGroupItem value={e.name} disabled={!e.available || full} aria-label={e.label}
                      className={cn(
                        'h-9 gap-2 rounded-md border border-border bg-surface px-3 text-[13px] font-medium text-foreground shadow-none transition-colors',
                        'hover:bg-subtle hover:text-foreground focus-visible:ring-ring/35',
                        'data-[state=on]:border-primary/40 data-[state=on]:bg-primary/10 data-[state=on]:text-foreground',
                      )}>
                      <span className={cn('inline-flex', on ? 'text-primary' : 'text-muted-foreground')}><EngineIcon name={e.name} size={15} /></span>
                      <span>{e.label}</span>
                      {!e.available && <span className="text-xs font-normal text-muted-foreground">unavailable</span>}
                    </ToggleGroupItem>
                  </span>
                )
              })}
            </ToggleGroup>
          )}
      </div>
      {store.ready && available.length < MIN_ENGINES && (
        <p className="m-0 flex items-start gap-2 text-[13px] text-muted-foreground">
          <Icon name="warning" size={15} className="mt-0.5 shrink-0 text-warn" />
          <span>At least {MIN_ENGINES} available engines are needed to compare. Set them up in <a href="#/settings" className="font-medium text-primary underline-offset-2 hover:underline">Settings</a>.</span>
        </p>
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
    if (await copyText(location.href)) { setCopied(true); toast.success('Link copied'); window.setTimeout(() => setCopied(false), 1500) }
    else toast.error('Could not copy the link')
  }
  const stop = async (qid: number) => {
    try { await cancelRun(qid) } catch (err) { toast.error(`Could not stop run #${qid}: ${errText(err)}`) }
  }

  if (loading && !local) {
    return (
      <section className="@container" aria-busy="true" aria-label="Loading comparison">
        <div className={cn('grid grid-cols-1 gap-4', COLS[3])}>
          {[0, 1, 2].map(i => (
            <div key={i} className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4">
              <Skeleton height={20} width="55%" />
              <Skeleton height={40} radius={8} />
              <Skeleton height={120} radius={8} />
            </div>
          ))}
        </div>
      </section>
    )
  }
  if (!pairs.length) {
    return (
      <Card>
        <EmptyState icon="compare" title="Comparison not found" text={error ?? 'This comparison has no runs.'}
          action={<Button variant="secondary" onClick={() => navigate('/compare')}>New comparison</Button>} />
      </Card>
    )
  }

  return (
    <section className="@container flex min-w-0 flex-col gap-4" aria-label="Comparison result">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-0.5">
          <span className="text-[13px] text-muted-foreground">Question</span>
          <p className="m-0 text-base font-semibold leading-snug text-foreground [overflow-wrap:anywhere]">{query || '…'}</p>
        </div>
        <IconButton icon={copied ? 'check' : 'link'} label={copied ? 'Link copied' : 'Copy link to this comparison'} onClick={() => void share()} />
      </div>
      {error && !data && (
        <p role="status" className="m-0 flex items-start gap-2 rounded-md border border-border bg-subtle px-3 py-2 text-[13px] text-muted-foreground">
          <Icon name="alert" size={15} className="mt-0.5 shrink-0 text-warn" />
          <span>Could not refresh this comparison from the server ({error}). Showing what this browser remembers.</span>
        </p>
      )}
      <div className={cn('grid grid-cols-1 gap-4', COLS[Math.min(MAX_ENGINES, Math.max(1, columns.length))])}>
        {columns.map(c => <ResultCard key={c.qid} c={c} label={engineLabel(store.engines, c.engine)} fastest={c.qid === fastest} onStop={() => void stop(c.qid)} />)}
      </div>
    </section>
  )
}

function ResultCard({ c, label, fastest, onStop }: { c: Column; label: string; fastest: boolean; onStop: () => void }) {
  const cursor = c.streaming && c.status === 'running'
  return (
    <article aria-label={`${c.engine} answer`} className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-4">
      <header className="flex min-h-8 items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2 text-sm font-semibold text-foreground">
          <span className="inline-flex shrink-0 text-muted-foreground"><EngineIcon name={c.engine} size={16} /></span>
          <span className="truncate">{label}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          {fastest && <Badge tone="ok" icon="bolt">fastest</Badge>}
          <StatusBadge status={c.status} />
          {c.status === 'running' && <IconButton icon="stop" size="sm" label={`Stop ${c.engine} run`} onClick={onStop} />}
        </span>
      </header>
      <dl className="m-0 grid grid-cols-3 gap-2 rounded-md bg-subtle px-3 py-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <dt className="text-xs text-muted-foreground">Time</dt>
          <dd className="m-0 truncate text-sm font-medium tabular-nums text-foreground">{c.status === 'running' ? '…' : ms(c.total_ms)}</dd>
        </div>
        <div className="flex min-w-0 flex-col gap-0.5">
          <dt className="text-xs text-muted-foreground">Tokens</dt>
          <dd className="m-0 truncate text-sm font-medium tabular-nums text-foreground" title="Approximate output tokens (answer length ÷ 4)">≈{approxTokens(c.answer).toLocaleString()}</dd>
        </div>
        <div className="flex min-w-0 flex-col gap-0.5">
          <dt className="text-xs text-muted-foreground">Steps</dt>
          <dd className="m-0 truncate text-sm font-medium tabular-nums text-foreground">{c.plan.length || 'n/a'}</dd>
        </div>
      </dl>
      {c.agents.length > 0 && <div className="flex flex-wrap gap-1.5">{c.agents.map((a, i) => <AgentBadge key={i} agent={a} />)}</div>}
      {c.plan.length > 1 && (
        <details className="group rounded-md border border-border">
          <summary className="flex cursor-pointer list-none items-center gap-1.5 rounded-md px-3 py-2 text-[13px] font-medium text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35 [&::-webkit-details-marker]:hidden">
            <Icon name="next" size={14} className="shrink-0 group-open:rotate-90" />
            Plan <span className="tabular-nums">({c.plan.length} steps)</span>
          </summary>
          <ol className="m-0 flex list-decimal flex-col gap-1 border-t border-border py-2 pr-3 pl-8 text-[13px] leading-relaxed text-foreground">
            {c.plan.map((p, i) => <li key={i}>{p}</li>)}
          </ol>
        </details>
      )}
      <div className={cn(
        'min-w-0 text-sm leading-relaxed text-foreground [overflow-wrap:anywhere] md:max-h-[440px] md:overflow-y-auto',
        cursor && "[&_.md>:last-child]:after:ml-px [&_.md>:last-child]:after:animate-pulse [&_.md>:last-child]:after:text-primary [&_.md>:last-child]:after:content-['▍']",
      )}>
        {c.answer ? <Markdown text={c.answer} />
          : c.status === 'running' ? <Skeleton lines={3} />
          : <span className="text-muted-foreground">{c.error ?? 'No answer.'}</span>}
      </div>
      <a href={`#/runs/${c.qid}`}
        className="mt-auto inline-flex w-fit items-center gap-1 rounded-sm pt-1 text-[13px] font-medium text-primary underline-offset-2 outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/35">
        View trace <Icon name="external" size={12} />
      </a>
    </article>
  )
}

function engineLabel(engines: Array<{ name: string; label: string }>, name: string) {
  return engines.find(e => e.name === name)?.label ?? name
}

function HistoryList({ history, current, onClear }: { history: HistoryEntry[]; current?: string; onClear: () => void }) {
  const [confirming, setConfirming] = useState(false)
  const now = useNow()
  return (
    <Card flush aria-label="Past comparisons" title="Past comparisons" icon="history"
      actions={history.length > 0 && (confirming
        ? <span className="flex items-center gap-1.5">
            <span className="text-[13px] text-muted-foreground">Clear all?</span>
            <Button size="sm" variant="danger" onClick={() => { onClear(); setConfirming(false) }}>Clear</Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirming(false)}>Cancel</Button>
          </span>
        : <Button size="sm" variant="ghost" onClick={() => setConfirming(true)}>Clear history</Button>)}>
      {history.length === 0
        ? <EmptyState compact icon="history" title="No comparisons yet" text="Comparisons you run in this browser show up here." />
        : (
          <ul className="m-0 flex list-none flex-col gap-0.5 p-2">
            {history.map(h => {
              const on = h.id === current
              return (
                <li key={h.id}>
                  <a href={`#/compare/${h.id}`} aria-current={on ? 'page' : undefined}
                    className={cn(
                      'flex min-w-0 flex-col gap-1 rounded-md px-3 py-2 outline-none transition-colors hover:bg-subtle focus-visible:ring-[3px] focus-visible:ring-ring/35',
                      on && 'bg-subtle',
                    )}>
                    <span className={cn('truncate text-sm', on ? 'font-medium text-foreground' : 'text-foreground')}>{h.query}</span>
                    <span className="flex items-center gap-2 text-muted-foreground">
                      <span className="flex items-center gap-1">{h.runs.map(r => <EngineIcon key={r.qid} name={r.engine} size={13} />)}</span>
                      <span className="text-xs tabular-nums">{timeAgo(h.at, now)}</span>
                    </span>
                  </a>
                </li>
              )
            })}
          </ul>
        )}
    </Card>
  )
}

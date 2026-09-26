import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { listRuns, errorText, ApiError } from '../api'
import type { RunRecord } from '../protocol'
import { useStore } from '../store'
import { fromRecord, type Run } from '../useEventStream'
import { Button, Card, EmptyState, Skeleton, StatusBadge, navigate, timeAgo, useNow } from '../ui'
import { EngineIcon, Icon } from '../icons'
import { Chip } from '../components/Panels'
import { TopActions } from '../components/Shell'

// Every run, newest first: search, filter by source, status and engine, and page back with "Load more".
// The first page merges in live runs from the store so statuses update as they finish.

const PAGE = 50
const SOURCES = ['you', 'chat', 'compare', 'eval', 'autopilot']
const STATUSES = ['running', 'done', 'cancelled', 'timeout', 'error']

const secs = (v: number | null) => (v == null ? '–' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')

export default function Runs() {
  const { store } = useStore()
  const now = useNow(30000)
  const [q, setQ] = useState('')
  const [debounced, setDebounced] = useState('')
  const [source, setSource] = useState('')
  const [status, setStatus] = useState('')
  const [engine, setEngine] = useState('')
  const [pages, setPages] = useState<Run[]>([])
  const [loading, setLoading] = useState(true)
  const [more, setMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fallback, setFallback] = useState(false) // server has no /api/runs: show the in-memory history
  const search = useRef<HTMLInputElement>(null)
  const [nonce, setNonce] = useState(0)

  useEffect(() => { const t = window.setTimeout(() => setDebounced(q.trim()), 250); return () => clearTimeout(t) }, [q])

  const params = useMemo(() => ({ q: debounced || undefined, source: source || undefined, status: status || undefined, engine: engine && engine !== 'default' ? engine : undefined }), [debounced, source, status, engine])

  const load = useCallback(async (before?: number) => {
    const res = await listRuns({ limit: PAGE, before, ...params })
    return res.runs.map((r: RunRecord) => fromRecord(r))
  }, [params])

  useEffect(() => {
    let alive = true
    setLoading(true)
    load().then(
      runs => { if (!alive) return; setPages(runs); setMore(runs.length >= PAGE); setError(null); setFallback(false) },
      e => { if (!alive) return; setPages([]); setMore(false); if (e instanceof ApiError && (e.status === 404 || e.status === 405)) setFallback(true); else setError(errorText(e)) },
    ).finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [load, nonce])

  const loadMore = async () => {
    const oldest = pages[pages.length - 1]
    if (!oldest) return
    setLoadingMore(true)
    try {
      const runs = await load(oldest.qid)
      setPages(p => [...p, ...runs.filter(r => !p.some(x => x.qid === r.qid))])
      setMore(runs.length >= PAGE)
    } catch (e) { setError(errorText(e)) } finally { setLoadingMore(false) }
  }

  // Server filters are advisory; the same filters apply client-side so live runs and older servers agree.
  const matches = useCallback((r: Run) => {
    if (source && r.source !== source) return false
    if (status && (r.done ? r.status : 'running') !== status) return false
    if (engine && (r.engine ?? 'default') !== engine) return false
    if (debounced && !r.text.toLowerCase().includes(debounced.toLowerCase()) && String(r.qid) !== debounced.replace('#', '')) return false
    return true
  }, [source, status, engine, debounced])

  const rows = useMemo(() => {
    const byQid = new Map<number, Run>()
    for (const r of pages) byQid.set(r.qid, r)
    const newest = pages[0]?.qid ?? 0
    for (const r of store.runs) {
      if (!r.text) continue
      // Live copies replace fetched rows (fresher status) and newer live runs join the top of the first page.
      if (byQid.has(r.qid) || r.qid > newest || fallback) byQid.set(r.qid, r)
    }
    return [...byQid.values()].filter(matches).sort((a, b) => b.qid - a.qid)
  }, [pages, store.runs, matches, fallback])

  const engines = useMemo(() => {
    const set = new Set<string>(store.engines.map(e => e.name))
    for (const r of rows) if (r.engine) set.add(r.engine)
    return [...set]
  }, [store.engines, rows])

  const filtered = !!(debounced || source || status || engine)
  const clear = () => { setQ(''); setSource(''); setStatus(''); setEngine(''); search.current?.focus() }

  return (
    <div className="wrap runs-page">
      <TopActions><span className="muted small num">{rows.length} shown</span></TopActions>
      <div className="filters" role="search">
        <label className="search-field">
          <Icon name="search" size={16} />
          <input ref={search} value={q} onChange={e => setQ(e.target.value)} placeholder="Search queries or #id" aria-label="Search runs" type="search" />
        </label>
        <Select label="Source" value={source} onChange={setSource} options={SOURCES} />
        <Select label="Status" value={status} onChange={setStatus} options={STATUSES} />
        <Select label="Engine" value={engine} onChange={setEngine} options={['default', ...engines]} />
        {filtered && <Button variant="ghost" size="sm" icon="close" onClick={clear}>Clear</Button>}
      </div>
      {fallback && <p className="notice"><Icon name="info" size={14} />This server does not list stored runs yet, so only the runs in memory are shown.</p>}

      <Card flush className="runs-card">
        {loading && !rows.length ? (
          <div className="table-skel">{Array.from({ length: 8 }, (_, i) => <Skeleton key={i} height={18} />)}</div>
        ) : error && !rows.length ? (
          <EmptyState icon="alert" title="Could not load runs" text={error} action={<Button variant="secondary" icon="refresh" onClick={() => setNonce(n => n + 1)}>Retry</Button>} />
        ) : !rows.length ? (
          filtered
            ? <EmptyState icon="filter" title="No runs match" text="Try a different search or clear the filters." action={<Button variant="secondary" onClick={clear}>Clear filters</Button>} />
            : <EmptyState icon="runs" title="No runs yet" text="Ask something in Chat or on the Live page and it will show up here." action={<Button icon="chat" onClick={() => navigate('/')}>Open chat</Button>} />
        ) : (
          <div className="table-wrap runs-table">
            <table>
              <thead>
                <tr>
                  <th className="col-id">#</th><th>Query</th><th className="hide-sm">Source</th><th>Status</th>
                  <th className="hide-md">Engine</th><th className="hide-sm">Agents</th><th className="num-h hide-xs">Time</th><th className="num-h hide-md">When</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(r => {
                  const agents = [...new Set(r.order.map(t => r.tasks[t]?.routed?.agent).filter((a): a is string => !!a))]
                  return (
                    <tr key={r.qid} className="row-link" onClick={e => { if (!(e.target as HTMLElement).closest('a')) navigate('/runs/' + r.qid) }}>
                      <td className="num col-id">{r.qid}</td>
                      <td className="col-q">
                        <a href={`#/runs/${r.qid}`} className="q-link">{r.text}</a>
                        {r.files.length > 0 && <span className="muted small" title={`${r.files.length} file(s)`}> <Icon name="paperclip" size={12} />{r.files.length}</span>}
                        <span className="show-sm muted small"> · {r.source} · {timeAgo(r.at, now)}</span>
                      </td>
                      <td className="hide-sm"><span className="src-tag">{r.source}</span></td>
                      <td><StatusBadge status={r.done ? r.status : 'running'} /></td>
                      <td className="hide-md">{r.engine ? <span className="eng"><EngineIcon name={r.engine} size={13} />{r.engine}</span> : <span className="muted">default</span>}</td>
                      <td className="hide-sm"><span className="chips">{agents.slice(0, 3).map(a => <Chip key={a} agent={a} />)}{agents.length > 3 && <span className="muted small">+{agents.length - 3}</span>}</span></td>
                      <td className="num hide-xs">{r.done ? secs(r.total_ms) : '…'}</td>
                      <td className="num hide-md" title={new Date(r.at * 1000).toLocaleString()}>{timeAgo(r.at, now)}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        )}
        {more && !fallback && rows.length > 0 && (
          <div className="load-more"><Button variant="secondary" loading={loadingMore} onClick={() => void loadMore()} icon="chevron-down">Load more</Button></div>
        )}
      </Card>
    </div>
  )
}

function Select({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: string[] }) {
  return (
    <label className={'select' + (value ? ' on' : '')}>
      <span className="sr-only">{label}</span>
      <select value={value} onChange={e => onChange(e.target.value)} aria-label={label}>
        <option value="">{label}: all</option>
        {options.map(o => <option key={o} value={o}>{label}: {o}</option>)}
      </select>
      <Icon name="chevron-down" size={14} />
    </label>
  )
}

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { listRuns, errorText, ApiError } from '../api'
import type { RunRecord } from '../protocol'
import { useStore } from '../store'
import { fromRecord, type Run } from '../useEventStream'
import { Badge, Button, EmptyState, buttonClass, Skeleton, navigate, timeAgo, useNow } from '../ui'
import { Icon } from '../icons'
import { AgentBadge, EngineBadge, PageBody, StatusDot } from '../components/app'
import { TopActions } from '../components/Shell'
import { Select as UiSelect, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'

// Every run, newest first: search, filter by source, status and engine, and page back with "Load more".
// The first page merges in live runs from the store so statuses update as they finish.

const PAGE = 50
const SOURCES = ['you', 'chat', 'compare', 'eval', 'autopilot']
const STATUSES = ['running', 'done', 'cancelled', 'timeout', 'error']

const secs = (v: number | null) => (v == null ? '-' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')

// Column visibility, shared by the header, the rows and the skeleton so they always line up.
const COL = {
  id: 'w-14 pl-4 sm:pl-5 text-right font-mono text-xs tabular-nums text-muted-foreground',
  query: 'w-full min-w-[14rem] whitespace-normal',
  source: 'hidden md:table-cell',
  status: 'hidden md:table-cell',
  engine: 'hidden lg:table-cell',
  agents: 'hidden xl:table-cell',
  suspects: 'hidden lg:table-cell text-right',
  time: 'hidden md:table-cell text-right tabular-nums',
  when: 'hidden lg:table-cell pr-4 sm:pr-5 text-right tabular-nums',
}

export default function Runs() {
  const { store } = useStore()
  const now = useNow(30000)
  const [q, setQ] = useState('')
  const [debounced, setDebounced] = useState('')
  const [source, setSource] = useState('')
  const [status, setStatus] = useState('')
  const [engine, setEngine] = useState('')
  const [suspect, setSuspect] = useState(false) // only runs the suspect checks flagged
  const [pages, setPages] = useState<Run[]>([])
  const [loading, setLoading] = useState(true)
  const [more, setMore] = useState(false)
  const [loadingMore, setLoadingMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [fallback, setFallback] = useState(false) // server has no /api/runs: show the in-memory history
  const search = useRef<HTMLInputElement>(null)
  const [nonce, setNonce] = useState(0)
  const requestVersion = useRef(0)

  useEffect(() => { const t = window.setTimeout(() => setDebounced(q.trim()), 250); return () => clearTimeout(t) }, [q])

  const params = useMemo(() => ({ q: debounced || undefined, source: source || undefined, status: status || undefined, engine: engine || undefined,
    suspect: suspect || undefined }), [debounced, source, status, engine, suspect])

  const load = useCallback(async (before?: number) => {
    const res = await listRuns({ limit: PAGE, before, ...params })
    return res.runs.map((r: RunRecord) => fromRecord(r))
  }, [params])

  useEffect(() => {
    let alive = true
    requestVersion.current += 1
    setLoadingMore(false)
    setError(null)
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
    if (loading || loadingMore) return
    const version = requestVersion.current
    setLoadingMore(true)
    try {
      const runs = await load(oldest.qid)
      if (version !== requestVersion.current) return
      setPages(p => [...p, ...runs.filter(r => !p.some(x => x.qid === r.qid))])
      setMore(runs.length >= PAGE)
    } catch (e) { if (version === requestVersion.current) setError(errorText(e)) } finally { if (version === requestVersion.current) setLoadingMore(false) }
  }

  // Server filters are advisory; the same filters apply client-side so live runs and older servers agree.
  const matches = useCallback((r: Run) => {
    if (source && r.source !== source) return false
    if (status && (r.done ? r.status : 'running') !== status) return false
    if (engine && (r.engine ?? 'none') !== engine) return false
    if (suspect && !r.suspects?.length) return false
    if (debounced && !r.text.toLowerCase().includes(debounced.toLowerCase()) && String(r.qid) !== debounced.replace('#', '')) return false
    return true
  }, [source, status, engine, debounced, suspect])

  const rows = useMemo(() => {
    const byQid = new Map<number, Run>()
    for (const r of pages) byQid.set(r.qid, r)
    const newest = pages[0]?.qid ?? 0
    for (const r of store.runs) {
      if (!r.text) continue
      // Live copies replace fetched rows (fresher status) and newer live runs join the top of the first page.
      // Suspects are computed when a run is saved, so a live copy keeps the fetched row's.
      const prev = byQid.get(r.qid)
      if (prev || r.qid > newest || fallback) byQid.set(r.qid, prev?.suspects && !r.suspects ? { ...r, suspects: prev.suspects } : r)
    }
    return [...byQid.values()].filter(matches).sort((a, b) => b.qid - a.qid)
  }, [pages, store.runs, matches, fallback])

  const engines = useMemo(() => {
    const set = new Set<string>(store.engines.map(e => e.name))
    for (const r of rows) if (r.engine) set.add(r.engine)
    return [...set]
  }, [store.engines, rows])

  const filtered = !!(debounced || source || status || engine || suspect)
  const activeSelects = [source, status, engine].filter(Boolean).length + (suspect ? 1 : 0)
  const clear = () => { setQ(''); setSource(''); setStatus(''); setEngine(''); setSuspect(false); search.current?.focus() }

  const selects = (full?: boolean) => (
    <>
      <FilterSelect label="Source" value={source} onChange={setSource} options={SOURCES} full={full} />
      <FilterSelect label="Status" value={status} onChange={setStatus} options={STATUSES} full={full} />
      <FilterSelect label="Engine" value={engine} onChange={setEngine} options={[...new Set(['none', ...engines])]} full={full} />
      <button type="button" data-slot="button" aria-pressed={suspect} onClick={() => setSuspect(v => !v)}
        title="Only runs that the checks flagged as likely wrong"
        className={cn(buttonClass('secondary', 'sm'), 'h-8 bg-surface text-[13px] font-normal shadow-none', full && 'w-full justify-start',
          suspect && 'border-primary/40 bg-primary/5 text-foreground dark:bg-primary/10')}>
        <Icon name="flag" size={14} className={suspect ? 'text-warn' : 'text-muted-foreground'} />Suspect
      </button>
    </>
  )

  return (
    <PageBody>
      <TopActions><span className="text-xs tabular-nums text-muted-foreground">{rows.length} shown</span></TopActions>

      <div className="flex flex-col gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-2" role="search">
          <label className="relative min-w-0 flex-1 basis-56 md:max-w-sm">
            <Icon name="search" size={16} className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-muted-foreground" />
            <input ref={search} value={q} onChange={e => setQ(e.target.value)} placeholder="Search queries or #id" aria-label="Search runs" type="search"
              data-slot="input"
              className="h-9 w-full min-w-0 rounded-md border border-input bg-surface pr-3 pl-9 text-sm text-foreground outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/35" />
          </label>

          <div className="hidden items-center gap-2 md:flex">{selects()}</div>

          <Popover>
            <PopoverTrigger data-slot="button" className={buttonClass('secondary', 'md', 'md:hidden')}
              aria-label={activeSelects ? `Filters, ${activeSelects} active` : 'Filters'}>
              <Icon name="filter" size={16} strokeWidth={2} />
              <span>Filters</span>
              {activeSelects > 0 && <span className="grid size-5 place-items-center rounded-sm bg-primary/10 text-xs font-medium tabular-nums text-primary">{activeSelects}</span>}
            </PopoverTrigger>
            <PopoverContent align="end" className="flex w-64 flex-col gap-2 p-3">
              {selects(true)}
            </PopoverContent>
          </Popover>

          {filtered && <Button variant="ghost" size="sm" icon="close" onClick={clear}>Clear</Button>}
        </div>

        {fallback && <Notice icon="info">This server does not list stored runs yet, so only the runs in memory are shown.</Notice>}
        {error && rows.length > 0 && <Notice icon="alert" tone="bad" role="alert">{error}</Notice>}
      </div>

      <section className="min-w-0 overflow-clip rounded-lg border border-border bg-surface md:[&_[data-slot=table-container]]:overflow-visible">
        {loading && !rows.length ? (
          <RunsSkeleton />
        ) : error && !rows.length ? (
          <EmptyState icon="alert" title="Could not load runs" text={error} action={<Button variant="secondary" icon="refresh" onClick={() => setNonce(n => n + 1)}>Retry</Button>} />
        ) : !rows.length ? (
          filtered
            ? <EmptyState icon="filter" title="No runs match" text="Try a different search or clear the filters." action={<Button variant="secondary" onClick={clear}>Clear filters</Button>} />
            : <EmptyState icon="runs" title="No runs yet" text="Ask something in Chat or on the Live page and it will show up here." action={<Button icon="chat" onClick={() => navigate('/')}>Open chat</Button>} />
        ) : (
          <Table>
            <RunsHead />
            <TableBody>
              {rows.map(r => {
                const agents = [...new Set(r.order.map(t => r.tasks[t]?.routed?.agent).filter((a): a is string => !!a))]
                const st = r.done ? r.status : 'running'
                const when = timeAgo(r.at, now)
                return (
                  <TableRow key={r.qid} className="cursor-pointer border-border hover:bg-subtle/60 focus-within:bg-subtle/60"
                    onClick={e => { if (!(e.target as HTMLElement).closest('a')) navigate('/runs/' + r.qid) }}>
                    <TableCell className={cn(COL.id, 'py-3 align-top md:align-middle')}>{r.qid}</TableCell>
                    <TableCell className={cn(COL.query, 'py-3')}>
                      <div className="flex min-w-0 items-start gap-2">
                        <a href={`#/runs/${r.qid}`}
                          className="line-clamp-2 min-w-0 rounded-sm font-medium text-foreground outline-none hover:underline hover:underline-offset-2 focus-visible:ring-[3px] focus-visible:ring-ring/35">
                          {r.text}
                        </a>
                        {r.files.length > 0 && (
                          <span className="inline-flex shrink-0 items-center gap-0.5 pt-0.5 text-xs tabular-nums text-muted-foreground" title={`${r.files.length} file(s)`}>
                            <Icon name="paperclip" size={12} />{r.files.length}
                          </span>
                        )}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground lg:hidden">
                        <span className="md:hidden">{r.source}</span>
                        <StatusDot status={st} className="text-xs md:hidden" />
                        <span className="tabular-nums md:hidden">{r.done ? secs(r.total_ms) : '…'}</span>
                        <span className="tabular-nums" title={new Date(r.at * 1000).toLocaleString()}>{when}</span>
                        {!!r.suspects?.length && <span className="inline-flex items-center gap-1 text-warn lg:hidden"><Icon name="flag" size={12} />{r.suspects.length} suspect</span>}
                      </div>
                    </TableCell>
                    <TableCell className={COL.source}><Badge tone="neutral">{r.source}</Badge></TableCell>
                    <TableCell className={COL.status}><StatusDot status={st} /></TableCell>
                    <TableCell className={COL.engine}><EngineBadge name={r.engine} /></TableCell>
                    <TableCell className={COL.agents}>
                      <span className="flex items-center gap-1">
                        {agents.slice(0, 3).map(a => <AgentBadge key={a} agent={a} />)}
                        {agents.length > 3 && <span className="text-xs tabular-nums text-muted-foreground">+{agents.length - 3}</span>}
                        {!agents.length && <span className="text-[13px] text-muted-foreground">-</span>}
                      </span>
                    </TableCell>
                    <TableCell className={COL.suspects}>
                      {r.suspects?.length
                        ? <Badge tone="warn" icon="flag" title={r.suspects.map(x => x.note || x.code).join('\n')}>{r.suspects.length}</Badge>
                        : <span className="text-[13px] text-muted-foreground">-</span>}
                    </TableCell>
                    <TableCell className={cn(COL.time, 'text-[13px] text-muted-foreground')}>{r.done ? secs(r.total_ms) : '…'}</TableCell>
                    <TableCell className={cn(COL.when, 'text-[13px] text-muted-foreground')} title={new Date(r.at * 1000).toLocaleString()}>{when}</TableCell>
                  </TableRow>
                )
              })}
            </TableBody>
          </Table>
        )}
        {more && !fallback && !loading && (
          <div className="flex justify-center border-t border-border p-3">
            <Button variant="secondary" size="sm" loading={loadingMore} onClick={() => void loadMore()} icon="chevron-down">Load more</Button>
          </div>
        )}
      </section>
    </PageBody>
  )
}

function RunsHead() {
  const th = 'md:sticky md:top-[var(--topbar-h)] z-10 h-10 bg-surface text-xs font-medium text-muted-foreground'
  return (
    <TableHeader>
      <TableRow className="border-border hover:bg-transparent">
        <TableHead className={cn(th, COL.id)}>#</TableHead>
        <TableHead className={cn(th, COL.query)}>Query</TableHead>
        <TableHead className={cn(th, COL.source)}>Source</TableHead>
        <TableHead className={cn(th, COL.status)}>Status</TableHead>
        <TableHead className={cn(th, COL.engine)}>Engine</TableHead>
        <TableHead className={cn(th, COL.agents)}>Agents</TableHead>
        <TableHead className={cn(th, COL.suspects)}>Suspect</TableHead>
        <TableHead className={cn(th, COL.time)}>Time</TableHead>
        <TableHead className={cn(th, COL.when)}>When</TableHead>
      </TableRow>
    </TableHeader>
  )
}

function RunsSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading runs">
      <Table>
        <RunsHead />
        <TableBody>
          {Array.from({ length: 8 }, (_, i) => (
            <TableRow key={i} className="border-border hover:bg-transparent">
              <TableCell className={cn(COL.id, 'py-3')}><Skeleton width="70%" height={12} className="ml-auto" /></TableCell>
              <TableCell className={cn(COL.query, 'py-3')}>
                <Skeleton width={`${55 + ((i * 17) % 35)}%`} height={14} />
                <Skeleton width="40%" height={10} className="mt-2 lg:hidden" />
              </TableCell>
              <TableCell className={COL.source}><Skeleton width={48} height={18} /></TableCell>
              <TableCell className={COL.status}><Skeleton width={56} height={12} /></TableCell>
              <TableCell className={COL.engine}><Skeleton width={64} height={12} /></TableCell>
              <TableCell className={COL.agents}><Skeleton width={120} height={22} /></TableCell>
              <TableCell className={COL.suspects}><Skeleton width={24} height={18} className="ml-auto" /></TableCell>
              <TableCell className={COL.time}><Skeleton width={40} height={12} className="ml-auto" /></TableCell>
              <TableCell className={COL.when}><Skeleton width={48} height={12} className="ml-auto" /></TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  )
}

function Notice({ icon, tone, role, children }: { icon: 'info' | 'alert'; tone?: 'bad'; role?: string; children: ReactNode }) {
  return (
    <p role={role} className="m-0 flex items-start gap-2 rounded-md border border-border bg-subtle px-3 py-2 text-[13px] text-muted-foreground">
      <Icon name={icon} size={14} className={cn('mt-0.5 shrink-0', tone === 'bad' ? 'text-destructive' : 'text-muted-foreground')} />
      <span className="min-w-0">{children}</span>
    </p>
  )
}

// Radix Select cannot hold an empty value, so "all" stands in for the empty filter.
function FilterSelect({ label, value, onChange, options, full }: { label: string; value: string; onChange: (v: string) => void; options: string[]; full?: boolean }) {
  return (
    <UiSelect value={value || 'all'} onValueChange={v => onChange(v === 'all' ? '' : v)}>
      <SelectTrigger size="sm" aria-label={label}
        className={cn('min-w-0 bg-surface text-[13px] shadow-none dark:bg-surface', full ? 'w-full' : 'w-auto', value && 'border-primary/40 bg-primary/5 dark:bg-primary/10')}>
        <span className="text-muted-foreground">{label}:</span>
        <SelectValue />
      </SelectTrigger>
      <SelectContent position="popper" align="start">
        <SelectItem value="all">all</SelectItem>
        {options.map(o => <SelectItem key={o} value={o}>{o === 'none' ? 'keyless' : o}</SelectItem>)}
      </SelectContent>
    </UiSelect>
  )
}

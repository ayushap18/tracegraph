import { useCallback, useEffect, useMemo, useState } from 'react'
import { cancelEval, errorText as errText, getEval, listEvals, runEvalWith } from '../api'
import { Badge, Button, Card, EmptyState, ProgressBar, Skeleton, StatusBadge, navigate, useToast } from '../ui'
import { useStore } from '../store'
import { EngineIcon, Icon } from '../icons'
import type { EvalCase, EvalDetail, EvalSummary } from '../protocol'
import { AgentBadge, BackLink, EngineBadge, PageBody, Stat, StatStrip } from '../components/app'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'
import { Sparkline } from '../components/Viz'
import { ms } from '../lib'
import EvalCompare from './evals/EvalCompare'
import { CELL, CHIP, HEAD, LINK_ICON, accTone, compareHref, parseCompare, pctOf, when } from './evals/shared'
import { DEFAULT_OPTS, RunOptions, type RunOpts } from './evals/RunOptions'
import { CaseExtras, CaseFlags, CaseTime, TagScorecard, extrasLabel, hasExtras, tagScores } from './evals/CaseExtras'

// Eval suite: run the cases in evals/cases.jsonl through the real pipeline, watch progress live over SSE,
// and compare accuracy across runs. `#/evals/:id` drills into one run's cases; `#/evals/<a>...<b>` compares two.

const None = () => <span className="text-muted-foreground">none</span>

export default function Evals({ params }: { params: Record<string, string> }) {
  const pair = parseCompare(params.id)
  if (pair) return <EvalCompare a={pair[0]} b={pair[1]} />
  return params.id ? <EvalDetailPage key={params.id} id={params.id} /> : <EvalList />
}

// Route examples for one eval run: follow the server switch, or force them on or off for this run only.
type ExamplesMode = 'current' | 'on' | 'off'
const EXAMPLES_LABEL: Record<ExamplesMode, string> = { current: 'Current setting', on: 'On', off: 'Off' }

function ExamplesBadge() {
  return <Badge tone="accent" title="Route examples were on for this run">examples</Badge>
}

interface Progress { eval_id: string; done: number; total: number; passed: number }

function EvalList() {
  const { store, subscribe } = useStore()
  const toast = useToast()
  const [evals, setEvals] = useState<EvalSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [engine, setEngine] = useState<string>('')
  const [starting, setStarting] = useState(false)
  const [progress, setProgress] = useState<Progress | null>(null)
  const [examples, setExamples] = useState<ExamplesMode>('current')
  const [opts, setOpts] = useState<RunOpts>(DEFAULT_OPTS)
  const [picked, setPicked] = useState<string[]>([]) // up to two eval ids to compare
  const pick = (id: string) => setPicked(p => p.includes(id) ? p.filter(x => x !== id) : [...p, id].slice(-2))

  const load = useCallback(async () => {
    try { const res = await listEvals(); setEvals(res.evals); setError(null) } catch (err) { setError(errText(err)) }
  }, [])
  useEffect(() => { void load() }, [load])

  // Default the engine select to the active engine once it is known.
  useEffect(() => { if (!engine && store.ready) setEngine(store.engine?.name ?? 'none') }, [store.ready, store.engine, engine])

  // The server's list is the source of truth: seed progress for an eval already running when the page opens, and
  // clear it once that eval is no longer running (a late progress event after Cancel would otherwise stick).
  useEffect(() => {
    const running = evals?.find(e => e.status === 'running')
    if (running) setProgress({ eval_id: running.eval_id, done: running.done ?? 0, total: running.total, passed: running.passed })
    else if (progress && evals?.some(e => e.eval_id === progress.eval_id && e.status !== 'running')) setProgress(null)
  }, [evals]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => subscribe(e => {
    if (e.type === 'eval_progress') {
      setProgress({ eval_id: e.eval_id, done: e.done, total: e.total, passed: e.passed })
    } else if (e.type === 'eval_done') {
      setProgress(p => (p && p.eval_id !== e.eval_id ? p : null))
      if (e.status === 'cancelled') toast.info(`Eval cancelled after ${e.passed}/${e.total} passed`)
      else toast.success(`Eval finished: ${e.passed}/${e.total} passed (${pctOf(e.accuracy)})`)
      void load()
    }
  }), [subscribe, load, toast])

  const start = async () => {
    setStarting(true)
    try {
      const res = await runEvalWith({
        ...(engine ? { engine } : {}), ...(examples === 'current' ? {} : { examples: examples === 'on' }),
        split: opts.split, repeat: opts.repeat, judge: opts.judge === 'none' ? null : opts.judge,
        ...(opts.tags.length ? { tags: opts.tags } : {}),
      })
      setProgress(p => p?.eval_id === res.eval_id ? p : { eval_id: res.eval_id, done: 0, total: 0, passed: 0 })
      void load()
    } catch (err) {
      toast.error(`Could not start the eval: ${errText(err)}`)
    } finally { setStarting(false) }
  }
  const stop = async () => {
    if (!progress) return
    try { await cancelEval(progress.eval_id); setProgress(null); void load() }
    catch (err) { toast.error(`Could not cancel: ${errText(err)}`) }
  }

  useEffect(() => {
    if (!progress) return
    const timer = window.setInterval(() => void load(), 3000)
    return () => clearInterval(timer)
  }, [!!progress, load])

  const sorted = useMemo(() => (evals ?? []).slice().sort((a, b) => b.at - a.at), [evals])
  const trend = useMemo(() => sorted.filter(e => e.status === 'done' && e.total > 0).map(e => e.accuracy).reverse(), [sorted])
  const latest = sorted.find(e => e.status === 'done')
  const prev = sorted.filter(e => e.status === 'done')[1]
  const delta = latest && prev ? latest.accuracy - prev.accuracy : null
  const best = trend.length ? Math.max(...trend) : null
  // Tags seen in earlier runs feed the tag filter; the server has no separate list of them.
  const knownTags = useMemo(() => [...new Set((evals ?? []).flatMap(e => [...Object.keys(e.by_tag ?? {}), ...(e.tags ?? [])]))].sort(), [evals])

  return (
    <PageBody>
      <section aria-label="Run an eval" className="flex min-w-0 flex-col gap-4 rounded-lg border border-border bg-surface p-4 sm:p-5">
        <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between md:gap-6">
          <p className="m-0 max-w-[70ch] text-[13px] leading-relaxed text-muted-foreground">
            Runs the cases in <code className="rounded-sm bg-subtle px-1 font-mono text-[12px] text-foreground">evals/cases.jsonl</code> through the real pipeline and scores agents, outcomes, answers, latency budgets and, with a judge, open-ended answers.
          </p>
          <div className="flex shrink-0 flex-col gap-2 sm:flex-row sm:items-center">
            <label className="sr-only" htmlFor="ev-engine">Engine</label>
            <Select value={engine} onValueChange={setEngine} disabled={!!progress}>
              <SelectTrigger id="ev-engine" className="h-9 w-full bg-background sm:w-52">
                <SelectValue placeholder="Engine" />
              </SelectTrigger>
              <SelectContent position="popper" align="end">
                <SelectItem value="none">Keyless (no engine)</SelectItem>
                {store.engines.filter(e => e.name !== 'none').map(e => (
                  <SelectItem key={e.name} value={e.name} disabled={!e.available}>{e.label}{e.available ? '' : ' (unavailable)'}</SelectItem>
                ))}
              </SelectContent>
            </Select>
            <label className="sr-only" htmlFor="ev-examples">Route examples</label>
            <Select value={examples} onValueChange={v => setExamples(v as ExamplesMode)} disabled={!!progress}>
              <SelectTrigger id="ev-examples" className="h-9 w-full bg-background sm:w-56" title="Route examples for this run">
                <span className="truncate"><span className="text-muted-foreground">Examples: </span>{EXAMPLES_LABEL[examples]}</span>
              </SelectTrigger>
              <SelectContent position="popper" align="end">
                {(['current', 'on', 'off'] as ExamplesMode[]).map(m => <SelectItem key={m} value={m}>{EXAMPLES_LABEL[m]}</SelectItem>)}
              </SelectContent>
            </Select>
            {progress
              ? <Button variant="danger" icon="stop" onClick={() => void stop()}>Cancel</Button>
              : <Button icon="play" onClick={() => void start()} disabled={starting}>{starting ? 'Starting…' : 'Run eval'}</Button>}
          </div>
        </div>
        <RunOptions opts={opts} onChange={setOpts} knownTags={knownTags} engines={store.engines} engine={engine} disabled={!!progress} />
        {progress && (
          <div aria-live="polite" className="flex flex-col gap-2 rounded-md bg-subtle px-3 py-2.5">
            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[13px]">
              <span className="min-w-0 truncate text-muted-foreground">
                Running <a href={`#/evals/${progress.eval_id}`} className="font-mono text-xs font-medium text-primary underline-offset-2 hover:underline">{progress.eval_id}</a>
              </span>
              <span className="tabular-nums text-foreground">{progress.total ? `${progress.done}/${progress.total} cases, ${progress.passed} passed` : 'starting…'}</span>
            </div>
            <ProgressBar value={progress.total ? progress.done / progress.total : 0} label="Eval progress" />
          </div>
        )}
      </section>

      <section aria-label="Accuracy summary">
        <StatStrip cols={4}>
          <Stat label="Latest accuracy" value={latest ? pctOf(latest.accuracy) : 'n/a'} tone={latest ? accTone(latest.accuracy) : undefined}
            note={delta == null ? 'no previous run' : `${delta >= 0 ? '+' : ''}${Math.round(delta * 100)} pts vs previous`} />
          <Stat label="Trend" value={best == null ? 'n/a' : pctOf(best)}
            chart={trend.length > 1 ? <Sparkline values={trend} min={0} height={28} /> : undefined}
            note={trend.length > 1 ? undefined : 'best accuracy'} />
          <Stat label="Silent wrong" value={latest ? latest.silent_wrong : 'n/a'} tone={latest?.silent_wrong ? 'bad' : undefined}
            note="failed cases that looked ok" />
          <Stat label="Runs" value={evals ? evals.length : 'n/a'} note={latest ? `last ${when(latest.at)}` : 'none yet'} />
        </StatStrip>
      </section>

      <Card flush title="History" icon="history" aria-label="Eval history"
        actions={evals && evals.length > 1 ? <span className="hidden text-xs text-muted-foreground sm:inline">Tick two runs to compare them</span> : undefined}>
        {picked.length > 0 && (
          <div role="status" className="flex flex-wrap items-center justify-between gap-2 border-b border-border bg-primary/5 px-4 py-2.5 sm:px-5">
            <span className="text-[13px] text-foreground">{picked.length === 1 ? 'Pick one more run to compare with.' : 'Two runs picked. The older one is A.'}</span>
            <span className="flex items-center gap-2">
              <Button size="sm" variant="ghost" onClick={() => setPicked([])}>Clear</Button>
              <Button size="sm" icon="compare" disabled={picked.length < 2} onClick={() => {
                const [x, y] = picked.map(id => sorted.find(e => e.eval_id === id)!).sort((p, q) => p.at - q.at)
                navigate(compareHref(x.eval_id, y.eval_id))
              }}>Compare</Button>
            </span>
          </div>
        )}
        {evals == null && !error
          ? <div className="flex flex-col gap-3 p-4 sm:p-5" aria-busy="true">{[0, 1, 2, 3].map(i => <Skeleton key={i} height={28} radius={6} />)}</div>
          : error && !evals ? <EmptyState icon="evals" title="Could not load evals" text={error}
              action={<Button variant="secondary" onClick={() => void load()}>Retry</Button>} />
          : sorted.length === 0 ? <EmptyState icon="evals" title="No evals yet" text="Run the suite to get a baseline accuracy." />
          : (
            <>
              <div className="hidden md:block">
                <Table>
                  <TableHeader>
                    <TableRow className="border-border hover:bg-transparent">
                      <TableHead className={cn(HEAD, 'w-10')}><span className="sr-only">Compare</span></TableHead>
                      <TableHead className={HEAD}>When</TableHead>
                      <TableHead className={HEAD}>Engine</TableHead>
                      <TableHead className={HEAD}>Status</TableHead>
                      <TableHead className={cn(HEAD, 'text-right')}>Passed</TableHead>
                      <TableHead className={cn(HEAD, 'hidden text-right lg:table-cell')}>p50 / p95</TableHead>
                      <TableHead className={cn(HEAD, 'text-right')}>Accuracy</TableHead>
                      <TableHead className={cn(HEAD, 'text-right')}>Silent wrong</TableHead>
                      <TableHead className={cn(HEAD, 'w-10')}><span className="sr-only">Open</span></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {sorted.map(e => (
                      <TableRow key={e.eval_id} className="cursor-pointer border-border hover:bg-subtle" onClick={() => navigate(`/evals/${e.eval_id}`)}>
                        <TableCell className={CELL} onClick={ev => ev.stopPropagation()}>
                          <PickBox checked={picked.includes(e.eval_id)} onChange={() => pick(e.eval_id)} label={`Compare eval from ${when(e.at)}`} />
                        </TableCell>
                        <TableCell className={cn(CELL, 'tabular-nums text-foreground')}>{when(e.at)}</TableCell>
                        <TableCell className={CELL}>
                          <span className="flex flex-wrap items-center gap-2"><EngineBadge name={e.engine ?? 'none'} label={e.engine ?? 'keyless'} />{e.examples && <ExamplesBadge />}<RunBadges e={e} /></span>
                        </TableCell>
                        <TableCell className={CELL}><StatusBadge status={e.status} /></TableCell>
                        <TableCell className={cn(CELL, 'text-right tabular-nums')}>
                          <span className="flex flex-col items-end">{e.passed}/{e.total}{!!e.flaky && <span className="text-xs font-medium text-warn">{e.flaky} flaky</span>}</span>
                        </TableCell>
                        <TableCell className={cn(CELL, 'hidden text-right tabular-nums text-muted-foreground lg:table-cell')}>{latencyPair(e)}</TableCell>
                        <TableCell className={cn(CELL, 'text-right')}><AccBar v={e.accuracy} /></TableCell>
                        <TableCell className={cn(CELL, 'text-right tabular-nums', e.silent_wrong ? 'font-medium text-destructive' : 'text-muted-foreground')}>{e.silent_wrong}</TableCell>
                        <TableCell className={cn(CELL, 'text-right')}>
                          <a href={`#/evals/${e.eval_id}`} onClick={ev => ev.stopPropagation()} aria-label={`Open eval ${e.eval_id}`} className={LINK_ICON}>
                            <Icon name="next" size={15} />
                          </a>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
              <ul className="m-0 flex list-none flex-col divide-y divide-border p-0 md:hidden">
                {sorted.map(e => (
                  <li key={e.eval_id} className="flex items-start">
                    <span className="py-3 pl-4"><PickBox checked={picked.includes(e.eval_id)} onChange={() => pick(e.eval_id)} label={`Compare eval from ${when(e.at)}`} /></span>
                    <a href={`#/evals/${e.eval_id}`} aria-label={`Open eval ${e.eval_id}`}
                      className="flex min-w-0 flex-1 flex-col gap-2 py-3 pr-4 pl-3 outline-none transition-colors hover:bg-subtle focus-visible:bg-subtle">
                      <span className="flex items-center justify-between gap-3">
                        <span className="flex min-w-0 flex-wrap items-center gap-2"><EngineBadge name={e.engine ?? 'none'} label={e.engine ?? 'keyless'} />{e.examples && <ExamplesBadge />}<RunBadges e={e} /></span>
                        <StatusBadge status={e.status} />
                      </span>
                      <span className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-[13px]">
                        <span className="tabular-nums text-muted-foreground">{when(e.at)}</span>
                        <AccBar v={e.accuracy} />
                      </span>
                      <span className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
                        <span className="tabular-nums">{e.passed}/{e.total} passed</span>
                        <span className={cn('tabular-nums', e.silent_wrong > 0 && 'font-medium text-destructive')}>{e.silent_wrong} silent wrong</span>
                        {!!e.flaky && <span className="tabular-nums font-medium text-warn">{e.flaky} flaky</span>}
                        {e.p50_ms != null && <span className="tabular-nums">{latencyPair(e)}</span>}
                      </span>
                    </a>
                  </li>
                ))}
              </ul>
            </>
          )}
      </Card>
    </PageBody>
  )
}

const fmtS = (v: number | null | undefined) => (v == null ? '-' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')
const latencyPair = (e: EvalSummary) => (e.p50_ms == null && e.p95_ms == null ? '-' : `${fmtS(e.p50_ms)} / ${fmtS(e.p95_ms)}`)

/** Split, repeat count and judge of one run, shown only when they differ from a plain single pass over all cases. */
function RunBadges({ e }: { e: EvalSummary }) {
  return (
    <>
      {e.split && e.split !== 'all' && <Badge tone={e.split === 'holdout' ? 'info' : 'neutral'} title={`Split: ${e.split}`}>{e.split}</Badge>}
      {e.repeat != null && e.repeat > 1 && <Badge title={`Each case ran ${e.repeat} times`}>x{e.repeat}</Badge>}
      {e.judge && <Badge title={`Open-ended answers judged by ${e.judge}`}>judge {e.judge}</Badge>}
      {!!e.tags?.length && <Badge title={`Only cases tagged ${e.tags.map(t => '#' + t).join(', ')}`}>{e.tags.length === 1 ? '#' + e.tags[0] : `${e.tags.length} tags`}</Badge>}
    </>
  )
}

function PickBox({ checked, onChange, label }: { checked: boolean; onChange: () => void; label: string }) {
  return (
    <input type="checkbox" checked={checked} onChange={onChange} aria-label={label}
      className="size-4 cursor-pointer rounded-sm accent-primary align-middle focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35" />
  )
}

const BAR_TONE = { ok: 'bg-ok', warn: 'bg-warn', bad: 'bg-destructive' }

function AccBar({ v }: { v: number }) {
  return (
    <span className="inline-flex items-center justify-end gap-2 tabular-nums text-foreground">
      <span className="flex h-1.5 w-12 justify-start" aria-hidden="true">
        <span className={cn('h-full rounded-full', BAR_TONE[accTone(v)])} style={{ width: `${Math.max(0, Math.min(1, v)) * 100}%` }} />
      </span>
      <span className="w-10 text-right">{pctOf(v)}</span>
    </span>
  )
}

type PassFilter = 'all' | 'pass' | 'fail' | 'flaky'
const ANY_TAG = '__any'
const USER_TAG = 'user'

function EvalDetailPage({ id }: { id: string }) {
  const { subscribe } = useStore()
  const [data, setData] = useState<EvalDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<PassFilter>('all')
  const [tag, setTag] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)

  const load = useCallback(async () => {
    try { setData(await getEval(id)); setError(null) } catch (err) { setError(errText(err)) }
  }, [id])
  useEffect(() => { void load() }, [load])

  // While this eval runs, refresh after each finished case.
  useEffect(() => subscribe(e => {
    if ((e.type === 'eval_progress' || e.type === 'eval_done') && e.eval_id === id) void load()
  }), [subscribe, id, load])

  useEffect(() => {
    if (data?.status !== 'running') return
    const timer = window.setInterval(() => void load(), 3000)
    return () => clearInterval(timer)
  }, [data?.status, load])

  const cases = data?.cases ?? []
  // "user" (cases promoted from your labels) leads the tag chips.
  const tags = useMemo(() => [...new Set(cases.flatMap(c => c.tags ?? []))].sort((x, y) => Number(y === USER_TAG) - Number(x === USER_TAG) || x.localeCompare(y)), [cases])
  const tagCount = (t: string) => cases.filter(c => c.tags?.includes(t)).length
  const shown = cases.filter(c => (filter === 'all' || (filter === 'flaky' ? !!c.flaky : (filter === 'pass') === c.pass)) && (!tag || c.tags?.includes(tag)))
  const failCount = cases.filter(c => !c.pass).length
  const flakyCount = cases.filter(c => c.flaky).length
  const scores = useMemo(() => tagScores(data?.by_tag, cases), [data?.by_tag, cases])
  const overBudget = cases.filter(c => c.over_budget).length

  if (!data) {
    return (
      <PageBody>
        <BackLink href="#/evals">All evals</BackLink>
        {error
          ? <Card><EmptyState icon="evals" title="Eval not found" text={error}
              action={<Button variant="secondary" onClick={() => navigate('/evals')}>Back to evals</Button>} /></Card>
          : (
            <div className="flex flex-col gap-6" aria-busy="true">
              <Skeleton height={92} radius={12} />
              <div className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4 sm:p-5">
                <Skeleton height={32} width={260} radius={8} />
                {[0, 1, 2, 3].map(i => <Skeleton key={i} height={28} radius={6} />)}
              </div>
            </div>
          )}
      </PageBody>
    )
  }

  const toggles = (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border px-4 py-3 sm:px-5">
      <ToggleGroup type="single" value={filter} onValueChange={v => { if (v) setFilter(v as PassFilter) }} spacing={1} aria-label="Filter by result" className="flex-wrap">
        {(['all', 'pass', 'fail', ...(flakyCount ? ['flaky'] : [])] as PassFilter[]).map(f => (
          <ToggleGroupItem key={f} value={f} className={CHIP}>
            {f === 'all' ? 'All' : f === 'pass' ? 'Passed' : f === 'fail' ? 'Failed' : 'Flaky'}
            <span className="tabular-nums text-muted-foreground">{f === 'all' ? cases.length : f === 'pass' ? cases.length - failCount : f === 'fail' ? failCount : flakyCount}</span>
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      {tags.length > 0 && (
        <ToggleGroup type="single" value={tag ?? ANY_TAG} onValueChange={v => setTag(!v || v === ANY_TAG ? null : v)} spacing={1} aria-label="Filter by tag" className="flex-wrap">
          <ToggleGroupItem value={ANY_TAG} className={CHIP}>any tag</ToggleGroupItem>
          {tags.map(t => (
            <ToggleGroupItem key={t} value={t} className={cn(CHIP, 'font-mono text-xs')} title={t === USER_TAG ? 'Cases added from your route labels' : undefined}>
              #{t}<span className="font-sans tabular-nums text-muted-foreground">{tagCount(t)}</span>
            </ToggleGroupItem>
          ))}
        </ToggleGroup>
      )}
    </div>
  )

  return (
    <PageBody>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <BackLink href="#/evals">All evals</BackLink>
        <CompareWith id={id} />
      </div>
      <section aria-label="Eval summary" className="flex flex-col gap-3">
        <StatStrip cols={4}>
          <Stat label="Accuracy" value={pctOf(data.accuracy)} tone={data.status === 'done' ? accTone(data.accuracy) : undefined} note={`${data.passed}/${data.total} passed`} />
          <Stat label="Engine" value={<span className="inline-flex items-center gap-2 text-lg"><EngineIcon name={data.engine ?? 'none'} size={18} />{data.engine ?? 'keyless'}</span>}
            note={`${when(data.at)}${data.examples == null ? '' : data.examples ? ', examples on' : ', examples off'}`} />
          <Stat label="Status" value={<span className="inline-flex h-8 items-center"><StatusBadge status={data.status} /></span>} note={`${cases.length} of ${data.total} cases recorded`} />
          <Stat label="Silent wrong" value={data.silent_wrong} tone={data.silent_wrong ? 'bad' : undefined} note="failed, yet every answer said ok" />
        </StatStrip>
        {(data.split != null || data.repeat != null || data.p50_ms != null || data.judge != null) && (
          <StatStrip cols={4}>
            <Stat label="Latency p50 / p95" size="md" value={data.p50_ms == null && data.p95_ms == null ? 'n/a' : `${fmtS(data.p50_ms)} / ${fmtS(data.p95_ms)}`}
              tone={overBudget ? 'warn' : undefined} note={overBudget ? `${overBudget} case${overBudget === 1 ? '' : 's'} over budget` : 'no case over budget'} />
            <Stat label="Flaky" value={data.flaky ?? flakyCount} tone={(data.flaky ?? flakyCount) ? 'warn' : undefined}
              note={(data.repeat ?? 1) > 1 ? `each case ran ${data.repeat} times` : 'ran once, repeat to measure'} />
            <Stat label="Judge mean" value={data.judge_mean == null ? 'n/a' : data.judge_mean.toFixed(1)}
              tone={data.judge_errors?.length ? 'bad' : data.judge_mean == null ? undefined : data.judge_mean >= 3.5 ? 'ok' : 'bad'}
              note={data.judge_errors?.length ? `${data.judge_errors.length} case${data.judge_errors.length === 1 ? '' : 's'} not judged: ${data.judge_errors[0].replace(/^[^:]*:\s*/, '')}`
                : data.judge ? `of 5, judged by ${data.judge}` : 'no judge for this run'} />
            <Stat label="Split" size="md" value={data.split === 'dev' ? 'Dev' : data.split === 'holdout' ? 'Holdout' : 'All cases'}
              note={data.tags?.length ? data.tags.map(t => '#' + t).join(' ') : 'every tag'} />
          </StatStrip>
        )}
        {data.status === 'running' && <ProgressBar value={data.total ? cases.length / data.total : 0} label="Eval progress" />}
      </section>

      {Object.keys(scores).length > 0 && (
        <Card title="By tag" icon="tag" subtitle="Weakest first. Click a tag to show only its cases.">
          <TagScorecard scores={scores} active={tag} onPick={setTag} />
        </Card>
      )}

      <Card flush title="Cases" icon="evals" aria-label="Cases">
        {toggles}
        {shown.length === 0
          ? <EmptyState icon="search" title="No matching cases" text={cases.length ? 'Try another filter.' : 'No cases have finished yet.'} />
          : (
            <>
              <div className="hidden md:block">
                <Table className="table-fixed">
                  <TableHeader>
                    <TableRow className="border-border hover:bg-transparent">
                      <TableHead className={cn(HEAD, 'w-28')}>Result</TableHead>
                      <TableHead className={cn(HEAD, 'w-[24%]')}>Query</TableHead>
                      <TableHead className={cn(HEAD, 'w-[18%]')}>Agents (expected / got)</TableHead>
                      <TableHead className={cn(HEAD, 'w-[20%]')}>Reasons</TableHead>
                      <TableHead className={HEAD}>Answer</TableHead>
                      <TableHead className={cn(HEAD, 'w-20 text-right')}>Time</TableHead>
                      <TableHead className={cn(HEAD, 'w-12')}><span className="sr-only">Run</span></TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {shown.map((c, i) => <CaseRow key={c.id + ':' + i} c={c} open={open === c.id} onToggle={() => setOpen(open === c.id ? null : c.id)} />)}
                  </TableBody>
                </Table>
              </div>
              <ul className="m-0 flex list-none flex-col divide-y divide-border p-0 md:hidden">
                {shown.map((c, i) => <CaseCard key={c.id + ':' + i} c={c} open={open === c.id} onToggle={() => setOpen(open === c.id ? null : c.id)} />)}
              </ul>
            </>
          )}
      </Card>
    </PageBody>
  )
}

/** "Compare with..." on one run: this run is B, the picked one is A. */
function CompareWith({ id }: { id: string }) {
  const [others, setOthers] = useState<EvalSummary[] | null>(null)
  useEffect(() => { listEvals().then(r => setOthers(r.evals.filter(e => e.eval_id !== id && e.status !== 'running').sort((a, b) => b.at - a.at)), () => setOthers([])) }, [id])
  if (!others?.length) return null
  return (
    <Select value="" onValueChange={v => navigate(compareHref(v, id))}>
      <SelectTrigger aria-label="Compare with another eval" className="h-8 w-full bg-background text-[13px] sm:w-56">
        <span className="flex items-center gap-1.5 text-muted-foreground"><Icon name="compare" size={14} />Compare with…</span>
      </SelectTrigger>
      <SelectContent position="popper" align="end">
        {others.map(e => (
          <SelectItem key={e.eval_id} value={e.eval_id}>{when(e.at)}, {e.engine ?? 'keyless'}{e.examples ? ', examples' : ''}, {pctOf(e.accuracy)}</SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}

// Pieces shared by the table row (md+) and the stacked card (phones).

function ResultBadge({ pass }: { pass: boolean }) {
  return <Badge tone={pass ? 'ok' : 'bad'} icon={pass ? 'success' : 'error'}>{pass ? 'pass' : 'fail'}</Badge>
}

/** Opens the case's extras (turns, judge scores, attempts); it shares the row's open state with the long answer. */
function ExtrasToggle({ c, open, onToggle }: { c: EvalCase; open: boolean; onToggle: () => void }) {
  if (!hasExtras(c)) return null
  return (
    <button type="button" data-slot="button" aria-expanded={open} onClick={onToggle}
      className="inline-flex cursor-pointer items-center gap-1 self-start rounded-sm bg-transparent p-0 text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/35">
      <Icon name="chevron-down" size={12} className={cn('transition-transform', open && 'rotate-180')} />
      {open ? 'Hide' : 'Show'} {extrasLabel(c).toLowerCase()}
    </button>
  )
}

function Tags({ tags }: { tags: string[] | undefined }) {
  if (!tags?.length) return null
  return (
    <span className="flex flex-wrap gap-1">
      {tags.map(t => <span key={t} className={cn('rounded-sm px-1 font-mono text-[11px]', t === USER_TAG ? 'bg-primary/10 text-primary' : 'bg-subtle text-muted-foreground')}>#{t}</span>)}
    </span>
  )
}

function CaseAgents({ c }: { c: EvalCase }) {
  // The API may echo the case's expectations; show them when present.
  const expected = (c as EvalCase & { expect_agents?: string[] }).expect_agents ?? []
  return (
    <div className="flex flex-col gap-1.5">
      {expected.length > 0 && (
        <span className="flex flex-wrap items-center gap-1">
          <span className="w-14 shrink-0 text-xs text-muted-foreground">expected</span>
          {expected.map(a => <AgentBadge key={a} agent={a} />)}
        </span>
      )}
      <span className="flex flex-wrap items-center gap-1">
        {expected.length > 0 && <span className="w-14 shrink-0 text-xs text-muted-foreground">got</span>}
        {c.agents?.length ? c.agents.map((a, i) => <AgentBadge key={i} agent={a} />) : <None />}
      </span>
    </div>
  )
}

function CaseReasons({ reasons }: { reasons: string[] | undefined }) {
  return reasons?.length
    ? <ul className="m-0 flex list-disc flex-col gap-0.5 pl-4 text-[13px] leading-snug text-foreground marker:text-muted-foreground">{reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
    : <None />
}

function CaseAnswer({ answer, open, onToggle }: { answer: string; open: boolean; onToggle: () => void }) {
  if (answer.length > 160) {
    return (
      <button type="button" data-slot="button" aria-expanded={open} onClick={onToggle}
        className="cursor-pointer rounded-sm bg-transparent p-0 text-left text-[13px] leading-snug text-foreground outline-none [overflow-wrap:anywhere] focus-visible:ring-[3px] focus-visible:ring-ring/35">
        {open ? answer : answer.slice(0, 160) + '…'}
        <span className="ml-1 font-medium text-primary">{open ? 'less' : 'more'}</span>
      </button>
    )
  }
  return <span className="text-[13px] leading-snug text-foreground [overflow-wrap:anywhere]">{answer || <None />}</span>
}

function RunLink({ qid }: { qid: number | null }) {
  if (qid == null) return null
  return <a href={`#/runs/${qid}`} aria-label={`Open run ${qid}`} title={`Run #${qid}`} className={LINK_ICON}><Icon name="external" size={14} /></a>
}

function CaseRow({ c, open, onToggle }: { c: EvalCase; open: boolean; onToggle: () => void }) {
  const tone = c.pass ? 'hover:bg-subtle/60' : 'bg-destructive/[0.04] hover:bg-destructive/[0.07]'
  return (
    <>
      <TableRow className={cn('border-border align-top', tone, open && hasExtras(c) && 'border-b-0')}>
        <TableCell className={cn(CELL, 'align-top whitespace-normal')}><span className="flex flex-col items-start gap-1"><ResultBadge pass={c.pass} /><CaseFlags c={c} /></span></TableCell>
        <TableCell className={cn(CELL, 'align-top whitespace-normal')}>
          <div className="flex flex-col gap-1">
            <span className="text-[13px] font-medium leading-snug text-foreground [overflow-wrap:anywhere]">{c.query}</span>
            <Tags tags={c.tags} />
            <ExtrasToggle c={c} open={open} onToggle={onToggle} />
          </div>
        </TableCell>
        <TableCell className={cn(CELL, 'align-top whitespace-normal')}><CaseAgents c={c} /></TableCell>
        <TableCell className={cn(CELL, 'align-top whitespace-normal')}><CaseReasons reasons={c.reasons} /></TableCell>
        <TableCell className={cn(CELL, 'align-top whitespace-normal')}><CaseAnswer answer={c.answer ?? ''} open={open} onToggle={onToggle} /></TableCell>
        <TableCell className={cn(CELL, 'align-top text-right text-[13px] tabular-nums text-muted-foreground')}><CaseTime c={c} /></TableCell>
        <TableCell className={cn(CELL, 'align-top text-right')}><RunLink qid={c.qid} /></TableCell>
      </TableRow>
      {open && hasExtras(c) && (
        <TableRow className={cn('border-border hover:bg-transparent', !c.pass && 'bg-destructive/[0.04] hover:bg-destructive/[0.04]')}>
          <TableCell colSpan={7} className={cn(CELL, 'whitespace-normal pt-0 pb-4')}>
            <div className="rounded-md border border-border bg-surface p-3 sm:p-4"><CaseExtras c={c} /></div>
          </TableCell>
        </TableRow>
      )}
    </>
  )
}

function CaseCard({ c, open, onToggle }: { c: EvalCase; open: boolean; onToggle: () => void }) {
  return (
    <li className={cn('flex flex-col gap-3 px-4 py-3', !c.pass && 'bg-destructive/[0.04]')}>
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="text-sm font-medium leading-snug text-foreground [overflow-wrap:anywhere]">{c.query}</span>
          <Tags tags={c.tags} />
        </div>
        <span className="flex shrink-0 items-center gap-1">
          <ResultBadge pass={c.pass} />
          <RunLink qid={c.qid} />
        </span>
      </div>
      {(c.flaky || c.over_budget || c.judge || c.judge_error || c.split === 'holdout') && <span className="flex flex-wrap gap-1"><CaseFlags c={c} /></span>}
      <dl className="m-0 grid grid-cols-[4.5rem_minmax(0,1fr)] gap-x-3 gap-y-2 text-[13px]">
        <dt className="text-xs text-muted-foreground">Agents</dt>
        <dd className="m-0 min-w-0"><CaseAgents c={c} /></dd>
        <dt className="text-xs text-muted-foreground">Reasons</dt>
        <dd className="m-0 min-w-0"><CaseReasons reasons={c.reasons} /></dd>
        <dt className="text-xs text-muted-foreground">Answer</dt>
        <dd className="m-0 min-w-0"><CaseAnswer answer={c.answer ?? ''} open={open} onToggle={onToggle} /></dd>
        <dt className="text-xs text-muted-foreground">Time</dt>
        <dd className="m-0 tabular-nums text-muted-foreground">{ms(c.ms)}{c.max_ms != null && <span className={cn('ml-1', c.over_budget && 'font-medium text-warn')}>(budget {ms(c.max_ms)})</span>}</dd>
      </dl>
      <ExtrasToggle c={c} open={open} onToggle={onToggle} />
      {open && hasExtras(c) && <div className="rounded-md border border-border bg-surface p-3"><CaseExtras c={c} /></div>}
    </li>
  )
}

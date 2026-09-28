import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { colorOf, type ControlState } from '../protocol'
import type { Run, Store, Task } from '../useEventStream'
import { DEFAULT_LIMITS, post } from '../useEventStream'
import { clock, costs, latency, money, ms, pct, rows, safeHref } from '../lib'
import Markdown from './Markdown'
import { StepPolicy } from './StepPolicy'
import { Icon, type UiIconName } from '../icons'
import { Sparkline, useCountUp } from './Viz'
import { AgentBadge, ChartCard, EngineBadge, Meta, Stat, StatStrip } from './app'
import { Badge, Button, Card, EmptyState, IconButton, Kbd, iconButtonClass } from '../ui'
import { Switch } from '@/components/ui/switch'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'
import { askOrConfirm, errorText } from '../api'
import type { AskBody } from '../protocol'
import { useCostConfirm } from './chat/CostDialog'

/** An agent label. Kept as `Chip` for existing call sites; renders the shared AgentBadge. */
export function Chip({ agent, pct }: { agent: string | undefined; pct?: number | string }) {
  return <AgentBadge agent={agent} pct={pct} />
}

/** Routing probabilities as thin trackless bars in each agent's colour. */
export function Bars({ probs, max = 5 }: { probs: Record<string, number>; max?: number }) {
  const list = Object.entries(probs).sort((a, b) => b[1] - a[1]).slice(0, max)
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      {list.map(([a, p]) => (
        <div className="grid grid-cols-[5.5rem_minmax(0,1fr)_2.75rem] items-center gap-2 text-xs" key={a}>
          <span className="truncate text-muted-foreground">{a}</span>
          <div className="h-1.5 min-w-0">
            <div className="h-full min-w-[2px] rounded-full transition-[width] duration-500" style={{ width: `${p * 100}%`, background: colorOf(a) }} />
          </div>
          <span className="text-right tabular-nums text-muted-foreground">{pct(p)}</span>
        </div>
      ))}
    </div>
  )
}

const RANGE_CLS = 'h-4 w-28 cursor-pointer accent-primary'

export function AskBox({ samples, state, inputRef, maxChars = DEFAULT_LIMITS.query_chars }: {
  samples: string[]; state: ControlState; inputRef: RefObject<HTMLInputElement>; maxChars?: number // the server's query limit
}) {
  const [q, setQ] = useState('')
  const past = useRef<string[]>([])
  const cursor = useRef(-1)
  const [interval, setIntervalV] = useState(state.interval)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const switchId = useId()
  useEffect(() => setIntervalV(state.interval), [state.interval])
  // A costly run comes back with an estimate and starts only after Continue (docs/PLAN-files-robust.md 6.2).
  const cost = useCostConfirm()
  const sendBody = async (body: AskBody): Promise<void> => {
    setBusy(true)
    setError(null)
    try {
      const r = await askOrConfirm(body)
      if (r.kind === 'confirm') cost.ask({ estimate: r.estimate, body, go: sendBody, onCancel: () => setQ(q => q || body.query) })
    } catch (e) {
      setError(`Could not send that query: ${errorText(e)}`)
    } finally {
      setBusy(false)
    }
  }
  const ask = async (text: string) => {
    const v = text.trim().slice(0, maxChars)
    if (!v) return
    past.current = [v, ...past.current.filter(p => p !== v)].slice(0, 30)
    cursor.current = -1
    await sendBody({ query: v })
  }
  const submit = (e: FormEvent) => { e.preventDefault(); void ask(q); setQ('') }
  // Up/Down walk back through what you asked this session, like a shell.
  const recall = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
    const list = past.current
    if (!list.length) return
    e.preventDefault()
    cursor.current = Math.max(-1, Math.min(list.length - 1, cursor.current + (e.key === 'ArrowUp' ? 1 : -1)))
    setQ(cursor.current < 0 ? '' : list[cursor.current])
  }
  const commitInterval = () => void post('/control', { interval })
  const range = (
    <input type="range" min={1} max={15} step={0.5} value={interval} aria-label="Autopilot interval in seconds" className={RANGE_CLS}
      onChange={e => setIntervalV(+e.target.value)} onPointerUp={commitInterval} onKeyUp={commitInterval} />
  )
  return (
    <section className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-3 sm:p-4" aria-label="Ask">
      <form className="flex flex-wrap items-center gap-2 sm:gap-3" onSubmit={submit}>
        <div className="relative min-w-0 flex-[1_1_320px]">
          <Icon name="search" size={18} className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-muted-foreground" />
          <input ref={inputRef} value={q} onChange={e => setQ(e.target.value)} onKeyDown={recall} maxLength={maxChars} autoComplete="off" aria-label="Query"
            placeholder="Ask anything, or several things: weather in Paris and convert 100 EUR to INR"
            className="h-11 w-full min-w-0 rounded-md border border-input bg-background pr-10 pl-10 text-base text-foreground shadow-xs outline-none transition-[border-color,box-shadow] placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/35 sm:text-[15px]" />
          <span className="pointer-events-none absolute top-1/2 right-3 hidden -translate-y-1/2 sm:block"><Kbd>/</Kbd></span>
        </div>
        <Button type="submit" size="lg" icon="send" className="h-11" disabled={busy || !q.trim()}>Route</Button>
        <div className="flex items-center gap-2 sm:border-l sm:border-border sm:pl-3">
          <Switch id={switchId} checked={state.autopilot} onCheckedChange={() => void post('/control', { autopilot: !state.autopilot })} />
          <label htmlFor={switchId} className="cursor-pointer text-sm font-medium text-foreground">Autopilot</label>
          <label className="hidden items-center gap-2 text-[13px] text-muted-foreground xl:flex">
            every {range} <span className="w-9 tabular-nums text-foreground">{interval}s</span>
          </label>
          <span className="flex items-center gap-1 xl:hidden">
            <span className="text-[13px] tabular-nums text-muted-foreground">every {interval}s</span>
            <Popover>
              <PopoverTrigger asChild>
                <button type="button" data-slot="button" className={iconButtonClass('ghost', 'sm')} aria-label="Autopilot interval" title="Autopilot interval">
                  <Icon name="clock" size={15} strokeWidth={1.9} />
                </button>
              </PopoverTrigger>
              <PopoverContent align="end" className="w-64 rounded-lg p-3">
                <label className="flex flex-col gap-2 text-[13px] text-muted-foreground">
                  <span className="flex items-center justify-between text-foreground">
                    <span className="font-medium">Autopilot interval</span>
                    <span className="tabular-nums">{interval}s</span>
                  </span>
                  <input type="range" min={1} max={15} step={0.5} value={interval} className="h-4 w-full cursor-pointer accent-primary"
                    onChange={e => setIntervalV(+e.target.value)} onPointerUp={commitInterval} onKeyUp={commitInterval} />
                  <span className="text-xs">One sample query every {interval} seconds while autopilot is on.</span>
                </label>
              </PopoverContent>
            </Popover>
          </span>
        </div>
      </form>
      {error && <p className="m-0 text-[13px] text-destructive" role="alert">{error}</p>}
      {cost.dialog}
      {samples.length > 0 && (
        <div role="group" aria-label="Sample queries"
          className="-mb-1 flex gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none] [mask-image:linear-gradient(to_right,black_calc(100%-48px),transparent)] [&::-webkit-scrollbar]:hidden">
          {samples.slice(0, 14).map(s => (
            <button type="button" data-slot="button" key={s} onClick={() => void ask(s)}
              className="h-7 shrink-0 cursor-pointer whitespace-nowrap rounded-md border border-border bg-surface px-2.5 text-xs text-muted-foreground outline-none transition-colors last:mr-10 hover:bg-subtle hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35">
              {s}
            </button>
          ))}
        </div>
      )}
    </section>
  )
}

function Kpi({ icon, label, value, fmt, spark, color, min, note }: {
  icon: UiIconName; label: string; value: number | null; fmt: (v: number) => string; spark?: number[]; color?: string; min?: number; note?: string
}) {
  const v = useCountUp(value ?? 0)
  return (
    <Stat icon={icon} label={label} value={value == null ? '-' : fmt(v)} note={note}
      chart={spark && spark.length > 1 ? <Sparkline values={spark} color={color} min={min} /> : undefined} />
  )
}

export function Kpis({ store }: { store: Store }) {
  const routed = rows(store.runs).map(r => r.task.routed).filter((r): r is NonNullable<Task['routed']> => !!r)
  const avg = (f: (x: NonNullable<Task['routed']>) => number) => (routed.length ? routed.reduce((s, r) => s + f(r), 0) / routed.length : null)
  const c = costs(store.stats, store.prices)
  const recent = routed.slice(-30)
  const done = store.runs.filter(r => r.done && r.total_ms != null).slice(-30)
  const multi = store.runs.filter(r => r.order.length > 1).length
  const int = (v: number) => Math.round(v).toLocaleString()
  return (
    <section aria-label="Key numbers">
      <StatStrip cols={6}>
        <Kpi icon="queries" label="Queries" value={store.stats.queries} fmt={int} spark={store.runs.slice(-30).map(r => r.order.length)} min={0}
          note={`${multi} multi-agent`} />
        <Kpi icon="subtasks" label="Subtasks routed" value={store.stats.subtasks} fmt={int} note={`${store.stats.errors} errors`} />
        <Kpi icon="latency" label="Avg Jev latency" value={avg(r => r.jev_ms)} fmt={v => int(v) + ' ms'} spark={recent.map(r => r.jev_ms)} />
        <Kpi icon="confidence" label="Avg confidence" value={avg(r => r.confidence)} fmt={v => Math.round(v * 100) + '%'} spark={recent.map(r => r.confidence)} color="var(--ok)" min={0} />
        <Kpi icon="activity" label="End-to-end" value={done.length ? done.reduce((s, r) => s + (r.total_ms ?? 0), 0) / done.length : null} fmt={v => int(v) + ' ms'}
          spark={done.map(r => r.total_ms ?? 0)} color="var(--warn)" min={0} />
        <Kpi icon="spend" label="Spend" value={c.jev + c.claude} fmt={money}
          note={!store.engine ? `Jev only · ${store.stats.jev_input_tokens.toLocaleString()} tokens`
            : store.engine.billing === 'subscription' ? `Jev ${money(c.jev)} · ${store.engine.label} on subscription`
            : `Jev ${money(c.jev)} · ${store.engine.label} ${money(c.claude)}`} />
      </StatStrip>
    </section>
  )
}

const WELL = 'min-w-0 break-words rounded-md bg-subtle px-3 py-2.5 text-sm text-foreground'
const CURSOR = "after:ml-0.5 after:animate-pulse after:text-primary after:content-['▍']"

function Def({ label, children, wide }: { label: string; children: ReactNode; wide?: boolean }) {
  return (
    <div className={cn('flex min-w-0 flex-col gap-0.5', wide && 'col-span-2 sm:col-span-4')}>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="m-0 min-w-0 break-words tabular-nums text-foreground">{children}</dd>
    </div>
  )
}

export function TaskCard({ task, className, bare }: { task: Task; className?: string; bare?: boolean }) {
  const r = task.routed, a = task.answered
  const text = a ? a.answer : task.stream
  return (
    <div className={cn('flex min-w-0 flex-col gap-3', !bare && 'rounded-lg border border-border bg-surface p-3.5', className)}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
        <span className="font-mono text-xs tabular-nums text-muted-foreground">{task.tid}</span>
        {r ? <AgentBadge agent={r.agent} pct={r.confidence} /> : <span className="text-xs text-muted-foreground">{task.error ? 'not routed' : 'routing…'}</span>}
        {(r || a) && (
          <span className="ml-auto flex items-center gap-3 text-xs tabular-nums text-muted-foreground">
            {r && <span>Jev {r.jev_ms} ms</span>}
            {a && <span>agent {a.agent_ms} ms</span>}
          </span>
        )}
      </div>
      <p className="m-0 min-w-0 break-words text-sm font-medium text-foreground">{task.text || '…'}</p>
      {r && (
        <dl className="m-0 grid grid-cols-2 gap-x-4 gap-y-2 text-xs sm:grid-cols-4">
          {r.reason && <Def label="Reason" wide>{r.reason}</Def>}
          <Def label="Urgency">{r.urgency.toFixed(1)}</Def>
          <Def label="Clear">{pct(r.clear)}</Def>
          <Def label="Unsafe">{pct(r.unsafe)}</Def>
          <Def label="Confidence">{pct(r.confidence)}</Def>
        </dl>
      )}
      {r && <Bars probs={r.probabilities} max={4} />}
      {task.error && !r && <div className={cn(WELL, 'text-muted-foreground')}>{task.error}</div>}
      {(text || r) && (
        <div className={cn(WELL, a && !a.ok && 'text-muted-foreground', !a && r && CURSOR)}>{text ? <Markdown text={text} /> : 'Agent working…'}</div>
      )}
      {a && (
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
          <EngineBadge name={a.engine} className="text-xs" />
          {a.source && <a href={safeHref(a.source)} target="_blank" rel="noopener noreferrer" className="min-w-0 break-all text-primary hover:underline">{a.source}</a>}
        </div>
      )}
    </div>
  )
}

export function LatestRun({ run, engine }: { run: Run | undefined; engine: Store['engine'] }) {
  if (!run) return (
    <Card title="Latest run" icon="activity">
      <EmptyState compact icon="activity" title="No runs yet"
        text={`Ask something above, or switch on Autopilot to stream sample queries. ${engine ? `LLM engine: ${engine.label}.` : 'Running keyless (no LLM engine).'}`} />
    </Card>
  )
  const multi = run.order.length > 1
  const mergeText = run.merged?.answer ?? run.mergeStream
  return (
    <Card title="Latest run" icon="activity" className="lg:max-h-[760px] lg:overflow-auto"
      actions={<span className="text-xs tabular-nums text-muted-foreground">#{run.qid}, {run.done ? ms(run.total_ms) : 'running'}</span>}>
      <div className="flex min-w-0 flex-col gap-3">
        <p className="m-0 break-words text-[15px] font-semibold text-foreground">
          {run.text || '…'} {run.source === 'autopilot' && <Badge className="ml-1 align-middle">auto</Badge>}
        </p>
        <Meta className="text-xs tabular-nums">
          {run.plan ? <>
            <Badge>{run.plan.planner} planner</Badge>
            <span>{run.order.length} subtask{run.order.length === 1 ? '' : 's'}</span>
            {run.plan.ms != null && <span>{run.plan.ms} ms</span>}
            {run.plan.multi != null && <span>multi {pct(run.plan.multi)}</span>}
          </> : <span>planning…</span>}
        </Meta>
        <div className="flex min-w-0 flex-col gap-3">{run.order.map(tid => run.tasks[tid] && <TaskCard key={tid} task={run.tasks[tid]} />)}</div>
        {(multi || (run.merged && run.merged.engine !== 'single')) && (
          <div className="mt-1 flex min-w-0 flex-col gap-2">
            <h3 className="m-0 flex flex-wrap items-center gap-2 text-[13px] font-semibold text-foreground">
              Merged answer {run.merged && <Badge>{run.merged.engine}</Badge>}
              {run.merged?.ms != null && <span className="text-xs font-normal tabular-nums text-muted-foreground">{run.merged.ms} ms</span>}
            </h3>
            <div className={cn('min-w-0 break-words rounded-md border border-border bg-subtle px-3.5 py-3 text-[14.5px] text-foreground', !run.merged && CURSOR)}>
              {mergeText ? <Markdown text={mergeText} /> : 'Waiting for agents…'}
            </div>
          </div>
        )}
        {run.error && <p className="m-0 text-[13px] text-destructive">{run.error}</p>}
      </div>
    </Card>
  )
}

export function RoutingLog({ runs }: { runs: Run[] }) {
  const list = rows(runs).reverse().slice(0, 30)
  return (
    <Card flush title="Routing log" icon="log"
      actions={<span className="text-xs tabular-nums text-muted-foreground">{list.length ? `Last ${list.length} subtasks` : 'Empty'}</span>}>
      <div className="max-h-[420px] overflow-auto">
        <table data-slot="table" className="w-full caption-bottom border-collapse text-[13px]">
          <TableHeader className="sticky top-0 z-[1] bg-surface shadow-[inset_0_-1px_0_var(--line)] [&_tr]:border-0">
            <TableRow className="hover:bg-transparent">
              <TableHead className="hidden h-9 bg-surface px-4 text-xs font-medium tracking-normal normal-case text-muted-foreground sm:table-cell">Time</TableHead>
              <TableHead className="h-9 bg-surface px-4 text-xs font-medium tracking-normal normal-case text-muted-foreground sm:px-2">Query</TableHead>
              <TableHead className="h-9 bg-surface px-2 text-xs font-medium tracking-normal normal-case text-muted-foreground">Agent</TableHead>
              <TableHead className="h-9 bg-surface px-2 text-right text-xs font-medium tracking-normal normal-case text-muted-foreground">Conf</TableHead>
              <TableHead className="hidden h-9 bg-surface px-2 text-right text-xs font-medium tracking-normal normal-case text-muted-foreground md:table-cell">Jev ms</TableHead>
              <TableHead className="hidden h-9 bg-surface px-4 text-xs font-medium tracking-normal normal-case text-muted-foreground lg:table-cell">Answer</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {list.map(({ run, task }) => (
              <TableRow key={task.tid} className="border-border hover:bg-subtle">
                <TableCell className="hidden px-4 py-2 align-top tabular-nums text-muted-foreground sm:table-cell">{clock(run.at)}</TableCell>
                <TableCell className="min-w-[12rem] px-4 py-2 align-top whitespace-normal break-words text-foreground sm:px-2">
                  {task.text || run.text}
                  {run.order.length > 1 && <span className="ml-1.5 font-mono text-xs text-muted-foreground">{task.tid}</span>}
                  {run.source === 'autopilot' && <span className="ml-1.5 text-xs text-muted-foreground">auto</span>}
                </TableCell>
                <TableCell className="px-2 py-2 align-top"><AgentBadge agent={task.routed?.agent} /></TableCell>
                <TableCell className="px-2 py-2 text-right align-top tabular-nums text-muted-foreground">{pct(task.routed?.confidence)}</TableCell>
                <TableCell className="hidden px-2 py-2 text-right align-top tabular-nums text-muted-foreground md:table-cell">{task.routed?.jev_ms ?? '-'}</TableCell>
                <TableCell className="hidden max-w-[340px] truncate px-4 py-2 align-top text-muted-foreground lg:table-cell" title={task.answered?.answer ?? ''}>
                  {task.answered?.answer ?? task.stream}
                </TableCell>
              </TableRow>
            ))}
            {!list.length && (
              <TableRow className="hover:bg-transparent">
                <TableCell colSpan={6} className="h-24 px-4 text-center text-[13px] text-muted-foreground">No routed subtasks yet</TableCell>
              </TableRow>
            )}
          </TableBody>
        </table>
      </div>
    </Card>
  )
}

const HUB_ICON: Record<string, UiIconName> = { query: 'query', planner: 'planner', jev: 'jev', merger: 'merger', answer: 'answer' }

const HUB_INFO: Record<string, string> = {
  query: 'Incoming queries, from you or autopilot.',
  planner: 'Splits a query into up to 4 self-contained subtasks (the LLM engine when one is active, else a heuristic plus Jev\'s "multi" check).',
  jev: 'Jev routes every subtask with one system_one call: route choice, urgency, unsafe and clear.',
  merger: 'Combines subtask answers: single passes through, concat joins, the LLM engine writes one answer.',
  answer: 'The final answer streamed back to the page.',
}

export function Inspector({ id, store, onClose, run }: { id: string; store: Store; onClose: () => void; run?: Run }) {
  // A run fetched from the API (not in the live store) is searched first, so its subtasks still inspect.
  const all = run && !store.runs.some(r => r.qid === run.qid) ? [...rows([run]), ...rows(store.runs)] : rows(store.runs)
  const h3 = 'm-0 flex flex-wrap items-center gap-2 text-sm font-semibold text-foreground'
  let body: JSX.Element
  if (id.startsWith('a:')) {
    const agent = id.slice(2)
    const mine = all.filter(r => r.task.routed?.agent === agent)
    const conf = mine.length ? mine.reduce((s, r) => s + (r.task.routed?.confidence ?? 0), 0) / mine.length : undefined
    body = <>
      <h3 className={h3}><Chip agent={agent} /></h3>
      <p className="m-0 break-words text-[13px] text-muted-foreground">{store.agents[agent] ?? (store.guards.includes(agent) ? 'Guard outcome decided from Jev\'s other answers.' : '')}</p>
      <Meta className="text-xs tabular-nums"><span>{store.stats.by_agent[agent] ?? 0} subtasks</span><span>avg conf {pct(conf)}</span></Meta>
      <ul className="m-0 flex list-none flex-col gap-1 p-0 text-xs">
        {mine.slice(-6).reverse().map(r => (
          <li key={r.task.tid} className="flex min-w-0 items-baseline justify-between gap-3">
            <span className="min-w-0 break-words text-foreground">{r.task.text || r.run.text}</span>
            <span className="shrink-0 tabular-nums text-muted-foreground">{pct(r.task.routed?.confidence)}</span>
          </li>
        ))}
      </ul>
      {!mine.length && <p className="m-0 text-[13px] text-muted-foreground">No recent subtasks.</p>}
    </>
  } else if (id.startsWith('t:')) {
    const tid = id.slice(2)
    const hit = all.find(r => r.task.tid === tid)
    body = hit ? <>
      <h3 className={h3}><Icon name="subtasks" size={15} className="text-muted-foreground" />Subtask <span className="font-mono tabular-nums">{tid}</span></h3>
      <p className="m-0 break-words text-foreground">{hit.task.text}</p>
      {hit.task.routed
        ? <><div><Chip agent={hit.task.routed.agent} /></div><Bars probs={hit.task.routed.probabilities} max={8} /><StepPolicy routed={hit.task.routed} /></>
        : <p className="m-0 text-muted-foreground">{hit.task.error ?? 'routing…'}</p>}
      <div className={WELL}><Markdown text={hit.task.answered?.answer ?? (hit.task.stream || '…')} /></div>
    </> : <p className="m-0 text-muted-foreground">Subtask {tid} is no longer in memory.</p>
  } else {
    const done = store.runs.filter(r => r.done)
    const lat = done.map(latency)
    const avg = (f: (l: ReturnType<typeof latency>) => number) => (lat.length ? lat.reduce((s, l) => s + f(l), 0) / lat.length : null)
    const last = store.runs[store.runs.length - 1]
    body = <>
      <h3 className={h3}>{(id in HUB_ICON) && <Icon name={HUB_ICON[id]} size={15} className="text-muted-foreground" />}{id === 'jev' ? 'Jev router' : id.charAt(0).toUpperCase() + id.slice(1)}</h3>
      <p className="m-0 text-[13px] text-muted-foreground">{HUB_INFO[id] ?? ''}</p>
      <Meta className="text-xs tabular-nums">
        {id === 'jev' && <><span>avg {ms(avg(l => l.jev))}</span><span>{store.stats.jev_input_tokens.toLocaleString()} input tokens</span>
          {last && last.order[0] && last.tasks[last.order[0]]?.routed && <span className="font-mono">{last.tasks[last.order[0]].routed!.model}</span>}</>}
        {id === 'planner' && <><span>{store.engine ? store.engine.label : 'heuristic'}</span>{last?.plan && <span>last: {last.plan.planner}, {last.order.length} subtasks</span>}</>}
        {id === 'merger' && <><span>avg {ms(avg(l => l.merge))}</span>{last?.merged && <span>last: {last.merged.engine}</span>}</>}
        {(id === 'query' || id === 'answer') && <><span>{store.stats.queries} queries</span><span>{store.stats.errors} errors</span></>}
      </Meta>
    </>
  }
  return (
    <div role="dialog" aria-label="Node details"
      className="absolute top-[60px] right-4 z-10 flex max-h-[calc(100%-72px)] w-[min(340px,calc(100%-32px))] flex-col gap-2.5 overflow-auto rounded-lg border border-border bg-surface p-4 pr-11 text-sm text-foreground shadow-lg">
      <IconButton icon="close" label="Close" size="sm" onClick={onClose} className="absolute top-2 right-2" />
      {body}
    </div>
  )
}

// One template for every analytics panel. Kept for compatibility: a thin wrapper around the shared ChartCard.
export function ChartPanel({ icon, title, stat, note, legend, children }: {
  icon: UiIconName; title: string; stat: ReactNode; note?: ReactNode; legend?: ReactNode; children: ReactNode
}) {
  return <ChartCard icon={icon} title={title} stat={stat} statNote={note} footer={legend}>{children}</ChartCard>
}

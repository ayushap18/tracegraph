import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import Graph from './Graph'
import { Inspector } from './Panels'
import { useTrace } from './useTrace'
import { AgentIcon, Icon } from '../icons'
import { IconButton, Spinner } from '../ui'
import { cn } from '@/lib/utils'
import type { Run, Store } from '../useEventStream'

// The trace graph for one run with its own zoom toolbar and node inspector. Used by Chat and Run detail;
// the Live page keeps its richer toolbar (run browsing) around the same Graph.
export function TraceView({ run, store, compact, legend = true }: { run: Run | undefined; store: Store; compact?: boolean; legend?: boolean }) {
  const { allAgents, agentStats, jev } = useTrace(store)
  const [selected, setSelected] = useState<string | null>(null)
  const [resetKey, setResetKey] = useState(0)
  const [zoomBy, setZoomBy] = useState({ n: 0, k: 1 })
  const [full, setFull] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  // Agents this run used but that are not active now (custom agents deleted, files agents) still get a node.
  // Compact (side panel): only agents this run used or seriously considered, so the graph stays legible.
  const agents = useMemo(() => {
    const extra = run ? run.order.map(t => run.tasks[t]?.routed?.agent).filter((a): a is string => !!a && !allAgents.includes(a)) : []
    const all = extra.length ? [...allAgents, ...new Set(extra)] : allAgents
    if (!compact || !run) return all
    const keep = new Set<string>()
    for (const tid of run.order) {
      const r = run.tasks[tid]?.routed
      if (!r) continue
      keep.add(r.agent)
      for (const [a, p] of Object.entries(r.probabilities)) if (p >= 0.08) keep.add(a)
    }
    return all.filter(a => keep.has(a))
  }, [allAgents, run, compact])

  useEffect(() => { setSelected(null) }, [run?.qid])
  useEffect(() => {
    const on = () => setFull(!!document.fullscreenElement && document.fullscreenElement === box.current)
    document.addEventListener('fullscreenchange', on)
    return () => document.removeEventListener('fullscreenchange', on)
  }, [])
  const toggleFull = () => {
    const el = box.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => setFull(false))
    else if (full) setFull(false)
    else if (el.requestFullscreen) void el.requestFullscreen().catch(() => setFull(f => !f))
    else setFull(f => !f)
  }

  return (
    <div ref={box} className={cn(
      'relative min-w-0 bg-surface [&_.inspector]:top-12 [&:fullscreen]:overflow-auto [&:fullscreen]:bg-surface',
      full && 'fixed inset-0 z-[80] overflow-auto',
    )}>
      <div className={cn('flex min-h-12 flex-wrap items-center justify-end gap-3 px-3 py-2', compact && !full ? 'px-4' : 'border-b border-border')}>
        {legend && (
          <span className="mr-auto hidden items-center gap-3 text-xs text-muted-foreground sm:flex" aria-hidden="true">
            {LEGEND.map(([label, dot]) => (
              <span key={label} className="inline-flex items-center gap-1.5"><i className={cn('size-1.5 rounded-full', dot)} />{label}</span>
            ))}
          </span>
        )}
        {compact && !full && <span className="mr-auto text-[13px] font-medium text-muted-foreground">Execution steps</span>}
        {(!compact || full) && <div className="inline-flex items-center gap-0.5 rounded-md border border-border bg-surface p-0.5" role="group" aria-label="Zoom">
          <IconButton icon="zoom-out" label="Zoom out" size="sm" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1 / 1.25 }))} />
          <IconButton icon="fit" label="Fit to view" size="sm" onClick={() => setResetKey(k => k + 1)} />
          <IconButton icon="zoom-in" label="Zoom in" size="sm" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1.25 }))} />
        </div>}
        <IconButton icon={full ? 'minimize' : 'maximize'} label={full ? 'Exit fullscreen' : 'Fullscreen'} size="sm" onClick={toggleFull} />
      </div>
      {compact && !full ? <CompactTrace run={run} onSelect={setSelected} /> : <Graph run={run} agents={agents} agentStats={agentStats} jev={jev} selected={selected} onSelect={setSelected} resetKey={resetKey} zoomBy={zoomBy} />}
      {selected && <Inspector id={selected} store={store} run={run} onClose={() => setSelected(null)} />}
    </div>
  )
}

const LEGEND: Array<[string, string]> = [['running', 'bg-primary'], ['done', 'bg-ok'], ['no answer', 'bg-warn'], ['failed', 'bg-destructive']]

// One row of the compact stage list. The vertical connector runs from this row's icon tile to the next one's.
const STAGE = cn(
  'relative flex w-full items-start gap-3 rounded-md px-2 py-2.5 text-left text-foreground',
  "[&:not(:last-child)]:after:absolute [&:not(:last-child)]:after:left-[23.5px] [&:not(:last-child)]:after:top-[46px] [&:not(:last-child)]:after:-bottom-1.5 [&:not(:last-child)]:after:w-px [&:not(:last-child)]:after:bg-border [&:not(:last-child)]:after:content-['']",
)
const STAGE_BUTTON = 'transition-colors hover:bg-subtle focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35'
const TILE = 'grid size-8 shrink-0 place-items-center rounded-md border border-border bg-surface text-xs tabular-nums text-muted-foreground'
const TILE_ACTIVE = 'border-primary/30 bg-primary/10 text-primary'

function StageText({ title, note }: { title: ReactNode; note: ReactNode }) {
  return (
    <span className="min-w-0 flex-1 pt-px">
      <strong className="block text-[13px] font-semibold leading-snug [overflow-wrap:anywhere]">{title}</strong>
      <small className="mt-0.5 block text-xs leading-relaxed text-muted-foreground [overflow-wrap:anywhere]">{note}</small>
    </span>
  )
}

function CompactTrace({ run, onSelect }: { run: Run | undefined; onSelect: (id: string) => void }) {
  if (!run) return <p className="px-5 pb-5 text-sm text-muted-foreground">Send a message to follow its progress.</p>
  const tasks = run.order.map(id => run.tasks[id]).filter(Boolean)
  const finished = tasks.filter(t => t.answered || t.error).length
  const planning = !run.plan && !run.done
  return (
    <div className="flex flex-col px-2 pb-5">
      <button type="button" data-slot="button" className={cn(STAGE, STAGE_BUTTON)} onClick={() => onSelect('query')}>
        <span className={TILE}><Icon name="query" size={16} /></span>
        <StageText title="Your request" note={run.text || 'Receiving request…'} />
      </button>
      <div className={STAGE} role="status">
        <span className={cn(TILE, planning && TILE_ACTIVE, run.plan && 'text-ok')}>{planning ? <Spinner size={16} /> : <Icon name={run.plan ? 'check' : 'alert'} size={16} />}</span>
        <StageText title={run.plan ? `${tasks.length} step${tasks.length === 1 ? '' : 's'} planned` : run.done ? 'Planning stopped' : 'Planning your request'}
          note={run.plan ? `${finished} of ${tasks.length} completed` : run.done ? run.error || 'This run ended before a plan was ready.' : 'Finding the right steps and agents.'} />
      </div>
      {tasks.map((task, index) => {
        const settled = !!task.answered || !!task.error
        const waiting = task.depends_on.some(id => !run.tasks[id]?.answered && !run.tasks[id]?.error)
        const state = task.error ? 'Routing failed' : task.answered ? task.answered.ok ? 'Completed' : 'No answer' : run.done ? 'Stopped' : waiting ? 'Waiting for dependencies' : task.routed ? 'Working' : 'Selecting an agent'
        const active = !settled && !waiting && !run.done
        return <button type="button" data-slot="button" key={task.tid} className={cn(STAGE, STAGE_BUTTON)} onClick={() => onSelect('t:' + task.tid)}>
          <span className={cn(TILE, active && TILE_ACTIVE)}>{task.routed ? <AgentIcon agent={task.routed.agent} size={16} tinted /> : <span>{index + 1}</span>}</span>
          <StageText title={task.text || `Step ${index + 1}`}
            note={<>{task.routed && <span className="font-medium text-foreground">{task.routed.agent}: </span>}<span className={cn(task.error && 'text-destructive', task.answered && !task.answered.ok && 'text-warn')}>{state}</span></>} />
          {task.answered?.ok && <Icon name="check" size={14} className="mt-2 shrink-0 text-ok" />}
        </button>
      })}
      <button type="button" data-slot="button" className={cn(STAGE, STAGE_BUTTON)} onClick={() => onSelect('answer')}>
        <span className={cn(TILE, run.done && 'text-ok', run.done && run.status !== 'done' && 'text-warn')}><Icon name={run.done && run.status === 'done' ? 'check' : 'answer'} size={16} /></span>
        <StageText title={run.done ? run.status === 'done' ? 'Answer ready' : `Run ${run.status}` : 'Response'}
          note={run.done ? run.total_ms == null ? 'Run finished' : <span className="tabular-nums">{(run.total_ms / 1000).toFixed(1)} seconds total</span> : tasks.length && finished === tasks.length ? 'Combining the results…' : 'Waiting for the steps above.'} />
      </button>
    </div>
  )
}

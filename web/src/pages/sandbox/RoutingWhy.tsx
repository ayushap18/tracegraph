import { useState, type ReactNode } from 'react'
import type { Task } from '../../useEventStream'
import type { RoutingWhyProps } from './types'
import { Badge } from '../../ui'
import { Icon } from '../../icons'
import { colorOf } from '../../protocol'
import { pct } from '../../lib'
import { AgentBadge } from '../../components/app'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'

// "Why these agents": for one run, what Jev chose for each step, how sure it was, what it said, which guard
// signals stood out and which agents came close. Everything comes from the `routed` events already on the run.

// Mirrors jevrouter/config.py (BLOCK_AT 0.7, MIN_CLEAR 0.25, MIN_CONFIDENCE 0.45): flag signals well before the guard fires.
const UNSAFE_NOTE = 0.3
const CLEAR_NOTE = 0.5
const CONF_NOTE = 0.45
const RUNNER_UPS = 3

const ms = (v: number) => `${Math.round(v).toLocaleString()} ms`

function summary(tasks: Task[]): string {
  const routed = tasks.filter(t => t.routed)
  const failed = tasks.filter(t => t.error).length
  if (tasks.length === 1) {
    const t = tasks[0]
    if (t.error) return 'Routing failed'
    const r = t.routed!
    return `Routed to ${r.agent} (${pct(r.confidence)}), Jev ${ms(r.jev_ms)}`
  }
  const names = [...new Set(routed.map(t => t.routed!.agent))]
  const pending = tasks.length - routed.length - failed
  const head = `${routed.length} of ${tasks.length} steps routed`
  if (pending > 0) return `${head}: ${names.join(', ') || 'waiting'}`
  const base = routed.length === tasks.length ? `${tasks.length} steps routed: ${names.join(', ')}` : `${head}: ${names.join(', ') || 'none'}`
  return failed ? `${base} (${failed} failed)` : base
}

export function RoutingWhy({ run, agents, draftName }: RoutingWhyProps) {
  const [open, setOpen] = useState(false)
  const tasks = run.order.map(t => run.tasks[t]).filter((t): t is Task => !!t && (!!t.routed || !!t.error))
  if (!tasks.length) return null
  const all = run.order.map(t => run.tasks[t]).filter(Boolean)
  const draftPicked = !!draftName && tasks.some(t => t.routed?.agent === draftName || t.routed?.pick === draftName)

  return (
    <Collapsible open={open} onOpenChange={setOpen} className="min-w-0">
      <CollapsibleTrigger data-slot="button"
        className="group/why -mx-1 inline-flex max-w-full items-center gap-1.5 rounded-md px-1 py-0.5 text-left text-[13px] text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
        <Icon name="jev" size={14} className="shrink-0" />
        <span className="shrink-0 font-medium text-foreground">Why these agents</span>
        <span className="min-w-0 truncate tabular-nums">{summary(all.length ? all : tasks)}</span>
        {draftPicked && <Badge tone="accent" className="shrink-0">draft</Badge>}
        <Icon name="chevron-down" size={14} className="shrink-0 transition-transform group-data-[state=open]/why:rotate-180" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ol className="mt-2 flex flex-col gap-3 border-l border-border pl-3" aria-label="Routing decisions">
          {tasks.map(t => <Decision key={t.tid} task={t} agents={agents} draftName={draftName} showText={all.length > 1} />)}
        </ol>
      </CollapsibleContent>
    </Collapsible>
  )
}

function Decision({ task, agents, draftName, showText }: { task: Task; agents: Record<string, string>; draftName?: string; showText: boolean }) {
  const r = task.routed
  return (
    <li className="flex min-w-0 flex-col gap-1.5">
      {showText && task.text && <p className="m-0 truncate text-xs text-muted-foreground" title={task.text}>{task.text}</p>}
      {!r ? (
        <p className="m-0 flex items-start gap-1.5 text-[13px] text-destructive">
          <Icon name="alert" size={14} className="mt-0.5 shrink-0" />
          <span className="min-w-0 [overflow-wrap:anywhere]">{task.error || 'Jev could not route this step.'}</span>
        </p>
      ) : (
        <>
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <AgentBadge agent={r.agent} pct={r.confidence} title={`${r.agent}: ${pct(r.confidence)} confidence`} />
            {draftName && r.agent === draftName && <Badge tone="accent">draft</Badge>}
            {r.pick && r.pick !== r.agent && (
              <span className="text-xs text-muted-foreground">
                (Jev picked <span className="font-medium text-foreground">{r.pick}</span>
                {draftName && r.pick === draftName ? ' (draft)' : ''}, the guard sent it to {r.agent})
              </span>
            )}
            <span className="ml-auto text-xs tabular-nums text-muted-foreground">Jev {ms(r.jev_ms)}</span>
          </div>
          {r.reason && <p className="m-0 text-[13px] text-foreground [overflow-wrap:anywhere]">"{r.reason}"</p>}
          {agents[r.agent] && <p className="m-0 line-clamp-2 text-xs text-muted-foreground">{agents[r.agent]}</p>}
          <Signals r={r} />
          <RunnerUps probs={r.probabilities} chosen={r.pick || r.agent} agents={agents} draftName={draftName} />
          {task.error && <p className="m-0 text-xs text-destructive [overflow-wrap:anywhere]">{task.error}</p>}
        </>
      )}
    </li>
  )
}

function Signals({ r }: { r: NonNullable<Task['routed']> }) {
  const notes: Array<{ key: string; text: string; title: string }> = []
  if (r.unsafe >= UNSAFE_NOTE) notes.push({ key: 'unsafe', text: `Unsafe ${pct(r.unsafe)}`, title: 'Jev scored this step as possibly unsafe (blocked at 70%)' })
  if (r.clear < CLEAR_NOTE) notes.push({ key: 'clear', text: `Clear ${pct(r.clear)}`, title: 'Jev found this step unclear (asks a follow-up below 25%)' })
  if (r.confidence < CONF_NOTE) notes.push({ key: 'conf', text: `Low confidence ${pct(r.confidence)}`, title: 'Below 45% Jev asks a follow-up instead' })
  if (!notes.length) return null
  return (
    <div className="flex flex-wrap gap-1.5" aria-label="Guard signals">
      {notes.map(n => <Badge key={n.key} tone="warn" icon="shield" title={n.title} className="tabular-nums">{n.text}</Badge>)}
    </div>
  )
}

function RunnerUps({ probs, chosen, agents, draftName }: { probs: Record<string, number>; chosen: string; agents: Record<string, string>; draftName?: string }) {
  const list = Object.entries(probs ?? {}).filter(([a]) => a !== chosen).sort((a, b) => b[1] - a[1]).slice(0, RUNNER_UPS)
  if (!list.length) return null
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="text-xs text-muted-foreground">Runner-ups</span>
      <ul className="flex min-w-0 flex-col gap-1">
        {list.map(([a, p]) => (
          <li key={a} className="grid grid-cols-[minmax(0,7rem)_minmax(0,1fr)_2.75rem] items-center gap-2 text-xs">
            <Described desc={agents[a]}>
              <span className="flex min-w-0 items-center gap-1.5 text-muted-foreground">
                <span className="truncate">{a}</span>
                {draftName && a === draftName && <Badge tone="accent" className="shrink-0">draft</Badge>}
              </span>
            </Described>
            <div className="h-1 min-w-0" aria-hidden="true">
              <div className="h-full min-w-[2px] rounded-full" style={{ width: `${Math.max(0, Math.min(1, p)) * 100}%`, background: colorOf(a) }} />
            </div>
            <span className="text-right tabular-nums text-muted-foreground">{pct(p)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Wraps an agent label with its description as a tooltip (focusable so keyboard users get it too). */
function Described({ desc, children }: { desc?: string; children: ReactNode }) {
  if (!desc) return <>{children}</>
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0} className="inline-flex min-w-0 rounded-md focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">{children}</span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs">{desc}</TooltipContent>
    </Tooltip>
  )
}

export default RoutingWhy

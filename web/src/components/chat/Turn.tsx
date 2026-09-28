import { useEffect, useState, type ReactNode } from 'react'
import type { Run } from '../../useEventStream'
import { Badge, Button, IconButton, Spinner, StatusBadge, copyText, useToast } from '../../ui'
import { Icon, Logo } from '../../icons'
import { Chip } from '../Panels'
import Markdown from '../Markdown'
import { AgentBadge } from '../app'
import { pct } from '../../lib'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { cn } from '@/lib/utils'
import { AnswerBadges, TimingLine, secs } from './AnswerBadges'
import type { RunExtras } from './options'
import { Assumptions, CaveatsBox, withoutAssumptions, withoutCaveats } from './Caveats'

// One chat turn: the question and its answer. Chat renders these (and, for answer groups, several answers under
// one question); the Sandbox page reuses Turn through the re-export in pages/Chat.tsx.

export const answerOf = (run: Run) => {
  if (run.merged) return run.merged.answer
  if (run.mergeStream) return run.mergeStream
  const ts = run.order.map(t => run.tasks[t]).filter(Boolean)
  return ts.length === 1 ? ts[0].answered?.answer ?? ts[0].stream : ''
}

// Caveats that qualify an answer rather than report a miss (merger.HEDGES): the merger writes them into the answer and
// never lists them as caveats, so they never belong in the "couldn't do" box.
const HEDGES = ['Figures like these change every year; check the official source.']

/** What the run could not do: the merged answer's caveats once there is a merged answer (none when it lists none),
 *  else each step's own, without the hedges (a run still merging, or one that ended before its merge). */
export function caveatsOf(run: Run): string[] {
  if (run.merged) return run.merged.caveats ?? []
  const out: string[] = []
  for (const t of run.order)
    for (const c of run.tasks[t]?.answered?.caveats ?? []) if (!out.includes(c) && !HEDGES.includes(c)) out.push(c)
  return out
}

/** Stated assumptions (answered instead of asking a clarifying question), in step order. */
export const assumptionsOf = (run: Run) =>
  run.order.map(t => run.tasks[t]?.routed?.assumption).filter((a): a is string => !!a)

export function BotHead({ run, label }: { run?: Run; label?: string }) {
  return (
    <div className="flex min-w-0 items-center gap-2">
      <Logo size={18} />
      <span className="text-[13px] font-semibold">TraceGraph</span>
      {(label ?? run?.engine) && <Badge tone="neutral" icon="engine" className="min-w-0">{label ?? run?.engine}</Badge>}
    </div>
  )
}

export function UserMessage({ text, files, agent }: { text: string; files: string[]; agent?: string | null }) {
  return (
    <div className="flex flex-col items-end gap-1.5">
      <div className="max-w-[85%] rounded-2xl rounded-br-md bg-subtle px-4 py-2.5 text-[15px] leading-relaxed text-foreground">
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">
          {agent && <span className="mr-1.5 font-medium text-primary">@{agent}</span>}{text}
        </p>
      </div>
      {files.length > 0 && (
        <div className="flex max-w-[85%] flex-wrap justify-end gap-1.5">
          {files.map((n, i) => (
            <span key={i} className="inline-flex h-6 min-w-0 max-w-full items-center gap-1 rounded-md border border-border bg-surface px-2 text-xs text-muted-foreground">
              <Icon name="paperclip" size={12} className="shrink-0" /><span className="truncate">{n}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

const STEP_TONE: Record<string, string> = { done: 'text-ok', running: 'text-primary', warn: 'text-warn', error: 'text-warn', wait: 'text-muted-foreground' }
const STREAM_CURSOR = "[&>.md>:last-child]:after:ml-px [&>.md>:last-child]:after:text-primary [&>.md>:last-child]:after:content-['▍'] [&>.md>:last-child]:after:animate-[blink_1s_steps(2)_infinite]"

export interface AnswerProps {
  run: Run; selected: boolean; onShowTrace: () => void
  extras?: RunExtras
  actions?: ReactNode // extra footer buttons (e.g. "Try another engine")
  engineLabel?: string // shown in the head instead of the raw engine name
  hideHead?: boolean
}

/** One question and its answer. Also used by the Sandbox page. */
export function Turn({ run, fileName, ...rest }: AnswerProps & { fileName: (id: string) => string }) {
  const agent = rest.extras?.agent ?? null
  return (
    <div className="flex flex-col gap-4">
      <UserMessage text={run.text || '…'} files={run.files.map(fileName)} agent={agent} />
      <Answer run={run} {...rest} />
    </div>
  )
}

/** The answer half of a turn: steps, the answer, errors, badges and the footer. */
export function Answer({ run, selected, onShowTrace, extras, actions, engineLabel, hideHead }: AnswerProps) {
  const toast = useToast()
  const tasks = run.order.map(t => run.tasks[t]).filter(Boolean)
  const answer = answerOf(run)
  const caveats = caveatsOf(run)
  const assumptions = assumptionsOf(run)
  // The caveats box and the assumption lines show these, so the answer text does not repeat them.
  const shown = withoutAssumptions(withoutCaveats(answer, caveats), assumptions)
  const streaming = !run.done && !run.merged
  const multi = tasks.length > 1
  const finalFail = run.done && run.status !== 'done' && run.status !== 'running'
  const phase = !run.plan ? 'Planning…' : tasks.some(t => !t.routed && !t.error) ? `Routing ${tasks.length} step${tasks.length === 1 ? '' : 's'}…`
    : tasks.some(t => !t.answered && !t.error) ? 'Agents working…' : multi ? 'Merging answers…' : 'Finishing…'
  const copy = async () => { (await copyText(answer)) ? toast.success('Answer copied') : toast.error('Could not copy') }
  // Steps start open while the run works and fold once it is done, like the old <details open={!run.done}>.
  const [stepsOpen, setStepsOpen] = useState(!run.done)
  useEffect(() => { setStepsOpen(!run.done) }, [run.done])
  const quiet = run.status === 'cancelled' || run.status === 'timeout'

  return (
    <div className={cn('min-w-0 border-l-2 pl-4 transition-colors', selected ? 'border-primary' : 'border-transparent')}>
      {!hideHead && <BotHead run={run} label={engineLabel} />}
      {multi && (
        <Collapsible open={stepsOpen} onOpenChange={setStepsOpen} className="mt-3">
          <CollapsibleTrigger data-slot="button"
            className="group/steps -mx-1 inline-flex items-center gap-1.5 rounded-md px-1 py-0.5 text-[13px] text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
            <Icon name="subtasks" size={14} />{tasks.length} steps{run.plan ? `, ${run.plan.planner} plan` : ''}
            <Icon name="chevron-down" size={14} className="group-data-[state=open]/steps:rotate-180" />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <ol className="mt-2 flex flex-col gap-1.5 border-l border-border pl-3">
              {tasks.map(t => {
                const st = t.error ? 'error' : t.answered ? (t.answered.ok ? 'done' : 'warn') : t.routed ? 'running' : 'wait'
                return (
                  <li key={t.tid} className="flex min-h-6 items-center gap-2 text-[13px] max-sm:flex-wrap">
                    <span className={cn('inline-flex w-4 shrink-0 justify-center', STEP_TONE[st])} aria-hidden="true">{st === 'running' ? <Spinner size={12} /> : st === 'done' ? <Icon name="check" size={13} /> : st === 'wait' ? <Icon name="clock" size={12} /> : <Icon name="alert" size={13} />}</span>
                    <span className={cn('min-w-0 flex-1 [overflow-wrap:anywhere]', st === 'wait' ? 'text-muted-foreground' : 'text-foreground')}>
                      {t.text}{t.depends_on.length > 0 && <span className="text-xs text-muted-foreground"> (after {t.depends_on.join(', ')})</span>}
                    </span>
                    {t.routed && <span className="flex items-center gap-1 max-sm:ml-6">
                      {t.routed.bound && <Badge tone="accent" icon="agent" title="You picked this agent with @. It took this step; the other steps were routed as usual.">@{t.routed.agent}</Badge>}
                      <Chip agent={t.routed.agent} />
                    </span>}
                  </li>
                )
              })}
            </ol>
          </CollapsibleContent>
        </Collapsible>
      )}
      <Assumptions items={assumptions} className="mt-3" />
      {answer ? (
        <div className={cn('mt-3 text-[15px] leading-relaxed text-foreground [overflow-wrap:anywhere]', streaming && STREAM_CURSOR)}><Markdown text={shown} /></div>
      ) : !run.done ? (
        <div className="mt-3 flex items-start gap-2.5 py-1 text-sm text-foreground" role="status">
          <Spinner size={14} className="mt-0.5 shrink-0 text-muted-foreground" />
          <span>{phase}<small className="mt-1 block text-xs text-muted-foreground">{!run.plan ? 'Breaking your request into clear steps.' : 'Your answer will appear here as it is ready.'}</small></span>
        </div>
      ) : !finalFail ? <p className="mt-3 text-sm text-muted-foreground">No answer.</p> : null}
      {run.error && <p className={cn('mt-3 flex items-start gap-2 rounded-md border p-3 text-sm text-foreground', quiet ? 'border-warn/25 bg-warn/10' : 'border-destructive/25 bg-destructive/10')}>
        <Icon name="alert" size={15} className={cn('mt-0.5 shrink-0', quiet ? 'text-warn' : 'text-destructive')} /><span className="min-w-0 [overflow-wrap:anywhere]">{run.error}</span></p>}
      {finalFail && !run.error && <p className={cn('mt-3 flex items-start gap-2 rounded-md border p-3 text-sm text-foreground', quiet ? 'border-warn/25 bg-warn/10' : 'border-destructive/25 bg-destructive/10')}>
        <Icon name={run.status === 'cancelled' ? 'cancelled' : 'timeout'} size={15} className={cn('mt-0.5 shrink-0', quiet ? 'text-warn' : 'text-destructive')} />
        {run.status === 'cancelled' ? 'Stopped before it finished.' : run.status === 'timeout' ? 'Timed out.' : 'Something went wrong.'}</p>}
      {run.done && <CaveatsBox caveats={caveats} className="mt-3" />}
      <AnswerBadges run={run} extras={extras} />
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="flex min-w-0 flex-wrap gap-1.5">
          {tasks.filter(t => t.routed).map(t => (
            <AgentBadge key={t.tid} agent={t.routed!.agent} pct={pct(t.routed!.confidence)} title={`${t.routed!.agent}: ${pct(t.routed!.confidence)} confidence`} />
          ))}
        </span>
        <span className="ml-auto flex flex-wrap items-center gap-1">
          {run.done && run.status !== 'done' && <StatusBadge status={run.status} />}
          {run.total_ms != null && <span className="mx-1 inline-flex items-center gap-1 text-xs tabular-nums text-muted-foreground" title="Total time"><Icon name="latency" size={12} />{secs(run.total_ms)}</span>}
          {answer && run.done && <IconButton icon="copy" label="Copy answer" size="sm" onClick={() => void copy()} />}
          {actions}
          <Button variant="ghost" size="sm" icon="graph" iconRight="arrow-right" onClick={onShowTrace} aria-pressed={selected}
            className="aria-pressed:bg-primary/10 aria-pressed:text-primary">{selected ? 'Trace open' : 'View trace'}</Button>
        </span>
      </div>
      <TimingLine timings={extras?.timings} />
    </div>
  )
}

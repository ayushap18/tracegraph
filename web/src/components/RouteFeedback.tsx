import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { createLabel, deleteLabel, errorText, listLabels, promoteLabel } from '../api'
import { MERGE_TID, colorOf, type Label } from '../protocol'
import type { Task } from '../useEventStream'
import { AgentIcon } from '../icons'
import { Badge, Button, IconButton, Spinner, useToast } from '../ui'
import { AgentBadge } from './app'
import { pct } from '../lib'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Command, CommandEmpty, CommandGroup, CommandInput, CommandItem, CommandList } from '@/components/ui/command'
import { cn } from '@/lib/utils'

// Route feedback (docs/PLAN-learning.md): mark one routing decision right or wrong. Wrong opens a picker sorted by
// Jev's own probabilities, so the likely right agent is one click. Shared by Run detail, the Chat trace and Review.

const GUARDS = ['clarify', 'blocked', 'unsupported']

/** Only saved, routed subtasks can be labelled: not sandbox runs, not the merge step, not rows Jev never scored. */
export function canLabel(source: string, task: Task | undefined): task is Task & { routed: NonNullable<Task['routed']> } {
  return source !== 'sandbox' && !!task && task.tid !== MERGE_TID && !!task.routed && Object.keys(task.routed.probabilities).length > 0
}

/** Picker options: every offered agent by probability (highest first), then the guards; the picked one is left out. */
export function pickerOptions(picked: string, probabilities: Record<string, number>): Array<[string, number | null]> {
  const list: Array<[string, number | null]> = Object.entries(probabilities).sort((a, b) => b[1] - a[1])
  for (const g of GUARDS) if (!(g in probabilities)) list.push([g, null])
  return list.filter(([a]) => a !== picked)
}

/** Labels of one stored run, by subtask id. Quietly empty when the server has no labels yet. */
export function useRunLabels(qid: number | null | undefined, enabled = true) {
  const [labels, setLabels] = useState<Record<string, Label>>({})
  useEffect(() => {
    setLabels({})
    if (qid == null || !enabled) return
    let alive = true
    listLabels({ qid, limit: 200 }).then(
      r => { if (alive) setLabels(Object.fromEntries(r.labels.filter(l => l.qid === qid).map(l => [l.tid, l]))) },
      () => { /* no labels endpoint or no labels: nothing to show */ },
    )
    return () => { alive = false }
  }, [qid, enabled])
  const set = useCallback((tid: string, l: Label | null) => setLabels(m => {
    const next = { ...m }
    if (l) next[tid] = l
    else delete next[tid]
    return next
  }), [])
  return [labels, set] as const
}

/** A popover listing the other agents for "wrong". Type to filter, arrows and Enter to pick. */
export function AgentPicker({ picked, probabilities, open, onOpenChange, onPick, children, align = 'end' }: {
  picked: string; probabilities: Record<string, number>; open: boolean; onOpenChange: (open: boolean) => void
  onPick: (agent: string) => void; children: ReactNode; align?: 'start' | 'end'
}) {
  const options = pickerOptions(picked, probabilities)
  return (
    <Popover open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>{children}</PopoverTrigger>
      <PopoverContent align={align} className="w-[min(18rem,calc(100vw-32px))] p-0">
        <Command>
          <CommandInput placeholder="Which agent was right?" aria-label="Which agent was right?" />
          <CommandList>
            <CommandEmpty>No agent by that name.</CommandEmpty>
            <CommandGroup heading={`Jev picked ${picked}`}>
              {options.map(([a, p]) => (
                <CommandItem key={a} value={a} onSelect={() => { onOpenChange(false); onPick(a) }} className="gap-2.5">
                  <AgentIcon agent={a} size={15} strokeWidth={2} style={{ color: colorOf(a) }} />
                  <span className="min-w-0 flex-1 truncate">{a}</span>
                  {p != null && (
                    <span className="flex shrink-0 items-center gap-2">
                      <span className="h-1 w-10 overflow-hidden rounded-full bg-subtle" aria-hidden="true">
                        <span className="block h-full rounded-full" style={{ width: `${p * 100}%`, background: colorOf(a) }} />
                      </span>
                      <span className="w-8 text-right text-xs tabular-nums text-muted-foreground">{pct(p)}</span>
                    </span>
                  )}
                  {p == null && <span className="text-xs text-muted-foreground">guard</span>}
                </CommandItem>
              ))}
            </CommandGroup>
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  )
}

/** Thumbs up / down for one subtask, then the saved label with undo and "Add to evals". */
export function RouteFeedback({ qid, task, label, onLabel, className }: {
  qid: number; task: Task & { routed: NonNullable<Task['routed']> }; label: Label | undefined
  onLabel: (tid: string, label: Label | null) => void; className?: string
}) {
  const toast = useToast()
  const [busy, setBusy] = useState<'save' | 'undo' | 'promote' | null>(null)
  const [picking, setPicking] = useState(false)
  const picked = task.routed.agent

  const save = async (correct?: string) => {
    setBusy('save')
    try {
      const l = await createLabel(correct ? { qid, tid: task.tid, verdict: 'wrong', correct } : { qid, tid: task.tid, verdict: 'right' })
      onLabel(task.tid, l)
    } catch (e) { toast.error(`Could not save the label: ${errorText(e)}`) } finally { setBusy(null) }
  }
  const undo = async () => {
    if (!label) return
    setBusy('undo')
    try { await deleteLabel(label.id); onLabel(task.tid, null) } catch (e) { toast.error(`Could not remove the label: ${errorText(e)}`) } finally { setBusy(null) }
  }
  const promote = async () => {
    if (!label) return
    setBusy('promote')
    try {
      const r = await promoteLabel(label.id)
      onLabel(task.tid, { ...label, promoted: r.case_id })
      toast.success(r.created ? `Added to evals as ${r.case_id}` : 'Already in evals')
    } catch (e) { toast.error(`Could not add to evals: ${errorText(e)}`) } finally { setBusy(null) }
  }

  const row = 'flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1.5 text-[13px]'
  if (label) {
    return (
      <div className={cn(row, className)} aria-live="polite">
        {label.verdict === 'right'
          ? <Badge tone="ok" icon="thumbs-up">Right route</Badge>
          : <span className="inline-flex min-w-0 items-center gap-1.5"><Badge tone="warn" icon="thumbs-down">Should be</Badge><AgentBadge agent={label.correct} /></span>}
        <span className="ml-auto flex items-center gap-1">
          {label.promoted
            ? <Badge tone="neutral" icon="evals" title={`Eval case ${label.promoted}`}>In evals</Badge>
            : <Button variant="ghost" size="sm" icon="evals" loading={busy === 'promote'} disabled={!!busy} onClick={() => void promote()}>Add to evals</Button>}
          <IconButton icon="undo" label="Undo" size="sm" disabled={!!busy} onClick={() => void undo()} />
        </span>
      </div>
    )
  }
  return (
    <div className={cn(row, className)}>
      <span className="text-muted-foreground">Was <span className="font-medium text-foreground">{picked}</span> the right agent?</span>
      <span className="ml-auto flex items-center gap-1">
        {busy === 'save' && <Spinner size={14} label="Saving" />}
        <IconButton icon="thumbs-up" label="Right agent" size="sm" variant="secondary" disabled={!!busy} onClick={() => void save()} />
        <AgentPicker picked={picked} probabilities={task.routed.probabilities} open={picking} onOpenChange={setPicking} onPick={a => void save(a)}>
          <IconButton icon="thumbs-down" label="Wrong agent, pick the right one" size="sm" variant="secondary" disabled={!!busy} active={picking} />
        </AgentPicker>
      </span>
    </div>
  )
}

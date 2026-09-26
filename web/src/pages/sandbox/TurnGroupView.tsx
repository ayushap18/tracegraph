import { useEffect, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import type { TurnGroupViewProps } from './types'
import { Button, IconButton, Kbd, Spinner } from '../../ui'
import { Turn } from '../Chat'
import { RoutingWhy } from './RoutingWhy'
import { Textarea } from '@/components/ui/textarea'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

// One question in the sandbox, with every edited version of it. The active version is shown with its answer and
// the routing explanation; earlier versions stay available through the switcher, nothing is thrown away.

const MAX_CHARS = 500

export function TurnGroupView({ group, byQid, agents, draftName, selectedQid, busy, onShowTrace, onEdit, onSelectVersion, fileName = id => id }: TurnGroupViewProps) {
  const count = group.qids.length
  const index = Math.min(Math.max(group.active, 0), Math.max(count - 1, 0))
  const qid = group.qids[index]
  const run = qid != null ? byQid.get(qid) : undefined
  const original = byQid.get(group.qids[0])
  const routed = !!run && run.order.some(t => run.tasks[t]?.routed || run.tasks[t]?.error)

  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const editBtn = useRef<HTMLButtonElement>(null)
  const area = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (!editing) return
    const el = area.current
    if (el) { el.focus(); el.setSelectionRange(el.value.length, el.value.length) }
  }, [editing])

  const startEdit = () => {
    if (busy || !run) return
    setDraft(run.text.slice(0, MAX_CHARS))
    setEditing(true)
  }
  const cancel = () => {
    setEditing(false)
    requestAnimationFrame(() => editBtn.current?.focus())
  }
  const submit = () => {
    const text = draft.trim()
    if (!text || busy) return
    onEdit(group, text.slice(0, MAX_CHARS))
    setEditing(false)
  }
  const onKey = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); submit() }
    else if (e.key === 'Escape') { e.preventDefault(); cancel() }
  }

  const canRerun = !!draft.trim() && !busy
  const edited = index > 0 && !!original && !!run && original.text !== run.text

  return (
    <div className="group/turn flex min-w-0 flex-col gap-2">
      {!editing && (count > 1 || run) && (
        <div className="flex min-h-7 flex-wrap items-center justify-end gap-x-2 gap-y-1">
          {index > 0 && <EditedNote original={original?.text} edited={edited} />}
          {index === 0 && count > 1 && <span className="text-xs text-muted-foreground">Original</span>}
          {count > 1 && (
            <div role="group" aria-label={`Version ${index + 1} of ${count}`} className="inline-flex items-center">
              <IconButton icon="prev" size="sm" label="Previous version" disabled={index <= 0}
                onClick={() => onSelectVersion(group, index - 1)} />
              <span className="min-w-12 text-center text-xs tabular-nums text-muted-foreground" aria-live="polite">
                {index + 1} of {count}
              </span>
              <IconButton icon="next" size="sm" label="Next version" disabled={index >= count - 1}
                onClick={() => onSelectVersion(group, index + 1)} />
            </div>
          )}
          {run && (
            <IconButton ref={editBtn} icon="new" size="sm" label={busy ? 'Edit (wait for the current run)' : 'Edit and re-run'}
              disabled={busy} onClick={startEdit}
              className="transition-opacity sm:opacity-0 sm:group-hover/turn:opacity-100 sm:group-focus-within/turn:opacity-100 sm:focus-visible:opacity-100" />
          )}
        </div>
      )}

      {editing && run ? (
        <form className="flex flex-col items-end gap-2" onSubmit={e => { e.preventDefault(); submit() }}>
          <label htmlFor={`edit-${group.id}`} className="sr-only">Edit your message</label>
          <Textarea id={`edit-${group.id}`} ref={area} value={draft} maxLength={MAX_CHARS} rows={2}
            onChange={e => setDraft(e.target.value)} onKeyDown={onKey}
            className="max-h-[220px] w-full text-[15px] leading-relaxed sm:max-w-[85%]" />
          <div className="flex w-full flex-wrap items-center justify-end gap-2 sm:max-w-[85%]">
            <span className="mr-auto hidden items-center gap-1 text-xs text-muted-foreground sm:flex">
              <Kbd>Enter</Kbd> re-run <Kbd>Esc</Kbd> cancel
            </span>
            {draft.length > 400 && <span className="text-xs tabular-nums text-muted-foreground">{draft.length}/{MAX_CHARS}</span>}
            <Button variant="ghost" size="sm" onClick={cancel}>Cancel</Button>
            <Button type="submit" variant="primary" size="sm" icon="replay" disabled={!canRerun}>Re-run</Button>
          </div>
          <p className="m-0 w-full text-right text-xs text-muted-foreground">The current answer is kept as an earlier version.</p>
        </form>
      ) : null}

      {run ? (
        <div className={cn('min-w-0 transition-opacity', editing && 'opacity-60')}>
          <Turn run={run} fileName={fileName} selected={selectedQid === run.qid} onShowTrace={() => onShowTrace(run.qid)} />
        </div>
      ) : (
        <p className="m-0 flex items-center gap-2 text-[13px] text-muted-foreground" role="status">
          <Spinner size={14} />Starting this version…
        </p>
      )}

      {run && routed && (
        <div className={cn('min-w-0 pl-[18px]', editing && 'opacity-60')}>
          <RoutingWhy run={run} agents={agents} draftName={draftName} />
        </div>
      )}
    </div>
  )
}

/** "Edited" marker for versions after the first; the original question is in the tooltip. */
function EditedNote({ original, edited }: { original?: string; edited: boolean }) {
  const label = edited ? 'Edited' : 'Re-run'
  if (!original) return <span className="text-xs text-muted-foreground">{label}, earlier answers kept</span>
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span tabIndex={0}
          className="min-w-0 max-w-full truncate rounded-sm text-xs text-muted-foreground underline decoration-dotted underline-offset-2 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35 sm:max-w-[60%]">
          {label}, earlier answers kept
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-xs whitespace-pre-wrap">Original: {original}</TooltipContent>
    </Tooltip>
  )
}

export default TurnGroupView

import { useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { Run } from '../../useEventStream'
import { Badge, Button, Spinner } from '../../ui'
import { EngineIcon, Icon } from '../../icons'
import { cn } from '@/lib/utils'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { Answer, UserMessage } from './Turn'
import { secs } from './AnswerBadges'
import type { RunExtras } from './options'

// Several answers to one question (sent to 2 or 3 engines, or "Try another engine"): one tab per answer.
// Only the chosen answer is part of the conversation; the others sit in a dashed frame until picked.

export function RetryMenu({ engines, current, onPick, disabled }: {
  engines: Array<{ name: string; label: string }>; current: string | null; onPick: (engine: string) => void; disabled?: boolean
}) {
  const options = engines.filter(e => e.name !== current)
  if (!options.length) return null
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild disabled={disabled}>
        <Button variant="ghost" size="sm" icon="retry" aria-label="Try another engine" title="Ask the same question on another engine">
          <span className="max-sm:hidden">Try another engine</span>
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuLabel className="text-xs font-medium text-muted-foreground">Answer again with</DropdownMenuLabel>
        {options.map(e => (
          <DropdownMenuItem key={e.name} onSelect={() => onPick(e.name)}>
            <EngineIcon name={e.name} size={14} />{e.label}
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function AnswerGroup({ runs, extras, chosen, selectedQid, fileName, engineOf, onShowTrace, onChoose, choosing, actionsFor }: {
  runs: Run[] // ascending qid, at least 2
  extras: Record<number, RunExtras>
  chosen: number // qid of the chosen answer
  selectedQid: number | null // run whose trace is open
  fileName: (id: string) => string
  engineOf: (run: Run) => { name: string; label: string }
  onShowTrace: (qid: number) => void
  onChoose: (qid: number) => void
  choosing: number | null
  actionsFor: (run: Run) => ReactNode
}) {
  // Open on the chosen answer, or on a just-started extra answer (the group formed because of it).
  const [tab, setTab] = useState(() => {
    const newest = runs[runs.length - 1]
    return !newest.done && newest.qid !== chosen && runs.length > 1 && runs[0].done ? newest.qid : chosen
  })
  const count = useRef(runs.length)
  // A new answer (another engine) opens its tab; picking one keeps the current tab.
  useEffect(() => {
    if (runs.length > count.current) setTab(runs[runs.length - 1].qid)
    count.current = runs.length
  }, [runs])
  const shown = runs.find(r => r.qid === tab) ?? runs.find(r => r.qid === chosen) ?? runs[0]
  const isChosen = shown.qid === chosen
  const first = runs[0]
  const refs = useRef<Record<number, HTMLButtonElement | null>>({})

  const onKey = (e: KeyboardEvent) => {
    const i = runs.findIndex(r => r.qid === shown.qid)
    const next = e.key === 'ArrowRight' ? runs[(i + 1) % runs.length] : e.key === 'ArrowLeft' ? runs[(i - 1 + runs.length) % runs.length]
      : e.key === 'Home' ? runs[0] : e.key === 'End' ? runs[runs.length - 1] : null
    if (!next) return
    e.preventDefault()
    setTab(next.qid)
    refs.current[next.qid]?.focus()
  }

  return (
    <div className="flex flex-col gap-4">
      <UserMessage text={first.text || '…'} files={first.files.map(fileName)} agent={extras[first.qid]?.agent} />
      <div className="min-w-0">
        <div className="mb-3 flex items-center gap-2 text-[13px] text-muted-foreground">
          <Icon name="compare" size={14} className="shrink-0" />
          <span>{runs.length} answers. Pick the one to keep in the conversation.</span>
        </div>
        <div role="tablist" aria-label="Answers from different engines" onKeyDown={onKey}
          className="flex gap-1.5 overflow-x-auto pb-1 [scrollbar-width:none]">
          {runs.map(r => {
            const on = r.qid === shown.qid
            const pick = r.qid === chosen
            return (
              <button key={r.qid} ref={el => { refs.current[r.qid] = el }} type="button" role="tab" data-slot="tab"
                id={`ans-tab-${r.qid}`} aria-controls={`ans-panel-${first.qid}`} aria-selected={on} tabIndex={on ? 0 : -1}
                onClick={() => setTab(r.qid)}
                className={cn('inline-flex h-9 shrink-0 cursor-pointer items-center gap-1.5 rounded-md border px-2.5 text-[13px] font-medium transition-colors',
                  'outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35',
                  on ? 'border-primary/40 bg-primary/10 text-foreground' : 'border-border bg-surface text-muted-foreground hover:border-edge hover:text-foreground')}>
                <EngineIcon name={engineOf(r).name} size={14} />
                <span className="max-w-[140px] truncate">{engineOf(r).label}</span>
                {!r.done ? <Spinner size={12} className="text-muted-foreground" />
                  : r.total_ms != null && <span className="text-xs font-normal tabular-nums text-muted-foreground">{secs(r.total_ms)}</span>}
                {r.done && r.status !== 'done' && <Icon name="alert" size={13} className="text-warn" aria-label={r.status} />}
                {pick && <Icon name="check" size={13} className="text-ok" aria-label="chosen" />}
              </button>
            )
          })}
        </div>
        <div role="tabpanel" id={`ans-panel-${first.qid}`} aria-labelledby={`ans-tab-${shown.qid}`}
          className={cn('mt-2 rounded-lg', !isChosen && 'border border-dashed border-border bg-subtle/30 py-3 pr-3')}>
          <div className={cn('mb-1 flex flex-wrap items-center gap-2', isChosen ? 'pl-4' : 'pl-[18px]')}>
            {isChosen
              ? <Badge tone="ok" icon="check">In the conversation</Badge>
              : <>
                  <span className="text-[13px] text-muted-foreground">Not in the conversation. Follow-ups use the chosen answer.</span>
                  <Button size="sm" variant="primary" icon="check" className="ml-auto" loading={choosing === shown.qid}
                    disabled={!shown.done || shown.status !== 'done' || choosing != null}
                    title={!shown.done ? 'Wait for this answer to finish' : shown.status !== 'done' ? 'Only a finished answer can be used' : undefined}
                    onClick={() => onChoose(shown.qid)}>Use this answer</Button>
                </>}
          </div>
          <Answer run={shown} selected={selectedQid === shown.qid} onShowTrace={() => onShowTrace(shown.qid)}
            extras={extras[shown.qid]} engineLabel={engineOf(shown).label} actions={actionsFor(shown)} />
        </div>
      </div>
    </div>
  )
}

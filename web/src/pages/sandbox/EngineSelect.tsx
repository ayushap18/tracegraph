import { useId, useState } from 'react'
import { EngineIcon, Icon } from '../../icons'
import { Select, SelectContent, SelectItem, SelectSeparator, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import type { EngineInfo, EngineSelectProps } from './types'

// Engine picker for the sandbox composer: one engine per message, or two to four side by side.

const DEFAULT = '__default' // Radix Select items cannot use '' as a value; '' is mapped to this and back.
export const KEYLESS = 'none'
export const MIN_COMPARE = 2
export const MAX_COMPARE = 4

const KEYLESS_INFO: EngineInfo = { name: KEYLESS, label: 'Keyless (built-in agents)', billing: 'subscription', web: false, available: true, why: '' }

/** Engines the sandbox can pick from: the server's list plus keyless, if the server did not list it. */
export function engineOptions(engines: EngineInfo[]): EngineInfo[] {
  return engines.some(e => e.name === KEYLESS) ? engines : [...engines, KEYLESS_INFO]
}

/** Readable name of an engine choice ('none' is keyless). */
export function engineLabelOf(engines: EngineInfo[], name: string | null | undefined): string {
  if (!name || name === KEYLESS) return 'Keyless'
  return engines.find(e => e.name === name)?.label ?? name
}

export function EngineSelect({ engines, activeLabel, value, onChange, compareWith, onCompareWith, disabled }: EngineSelectProps) {
  const options = engineOptions(engines)
  const comparing = compareWith.length > 0
  const selectId = useId()

  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      <label htmlFor={selectId} className="sr-only">Engine for the next message</label>
      <Select value={value === '' ? DEFAULT : value} onValueChange={v => onChange(v === DEFAULT ? '' : v)} disabled={disabled}>
        <SelectTrigger id={selectId} size="sm" title={comparing ? 'Side by side is on: the engines picked there answer the next question' : undefined}
          className={cn('h-8 max-w-[220px] min-w-0 gap-1.5 rounded-md border-border bg-surface px-2.5 text-[13px] shadow-none transition-opacity',
            comparing && 'opacity-50')}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent position="popper" align="start" className="max-w-[min(320px,calc(100vw-32px))]">
          <SelectItem value={DEFAULT}>
            <EngineIcon name="auto" size={14} className="text-muted-foreground" />
            <span className="truncate">Default ({activeLabel || 'Keyless'})</span>
          </SelectItem>
          <SelectSeparator />
          {options.map(e => (
            <SelectItem key={e.name} value={e.name} disabled={!e.available} title={!e.available ? e.why : undefined}>
              <EngineIcon name={e.name} size={14} className="text-muted-foreground" />
              <span className="flex min-w-0 flex-col">
                <span className="truncate">{e.label}</span>
                {!e.available && e.why && <span className="truncate text-xs text-muted-foreground">{e.why}</span>}
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <SideBySidePicker options={options} value={compareWith} onChange={onCompareWith} disabled={disabled} />
    </div>
  )
}

export default EngineSelect

function SideBySidePicker({ options, value, onChange, disabled }: {
  options: EngineInfo[]; value: string[]; onChange: (names: string[]) => void; disabled?: boolean
}) {
  const [open, setOpen] = useState(false)
  const headingId = useId()
  const on = value.length > 0
  const toggle = (name: string) =>
    onChange(value.includes(name) ? value.filter(n => n !== name) : value.length >= MAX_COMPARE ? value : [...value, name])
  const tooFew = on && value.length < MIN_COMPARE

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button type="button" data-slot="button" disabled={disabled} aria-pressed={on}
          className={cn(
            'inline-flex h-8 shrink-0 cursor-pointer items-center gap-1.5 rounded-md border px-2.5 text-[13px] font-medium transition-colors outline-none',
            'focus-visible:ring-[3px] focus-visible:ring-ring/35 disabled:pointer-events-none disabled:opacity-50',
            on ? 'border-primary/40 bg-primary/10 text-foreground' : 'border-border bg-surface text-muted-foreground hover:bg-subtle hover:text-foreground',
          )}>
          <Icon name="compare" size={14} className={on ? 'text-primary' : undefined} />
          {on ? <span className="tabular-nums">Side by side, {value.length} {value.length === 1 ? 'engine' : 'engines'}</span> : <span>Side by side</span>}
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(320px,calc(100vw-32px))] rounded-lg p-3">
        <div className="flex flex-col gap-2.5">
          <div className="flex items-start justify-between gap-2">
            <div className="flex min-w-0 flex-col gap-0.5">
              <h3 id={headingId} className="m-0 text-sm font-semibold text-foreground">Side by side</h3>
              <p className="m-0 text-xs leading-relaxed text-muted-foreground">
                Ask the next question to <span className="tabular-nums">{MIN_COMPARE} to {MAX_COMPARE}</span> engines at once. These runs don't feed the follow-up memory.
              </p>
            </div>
          </div>
          <ul role="group" aria-labelledby={headingId} className="m-0 flex list-none flex-col gap-1 p-0">
            {options.map(e => {
              const picked = value.includes(e.name)
              const full = !picked && value.length >= MAX_COMPARE
              const why = !e.available ? e.why || 'Unavailable' : full ? `At most ${MAX_COMPARE} engines` : undefined
              return (
                <li key={e.name}>
                  <button type="button" data-slot="button" role="checkbox" aria-checked={picked} disabled={!e.available || full}
                    title={why} onClick={() => toggle(e.name)}
                    className={cn(
                      'flex w-full cursor-pointer items-center gap-2.5 rounded-md border px-2.5 py-1.5 text-left text-[13px] transition-colors outline-none',
                      'focus-visible:ring-[3px] focus-visible:ring-ring/35 disabled:cursor-not-allowed disabled:opacity-50',
                      picked ? 'border-primary/40 bg-primary/10 text-foreground' : 'border-border bg-surface text-foreground hover:bg-subtle',
                    )}>
                    <span aria-hidden="true" className={cn('grid size-4 shrink-0 place-items-center rounded-sm border',
                      picked ? 'border-primary bg-primary text-primary-foreground' : 'border-edge bg-background')}>
                      {picked && <Icon name="check" size={11} strokeWidth={3} />}
                    </span>
                    <EngineIcon name={e.name} size={15} className={picked ? 'text-primary' : 'text-muted-foreground'} />
                    <span className="flex min-w-0 flex-1 flex-col">
                      <span className="truncate font-medium">{e.label}</span>
                      {!e.available && e.why && <span className="truncate text-xs text-muted-foreground">{e.why}</span>}
                    </span>
                    {e.available && <span className="shrink-0 text-xs text-muted-foreground">{e.name === KEYLESS ? 'free' : e.billing === 'api' ? 'per token' : 'plan'}</span>}
                  </button>
                </li>
              )
            })}
          </ul>
          <div className="flex items-center justify-between gap-2 pt-0.5">
            <span className={cn('text-xs tabular-nums', tooFew ? 'text-warn' : 'text-muted-foreground')} aria-live="polite">
              {tooFew ? `Pick at least ${MIN_COMPARE}` : `${value.length} of ${MAX_COMPARE} picked`}
            </span>
            <span className="flex items-center gap-1">
              {on && (
                <button type="button" data-slot="button" onClick={() => onChange([])}
                  className="h-7 cursor-pointer rounded-md px-2 text-xs font-medium text-muted-foreground transition-colors outline-none hover:bg-subtle hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35">
                  Turn off
                </button>
              )}
              <button type="button" data-slot="button" onClick={() => setOpen(false)}
                className="h-7 cursor-pointer rounded-md border border-border bg-surface px-2.5 text-xs font-medium text-foreground transition-colors outline-none hover:bg-subtle focus-visible:ring-[3px] focus-visible:ring-ring/35">
                Done
              </button>
            </span>
          </div>
        </div>
      </PopoverContent>
    </Popover>
  )
}

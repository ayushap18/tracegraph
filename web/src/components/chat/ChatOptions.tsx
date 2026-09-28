import { useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import type { AnswerStyle, ChatMode } from '../../protocol'
import { EngineIcon, Icon } from '../../icons'
import { cn } from '@/lib/utils'
import {
  DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuRadioGroup,
  DropdownMenuRadioItem, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { MODES, PRESETS, STYLES, modeInfo, styleInfo, type Preset } from './options'

// The row above the composer: mode, answer style, "Compare engines" and presets.

const CHIP = 'inline-flex h-8 shrink-0 cursor-pointer items-center gap-1.5 rounded-md border px-2.5 text-[13px] font-medium outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/35'
const CHIP_OFF = 'border-border bg-surface text-muted-foreground hover:border-edge hover:text-foreground data-[state=open]:border-edge data-[state=open]:text-foreground'
const CHIP_ON = 'border-primary/40 bg-primary/10 text-primary'

/** Quick / Balanced / Deep / Research as a radio group (arrow keys move and select), the other controls after it,
 *  and one line of help for the mode under the pointer or focus (else the current one). */
export function ModeSwitch({ value, onChange, researchWhy, researchNote, children }: {
  value: ChatMode; onChange: (m: ChatMode) => void; researchWhy: string | null; researchNote?: string | null
  children?: ReactNode
}) {
  const [hint, setHint] = useState<ChatMode | null>(null)
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})
  const off = (m: ChatMode) => m === 'research' && researchWhy != null
  const onKey = (e: KeyboardEvent) => {
    const list = MODES.filter(m => !off(m.id))
    const i = list.findIndex(m => m.id === value)
    const next = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? list[(i + 1) % list.length]
      : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? list[(i - 1 + list.length) % list.length] : null
    if (!next) return
    e.preventDefault()
    onChange(next.id)
    refs.current[next.id]?.focus()
  }
  const shown = hint ?? value
  const help = off(shown) ? `Research is not available. ${researchWhy}`
    : shown === 'research' && researchNote ? `${modeInfo(shown).help} ${researchNote}` : modeInfo(shown).help
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <div role="radiogroup" aria-label="Answer mode" aria-describedby="chat-mode-help" onKeyDown={onKey}
          className="inline-flex w-fit max-w-full rounded-md border border-border bg-surface p-0.5">
          {MODES.map(m => {
            const on = m.id === value
            const disabled = off(m.id)
            return (
              <button key={m.id} ref={el => { refs.current[m.id] = el }} type="button" role="radio" data-slot="button"
                aria-checked={on} aria-disabled={disabled || undefined} tabIndex={on ? 0 : -1} title={disabled ? researchWhy ?? undefined : m.help}
                onClick={() => { if (!disabled) onChange(m.id); else setHint(m.id) }}
                onMouseEnter={() => setHint(m.id)} onMouseLeave={() => setHint(null)} onFocus={() => setHint(m.id)} onBlur={() => setHint(null)}
                className={cn('inline-flex h-7 min-w-0 cursor-pointer items-center gap-1.5 rounded-[5px] px-2 text-[13px] font-medium outline-none transition-colors',
                  'focus-visible:ring-[3px] focus-visible:ring-ring/35 sm:px-2.5',
                  on ? 'bg-primary/10 text-primary' : 'text-muted-foreground hover:text-foreground',
                  disabled && 'cursor-not-allowed opacity-50 hover:text-muted-foreground')}>
                <Icon name={m.icon} size={13} className="max-sm:hidden" />{m.label}
              </button>
            )
          })}
        </div>
        {children}
      </div>
      <p id="chat-mode-help" className="min-h-4 px-0.5 text-xs leading-snug text-muted-foreground">{help}</p>
    </div>
  )
}

export function StyleMenu({ value, onChange }: { value: AnswerStyle; onChange: (s: AnswerStyle) => void }) {
  const cur = styleInfo(value)
  return (
    <DropdownMenu>
      <DropdownMenuTrigger className={cn(CHIP, value === 'default' ? CHIP_OFF : CHIP_ON)} aria-label={`Answer style: ${cur.label}`}>
        <Icon name="log" size={13} /><span className="max-w-[120px] truncate">{value === 'default' ? 'Style' : cur.label}</span>
        <Icon name="chevron-down" size={13} />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-60">
        <DropdownMenuLabel className="text-xs font-medium text-muted-foreground">Answer style</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={value} onValueChange={v => onChange(v as AnswerStyle)}>
          {STYLES.map(s => (
            <DropdownMenuRadioItem key={s.id} value={s.id} className="flex-col items-start gap-0">
              <span>{s.label}</span><span className="text-xs text-muted-foreground">{s.help}</span>
            </DropdownMenuRadioItem>
          ))}
        </DropdownMenuRadioGroup>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** "Compare engines": on/off plus which 2 or 3 engines get the question. */
export function CompareMenu({ on, onToggle, picked, onPick, engines }: {
  on: boolean; onToggle: (v: boolean) => void; picked: string[]; onPick: (names: string[]) => void
  engines: Array<{ name: string; label: string }>
}) {
  const enough = engines.length >= 2
  const toggle = (name: string) => onPick(picked.includes(name) ? picked.filter(n => n !== name) : [...picked, name].slice(-3))
  return (
    <DropdownMenu>
      <DropdownMenuTrigger className={cn(CHIP, on ? CHIP_ON : CHIP_OFF)} aria-label={on ? `Compare engines: on, ${picked.length} engines` : 'Compare engines: off'}>
        <Icon name="compare" size={13} /><span>Compare</span>
        {on && <span className="rounded-sm bg-primary/15 px-1 text-xs tabular-nums">{picked.length}</span>}
        <Icon name="chevron-down" size={13} />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64">
        <DropdownMenuCheckboxItem checked={on} disabled={!enough} onCheckedChange={v => onToggle(!!v)} onSelect={e => e.preventDefault()}>
          <span className="flex flex-col">
            <span>Compare engines</span>
            <span className="text-xs text-muted-foreground">{enough ? 'Send to 2 or 3 engines, then keep the best answer' : 'Needs at least 2 engines, including Keyless'}</span>
          </span>
        </DropdownMenuCheckboxItem>
        <DropdownMenuSeparator />
        <DropdownMenuLabel className="text-xs font-medium text-muted-foreground">Engines ({picked.length} of 3)</DropdownMenuLabel>
        {engines.map(e => {
          const sel = picked.includes(e.name)
          return (
            <DropdownMenuCheckboxItem key={e.name} checked={sel} onCheckedChange={() => toggle(e.name)} onSelect={ev => ev.preventDefault()}>
              <EngineIcon name={e.name} size={14} />{e.label}
            </DropdownMenuCheckboxItem>
          )
        })}
        {on && picked.length < 2 && <p className="px-2 py-1.5 text-xs text-warn">Pick at least 2 engines.</p>}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function PresetMenu({ onPick }: { onPick: (p: Preset) => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger className={cn(CHIP, CHIP_OFF)} aria-label="Presets">
        <Icon name="sparkles" size={13} /><span>Presets</span><Icon name="chevron-down" size={13} />
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-64">
        <DropdownMenuLabel className="text-xs font-medium text-muted-foreground">Start from a template</DropdownMenuLabel>
        {PRESETS.map(p => (
          <DropdownMenuItem key={p.id} onSelect={() => onPick(p)}>
            <Icon name={p.icon} size={14} />
            <span className="flex min-w-0 flex-col">
              <span>{p.label}</span>
              <span className="text-xs text-muted-foreground">{modeInfo(p.mode).label}{p.style !== 'default' ? `, ${styleInfo(p.style).label.toLowerCase()}` : ''}</span>
            </span>
          </DropdownMenuItem>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Presets as one-click chips (empty chat). */
export function PresetRow({ onPick, className }: { onPick: (p: Preset) => void; className?: string }) {
  return (
    <div className={cn('flex flex-wrap justify-center gap-1.5', className)} role="group" aria-label="Presets">
      {PRESETS.map(p => (
        <button key={p.id} type="button" data-slot="button" onClick={() => onPick(p)} className={cn(CHIP, CHIP_OFF, 'rounded-full')}>
          <Icon name={p.icon} size={13} />{p.label}
        </button>
      ))}
    </div>
  )
}

/** The composer counter shows from this many characters (docs/PLAN-accuracy-v2.md B4). */
export const COUNT_FROM = 3500
export const tooLong = (text: string, limit: number) => text.trim().length > limit

/** "3,612 / 4,000" once a message gets long; over the limit it turns red and says so. Send is disabled by the caller. */
export function LengthCounter({ id, length, limit, className }: { id?: string; length: number; limit: number; className?: string }) {
  const over = length > limit
  return (
    <span id={id} aria-live="polite" className={cn('shrink-0 tabular-nums', over ? 'font-medium text-destructive' : 'text-muted-foreground', className)}>
      {length >= Math.min(COUNT_FROM, limit) && <>
        {length.toLocaleString()} / {limit.toLocaleString()}{over && ` · ${(length - limit).toLocaleString()} over the limit`}
      </>}
    </span>
  )
}

import { useRef, useState, type KeyboardEvent } from 'react'
import { cn } from '@/lib/utils'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import type { EngineInfo } from '../protocol'
import { EngineIcon, Icon } from '../icons'
import { setEngine, setEngineOrder, errorText } from '../api'
import { iconButtonClass, useToast } from '../ui'

// Which LLM backend writes plans, answers and merges. Subscription CLIs (Claude Code, Codex, Antigravity) run on the
// user's own plan; the Anthropic API bills per token; "Keyless" uses only the built-in agents. "Auto" tries them in
// the user's order and moves on to the next one when a call fails (out of quota, logged out, timed out).
export function EnginePicker({ engine, engines, placement = 'down', compact = false }: {
  engine: EngineInfo | null; engines: EngineInfo[]; placement?: 'down' | 'up'; compact?: boolean
}) {
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const list = useRef<HTMLUListElement>(null)
  const toast = useToast()

  const pick = async (name: string, label: string) => {
    setOpen(false)
    if (name === (engine?.name ?? 'none')) return
    setBusy(true)
    try {
      await setEngine(name)
      toast.success(`Engine switched to ${label}`)
    } catch (e) {
      toast.error(`Could not switch engine: ${errorText(e)}`)
    } finally { setBusy(false) }
  }

  const move = async (order: string[], i: number, by: number) => {
    const next = [...order]
    ;[next[i], next[i + by]] = [next[i + by], next[i]]
    try { await setEngineOrder(next) } catch (e) { toast.error(`Could not change the order: ${errorText(e)}`) }
  }

  const onKey = (e: KeyboardEvent) => {
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp' && e.key !== 'Home' && e.key !== 'End') return
    e.preventDefault()
    const items = [...(list.current?.querySelectorAll<HTMLButtonElement>('button[role="option"]:not(:disabled)') ?? [])]
    const i = items.indexOf(document.activeElement as HTMLButtonElement)
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
    items[next]?.focus()
  }

  const current = engine?.name ?? 'none'
  const options: Array<EngineInfo | { name: 'none'; label: string; billing: 'free'; available: true; why: ''; web: false }> =
    [...engines.filter(e => e.name !== 'none'), { name: 'none', label: 'Keyless', billing: 'free', available: true, why: '', web: false }]
  const label = engine ? engine.label : 'Keyless'
  const byName = Object.fromEntries(engines.map(e => [e.name, e]))
  const auto = byName.auto
  const chain = (auto?.order ?? []).filter(n => byName[n]?.available).map(n => byName[n].label).join(' → ')

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button type="button" data-slot="button" aria-haspopup="listbox" aria-label={`LLM engine: ${label}`} title={`LLM engine: ${label}`} disabled={busy}
          className={cn(
            'flex h-9 cursor-pointer items-center gap-2 rounded-md border border-border bg-surface text-[13px] font-medium text-foreground shadow-xs',
            'outline-none transition-colors hover:border-edge focus-visible:ring-[3px] focus-visible:ring-ring/35 disabled:opacity-60',
            compact ? 'size-9 justify-center p-0' : 'w-full px-2.5',
          )}>
          <EngineIcon name={current} size={15} />
          {!compact && <span className="min-w-0 flex-1 truncate text-left">{label}</span>}
          {!compact && engine?.billing === 'subscription' && <span className="rounded-sm bg-ok/10 px-1 text-[11px] font-medium text-ok">plan</span>}
          {!compact && <Icon name="chevron-down" size={14} className="text-muted-foreground" />}
        </button>
      </PopoverTrigger>
      <PopoverContent side={placement === 'up' ? 'top' : 'bottom'} align="start" className="w-72 p-1.5"
        onOpenAutoFocus={e => {
          // Focus the current option so arrow keys work straight away.
          e.preventDefault()
          const cur = list.current?.querySelector<HTMLButtonElement>('button[aria-selected="true"]:not(:disabled)')
            ?? list.current?.querySelector<HTMLButtonElement>('button[role="option"]:not(:disabled)')
          cur?.focus()
        }}>
        <ul ref={list} role="listbox" aria-label="LLM engine" onKeyDown={onKey} className="flex flex-col gap-0.5">
          {options.map(o => (
            <li key={o.name}>
              <button type="button" data-slot="button" role="option" aria-selected={o.name === current} disabled={!o.available}
                onClick={() => void pick(o.name, o.label)} title={o.available ? '' : o.why}
                className={cn(
                  'flex w-full cursor-pointer items-start gap-2.5 rounded-sm px-2 py-1.5 text-left outline-none transition-colors',
                  'hover:bg-subtle focus-visible:bg-subtle disabled:cursor-not-allowed disabled:opacity-50',
                  o.name === current && 'bg-subtle',
                )}>
                <span className="mt-0.5 text-muted-foreground"><EngineIcon name={o.name} size={15} /></span>
                <span className="flex min-w-0 flex-1 flex-col">
                  <span className="text-[13px] font-medium text-foreground">{o.label}</span>
                  <span className="text-xs text-muted-foreground">
                    {!o.available ? o.why
                      : o.name === 'auto' ? `tries ${chain || 'each engine'} in order`
                      : o.billing === 'subscription' ? `your subscription${o.web ? ', web search' : ''}`
                      : o.billing === 'api' ? `API key, pay per token${o.web ? ', web search' : ''}`
                      : 'built-in agents only, no LLM'}
                  </span>
                </span>
                {o.name === current && <Icon name="check" size={15} strokeWidth={2.2} className="mt-0.5 text-primary" />}
              </button>
            </li>
          ))}
          {current === 'auto' && auto?.order && (
            <li className="mt-1 border-t border-border px-2 pt-2 pb-1" aria-label="Order Auto tries engines in">
              <span className="text-xs font-medium text-muted-foreground">Try in this order</span>
              <ol className="mt-1 flex flex-col">
                {auto.order.map((n, i, order) => {
                  const e = byName[n]
                  const note = !e?.available ? e?.why : auto.cooling?.[n] ? `skipped for now: ${auto.cooling[n]}` : n === auto.lead ? 'used first' : ''
                  return (
                    <li key={n} title={note} className={cn('flex items-center gap-2 py-1', !e?.available && 'opacity-50')}>
                      <EngineIcon name={n} size={14} />
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span className="truncate text-[13px] text-foreground">{e?.label ?? n}</span>
                        {note && <span className="truncate text-xs text-muted-foreground">{note}</span>}
                      </span>
                      <button type="button" data-slot="icon-button" className={iconButtonClass('ghost', 'sm', false, 'size-6')} aria-label={`Move ${e?.label ?? n} up`} disabled={i === 0}
                        onClick={() => void move(order, i, -1)}><Icon name="arrow-up" size={13} /></button>
                      <button type="button" data-slot="icon-button" className={iconButtonClass('ghost', 'sm', false, 'size-6')} aria-label={`Move ${e?.label ?? n} down`} disabled={i === order.length - 1}
                        onClick={() => void move(order, i, 1)}><Icon name="arrow-down" size={13} /></button>
                    </li>
                  )
                })}
              </ol>
            </li>
          )}
        </ul>
      </PopoverContent>
    </Popover>
  )
}

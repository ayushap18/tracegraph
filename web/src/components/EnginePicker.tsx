import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
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
  const ref = useRef<HTMLDivElement>(null)
  const btn = useRef<HTMLButtonElement>(null)
  const list = useRef<HTMLUListElement>(null)
  const toast = useToast()

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent) => { if (!ref.current?.contains(e.target as Node)) setOpen(false) }
    window.addEventListener('mousedown', close)
    // Focus the current option so arrow keys work straight away.
    const cur = list.current?.querySelector<HTMLButtonElement>('button[aria-selected="true"]:not(:disabled)') ?? list.current?.querySelector<HTMLButtonElement>('button[role="option"]:not(:disabled)')
    cur?.focus()
    return () => window.removeEventListener('mousedown', close)
  }, [open])

  const pick = async (name: string, label: string) => {
    setOpen(false)
    btn.current?.focus()
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
    if (e.key === 'Escape') { e.stopPropagation(); setOpen(false); btn.current?.focus(); return }
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
    <div className={'engine-picker' + (compact ? ' compact' : '') + (placement === 'up' ? ' up' : '')} ref={ref} onKeyDown={onKey}>
      <button ref={btn} type="button" className={'engine-btn' + (engine ? ' on' : '')} aria-haspopup="listbox" aria-expanded={open}
        aria-label={`LLM engine: ${label}`} onClick={() => setOpen(o => !o)} title={`LLM engine: ${label}`} disabled={busy}>
        <EngineIcon name={current} />
        {!compact && <span className="engine-label">{label}</span>}
        {!compact && engine?.billing === 'subscription' && <span className="engine-sub">plan</span>}
        {!compact && <Icon name="chevron-down" size={14} />}
      </button>
      {open && (
        <ul ref={list} className="engine-menu" role="listbox" aria-label="LLM engine">
          {options.map(o => (
            <li key={o.name}>
              <button type="button" role="option" aria-selected={o.name === current} disabled={!o.available}
                onClick={() => void pick(o.name, o.label)} title={o.available ? '' : o.why}>
                <EngineIcon name={o.name} size={16} />
                <span className="engine-opt">
                  <span className="engine-name">{o.label}</span>
                  <span className="engine-meta">
                    {!o.available ? o.why
                      : o.name === 'auto' ? `tries ${chain || 'each engine'} in order`
                      : o.billing === 'subscription' ? `your subscription${o.web ? ' · web search' : ''}`
                      : o.billing === 'api' ? `API key, pay per token${o.web ? ' · web search' : ''}`
                      : 'built-in agents only, no LLM'}
                  </span>
                </span>
                {o.name === current && <Icon name="check" size={15} strokeWidth={2.2} />}
              </button>
            </li>
          ))}
          {current === 'auto' && auto?.order && (
            <li className="engine-order" aria-label="Order Auto tries engines in">
              <span className="engine-order-title">Try in this order</span>
              <ol>
                {auto.order.map((n, i, order) => {
                  const e = byName[n]
                  const note = !e?.available ? e?.why : auto.cooling?.[n] ? `skipped for now: ${auto.cooling[n]}` : n === auto.lead ? 'used first' : ''
                  return (
                    <li key={n} className={e?.available ? '' : 'off'} title={note}>
                      <EngineIcon name={n} size={14} />
                      <span className="engine-opt"><span className="engine-name">{e?.label ?? n}</span>{note && <span className="engine-meta">{note}</span>}</span>
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
      )}
    </div>
  )
}

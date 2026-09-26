import { useEffect, useRef, useState } from 'react'
import type { EngineInfo } from '../protocol'
import { EngineIcon, Icon } from '../icons'

// Which LLM backend writes plans, answers and merges. Subscription CLIs (Claude Code, Codex, Antigravity) run on the
// user's own plan; the Anthropic API bills per token; "Keyless" uses only the built-in agents.
export function EnginePicker({ engine, engines }: { engine: EngineInfo | null; engines: EngineInfo[] }) {
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === 'Escape' : !ref.current?.contains(e.target as Node)) setOpen(false)
    }
    window.addEventListener('mousedown', close)
    window.addEventListener('keydown', close)
    return () => { window.removeEventListener('mousedown', close); window.removeEventListener('keydown', close) }
  }, [open])

  const pick = async (name: string) => {
    setOpen(false)
    setError(null)
    const r = await fetch('/control', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ engine: name }) })
      .catch(() => null)
    if (!r || !r.ok) setError(((await r?.json().catch(() => null)) as { error?: string } | null)?.error ?? 'could not switch engine')
  }

  const current = engine?.name ?? 'none'
  const options: Array<EngineInfo | { name: 'none'; label: string; billing: 'free'; available: true; why: ''; web: false }> =
    [...engines, { name: 'none', label: 'Keyless', billing: 'free', available: true, why: '', web: false }]

  return (
    <div className="engine-picker" ref={ref}>
      <button type="button" className={'engine-btn' + (engine ? ' on' : '')} aria-haspopup="listbox" aria-expanded={open}
        onClick={() => setOpen(o => !o)} title={error ?? 'LLM engine for planning, answers and merging'}>
        <EngineIcon name={current} />
        <span>{engine ? engine.label : 'Keyless'}</span>
        {engine?.billing === 'subscription' && <span className="engine-sub">plan</span>}
        <Icon name="chevron-down" size={14} />
      </button>
      {open && (
        <ul className="engine-menu" role="listbox" aria-label="LLM engine">
          {options.map(o => (
            <li key={o.name}>
              <button type="button" role="option" aria-selected={o.name === current} disabled={!o.available}
                onClick={() => void pick(o.name)} title={o.available ? '' : o.why}>
                <EngineIcon name={o.name} size={16} />
                <span className="engine-opt">
                  <span className="engine-name">{o.label}</span>
                  <span className="engine-meta">
                    {!o.available ? o.why
                      : o.billing === 'subscription' ? `your subscription${o.web ? ' · web search' : ''}`
                      : o.billing === 'api' ? `API key, pay per token${o.web ? ' · web search' : ''}`
                      : 'built-in agents only, no LLM'}
                  </span>
                </span>
                {o.name === current && <Icon name="check" size={15} strokeWidth={2.2} />}
              </button>
            </li>
          ))}
        </ul>
      )}
      {error && <span className="engine-err" role="alert">{error}</span>}
    </div>
  )
}

import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { EngineIcon, Icon, type UiIconName } from '../icons'
import { NAV } from '../routes'
import { useStore } from '../store'
import { useTheme, THEME_LABEL } from '../theme'
import { navigate, useToast } from '../ui'
import { setEngine, errorText } from '../api'

// ⌘K / Ctrl+K: fuzzy jump to pages, actions, engines, sample queries and recent runs.

interface Cmd { id: string; group: string; label: string; hint?: string; icon?: UiIconName; engine?: string; keywords?: string; run: () => void }

/** Subsequence match with bonuses for word starts and runs; null when not all characters are found in order. */
export function fuzzy(query: string, text: string): number | null {
  const q = query.toLowerCase().trim(), t = text.toLowerCase()
  if (!q) return 0
  const direct = t.indexOf(q)
  if (direct >= 0) return 1000 - direct * 2 - (t.length - q.length) * 0.1 + (direct === 0 || /\W/.test(t[direct - 1]) ? 200 : 0)
  let score = 0, ti = 0, prev = -2
  for (const ch of q) {
    if (ch === ' ') continue
    const i = t.indexOf(ch, ti)
    if (i < 0) return null
    score += i === prev + 1 ? 8 : 1
    if (i === 0 || /\W/.test(t[i - 1])) score += 6
    prev = i; ti = i + 1
  }
  return score - t.length * 0.02
}

export function Palette({ open, onClose, onToggleSidebar }: { open: boolean; onClose: () => void; onToggleSidebar: () => void }) {
  const { store } = useStore()
  const { cycle, theme } = useTheme()
  const toast = useToast()
  const [q, setQ] = useState('')
  const [idx, setIdx] = useState(0)
  const input = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLUListElement>(null)
  const restore = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) return
    restore.current = document.activeElement as HTMLElement | null
    setQ(''); setIdx(0)
    requestAnimationFrame(() => input.current?.focus())
    return () => { restore.current?.focus?.() }
  }, [open])

  const cmds = useMemo<Cmd[]>(() => {
    const out: Cmd[] = []
    for (const n of NAV) out.push({ id: 'p:' + n.name, group: 'Pages', label: n.label, hint: n.subtitle, icon: n.icon, keywords: n.keywords, run: () => navigate(n.path) })
    out.push({ id: 'a:new', group: 'Actions', label: 'New chat', icon: 'new', keywords: 'start conversation', run: () => navigate('/?new=1') })
    out.push({ id: 'a:theme', group: 'Actions', label: `Toggle theme (now ${THEME_LABEL[theme]})`, icon: 'moon', keywords: 'dark light', run: cycle })
    out.push({ id: 'a:sidebar', group: 'Actions', label: 'Toggle sidebar', icon: 'sidebar-close', keywords: 'collapse expand', run: onToggleSidebar })
    const cur = store.engine?.name ?? 'none'
    for (const e of [...store.engines, { name: 'none', label: 'Keyless', available: true, why: '' }]) {
      if (e.name === cur) continue
      out.push({
        id: 'e:' + e.name, group: 'Engines', label: `Switch engine: ${e.label}`, hint: e.available ? undefined : e.why, engine: e.name, keywords: 'llm model',
        run: () => {
          if (!e.available) { toast.error(`${e.label} is not available: ${e.why}`); return }
          setEngine(e.name).then(() => toast.success(`Engine switched to ${e.label}`), err => toast.error(`Could not switch engine: ${errorText(err)}`))
        },
      })
    }
    for (const s of store.samples.slice(0, 8)) out.push({ id: 's:' + s, group: 'Ask', label: s, icon: 'send', hint: 'new chat', run: () => navigate('/?new=1&q=' + encodeURIComponent(s)) })
    for (const r of [...store.runs].reverse().filter(r => r.text).slice(0, 10)) {
      out.push({ id: 'r:' + r.qid, group: 'Recent runs', label: r.text, hint: `#${r.qid} · ${r.done ? r.status : 'running'}`, icon: 'runs', run: () => navigate('/runs/' + r.qid) })
    }
    return out
  }, [store.engines, store.engine, store.samples, store.runs, theme, cycle, onToggleSidebar, toast])

  const results = useMemo(() => {
    if (!q.trim()) return cmds.filter(c => c.group !== 'Ask' || cmds.filter(x => x.group === 'Ask').indexOf(c) < 4)
    // A free query can always be asked as a new chat.
    const scored = cmds.map(c => ({ c, s: Math.max(fuzzy(q, c.label) ?? -Infinity, (fuzzy(q, c.keywords ?? '') ?? -Infinity) - 50) }))
      .filter(x => x.s > -Infinity).sort((a, b) => b.s - a.s).slice(0, 30).map(x => x.c)
    scored.push({ id: 'ask:' + q, group: 'Ask', label: `Ask “${q.trim()}”`, icon: 'send', hint: 'new chat', run: () => navigate('/?new=1&q=' + encodeURIComponent(q.trim())) })
    return scored
  }, [q, cmds])

  useEffect(() => { setIdx(0) }, [q])
  useEffect(() => {
    listRef.current?.querySelector(`[data-i="${idx}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [idx])

  if (!open) return null

  const exec = (c: Cmd | undefined) => { if (!c) return; onClose(); c.run() }
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setIdx(i => Math.min(results.length - 1, i + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setIdx(i => Math.max(0, i - 1)) }
    else if (e.key === 'Home') { e.preventDefault(); setIdx(0) }
    else if (e.key === 'End') { e.preventDefault(); setIdx(results.length - 1) }
    else if (e.key === 'Enter') { e.preventDefault(); exec(results[idx]) }
    else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); onClose() }
    else if (e.key === 'Tab') e.preventDefault() // keep focus inside the dialog
  }

  let lastGroup = ''
  return (
    <div className="palette-backdrop" onMouseDown={onClose}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette" onMouseDown={e => e.stopPropagation()} onKeyDown={onKey}>
        <div className="palette-input">
          <Icon name="search" size={17} />
          <input ref={input} value={q} onChange={e => setQ(e.target.value)} placeholder="Search pages, actions, engines, runs… or type a question"
            role="combobox" aria-expanded="true" aria-controls="palette-list" aria-activedescendant={results[idx] ? 'pal-' + idx : undefined}
            aria-autocomplete="list" autoComplete="off" spellCheck={false} />
          <kbd>esc</kbd>
        </div>
        <ul className="palette-list" id="palette-list" role="listbox" ref={listRef} aria-label="Results">
          {results.map((c, i) => {
            const head = c.group !== lastGroup ? (lastGroup = c.group) : null
            return [
              head && <li key={'g:' + head} className="palette-group" role="presentation">{head}</li>,
              <li key={c.id} id={'pal-' + i} data-i={i} role="option" aria-selected={i === idx} className={'palette-item' + (i === idx ? ' on' : '')}
                onMouseMove={() => setIdx(i)} onClick={() => exec(c)}>
                <span className="pi-icon">{c.engine ? <EngineIcon name={c.engine} size={15} /> : c.icon && <Icon name={c.icon} size={15} />}</span>
                <span className="pi-label">{c.label}</span>
                {c.hint && <span className="pi-hint">{c.hint}</span>}
                {i === idx && <Icon name="enter" size={14} className="pi-enter" />}
              </li>,
            ]
          })}
          {!results.length && <li className="palette-empty">No matches</li>}
        </ul>
        <div className="palette-foot" aria-hidden="true">
          <span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span><span><kbd>esc</kbd> close</span>
        </div>
      </div>
    </div>
  )
}

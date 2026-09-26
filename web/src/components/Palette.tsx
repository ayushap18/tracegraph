import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { Dialog as DialogPrimitive } from 'radix-ui'
import { cn } from '@/lib/utils'
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

  useEffect(() => {
    if (!open) return
    setQ(''); setIdx(0)
  }, [open])

  const cmds = useMemo<Cmd[]>(() => {
    const out: Cmd[] = []
    for (const n of NAV) out.push({ id: 'p:' + n.name, group: 'Pages', label: n.label, hint: n.subtitle, icon: n.icon, keywords: n.keywords, run: () => navigate(n.path) })
    out.push({ id: 'a:new', group: 'Actions', label: 'New chat', icon: 'new', keywords: 'start conversation', run: () => navigate('/?new=1') })
    out.push({ id: 'a:theme', group: 'Actions', label: `Toggle theme (now ${THEME_LABEL[theme]})`, icon: 'moon', keywords: 'dark light', run: cycle })
    out.push({ id: 'a:sidebar', group: 'Actions', label: 'Toggle sidebar', icon: 'sidebar-close', keywords: 'collapse expand', run: onToggleSidebar })
    const cur = store.engine?.name ?? 'none'
    for (const e of [...store.engines.filter(e => e.name !== 'none'), { name: 'none', label: 'Keyless', available: true, why: '' }]) {
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

  const exec = (c: Cmd | undefined) => { if (!c) return; onClose(); c.run() }
  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setIdx(i => Math.min(results.length - 1, i + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setIdx(i => Math.max(0, i - 1)) }
    else if (e.key === 'Home') { e.preventDefault(); setIdx(0) }
    else if (e.key === 'End') { e.preventDefault(); setIdx(results.length - 1) }
    else if (e.key === 'Enter') { e.preventDefault(); exec(results[idx]) }
    else if (e.key === 'Tab') e.preventDefault() // the input is the only stop; arrows move the selection
  }
  const kbd = 'inline-flex h-5 min-w-5 items-center justify-center rounded-sm border border-border bg-subtle px-1 font-mono text-[11px] text-muted-foreground'

  let lastGroup = ''
  return (
    <DialogPrimitive.Root open={open} onOpenChange={v => { if (!v) onClose() }}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-scrim backdrop-blur-[2px] data-[state=open]:animate-in data-[state=open]:fade-in-0" />
        <DialogPrimitive.Content aria-label="Command palette" onKeyDown={onKey}
          onOpenAutoFocus={e => { e.preventDefault(); input.current?.focus() }}
          className={cn(
            'tg-scope fixed top-[12vh] left-1/2 z-50 flex max-h-[min(560px,76vh)] w-[min(640px,calc(100vw-24px))] -translate-x-1/2 flex-col overflow-hidden',
            'rounded-xl border border-border bg-surface text-foreground shadow-lg outline-none',
            'data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-[0.98]',
          )}>
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <DialogPrimitive.Description className="sr-only">Jump to a page, run an action, switch engine, open a recent run or ask a question.</DialogPrimitive.Description>
          <div className="flex h-12 shrink-0 items-center gap-2.5 border-b border-border px-4">
            <Icon name="search" size={17} className="text-muted-foreground" />
            <input ref={input} value={q} onChange={e => setQ(e.target.value)} placeholder="Search pages, actions, engines, runs… or type a question"
              role="combobox" aria-expanded="true" aria-controls="palette-list" aria-activedescendant={results[idx] ? 'pal-' + idx : undefined}
              aria-autocomplete="list" autoComplete="off" spellCheck={false}
              className="h-full min-w-0 flex-1 border-0 bg-transparent text-[15px] text-foreground outline-none placeholder:text-muted-foreground" />
            <kbd className={kbd}>esc</kbd>
          </div>
          <ul className="m-0 min-h-0 flex-1 list-none overflow-y-auto p-1.5" id="palette-list" role="listbox" ref={listRef} aria-label="Results">
            {results.map((c, i) => {
              const head = c.group !== lastGroup ? (lastGroup = c.group) : null
              return [
                head && <li key={'g:' + head} role="presentation" className="px-2.5 pt-2.5 pb-1 text-xs font-medium text-muted-foreground">{head}</li>,
                <li key={c.id} id={'pal-' + i} data-i={i} role="option" aria-selected={i === idx}
                  className={cn('flex h-9 cursor-pointer items-center gap-2.5 rounded-md px-2.5 text-sm', i === idx ? 'bg-subtle text-foreground' : 'text-foreground/90')}
                  onMouseMove={() => setIdx(i)} onClick={() => exec(c)}>
                  <span className={cn('flex w-4 justify-center', i === idx ? 'text-primary' : 'text-muted-foreground')}>
                    {c.engine ? <EngineIcon name={c.engine} size={15} /> : c.icon && <Icon name={c.icon} size={15} />}
                  </span>
                  <span className="min-w-0 flex-1 truncate">{c.label}</span>
                  {c.hint && <span className="hidden max-w-[45%] truncate text-xs text-muted-foreground sm:inline">{c.hint}</span>}
                  {i === idx && <Icon name="enter" size={14} className="text-muted-foreground" />}
                </li>,
              ]
            })}
            {!results.length && <li className="px-3 py-8 text-center text-sm text-muted-foreground">No matches</li>}
          </ul>
          <div className="hidden shrink-0 items-center gap-4 border-t border-border px-4 py-2 text-xs text-muted-foreground sm:flex" aria-hidden="true">
            <span className="flex items-center gap-1"><kbd className={kbd}>↑</kbd><kbd className={kbd}>↓</kbd> move</span>
            <span className="flex items-center gap-1"><kbd className={kbd}>↵</kbd> open</span>
            <span className="flex items-center gap-1"><kbd className={kbd}>esc</kbd> close</span>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

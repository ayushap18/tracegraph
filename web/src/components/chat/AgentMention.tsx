import { useCallback, useEffect, useMemo, useState, type KeyboardEvent, type RefObject } from 'react'
import { AgentIcon } from '../../icons'
import { Icon } from '../../icons'
import { colorOf } from '../../protocol'
import { cn } from '@/lib/utils'

// "@agent" in the composer: typing @ opens a list of offered agents; picking one turns it into a chip next to the
// text (sent as `agent`, so routing is skipped) and removes the "@…" from the question.

export interface AgentOption { name: string; description: string }
interface Mention { start: number; query: string }

function findMention(text: string, caret: number): Mention | null {
  const m = /(^|\s)@([\w-]*)$/.exec(text.slice(0, caret))
  return m ? { start: caret - m[2].length - 1, query: m[2].toLowerCase() } : null
}

export function useMention({ agents, text, setText, input, onPick }: {
  agents: AgentOption[]; text: string; setText: (t: string) => void; input: RefObject<HTMLTextAreaElement>; onPick: (agent: string) => void
}) {
  const [mention, setMention] = useState<Mention | null>(null)
  const [active, setActive] = useState(0)
  // Escape closes the list for this "@…" only: typing more of it keeps it closed; once the mention ends (deleted,
  // a space, sent, caret moved away) or another @ starts, the next @ opens the list again, at any offset.
  const [dismissed, setDismissed] = useState<number | null>(null)

  const items = useMemo(() => {
    if (!mention) return []
    const q = mention.query
    const starts = agents.filter(a => a.name.startsWith(q))
    const has = agents.filter(a => !a.name.startsWith(q) && a.name.includes(q))
    return [...starts, ...has].slice(0, 8)
  }, [agents, mention])
  const open = !!mention && items.length > 0 && dismissed !== mention.start

  /** Call after every change or caret move in the textarea. */
  const sync = useCallback(() => {
    const el = input.current
    if (!el) return
    const m = el.selectionStart === el.selectionEnd ? findMention(el.value, el.selectionStart) : null
    setMention(prev => (prev?.start === m?.start && prev?.query === m?.query ? prev : m))
    setDismissed(d => (d !== null && m?.start === d ? d : null))
    setActive(0)
  }, [input])
  // The text also changes without a keystroke (sent and cleared, a preset, a pick): re-read the mention then too.
  useEffect(() => { sync() }, [text, sync])

  const pick = useCallback((name: string) => {
    if (!mention) return
    const el = input.current
    const end = mention.start + 1 + mention.query.length
    const rest = text.slice(end).replace(/^\s+/, '')
    const head = text.slice(0, mention.start).replace(/\s+$/, '')
    const next = head && rest ? `${head} ${rest}` : head || rest
    setText(next)
    setMention(null)
    onPick(name)
    requestAnimationFrame(() => { if (el) { el.focus(); const at = Math.min(head.length + (head && rest ? 1 : 0), next.length); el.setSelectionRange(at, at) } })
  }, [mention, text, setText, onPick, input])

  /** Returns true when the key was handled by the list. */
  const onKeyDown = useCallback((e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (!open) return false
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault()
      setActive(i => (i + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length)
      return true
    }
    if ((e.key === 'Enter' && !e.shiftKey) || e.key === 'Tab') {
      e.preventDefault()
      pick(items[active]?.name ?? items[0].name)
      return true
    }
    if (e.key === 'Escape') { e.preventDefault(); setDismissed(mention!.start); return true }
    return false
  }, [open, items, active, pick, mention])

  const optionId = (i: number) => `chat-agent-opt-${i}`
  const inputProps = {
    role: 'combobox' as const, 'aria-autocomplete': 'list' as const, 'aria-expanded': open,
    'aria-controls': open ? 'chat-agent-list' : undefined, 'aria-activedescendant': open ? optionId(active) : undefined,
  }
  const close = useCallback(() => { setMention(null); setDismissed(null) }, [])
  return { open, items, active, setActive, pick, sync, onKeyDown, inputProps, optionId, close }
}

export function MentionList({ m }: { m: ReturnType<typeof useMention> }) {
  if (!m.open) return null
  return (
    <ul id="chat-agent-list" role="listbox" aria-label="Agents"
      className="absolute bottom-full left-0 z-30 mb-2 max-h-72 w-[min(340px,100%)] overflow-y-auto rounded-lg border border-border bg-popover p-1 shadow-md">
      <li role="presentation" className="px-2 pb-1 pt-1.5 text-xs font-medium text-muted-foreground">Ask one agent directly</li>
      {m.items.map((a, i) => (
        <li key={a.name} id={m.optionId(i)} role="option" aria-selected={i === m.active}
          onMouseDown={e => e.preventDefault()} onMouseEnter={() => m.setActive(i)} onClick={() => m.pick(a.name)}
          className={cn('flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 text-[13px]', i === m.active ? 'bg-subtle text-foreground' : 'text-foreground')}>
          <AgentIcon agent={a.name} size={14} tinted className="mt-0.5 shrink-0" />
          <span className="flex min-w-0 flex-col">
            <span className="font-medium">@{a.name}</span>
            {a.description && <span className="truncate text-xs text-muted-foreground">{a.description}</span>}
          </span>
        </li>
      ))}
    </ul>
  )
}

export function AgentChip({ agent, onRemove }: { agent: string; onRemove: () => void }) {
  return (
    <span className="inline-flex h-7 min-w-0 max-w-full items-center gap-1.5 rounded-md border bg-subtle/60 pl-2 pr-0.5 text-[13px]"
      style={{ borderColor: `color-mix(in srgb, ${colorOf(agent)} 35%, transparent)` }}>
      <AgentIcon agent={agent} size={14} tinted className="shrink-0" />
      <span className="truncate font-medium">@{agent}</span>
      <span className="text-xs text-muted-foreground max-sm:hidden">answers directly</span>
      <button type="button" data-slot="button" aria-label={`Remove @${agent}, let routing pick the agent`} onClick={onRemove}
        className="grid size-6 shrink-0 place-items-center rounded-sm text-muted-foreground transition-colors hover:bg-subtle hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
        <Icon name="close" size={12} />
      </button>
    </span>
  )
}

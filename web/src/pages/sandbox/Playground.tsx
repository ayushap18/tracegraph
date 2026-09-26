import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Button, Field, IconButton, Toggle } from '../../ui'
import { Icon } from '../../icons'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { cn } from '@/lib/utils'
import type { DraftAgent, PlaygroundProps, Run } from './types'

// Agent playground (docs/PLAN-sandbox.md, feature 5): draft a custom agent that exists only for this sandbox's
// runs. Validation mirrors Agents.tsx (and the server), plus the draft may not reuse an existing agent name.

const NAME_RE = /^[a-z][a-z0-9_-]{1,23}$/
const DESC_MIN = 10, DESC_MAX = 200
const PROMPT_MIN = 10, PROMPT_MAX = 4000

interface Form { name: string; description: string; prompt: string; web: boolean }
type Errors = Partial<Record<keyof Form, string>>
const EMPTY: Form = { name: '', description: '', prompt: '', web: false }
const toForm = (v: DraftAgent | null): Form => (v ? { name: v.name, description: v.description, prompt: v.prompt, web: !!v.web } : EMPTY)

function validate(d: Form, builtIn: string[]): Errors {
  const out: Errors = {}
  const name = d.name.trim()
  if (!NAME_RE.test(name)) {
    out.name = name.length < 2 || name.length > 24
      ? 'Use 2 to 24 characters.'
      : 'Start with a lowercase letter; then lowercase letters, digits, _ or - only.'
  } else if (builtIn.includes(name)) {
    out.name = `"${name}" is already an agent. Pick another name.`
  }
  const desc = d.description.trim().length
  if (desc < DESC_MIN || desc > DESC_MAX) out.description = `Use ${DESC_MIN} to ${DESC_MAX} characters (now ${desc}).`
  const prompt = d.prompt.trim().length
  if (prompt < PROMPT_MIN || prompt > PROMPT_MAX) out.prompt = `Use ${PROMPT_MIN} to ${PROMPT_MAX} characters (now ${prompt}).`
  return out
}

/** Runs that had at least one task routed, and those among them where the draft got a task. */
function draftStats(runs: Run[], name: string | undefined) {
  let routed = 0
  const hits: Run[] = []
  for (const r of runs) {
    const tasks = r.order.map(t => r.tasks[t]).filter(Boolean)
    if (!tasks.some(t => t.routed)) continue
    routed++
    if (name && tasks.some(t => t.routed?.agent === name)) hits.push(r)
  }
  return { routed, hits }
}

export function Playground({ value, onChange, engineOk, runs, builtIn, open, onOpenChange }: PlaygroundProps) {
  const [d, setD] = useState<Form>(() => toForm(value))
  const [touched, setTouched] = useState<Partial<Record<keyof Form, boolean>>>({})
  const [submitted, setSubmitted] = useState(false)

  // Each time the sheet opens, start from the active draft (or keep what was typed if there is none).
  useEffect(() => {
    if (!open) return
    if (value) setD(toForm(value))
    setTouched({}); setSubmitted(false)
  }, [open]) // eslint-disable-line react-hooks/exhaustive-deps

  const errors = validate(d, builtIn)
  const valid = Object.keys(errors).length === 0
  const show = (k: keyof Form) => (submitted || touched[k] ? errors[k] : undefined)
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setD(p => ({ ...p, [k]: v }))
  const blur = (k: keyof Form) => () => setTouched(t => ({ ...t, [k]: true }))

  const unchanged = !!value && value.name === d.name.trim() && value.description === d.description.trim()
    && value.prompt === d.prompt.trim() && !!value.web === d.web

  const submit = (e: FormEvent) => {
    e.preventDefault()
    setSubmitted(true)
    if (!valid) return
    const draft: DraftAgent = { name: d.name.trim(), description: d.description.trim(), prompt: d.prompt.trim() }
    if (d.web) draft.web = true
    onChange(draft)
    onOpenChange(false)
  }

  const stats = useMemo(() => draftStats(runs, value?.name), [runs, value?.name])

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" showCloseButton={false} className="w-full gap-0 border-border sm:max-w-md"
        onOpenAutoFocus={e => { e.preventDefault(); document.getElementById('sbx-draft-name')?.focus() }}>
        <SheetHeader className="flex-row items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <SheetTitle className="text-[15px]">Agent playground</SheetTitle>
            <SheetDescription className="mt-1 text-[13px] text-pretty">
              Draft an agent and see which questions Jev sends to it. It is used only in this sandbox and never saved.
            </SheetDescription>
          </div>
          <IconButton icon="close" label="Close" size="sm" onClick={() => onOpenChange(false)} />
        </SheetHeader>

        <form className="flex min-h-0 flex-1 flex-col" onSubmit={submit} noValidate aria-label="Draft agent">
          <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-5 py-5">
            {!engineOk && (
              <p className="m-0 flex items-start gap-2 rounded-md border border-warn/30 bg-warn/5 px-3 py-2 text-[13px] text-foreground" role="status">
                <Icon name="warning" size={15} className="mt-0.5 shrink-0 text-warn" />
                <span>Draft agents need an LLM engine; pick one in the engine menu.</span>
              </p>
            )}
            <Field id="sbx-draft-name" label="Name" hint="Lowercase, 2 to 24 characters: letters, digits, _ or -" error={show('name')}
              value={d.name} onChange={e => set('name', e.target.value.toLowerCase())} onBlur={blur('name')}
              placeholder="e.g. recipes" maxLength={24} autoComplete="off" spellCheck={false} className="[&_input]:font-mono" />
            <Field label="Description" hint="What it handles. Jev routes subtasks on this text." error={show('description')}
              value={d.description} onChange={e => set('description', e.target.value)} onBlur={blur('description')}
              placeholder="Cooking, recipes and ingredient substitutions" maxLength={DESC_MAX} />
            <Field label="System prompt" hint="Sent as the system prompt, after TraceGraph's own preamble." error={show('prompt')} textarea rows={8}
              value={d.prompt} onChange={e => set('prompt', e.target.value)} onBlur={blur('prompt')}
              placeholder="You are a friendly chef. Give concise recipes with metric quantities…" maxLength={PROMPT_MAX}
              className="[&_textarea]:font-mono [&_textarea]:text-[13px]" />
            <Toggle checked={d.web} onChange={(v: boolean) => set('web', v)} label="Allow web search (when the engine supports it)" />

            {value && <DraftStats name={value.name} routed={stats.routed} hits={stats.hits} />}
          </div>

          <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-5 py-4">
            {value
              ? <Button variant="ghost" icon="close" className="mr-auto" onClick={() => { onChange(null); setD(EMPTY); setTouched({}); setSubmitted(false) }}>Stop using</Button>
              : (d.name || d.description || d.prompt) && <Button variant="ghost" onClick={() => { setD(EMPTY); setTouched({}); setSubmitted(false) }}>Reset</Button>}
            <Button type="submit" icon={value ? 'check' : 'sandbox'} disabled={unchanged}>{value ? 'Update draft' : 'Use in sandbox'}</Button>
          </div>
        </form>
      </SheetContent>
    </Sheet>
  )
}

function DraftStats({ name, routed, hits }: { name: string; routed: number; hits: Run[] }) {
  const recent = hits.slice(-5).reverse()
  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border bg-subtle/40 p-4" aria-labelledby="sbx-draft-stats-h">
      <h3 id="sbx-draft-stats-h" className="m-0 text-sm font-semibold">How Jev uses <span className="font-mono">{name}</span></h3>
      {routed === 0
        ? <p className="m-0 text-[13px] text-muted-foreground">No routed runs yet. Ask something in the sandbox to see where Jev sends it.</p>
        : (
          <>
            <p className="m-0 text-[13px] text-muted-foreground">
              <span className="font-medium tabular-nums text-foreground">{hits.length}</span> of{' '}
              <span className="tabular-nums">{routed}</span> routed {routed === 1 ? 'run' : 'runs'} sent a task to the draft
              {routed > 0 && <span className="tabular-nums"> ({Math.round((hits.length / routed) * 100)}%)</span>}.
            </p>
            {recent.length > 0 && (
              <ul className="m-0 flex list-none flex-col gap-1.5 p-0" aria-label="Recent questions routed to the draft">
                {recent.map(r => (
                  <li key={r.qid} className="flex min-w-0 items-baseline gap-2 text-[13px]">
                    <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">#{r.qid}</span>
                    <span className="min-w-0 truncate text-foreground" title={r.text}>{r.text}</span>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
    </section>
  )
}

/** Composer toolbar control: "Draft: name" with edit and remove when a draft is active, else a "Playground" button. */
export function PlaygroundChip({ value, onOpen, onClear, disabled }: {
  value: DraftAgent | null; onOpen: () => void; onClear: () => void; disabled?: boolean
}) {
  if (!value) {
    return <Button variant="ghost" size="sm" icon="agents" onClick={onOpen} disabled={disabled} aria-haspopup="dialog">Playground</Button>
  }
  return (
    <span className={cn('inline-flex h-7 min-w-0 max-w-full items-center gap-1 rounded-md border border-primary/30 bg-primary/10 pl-1 pr-0.5 text-[13px]',
      disabled && 'opacity-60')}>
      <button type="button" data-slot="button" onClick={onOpen} disabled={disabled} aria-haspopup="dialog"
        aria-label={`Edit draft agent ${value.name}`} title="Edit the draft agent"
        className="inline-flex min-w-0 items-center gap-1.5 rounded-sm border-0 bg-transparent px-1 py-0.5 text-primary transition-colors hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35 disabled:pointer-events-none">
        <Icon name="agents" size={14} className="shrink-0" />
        <span className="shrink-0">Draft:</span>
        <span className="min-w-0 max-w-[140px] truncate font-mono font-medium">{value.name}</span>
      </button>
      <button type="button" data-slot="button" onClick={onClear} disabled={disabled} aria-label={`Stop using draft agent ${value.name}`}
        className="grid size-6 shrink-0 place-items-center rounded-sm border-0 bg-transparent text-primary transition-colors hover:bg-primary/15 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35 disabled:pointer-events-none">
        <Icon name="close" size={12} />
      </button>
    </span>
  )
}

export default Playground

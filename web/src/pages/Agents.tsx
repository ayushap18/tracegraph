import { useCallback, useEffect, useMemo, useState, type Dispatch, type FormEvent, type ReactNode, type SetStateAction } from 'react'
import { createAgent, deleteAgent, errorText as errText, listAgents } from '../api'
import { Badge, Button, EmptyState, Field, IconButton, Skeleton, Toggle, useToast } from '../ui'
import { useStore } from '../store'
import { AgentIcon } from '../icons'
import { PageBody, Section } from '../components/app'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { cn } from '@/lib/utils'
import { colorOf, type AgentInfo } from '../protocol'

// Every agent Jev can route to: built-ins, guards and user-defined agents, plus a form to create new ones.
// Validation mirrors the server (PLAN-v4 §1.6) so errors show while typing, not after a round trip.

const NAME_RE = /^[a-z][a-z0-9_-]{1,23}$/
const DESC_MIN = 10, DESC_MAX = 200
const PROMPT_MIN = 10, PROMPT_MAX = 4000
const MAX_CUSTOM = 12

interface Draft { name: string; description: string; prompt: string; web: boolean }
const EMPTY: Draft = { name: '', description: '', prompt: '', web: false }

function validate(d: Draft, agents: AgentInfo[]): Partial<Record<keyof Draft | 'form', string>> {
  const out: Partial<Record<keyof Draft | 'form', string>> = {}
  const name = d.name.trim()
  if (!NAME_RE.test(name)) {
    out.name = name.length < 2 || name.length > 24
      ? 'Use 2 to 24 characters.'
      : 'Start with a lowercase letter; then lowercase letters, digits, _ or - only.'
  } else {
    const clash = agents.find(a => a.name === name)
    if (clash) out.name = clash.kind === 'custom' ? 'A custom agent already has this name.' : `"${name}" is a ${clash.kind === 'guard' ? 'guard' : 'built-in agent'}.`
  }
  const desc = d.description.trim().length
  if (desc < DESC_MIN || desc > DESC_MAX) out.description = `Use ${DESC_MIN} to ${DESC_MAX} characters (now ${desc}).`
  const prompt = d.prompt.trim().length
  if (prompt < PROMPT_MIN || prompt > PROMPT_MAX) out.prompt = `Use ${PROMPT_MIN} to ${PROMPT_MAX} characters (now ${prompt}).`
  if (agents.filter(a => a.kind === 'custom').length >= MAX_CUSTOM) out.form = `You already have the maximum of ${MAX_CUSTOM} custom agents.`
  return out
}

export default function Agents(_props: { params: Record<string, string> }) {
  const { store, subscribe } = useStore()
  const toast = useToast()
  const [agents, setAgents] = useState<AgentInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  // The draft lives here so closing the sheet by accident does not lose what was typed.
  const [draft, setDraft] = useState<Draft>(EMPTY)

  const load = useCallback(async () => {
    try { const res = await listAgents(); setAgents(res.agents); setError(null) } catch (err) { setError(errText(err)) }
  }, [])
  useEffect(() => { void load() }, [load])
  // Engine switches and agent changes arrive as `config` events; availability depends on them.
  useEffect(() => subscribe(e => { if (e.type === 'config') void load() }), [subscribe, load])

  const groups = useMemo(() => {
    const list = agents ?? []
    return {
      builtin: list.filter(a => a.kind === 'builtin'),
      guard: list.filter(a => a.kind === 'guard'),
      custom: list.filter(a => a.kind === 'custom'),
    }
  }, [agents])

  const remove = async (name: string) => {
    try {
      await deleteAgent(name)
      setAgents(a => (a ? a.filter(x => x.name !== name) : a))
      toast.success(`Deleted agent "${name}"`)
    } catch (err) { toast.error(`Could not delete "${name}": ${errText(err)}`) }
  }

  const loading = agents == null && !error
  const newButton = <Button icon="plus" size="sm" disabled={agents == null} onClick={() => setCreating(true)}>New agent</Button>

  if (error && !agents) {
    return (
      <PageBody>
        <div className="rounded-lg border border-border bg-surface">
          <EmptyState icon="agents" title="Could not load agents" text={error}
            action={<Button variant="secondary" icon="retry" onClick={() => void load()}>Retry</Button>} />
        </div>
      </PageBody>
    )
  }

  return (
    <PageBody>
      <Section headingId="ag-custom-h" icon="agents" title="Your agents" count={`${groups.custom.length}/${MAX_CUSTOM}`} actions={newButton}
        description={!store.engine ? 'Custom agents run on an LLM engine; they are offered to Jev only while one is active.' : undefined}>
        {loading ? <Grid><SkeletonCards n={2} /></Grid>
          : groups.custom.length === 0
            ? (
              <div className="rounded-lg border border-dashed border-border bg-surface">
                <EmptyState icon="agents" title="No custom agents yet" text="Describe a specialist and give it a system prompt. Jev routes matching subtasks to it."
                  action={<Button icon="plus" onClick={() => setCreating(true)}>New agent</Button>} />
              </div>
            )
            : <Grid>{groups.custom.map(a => <AgentCard key={a.name} agent={a} onDelete={() => remove(a.name)} />)}</Grid>}
      </Section>

      <Section headingId="ag-builtin-h" icon="agent" title="Built-in agents">
        {loading ? <Grid><SkeletonCards n={6} /></Grid>
          : groups.builtin.length === 0 ? <p className="m-0 text-[13px] text-muted-foreground">None reported.</p>
          : <Grid>{groups.builtin.map(a => <AgentCard key={a.name} agent={a} />)}</Grid>}
      </Section>

      <Section headingId="ag-guard-h" icon="confidence" title="Guards" description="Guards catch unclear or unsafe subtasks before any agent runs.">
        {loading ? <Grid><SkeletonCards n={2} /></Grid>
          : groups.guard.length === 0 ? <p className="m-0 text-[13px] text-muted-foreground">None reported.</p>
          : <Grid>{groups.guard.map(a => <AgentCard key={a.name} agent={a} />)}</Grid>}
      </Section>

      <Sheet open={creating} onOpenChange={setCreating}>
        <SheetContent side="right" showCloseButton={false} className="w-full gap-0 border-border sm:max-w-md"
          onOpenAutoFocus={e => { e.preventDefault(); document.getElementById('ag-new-name')?.focus() }}>
          <SheetHeader className="flex-row items-start justify-between gap-3 border-b border-border px-5 py-4">
            <div className="min-w-0">
              <SheetTitle className="text-[15px]">New agent</SheetTitle>
              <SheetDescription className="mt-1 text-[13px]">Describe a specialist and give it a system prompt. Jev routes matching subtasks to it.</SheetDescription>
            </div>
            <IconButton icon="close" label="Close" size="sm" onClick={() => setCreating(false)} />
          </SheetHeader>
          <CreateForm agents={agents ?? []} disabled={agents == null} d={draft} setD={setDraft}
            onCreated={a => { setAgents(list => [...(list ?? []).filter(x => x.name !== a.name), a]); toast.success(`Created agent "${a.name}"`); setCreating(false) }} />
        </SheetContent>
      </Sheet>
    </PageBody>
  )
}

function Grid({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">{children}</div>
}

function SkeletonCards({ n }: { n: number }) {
  return <>{Array.from({ length: n }, (_, i) => (
    <div key={i} className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4" aria-hidden="true">
      <div className="flex items-center gap-3"><Skeleton width={36} height={36} radius={8} /><Skeleton width="45%" height={16} /></div>
      <Skeleton lines={2} />
    </div>
  ))}</>
}

function AgentCard({ agent: a, onDelete }: { agent: AgentInfo; onDelete?: () => Promise<void> }) {
  const [confirming, setConfirming] = useState(false)
  const [busy, setBusy] = useState(false)
  const [showPrompt, setShowPrompt] = useState(false)
  const del = async () => {
    if (!onDelete) return
    setBusy(true)
    try { await onDelete() } finally { setBusy(false); setConfirming(false) }
  }
  const c = colorOf(a.name)
  return (
    <article className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-4" aria-labelledby={`ag-${a.name}-name`}>
      <header className="flex min-w-0 items-start gap-3">
        <span aria-hidden="true" className={cn('grid size-9 shrink-0 place-items-center rounded-md', !a.available && 'opacity-60')}
          style={{ background: `color-mix(in oklab, ${c} 14%, transparent)`, color: c }}>
          <AgentIcon agent={a.name} size={18} />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
          <h3 id={`ag-${a.name}-name`} className={cn('m-0 truncate font-mono text-sm font-medium', a.available ? 'text-foreground' : 'text-muted-foreground')}>{a.name}</h3>
          <span className="flex flex-wrap gap-1.5">
            {a.engine_required && <Badge tone="info">needs engine</Badge>}
            {a.web && <Badge tone="muted">web</Badge>}
            <Badge tone={a.available ? 'ok' : 'muted'} dot>{a.available ? 'available' : 'inactive'}</Badge>
          </span>
        </div>
      </header>
      <p className="m-0 text-[13px] leading-relaxed text-muted-foreground text-pretty">{a.description}</p>
      {a.kind === 'custom' && a.prompt && (
        <div className="flex flex-col items-start gap-2">
          <button type="button" data-slot="button" aria-expanded={showPrompt} onClick={() => setShowPrompt(s => !s)}
            className="rounded-sm border-0 bg-transparent p-0 text-[13px] font-medium text-primary transition-colors hover:underline focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
            {showPrompt ? 'Hide prompt' : 'Show prompt'}
          </button>
          {showPrompt && <pre className="m-0 max-h-64 w-full overflow-auto rounded-md border border-border bg-subtle p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap break-words text-foreground">{a.prompt}</pre>}
        </div>
      )}
      {onDelete && (
        <footer className="mt-auto flex min-h-8 items-center border-t border-border pt-3">
          {confirming
            ? <span className="flex min-w-0 flex-wrap items-center gap-2" role="group" aria-label={`Confirm deleting ${a.name}`}>
                <span className="text-[13px] text-foreground">Delete “{a.name}”?</span>
                <Button size="sm" variant="danger" onClick={() => void del()} disabled={busy}>{busy ? 'Deleting…' : 'Delete'}</Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirming(false)} disabled={busy}>Cancel</Button>
              </span>
            : <Button size="sm" variant="ghost" icon="trash" onClick={() => setConfirming(true)} aria-label={`Delete agent ${a.name}`}>Delete</Button>}
        </footer>
      )}
    </article>
  )
}

function CreateForm({ agents, disabled, onCreated, d, setD }: {
  agents: AgentInfo[]; disabled: boolean; onCreated: (a: AgentInfo) => void
  d: Draft; setD: Dispatch<SetStateAction<Draft>>
}) {
  const [touched, setTouched] = useState<Partial<Record<keyof Draft, boolean>>>({})
  const [submitted, setSubmitted] = useState(false)
  const [busy, setBusy] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)
  const errors = validate(d, agents)
  const valid = Object.keys(errors).length === 0
  const show = (k: keyof Draft) => (submitted || touched[k] ? errors[k] : undefined)
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => { setD(p => ({ ...p, [k]: v })); setServerError(null) }
  const blur = (k: keyof Draft) => () => setTouched(t => ({ ...t, [k]: true }))

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setSubmitted(true)
    if (!valid || busy || disabled) return
    setBusy(true)
    try {
      const a = await createAgent({ name: d.name.trim(), description: d.description.trim(), prompt: d.prompt.trim(), web: d.web })
      onCreated(a)
      setD(EMPTY); setTouched({}); setSubmitted(false)
    } catch (err) {
      setServerError(errText(err))
    } finally { setBusy(false) }
  }

  return (
    <form className="flex min-h-0 flex-1 flex-col" onSubmit={submit} noValidate aria-label="Create a custom agent">
      <div className="flex min-h-0 flex-1 flex-col gap-5 overflow-y-auto px-5 py-5">
        <Field id="ag-new-name" label="Name" hint="Lowercase, 2 to 24 characters: letters, digits, _ or -" error={show('name')}
          value={d.name} onChange={e => set('name', e.target.value.toLowerCase())} onBlur={blur('name')}
          placeholder="e.g. recipes" maxLength={24} autoComplete="off" spellCheck={false} className="[&_input]:font-mono" />
        <Field label="Description" hint="What it handles. Jev routes subtasks on this text." error={show('description')}
          value={d.description} onChange={e => set('description', e.target.value)} onBlur={blur('description')}
          placeholder="Cooking, recipes and ingredient substitutions" maxLength={DESC_MAX} />
        <Field label="System prompt" hint="Sent as the system prompt, after TraceGraph's own preamble." error={show('prompt')} textarea rows={8}
          value={d.prompt} onChange={e => set('prompt', e.target.value)} onBlur={blur('prompt')}
          placeholder="You are a friendly chef. Give concise recipes with metric quantities…" maxLength={PROMPT_MAX} className="[&_textarea]:font-mono [&_textarea]:text-[13px]" />
        <Toggle checked={d.web} onChange={(v: boolean) => set('web', v)} label="Allow web search (when the engine supports it)" />
        {(errors.form || serverError) && (
          <p className="m-0 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-[13px] text-destructive" role="alert">{serverError ?? errors.form}</p>
        )}
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border px-5 py-4">
        {(d.name || d.description || d.prompt) && <Button variant="ghost" type="button" onClick={() => { setD(EMPTY); setTouched({}); setSubmitted(false); setServerError(null) }}>Reset</Button>}
        <Button type="submit" icon="plus" disabled={disabled || busy || !!errors.form}>{busy ? 'Creating…' : 'Create agent'}</Button>
      </div>
    </form>
  )
}

import { useCallback, useEffect, useMemo, useState, type FormEvent } from 'react'
import { createAgent, deleteAgent, errorText as errText, listAgents } from '../api'
import { Badge, Button, EmptyState, Field, Skeleton, Toggle, useToast } from '../ui'
import { useStore } from '../store'
import { AgentIcon, Icon } from '../icons'
import { colorOf, type AgentInfo } from '../protocol'
import './agents.css'

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
      ? 'Use 2–24 characters.'
      : 'Start with a lowercase letter; then lowercase letters, digits, _ or - only.'
  } else {
    const clash = agents.find(a => a.name === name)
    if (clash) out.name = clash.kind === 'custom' ? 'A custom agent already has this name.' : `"${name}" is a ${clash.kind === 'guard' ? 'guard' : 'built-in agent'}.`
  }
  const desc = d.description.trim().length
  if (desc < DESC_MIN || desc > DESC_MAX) out.description = `Use ${DESC_MIN}–${DESC_MAX} characters (now ${desc}).`
  const prompt = d.prompt.trim().length
  if (prompt < PROMPT_MIN || prompt > PROMPT_MAX) out.prompt = `Use ${PROMPT_MIN}–${PROMPT_MAX} characters (now ${prompt}).`
  if (agents.filter(a => a.kind === 'custom').length >= MAX_CUSTOM) out.form = `You already have the maximum of ${MAX_CUSTOM} custom agents.`
  return out
}

export default function Agents(_props: { params: Record<string, string> }) {
  const { store, subscribe } = useStore()
  const toast = useToast()
  const [agents, setAgents] = useState<AgentInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)

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

  return (
    <div className="ag-page">
      {error && !agents && (
        <div className="panel">
          <EmptyState icon="agents" title="Could not load agents" text={error}
            action={<Button variant="ghost" onClick={() => void load()}>Retry</Button>} />
        </div>
      )}

      <section className="ag-section" aria-labelledby="ag-custom-h">
        <div className="ag-section-head">
          <h2 id="ag-custom-h"><Icon name="agents" size={15} /> Custom agents <span className="muted small num">{groups.custom.length}/{MAX_CUSTOM}</span></h2>
          {!store.engine && <p className="muted small">Custom agents run on an LLM engine; they are offered to Jev only while one is active.</p>}
        </div>
        <div className="ag-custom-grid">
          <CreateForm agents={agents ?? []} disabled={loading} onCreated={a => { setAgents(list => [...(list ?? []), a]); toast.success(`Created agent "${a.name}"`) }} />
          <div className="ag-custom-list">
            {loading ? <SkeletonCards n={2} />
              : groups.custom.length === 0
                ? <div className="panel"><EmptyState icon="agents" title="No custom agents yet" text="Describe a specialist and give it a system prompt. Jev routes matching subtasks to it." /></div>
                : groups.custom.map(a => <AgentCard key={a.name} agent={a} onDelete={() => remove(a.name)} />)}
          </div>
        </div>
      </section>

      <section className="ag-section" aria-labelledby="ag-builtin-h">
        <div className="ag-section-head"><h2 id="ag-builtin-h"><Icon name="agent" size={15} /> Built-in agents</h2></div>
        <div className="ag-grid">
          {loading ? <SkeletonCards n={6} />
            : groups.builtin.length === 0 ? <p className="muted small">None reported.</p>
            : groups.builtin.map(a => <AgentCard key={a.name} agent={a} />)}
        </div>
      </section>

      <section className="ag-section" aria-labelledby="ag-guard-h">
        <div className="ag-section-head">
          <h2 id="ag-guard-h"><Icon name="confidence" size={15} /> Guards</h2>
          <p className="muted small">Guards catch unclear or unsafe subtasks before any agent runs.</p>
        </div>
        <div className="ag-grid">
          {loading ? <SkeletonCards n={2} />
            : groups.guard.length === 0 ? <p className="muted small">None reported.</p>
            : groups.guard.map(a => <AgentCard key={a.name} agent={a} />)}
        </div>
      </section>
    </div>
  )
}

function SkeletonCards({ n }: { n: number }) {
  return <>{Array.from({ length: n }, (_, i) => <div key={i} className="panel ag-card" aria-hidden="true"><Skeleton height={18} /><Skeleton lines={2} /></div>)}</>
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
  return (
    <article className={'panel ag-card' + (a.available ? '' : ' unavailable')} style={{ ['--c' as string]: colorOf(a.name) }}>
      <header className="ag-card-head">
        <span className="ag-icon" aria-hidden="true"><AgentIcon agent={a.name} size={18} /></span>
        <h3 className="ag-name">{a.name}</h3>
        <span className="ag-badges">
          {a.engine_required && <Badge tone="info">needs engine</Badge>}
          {a.web && <Badge tone="muted">web</Badge>}
          <Badge tone={a.available ? 'ok' : 'muted'}>{a.available ? 'available' : 'inactive'}</Badge>
        </span>
      </header>
      <p className="ag-desc">{a.description}</p>
      {a.kind === 'custom' && a.prompt && (
        <div className="ag-prompt">
          <button type="button" className="ag-link small" aria-expanded={showPrompt} onClick={() => setShowPrompt(s => !s)}>
            {showPrompt ? 'Hide prompt' : 'Show prompt'}
          </button>
          {showPrompt && <pre className="ag-prompt-text">{a.prompt}</pre>}
        </div>
      )}
      {onDelete && (
        <footer className="ag-card-foot">
          {confirming
            ? <span className="ag-confirm" role="group" aria-label={`Confirm deleting ${a.name}`}>
                <span className="small">Delete “{a.name}”?</span>
                <Button size="sm" variant="danger" onClick={() => void del()} disabled={busy}>{busy ? 'Deleting…' : 'Delete'}</Button>
                <Button size="sm" variant="ghost" onClick={() => setConfirming(false)} disabled={busy}>Cancel</Button>
              </span>
            : <Button size="sm" variant="ghost" icon="trash" onClick={() => setConfirming(true)} aria-label={`Delete agent ${a.name}`}>Delete</Button>}
        </footer>
      )}
    </article>
  )
}

function CreateForm({ agents, disabled, onCreated }: { agents: AgentInfo[]; disabled: boolean; onCreated: (a: AgentInfo) => void }) {
  const [d, setD] = useState<Draft>(EMPTY)
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
    if (!valid || busy) return
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
    <form className="panel ag-form" onSubmit={submit} noValidate aria-label="Create a custom agent">
      <h3><Icon name="plus" size={15} /> New agent</h3>
      <Field label="Name" hint="Lowercase, 2–24 characters: letters, digits, _ or -" error={show('name')}
        value={d.name} onChange={e => set('name', e.target.value.toLowerCase())} onBlur={blur('name')}
        placeholder="e.g. recipes" maxLength={24} autoComplete="off" spellCheck={false} />
      <Field label="Description" hint="What it handles. Jev routes subtasks on this text." error={show('description')}
        value={d.description} onChange={e => set('description', e.target.value)} onBlur={blur('description')}
        placeholder="Cooking, recipes and ingredient substitutions" maxLength={DESC_MAX} />
      <Field label="System prompt" hint="Sent as the system prompt, after TraceGraph's own preamble." error={show('prompt')} textarea rows={6}
        value={d.prompt} onChange={e => set('prompt', e.target.value)} onBlur={blur('prompt')}
        placeholder="You are a friendly chef. Give concise recipes with metric quantities…" maxLength={PROMPT_MAX} />
      <Toggle checked={d.web} onChange={(v: boolean) => set('web', v)} label="Allow web search (when the engine supports it)" />
      {(errors.form || serverError) && <p className="err" role="alert">{serverError ?? errors.form}</p>}
      <div className="ag-form-actions">
        <Button type="submit" icon="plus" disabled={disabled || busy || !!errors.form}>{busy ? 'Creating…' : 'Create agent'}</Button>
        {(d.name || d.description || d.prompt) && <Button variant="ghost" type="button" onClick={() => { setD(EMPTY); setTouched({}); setSubmitted(false); setServerError(null) }}>Reset</Button>}
      </div>
    </form>
  )
}

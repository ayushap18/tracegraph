import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ask, cancelRun, clearSandbox, errorText } from '../api'
import type { AskBody, FileInfo } from '../protocol'
import { colorOf } from '../protocol'
import { useEventStream, type Run } from '../useEventStream'
import { Badge, Button, IconButton, Kbd, useToast } from '../ui'
import { AgentIcon, Icon } from '../icons'
import { TraceView } from '../components/TraceView'
import { Waterfall } from '../components/Viz'
import { TopActions } from '../components/Shell'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import type { DraftAgent, EngineChoice, TurnGroup } from './sandbox/types'
import { EngineSelect, KEYLESS, MIN_COMPARE, engineLabelOf } from './sandbox/EngineSelect'
import { UsageMeter } from './sandbox/UsageMeter'
import { SideBySide } from './sandbox/SideBySide'
import { ExportMenu } from './sandbox/ExportMenu'
import { TurnGroupView } from './sandbox/TurnGroupView'
import { Playground, PlaygroundChip } from './sandbox/Playground'
import { AttachButton, Attachments, clearPendingUploads, useSandboxDrop } from './sandbox/Attachments'
import { KeepDialog } from './sandbox/KeepDialog'

// Sandbox: a separate, throwaway environment and a test bench (docs/PLAN-sandbox.md). Runs go through the real
// pipeline, but the server stores nothing (no run, session, stats or history) and their events travel on a private
// stream for this sandbox id only. This component owns all state; the parts in ./sandbox render it.

const newSandboxId = () =>
  'sbx-' + (globalThis.crypto?.randomUUID?.().replace(/-/g, '') ?? Math.random().toString(36).slice(2) + Date.now().toString(36))
const newGroupId = () => Math.random().toString(36).slice(2, 10)

const MAX_CHARS = 500

export default function Sandbox() {
  const toast = useToast()
  const [sid, setSid] = useState(newSandboxId)
  // A private event stream: its hello carries no history, and it only receives this sandbox's runs.
  const { store } = useEventStream('/events?sandbox=' + encodeURIComponent(sid))

  const [groups, setGroups] = useState<TurnGroup[]>([])
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [engine, setEngine] = useState<EngineChoice>('')
  const [compareWith, setCompareWith] = useState<string[]>([])
  const [draft, setDraft] = useState<DraftAgent | null>(null)
  const [playgroundOpen, setPlaygroundOpen] = useState(false)
  const [files, setFiles] = useState<FileInfo[]>([])
  const [keepOpen, setKeepOpen] = useState(false)
  const [selQid, setSelQid] = useState<number | null>(null)
  const [traceOpen, setTraceOpen] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const fileNames = useRef(new Map<string, string>()) // attachment id -> name, for the question's file chips
  const thread = useRef<HTMLDivElement>(null)

  const byQid = useMemo(() => new Map(store.runs.map(r => [r.qid, r])), [store.runs])
  const allQids = groups.flatMap(g => g.qids)
  const runs = allQids.map(q => byQid.get(q)).filter((r): r is Run => !!r)
  const running = runs.filter(r => !r.done)
  const busy = sending || running.length > 0
  const empty = !groups.length && !sending

  const lastGroup = groups[groups.length - 1]
  const defaultQid = lastGroup ? (lastGroup.kind === 'single' ? lastGroup.qids[lastGroup.active] : lastGroup.qids[0]) : undefined
  const selected = byQid.get(selQid ?? defaultQid ?? -1)

  // What the chosen engine is, for draft agents (they need an LLM) and labels.
  const activeLabel = store.engine?.label ?? 'Keyless'
  const comparing = compareWith.length >= MIN_COMPARE
  const engineOk = engine === '' ? store.engine != null : engine !== KEYLESS && !!store.engines.find(e => e.name === engine && e.available)
  const builtIn = useMemo(() => [...Object.keys(store.agents), ...store.guards], [store.agents, store.guards])
  // The version of a turn the server remembers: the latest one that finished without being cancelled
  // (cancelled runs never enter the sandbox thread). "Keep" saves these; an edit replaces this one.
  const rememberedQid = (g: TurnGroup) => [...g.qids].reverse().find(q => { const r = byQid.get(q); return r?.done && r.status !== 'cancelled' })
  const keepQids = groups.filter(g => g.kind === 'single').map(rememberedQid).filter((q): q is number => q != null)

  const { dragging, bind } = useSandboxDrop(sid, files, setFiles, busy)

  // Forget this sandbox on the server when the page goes away (route change, tab close, reload).
  const sidRef = useRef(sid)
  sidRef.current = sid
  useEffect(() => {
    const bye = () => void clearSandbox(sidRef.current)
    window.addEventListener('pagehide', bye)
    return () => { window.removeEventListener('pagehide', bye); bye() }
  }, [])
  // Leaving with messages on screen loses them: let the browser ask first.
  useEffect(() => {
    if (!groups.length) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [groups.length])

  // Follow the conversation as it grows.
  const lastRun = runs[runs.length - 1]
  useEffect(() => {
    const el = thread.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [groups.length, lastRun?.mergeStream, lastRun?.merged])

  useLayoutEffect(() => {
    const el = input.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 220) + 'px'
    el.style.overflowY = el.scrollHeight > 220 ? 'auto' : 'hidden' // no scrollbar until it's actually needed
  }, [text])

  /** Fields every sandbox /ask shares. The draft agent only goes to runs that have an LLM engine. */
  const base = (withEngine: string, withFiles: boolean): Omit<AskBody, 'query'> => ({
    source: 'sandbox', sandbox_id: sid,
    ...(withEngine ? { engine: withEngine } : {}),
    ...(draft && (withEngine ? withEngine !== KEYLESS : store.engine != null) ? { draft_agent: draft } : {}),
    ...(withFiles && files.length ? { files: files.map(f => f.id) } : {}),
  })

  const send = useCallback(async (raw: string) => {
    const q = raw.trim().slice(0, MAX_CHARS)
    if (!q || busy) return
    if (draft && !comparing && !engineOk) {
      toast.error('The draft agent needs an LLM engine. Pick one in the engine menu, or stop using the draft.')
      return
    }
    setSending(true)
    setText('')
    try {
      if (comparing) {
        // Side by side: one run per engine, kept out of the follow-up memory so they don't pile up as turns.
        const results = await Promise.allSettled(compareWith.map(e => ask({ ...base(e, true), query: q, remember: false })))
        const ok = results.flatMap((r, i) => (r.status === 'fulfilled' ? [{ qid: r.value.qid, engine: compareWith[i] }] : []))
        results.forEach((r, i) => {
          if (r.status === 'rejected') toast.error(`${engineLabelOf(store.engines, compareWith[i])}: ${errorText(r.reason)}`)
        })
        if (ok.length) setGroups(g => [...g, { id: newGroupId(), kind: 'compare', qids: ok.map(x => x.qid), engines: ok.map(x => x.engine), active: 0 }])
        else setText(current => current || q)
      } else {
        const res = await ask({ ...base(engine, true), query: q })
        setGroups(g => [...g, { id: newGroupId(), kind: 'single', qids: [res.qid], active: 0 }])
      }
      for (const f of files) fileNames.current.set(f.id, f.name)
      setFiles([])
      setSelQid(null)
    } catch (e) {
      toast.error(`Could not send: ${errorText(e)}`)
      setText(current => current || q)
    } finally {
      setSending(false)
      input.current?.focus()
    }
  }, [busy, draft, comparing, engineOk, compareWith, engine, files, sid, store.engines, store.engine, toast])

  /** Edit and re-run: a new version of a turn. It replaces the turn the server remembers (the latest version),
   *  so its context is the turns before it and later follow-ups see the new answer. */
  const onEdit = async (group: TurnGroup, q: string) => {
    if (busy) return
    setSending(true)
    try {
      const replaces = rememberedQid(group)
      const res = await ask({ ...base(engine, false), query: q.slice(0, MAX_CHARS), ...(replaces != null ? { replaces } : {}) })
      setGroups(gs => gs.map(g => (g.id === group.id ? { ...g, qids: [...g.qids, res.qid], active: g.qids.length } : g)))
      setSelQid(null)
    } catch (e) {
      toast.error(`Could not re-run: ${errorText(e)}`)
    } finally { setSending(false) }
  }

  const onSelectVersion = (group: TurnGroup, index: number) => {
    setGroups(gs => gs.map(g => (g.id === group.id ? { ...g, active: index } : g)))
    if (selQid != null && group.qids.includes(selQid)) setSelQid(group.qids[index])
  }

  const showTrace = (qid: number) => {
    setSelQid(qid)
    setTraceOpen(o => !(o && selected?.qid === qid))
  }

  const stop = async (qids = running.map(r => r.qid)) => {
    for (const q of qids) {
      try { await cancelRun(q) } catch (e) { toast.error(`Could not stop: ${errorText(e)}`) }
    }
  }

  const clear = () => {
    void clearSandbox(sid)
    clearPendingUploads(sid)
    setGroups([]); setSelQid(null); setTraceOpen(false); setText(''); setFiles([])
    setSid(newSandboxId()) // a new id opens a fresh private stream, memory and file space
    toast.info('Sandbox cleared')
    input.current?.focus()
  }

  const onKey = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(text) }
  }

  const samples = store.samples.slice(0, 4)
  const canSend = !!text.trim() && !busy
  const exportInput = { groups, byQid, engines: store.engines, draft }

  return (
    <div className="relative grid h-full min-h-0 xl:grid-cols-[minmax(0,1fr)_auto]">
      <TopActions>
        <Badge tone="warn" icon="lock" className="hidden lg:inline-flex" title="Nothing in the sandbox is saved">Not saved</Badge>
        {!empty && <IconButton icon="graph" label={traceOpen ? 'Hide trace panel' : 'Show trace panel'} active={traceOpen} onClick={() => setTraceOpen(o => !o)} />}
        <span className="hidden sm:inline-flex"><ExportMenu input={exportInput} disabled={!runs.some(r => r.done)} /></span>
        <Button variant="secondary" size="sm" icon="download" className="hidden sm:inline-flex" onClick={() => setKeepOpen(true)} disabled={!keepQids.length || busy}
          title="Save this conversation as a normal chat">Keep</Button>
        <ClearButton turns={groups.length} disabled={empty} onClear={clear} />
      </TopActions>

      <section aria-label="Sandbox conversation" className="relative flex min-h-0 min-w-0 flex-col" {...bind}>
        {dragging && (
          <div className="pointer-events-none absolute inset-3 z-10 grid place-items-center rounded-xl border-2 border-dashed border-primary/60 bg-primary/5 text-sm font-medium text-primary">
            Drop to attach (kept in memory for this sandbox only)
          </div>
        )}
        <div ref={thread} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex w-full max-w-[960px] flex-col gap-8 px-4 pt-5 pb-6 sm:px-6">
            <div role="note" className="mx-auto flex w-full max-w-[760px] items-start gap-3 rounded-lg border border-dashed border-warn/40 bg-warn/[0.06] px-3.5 py-3 text-[13px] leading-relaxed text-muted-foreground">
              <Icon name="sandbox" size={16} className="mt-0.5 shrink-0 text-warn" />
              <p className="m-0 min-w-0">
                <span className="font-medium text-foreground">Separate environment.</span>{' '}
                Nothing here is saved unless you press Keep: messages, runs, traces, draft agents and files live only in this tab and
                disappear when you leave, reload or clear. Sandbox runs don't appear in Live, Runs or the stats.
              </p>
            </div>

            {empty ? (
              <div className="mx-auto flex w-full max-w-[760px] flex-col items-center gap-3 py-8 text-center">
                <span className="grid size-12 place-items-center rounded-xl border border-border bg-surface text-warn shadow-xs"><Icon name="sandbox" size={22} /></span>
                <h2 className="m-0 text-2xl font-semibold tracking-[-0.02em] text-foreground">A test bench. Nothing is kept.</h2>
                <p className="m-0 max-w-lg text-sm text-muted-foreground text-pretty">
                  Pick an engine or compare several side by side, draft an agent in the playground, attach a file, then see why Jev routed
                  each step. Edit any question to try it again.
                </p>
                {samples.length > 0 && (
                  <div className="mt-3 grid w-full max-w-[640px] grid-cols-1 gap-2 sm:grid-cols-2">
                    {samples.map(s => (
                      <button key={s} type="button" data-slot="button" onClick={() => void send(s)}
                        className="flex cursor-pointer items-center gap-2.5 rounded-lg border border-border bg-surface p-3 text-left text-[13.5px] text-foreground transition-colors hover:border-edge hover:bg-subtle/40 focus-visible:ring-[3px] focus-visible:ring-ring/35 focus-visible:outline-none">
                        <AgentIcon agent={guessAgent(s)} size={16} style={{ color: colorOf(guessAgent(s)) }} className="shrink-0" />
                        <span className="min-w-0">{s}</span>
                      </button>
                    ))}
                  </div>
                )}
              </div>
            ) : (
              <div className="flex flex-col gap-10" aria-live="polite">
                <h2 className="sr-only">Messages</h2>
                {groups.map(g => g.kind === 'compare' ? (
                  <SideBySide key={g.id} group={g} byQid={byQid} engines={store.engines}
                    selectedQid={traceOpen ? selected?.qid ?? null : null} onShowTrace={showTrace} onStop={q => void stop([q])} />
                ) : (
                  <div key={g.id} className="mx-auto w-full max-w-[760px]">
                    <TurnGroupView group={g} byQid={byQid} agents={store.agents} draftName={draft?.name}
                      selectedQid={traceOpen ? selected?.qid ?? null : null} busy={busy}
                      onShowTrace={showTrace} onEdit={(gr, t) => void onEdit(gr, t)} onSelectVersion={onSelectVersion}
                      fileName={id => fileNames.current.get(id) ?? id} />
                  </div>
                ))}
                {sending && <p className="mx-auto m-0 flex w-full max-w-[760px] items-center gap-2 text-[13px] text-muted-foreground"><Icon name="spinner" size={14} className="animate-spin" />Sending…</p>}
              </div>
            )}
          </div>
        </div>

        <form className="shrink-0 px-4 pb-4 sm:px-6" onSubmit={e => { e.preventDefault(); void send(text) }}>
          <div className="mx-auto w-full max-w-[760px]">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <EngineSelect engines={store.engines} activeLabel={activeLabel} value={engine} onChange={setEngine}
                compareWith={compareWith} onCompareWith={setCompareWith} disabled={busy} />
              <PlaygroundChip value={draft} onOpen={() => setPlaygroundOpen(true)} onClear={() => setDraft(null)} />
              <span className="ml-auto"><UsageMeter runs={runs} prices={store.prices} engines={store.engines} /></span>
            </div>
            <div className={cn('rounded-xl border border-dashed border-edge bg-surface p-2 shadow-sm focus-within:border-primary/60 focus-within:ring-[3px] focus-within:ring-ring/15',
              dragging && 'border-primary')}>
              <Attachments sandboxId={sid} files={files} onChange={setFiles} disabled={busy} />
              <div className="flex items-end gap-2">
                <AttachButton sandboxId={sid} files={files} onChange={setFiles} disabled={busy} />
                <label htmlFor="sandbox-input" className="sr-only">Message (not saved)</label>
                <textarea id="sandbox-input" ref={input} rows={1} value={text} maxLength={MAX_CHARS}
                  onChange={e => setText(e.target.value)} onKeyDown={onKey}
                  placeholder={comparing ? `Ask ${compareWith.length} engines at once, nothing is saved…` : 'Ask anything, nothing is saved…'}
                  className="max-h-[220px] min-h-10 min-w-0 flex-1 resize-none bg-transparent px-1 py-2 text-[15px] leading-relaxed text-foreground outline-none placeholder:text-muted-foreground" />
                {running.length
                  ? <Button variant="secondary" size="md" icon="stop" onClick={() => void stop()}>Stop</Button>
                  : <IconButton type="submit" icon="send" label="Send" variant="primary" size="lg" disabled={!canSend} />}
              </div>
            </div>
            <div className="mt-1.5 flex items-center justify-between gap-3 px-1 text-xs text-muted-foreground">
              <span className="hidden items-center gap-1 sm:flex"><Kbd>Enter</Kbd> send <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> new line</span>
              <span className="flex min-w-0 items-center gap-1.5">
                <Icon name="lock" size={12} className="shrink-0" />
                <span className="truncate">
                  Not saved, {comparing ? `${compareWith.length} engines side by side` : `engine: ${engine ? engineLabelOf(store.engines, engine) : activeLabel}`}
                  {draft ? `, draft: ${draft.name}` : ''}
                </span>
                {text.length > 400 && <span className="tabular-nums">{text.length}/{MAX_CHARS}</span>}
              </span>
            </div>
            {/* Export and Keep live in the top bar from sm up; on phones they sit here. */}
            {!empty && (
              <div className="mt-2 flex items-center gap-2 sm:hidden">
                <ExportMenu input={exportInput} disabled={!runs.some(r => r.done)} />
                <Button variant="secondary" size="sm" icon="download" onClick={() => setKeepOpen(true)} disabled={!keepQids.length || busy}>Keep</Button>
              </div>
            )}
          </div>
        </form>
      </section>

      {traceOpen && selected && (
        <aside aria-label="Trace of the selected sandbox run"
          className={cn('flex min-h-0 flex-col border-l border-border bg-background',
            'absolute inset-y-0 right-0 z-20 w-full shadow-lg sm:w-[420px] xl:static xl:w-[380px] xl:shadow-none')}>
          <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-4">
            <Icon name="graph" size={15} className="text-muted-foreground" />
            <span className="text-sm font-semibold">Trace</span>
            <span className="truncate text-xs text-muted-foreground">{engineLabelOf(store.engines, selected.engine)}</span>
            <Badge tone="warn">not saved</Badge>
            <IconButton icon="close" label="Hide trace panel" size="sm" className="ml-auto" onClick={() => setTraceOpen(false)} />
          </div>
          <div className="min-h-0 flex-1 overflow-y-auto">
            <TraceView run={selected} store={store} compact legend={false} />
            <div className="border-t border-border px-4 py-4">
              <h3 className="mb-3 flex items-center gap-1.5 text-[13px] font-semibold"><Icon name="timeline" size={14} className="text-muted-foreground" />Timeline</h3>
              <Waterfall run={selected} />
            </div>
          </div>
        </aside>
      )}

      <Playground value={draft} onChange={setDraft} engineOk={engineOk} runs={runs} builtIn={builtIn} open={playgroundOpen} onOpenChange={setPlaygroundOpen} />
      <KeepDialog sandboxId={sid} qids={keepQids} open={keepOpen} onOpenChange={setKeepOpen} />
    </div>
  )
}

/** Clear forgets everything and can't be undone, so it asks first whenever there is something to lose. */
function ClearButton({ turns, disabled, onClear }: { turns: number; disabled: boolean; onClear: () => void }) {
  const [open, setOpen] = useState(false)
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="secondary" size="sm" icon="trash" disabled={disabled}>Clear</Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-3">
        <p className="m-0 text-sm font-medium text-foreground">Clear this sandbox?</p>
        <p className="m-0 mt-1 text-[13px] leading-relaxed text-muted-foreground">
          {turns} {turns === 1 ? 'turn' : 'turns'}, the follow-up memory and attached files are forgotten. This can't be undone; use Keep first to save them.
        </p>
        <div className="mt-3 flex justify-end gap-2">
          <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>Cancel</Button>
          <Button variant="danger" size="sm" icon="trash" onClick={() => { setOpen(false); onClear() }}>Clear</Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}

/** A best-guess agent for a sample's icon (samples are plain strings). */
function guessAgent(s: string) {
  const t = s.toLowerCase()
  if (/weather|rain|cold|warm/.test(t)) return 'weather'
  if (/usd|eur|inr|gbp|jpy|convert/.test(t)) return 'currency'
  if (/time|date/.test(t)) return 'time'
  if (/[0-9][^a-z]*[+*/%^-]|square root|%/.test(t)) return 'math'
  if (/python|code|script/.test(t)) return 'code'
  return 'knowledge'
}

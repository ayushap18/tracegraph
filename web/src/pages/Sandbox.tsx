import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ask, cancelRun, clearSandbox, errorText } from '../api'
import { useEventStream } from '../useEventStream'
import { Badge, Button, IconButton, Kbd, useToast } from '../ui'
import { AgentIcon, Icon } from '../icons'
import { colorOf } from '../protocol'
import { TraceView } from '../components/TraceView'
import { Waterfall } from '../components/Viz'
import { TopActions } from '../components/Shell'
import { Turn } from './Chat'
import { cn } from '@/lib/utils'

// Sandbox: a separate, throwaway environment. Runs go through the real pipeline, but the server stores nothing
// (no run, no session, no stats, no history), and their events travel on a private stream for this sandbox id
// only, so they never show up in Live, Runs or anyone else's browser. Messages live in this component's state:
// leaving the page, reloading or pressing Clear forgets them, and tells the server to forget the follow-up context.

const newSandboxId = () =>
  'sbx-' + (globalThis.crypto?.randomUUID?.().replace(/-/g, '') ?? Math.random().toString(36).slice(2) + Date.now().toString(36))

const MAX_CHARS = 500

export default function Sandbox() {
  const toast = useToast()
  const [sid, setSid] = useState(newSandboxId)
  // A private event stream: its hello carries no history, and it only receives this sandbox's runs.
  const { store } = useEventStream('/events?sandbox=' + encodeURIComponent(sid))
  const [qids, setQids] = useState<number[]>([])
  const [text, setText] = useState('')
  const [sending, setSending] = useState(false)
  const [selQid, setSelQid] = useState<number | null>(null)
  const [traceOpen, setTraceOpen] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const thread = useRef<HTMLDivElement>(null)

  const byQid = useMemo(() => new Map(store.runs.map(r => [r.qid, r])), [store.runs])
  const turns = qids.map(q => byQid.get(q)).filter((r): r is NonNullable<typeof r> => !!r)
  const running = turns.find(r => !r.done)
  const selected = (selQid != null ? byQid.get(selQid) : undefined) ?? turns[turns.length - 1]
  const empty = !turns.length && !sending

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
    if (!qids.length) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault() }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [qids.length])

  // Follow the conversation as it grows.
  useEffect(() => {
    const el = thread.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [turns.length, running?.mergeStream, running?.merged])

  useLayoutEffect(() => {
    const el = input.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(el.scrollHeight, 220) + 'px'
    el.style.overflowY = el.scrollHeight > 220 ? 'auto' : 'hidden' // no scrollbar until it's actually needed
  }, [text])

  const send = useCallback(async (raw: string) => {
    const q = raw.trim()
    if (!q || sending || running) return
    setSending(true)
    setText('')
    try {
      const res = await ask({ query: q.slice(0, MAX_CHARS), source: 'sandbox', sandbox_id: sid })
      setQids(list => [...list, res.qid])
      setSelQid(null)
    } catch (e) {
      toast.error(`Could not send: ${errorText(e)}`)
      setText(current => current || q)
    } finally {
      setSending(false)
      input.current?.focus()
    }
  }, [sending, running, sid, toast])

  const stop = async () => {
    if (!running) return
    try { await cancelRun(running.qid) } catch (e) { toast.error(`Could not stop: ${errorText(e)}`) }
  }

  const clear = () => {
    void clearSandbox(sid)
    setQids([]); setSelQid(null); setTraceOpen(false); setText('')
    setSid(newSandboxId()) // a new id opens a fresh private stream and a fresh follow-up context
    toast.info('Sandbox cleared')
    input.current?.focus()
  }

  const onKey = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); void send(text) }
  }

  const samples = store.samples.slice(0, 4)
  const canSend = !!text.trim() && !sending && !running

  return (
    <div className="relative grid h-full min-h-0 xl:grid-cols-[minmax(0,1fr)_auto]">
      <TopActions>
        <Badge tone="warn" icon="lock" className="hidden sm:inline-flex" title="Nothing in the sandbox is saved">Not saved</Badge>
        {!empty && <IconButton icon="graph" label={traceOpen ? 'Hide trace panel' : 'Show trace panel'} active={traceOpen} onClick={() => setTraceOpen(o => !o)} />}
        <Button variant="secondary" size="sm" icon="trash" onClick={clear} disabled={empty}>Clear</Button>
      </TopActions>

      <section aria-label="Sandbox conversation" className="flex min-h-0 min-w-0 flex-col">
        <div ref={thread} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto flex w-full max-w-[760px] flex-col gap-8 px-4 pt-5 pb-6 sm:px-6">
            <div role="note" className="flex items-start gap-3 rounded-lg border border-dashed border-warn/40 bg-warn/[0.06] px-3.5 py-3 text-[13px] leading-relaxed text-muted-foreground">
              <Icon name="sandbox" size={16} className="mt-0.5 shrink-0 text-warn" />
              <p className="m-0 min-w-0">
                <span className="font-medium text-foreground">Separate environment.</span>{' '}
                Nothing here is saved: messages, runs and traces live only in this tab and disappear when you leave, reload or clear.
                Sandbox runs don't appear in Live, Runs or the stats.
              </p>
            </div>

            {empty ? (
              <div className="flex flex-col items-center gap-3 py-10 text-center">
                <span className="grid size-12 place-items-center rounded-xl border border-border bg-surface text-warn shadow-xs"><Icon name="sandbox" size={22} /></span>
                <h2 className="m-0 text-2xl font-semibold tracking-[-0.02em] text-foreground">Try anything. Nothing is kept.</h2>
                <p className="m-0 max-w-md text-sm text-muted-foreground text-pretty">
                  Same planner, router and agents as Chat, with follow-ups that remember the last few turns until you clear.
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
              <div className="flex flex-col gap-8" aria-live="polite">
                <h2 className="sr-only">Messages</h2>
                {turns.map(run => (
                  <Turn key={run.qid} run={run} fileName={id => id}
                    selected={traceOpen && selected?.qid === run.qid}
                    onShowTrace={() => { setSelQid(run.qid); setTraceOpen(o => !(o && selected?.qid === run.qid)) }} />
                ))}
                {sending && <p className="m-0 flex items-center gap-2 text-[13px] text-muted-foreground"><Icon name="spinner" size={14} className="animate-spin" />Sending…</p>}
              </div>
            )}
          </div>
        </div>

        <form className="shrink-0 px-4 pb-4 sm:px-6" onSubmit={e => { e.preventDefault(); void send(text) }}>
          <div className="mx-auto w-full max-w-[760px]">
            <div className="flex items-end gap-2 rounded-xl border border-dashed border-edge bg-surface p-2 shadow-sm focus-within:border-primary/60 focus-within:ring-[3px] focus-within:ring-ring/15">
              <label htmlFor="sandbox-input" className="sr-only">Message (not saved)</label>
              <textarea id="sandbox-input" ref={input} rows={1} value={text} maxLength={MAX_CHARS}
                onChange={e => setText(e.target.value)} onKeyDown={onKey}
                placeholder="Ask anything, nothing is saved…"
                className="max-h-[220px] min-h-10 min-w-0 flex-1 resize-none bg-transparent px-2 py-2 text-[15px] leading-relaxed text-foreground outline-none placeholder:text-muted-foreground" />
              {running
                ? <Button variant="secondary" size="md" icon="stop" onClick={() => void stop()}>Stop</Button>
                : <IconButton type="submit" icon="send" label="Send" variant="primary" size="lg" disabled={!canSend} />}
            </div>
            <div className="mt-1.5 flex items-center justify-between gap-3 px-1 text-xs text-muted-foreground">
              <span className="hidden items-center gap-1 sm:flex"><Kbd>Enter</Kbd> send <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> new line</span>
              <span className="flex min-w-0 items-center gap-1.5">
                <Icon name="lock" size={12} className="shrink-0" />
                <span className="truncate">Not saved, engine: {store.engine?.label ?? 'Keyless'}</span>
                {text.length > 400 && <span className="tabular-nums">{text.length}/{MAX_CHARS}</span>}
              </span>
            </div>
          </div>
        </form>
      </section>

      {traceOpen && selected && (
        <aside aria-label="Trace of the selected sandbox turn"
          className={cn('flex min-h-0 flex-col border-l border-border bg-background',
            'absolute inset-y-0 right-0 z-20 w-full shadow-lg sm:w-[420px] xl:static xl:w-[380px] xl:shadow-none')}>
          <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-4">
            <Icon name="graph" size={15} className="text-muted-foreground" />
            <span className="text-sm font-semibold">Trace</span>
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
    </div>
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

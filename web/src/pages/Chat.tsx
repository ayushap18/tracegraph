import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ask, cancelRun, deleteSession, errorText, getSession, listFiles, listSessions, uploadFile } from '../api'
import type { FileInfo, SessionSummary } from '../protocol'
import { useStore } from '../store'
import { fromRecord, type Run } from '../useEventStream'
import { Badge, Button, EmptyState, IconButton, Kbd, Skeleton, Spinner, StatusBadge, buttonClass, copyText, navigate, timeAgo, useHashPath, useNow, useToast } from '../ui'
import { AgentIcon, Icon, Logo } from '../icons'
import { Chip } from '../components/Panels'
import Markdown from '../components/Markdown'
import { TraceView } from '../components/TraceView'
import { Waterfall } from '../components/Viz'
import { TopActions } from '../components/Shell'
import { pct } from '../lib'
import { AgentBadge } from '../components/app'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { cn } from '@/lib/utils'

// Chat home: sessions on the left, the conversation in the middle, the selected turn's live trace on the right.
// Every message is a run with source "chat" and a session_id, so follow-ups reach the planner with context.

const ACCEPT = '.txt,.md,.csv,.json,.pdf'
const MAX_FILE = 10 * 1024 * 1024
const TRACE_KEY = 'tg-chat-trace'

interface Attachment { key: string; name: string; size: number; status: 'uploading' | 'ready' | 'error'; info?: FileInfo; error?: string }

const answerOf = (run: Run) => {
  if (run.merged) return run.merged.answer
  if (run.mergeStream) return run.mergeStream
  const ts = run.order.map(t => run.tasks[t]).filter(Boolean)
  return ts.length === 1 ? ts[0].answered?.answer ?? ts[0].stream : ''
}
const secs = (v: number | null) => (v == null ? '' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')
const fmtSize = (n: number) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`)

// The empty chat shows one sample per kind of agent (plus a multi-part one), each with that agent's icon,
// rather than the first six server samples, which are mostly math and weather.
const SAMPLE_KINDS: Array<[string, RegExp]> = [
  ['multi', /\b(and|then)\b|;/i], ['currency', /\b(usd|eur|inr|gbp|jpy|convert)\b/i], ['weather', /\b(weather|rain|cold|hot)\b/i],
  ['knowledge', /^(who|what is|tell me)/i], ['code', /\b(python|javascript|css|git|typeerror)\b/i], ['math', /\d\s*[%*/+^-]|\bsquare root\b/i],
  ['time', /\b(time|date)\b/i],
]

function pickSamples(samples: string[]): Array<{ text: string; kind: string }> {
  const out: Array<{ text: string; kind: string }> = []
  for (const [kind, re] of SAMPLE_KINDS) {
    const hit = samples.find(s => re.test(s) && !out.some(o => o.text === s))
    if (hit) out.push({ text: hit, kind })
  }
  for (const s of samples) if (out.length < 6 && !out.some(o => o.text === s)) out.push({ text: s, kind: 'chat' })
  return out.slice(0, 6)
}

function readTraceOpen() { try { return localStorage.getItem(TRACE_KEY) === '1' } catch { return false } }

export default function Chat() {
  const { store, subscribe } = useStore()
  const toast = useToast()
  const { query } = useHashPath()
  const sessionId = query.get('s')
  const now = useNow(30000)

  // ---------- sessions ----------
  const [sessions, setSessions] = useState<SessionSummary[] | null>(null)
  const [sessErr, setSessErr] = useState<string | null>(null)
  const loadSessions = useCallback(() => {
    listSessions(30).then(r => { setSessions(r.sessions); setSessErr(null) }, e => { setSessErr(errorText(e)); setSessions(s => s ?? []) })
  }, [])
  useEffect(() => { loadSessions() }, [loadSessions])

  const [detail, setDetail] = useState<{ id: string; runs: Run[]; title: string } | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [detailErr, setDetailErr] = useState<string | null>(null)
  useEffect(() => {
    if (!sessionId) { setDetail(null); setDetailErr(null); setDetailLoading(false); return }
    if (detail?.id === sessionId) { setDetailLoading(false); return }
    let alive = true
    setDetailLoading(true)
    setDetailErr(null)
    getSession(sessionId)
      .then(r => { if (alive) { setDetail({ id: r.id, title: r.title, runs: r.runs.map(fromRecord) }); setDetailErr(null) } })
      .catch(e => { if (alive) { setDetail({ id: sessionId, title: '', runs: [] }); setDetailErr(errorText(e)) } })
      .finally(() => { if (alive) setDetailLoading(false) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId])

  // qids this tab asked, per session, so turns show even before the server echoes session_id.
  const mine = useRef(new Map<number, string>())
  const knownFiles = useRef(new Map<string, string>()) // file id -> name
  const [sending, setSending] = useState<{ text: string; files: string[] } | null>(null)

  const turns = useMemo(() => {
    if (!sessionId) return [] as Run[]
    const byQid = new Map<number, Run>()
    if (detail?.id === sessionId) for (const r of detail.runs) byQid.set(r.qid, r)
    for (const r of store.runs) {
      if (r.session_id === sessionId || mine.current.get(r.qid) === sessionId) {
        const prev = byQid.get(r.qid)
        if (r.text || !prev) byQid.set(r.qid, prev && !r.text ? prev : r)
      }
    }
    return [...byQid.values()].sort((a, b) => a.qid - b.qid)
  }, [sessionId, detail, store.runs])

  // Keep the session list fresh as runs in this browser start and finish.
  useEffect(() => {
    let t = 0
    const off = subscribe(e => {
      if ((e.type === 'query' && e.session_id) || (e.type === 'done' && mine.current.has(e.qid))) {
        clearTimeout(t); t = window.setTimeout(loadSessions, 400)
      }
    })
    return () => { off(); clearTimeout(t) }
  }, [subscribe, loadSessions])

  // ---------- trace panel ----------
  const [traceOpen, setTraceOpen] = useState(readTraceOpen)
  const [selQid, setSelQid] = useState<number | null>(null)
  const toggleTrace = () => setTraceOpen(o => { try { localStorage.setItem(TRACE_KEY, o ? '0' : '1') } catch { /* ignore */ } return !o })
  useEffect(() => { setSelQid(null) }, [sessionId])
  const selected = (selQid != null && turns.find(t => t.qid === selQid)) || turns[turns.length - 1]
  const [drawer, setDrawer] = useState(false)

  // ---------- composer ----------
  const [text, setText] = useState('')
  const [files, setFiles] = useState<Attachment[]>([])
  const [dragging, setDragging] = useState(false)
  const ta = useRef<HTMLTextAreaElement>(null)
  const fileInput = useRef<HTMLInputElement>(null)
  const last = turns[turns.length - 1]
  const running = !!last && !last.done
  const uploading = files.some(f => f.status === 'uploading')
  const filesEnabled = store.features.files || !store.ready

  useLayoutEffect(() => {
    const el = ta.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = Math.min(220, el.scrollHeight) + 'px'
  }, [text])
  useEffect(() => { ta.current?.focus() }, [sessionId])

  // "/" focuses the composer from anywhere on the page.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      if (e.key !== '/' || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable || e.metaKey || e.ctrlKey) return
      e.preventDefault(); ta.current?.focus()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const addFiles = (list: FileList | File[]) => {
    for (const f of Array.from(list)) {
      const key = `${f.name}:${f.size}:${Math.random().toString(36).slice(2, 7)}`
      const ext = f.name.slice(f.name.lastIndexOf('.')).toLowerCase()
      if (!ACCEPT.split(',').includes(ext)) { toast.error(`${f.name}: only .txt, .md, .csv, .json and .pdf files are supported`); continue }
      if (f.size > MAX_FILE) { toast.error(`${f.name} is larger than 10 MB`); continue }
      setFiles(fs => [...fs, { key, name: f.name, size: f.size, status: 'uploading' }])
      uploadFile(f).then(
        info => setFiles(fs => fs.map(x => (x.key === key ? { ...x, status: 'ready', info } : x))),
        e => {
          setFiles(fs => fs.map(x => (x.key === key ? { ...x, status: 'error', error: errorText(e) } : x)))
          toast.error(`Upload failed for ${f.name}: ${errorText(e)}`)
        },
      )
    }
  }

  const sendLock = useRef(false)
  const send = useCallback(async (raw: string, sid: string | null) => {
    if (sendLock.current || files.some(f => f.status === 'uploading')) return
    const q = raw.trim().slice(0, 500)
    if (!q) return
    sendLock.current = true
    const attached = files
    const ready = files.filter(f => f.status === 'ready' && f.info).map(f => f.info!)
    setSending({ text: q, files: ready.map(f => f.name) })
    setText('')
    setFiles([])
    try {
      const res = await ask({ query: q, source: 'chat', ...(sid ? { session_id: sid } : {}), ...(ready.length ? { files: ready.map(f => f.id) } : {}) })
      const newSid = res.session_id ?? sid
      if (newSid) mine.current.set(res.qid, newSid)
      for (const f of ready) knownFiles.current.set(f.id, f.name)
      setSelQid(null)
      if (newSid && newSid !== sid) {
        setDetail({ id: newSid, title: q, runs: [] })
        location.replace('#/?s=' + encodeURIComponent(newSid))
      }
      loadSessions()
    } catch (e) {
      toast.error(`Could not send: ${errorText(e)}`)
      setText(current => current || q)
      setFiles(current => [...attached, ...current])
    } finally {
      sendLock.current = false
      setSending(null)
    }
  }, [files, loadSessions, toast])

  // Palette and deep links: #/?new=1 starts a fresh chat, &q=… also sends it.
  const handled = useRef('')
  useEffect(() => {
    const key = query.toString()
    if (!query.get('new') || handled.current === key) return
    handled.current = key
    const q = query.get('q')
    location.replace('#/')
    if (q) void send(q, null)
    else ta.current?.focus()
  }, [query, send])

  const onComposerKey = (e: ReactKeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      if (!running && !uploading && !sending) void send(text, sessionId)
    }
  }
  const stop = async () => {
    if (!last) return
    try { await cancelRun(last.qid); toast.info(`Stopping run #${last.qid}…`) } catch (e) { toast.error(`Could not stop: ${errorText(e)}`) }
  }

  // ---------- file names for turns that attached files ----------
  const [, bump] = useState(0)
  const needNames = turns.some(t => t.files.some(id => !knownFiles.current.has(id)))
  useEffect(() => {
    if (!needNames) return
    listFiles().then(r => { for (const f of r.files) knownFiles.current.set(f.id, f.name); bump(n => n + 1) }, () => {})
  }, [needNames])

  // ---------- scrolling ----------
  const thread = useRef<HTMLDivElement>(null)
  const stick = useRef(true)
  const onScroll = () => {
    const el = thread.current
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  }
  const lastText = last ? answerOf(last).length + last.order.length : 0
  useEffect(() => {
    const el = thread.current
    if (el && stick.current) el.scrollTop = el.scrollHeight
  }, [turns.length, lastText, sending])
  useEffect(() => { stick.current = true }, [sessionId])

  // ---------- delete ----------
  const [confirmDel, setConfirmDel] = useState<string | null>(null)
  const remove = async (id: string) => {
    setConfirmDel(null)
    try {
      await deleteSession(id)
      setSessions(s => s?.filter(x => x.id !== id) ?? null)
      if (id === sessionId) navigate('/')
      toast.success('Chat deleted')
    } catch (e) { toast.error(`Could not delete: ${errorText(e)}`) }
  }

  const onDrop = (e: DragEvent) => {
    e.preventDefault(); setDragging(false)
    if (!filesEnabled) return
    if (e.dataTransfer.files.length) addFiles(e.dataTransfer.files)
  }

  const title = sessionId ? (sessions?.find(s => s.id === sessionId)?.title || detail?.title || turns[0]?.text || 'Chat') : 'New chat'
  const empty = !sessionId && !sending

  const showTrace = traceOpen && !empty

  return (
    <div className={cn('relative grid h-full min-h-0 grid-cols-1 grid-rows-[minmax(0,1fr)] overflow-hidden bg-background',
      'lg:grid-cols-[260px_minmax(0,1fr)]', showTrace && 'xl:grid-cols-[260px_minmax(0,1fr)_380px]')}>
      <TopActions>
        <IconButton icon="history" label="Show chats" className="lg:hidden" onClick={() => setDrawer(d => !d)} active={drawer} />
        <Button variant="secondary" size="sm" icon="new" onClick={() => navigate('/?new=1')}>New chat</Button>
        {!empty && (<IconButton icon="graph" label={traceOpen ? 'Hide trace panel' : 'Show trace panel'} active={traceOpen} onClick={toggleTrace} />)}
      </TopActions>

      {/* sessions: a column from lg, a left drawer below it */}
      <aside aria-label="Chats" className={cn('flex min-h-0 min-w-0 flex-col border-r border-border bg-background',
        'max-lg:absolute max-lg:inset-y-0 max-lg:left-0 max-lg:z-30 max-lg:w-[min(300px,86vw)] max-lg:shadow-lg', !drawer && 'max-lg:hidden')}>
        <div className="flex shrink-0 items-center justify-between gap-2 px-4 pb-2 pt-4">
          <h2 className="text-sm font-semibold">Chats</h2>
          <Button size="sm" variant="secondary" icon="plus" onClick={() => { setDrawer(false); navigate('/?new=1') }}>New</Button>
        </div>
        {sessErr && sessions != null && sessions.length > 0 && (
          <div role="status" className="mx-2 mb-2 flex items-start gap-2 rounded-md border border-warn/25 bg-warn/10 px-2.5 py-2 text-xs text-foreground">
            <Icon name="alert" size={13} className="mt-0.5 shrink-0 text-warn" />
            <span className="min-w-0 flex-1 [overflow-wrap:anywhere]">Could not refresh chats: {sessErr}</span>
            <button type="button" data-slot="button" className="shrink-0 rounded-sm font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35" onClick={loadSessions}>Retry</button>
          </div>
        )}
        <div className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-2 pb-3">
          {sessions == null ? Array.from({ length: 5 }, (_, i) => <div key={i} className="flex flex-col gap-1.5 px-2.5 py-2.5"><Skeleton height={13} width="80%" /><Skeleton height={10} width="45%" /></div>)
            : sessions.length === 0 ? <EmptyState compact icon="chat" title="No chats yet" text={sessErr ? `Could not load chats: ${sessErr}` : 'Your conversations will show up here.'} />
            : sessions.map(s => {
              const on = s.id === sessionId
              return (
                <div key={s.id} className={cn('group relative flex items-center rounded-md transition-colors', on ? 'bg-subtle' : 'hover:bg-subtle/60 focus-within:bg-subtle/60')}>
                  {confirmDel === s.id ? (
                    <div className="flex w-full flex-wrap items-center gap-1.5 px-2.5 py-2 text-[13px]" role="group" aria-label={`Delete ${s.title}?`}>
                      <span className="basis-full font-medium">Delete this chat?</span>
                      <Button size="sm" variant="danger" onClick={() => void remove(s.id)} autoFocus>Delete</Button>
                      <Button size="sm" variant="ghost" onClick={() => setConfirmDel(null)}>Cancel</Button>
                    </div>
                  ) : (
                    <>
                      <a href={'#/?s=' + encodeURIComponent(s.id)} aria-current={on ? 'page' : undefined} onClick={() => setDrawer(false)}
                        className="flex min-w-0 flex-1 flex-col gap-0.5 rounded-md px-2.5 py-2 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
                        <span className="truncate text-[13.5px] font-medium text-foreground">{s.title || 'Untitled chat'}</span>
                        <span className="truncate text-xs tabular-nums text-muted-foreground">{s.turns} turn{s.turns === 1 ? '' : 's'} · {timeAgo(s.updated, now)}</span>
                      </a>
                      <IconButton icon="trash" label={`Delete chat: ${s.title}`} size="sm" onClick={() => setConfirmDel(s.id)}
                        className="mr-1 shrink-0 hover:text-destructive sm:opacity-0 sm:focus-visible:opacity-100 sm:group-hover:opacity-100 sm:group-focus-within:opacity-100" />
                    </>
                  )}
                </div>
              )
            })}
        </div>
      </aside>
      {drawer && <div className="absolute inset-0 z-[25] bg-scrim lg:hidden" onClick={() => setDrawer(false)} aria-hidden="true" />}

      {/* conversation */}
      <section className="relative flex min-h-0 min-w-0 flex-col" aria-label="Conversation"
        onDragOver={e => { if (filesEnabled && e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true) } }}
        onDragLeave={e => { if (e.currentTarget === e.target) setDragging(false) }} onDrop={onDrop}>
        <div className="min-h-0 flex-1 overflow-y-auto overflow-x-hidden scroll-smooth motion-reduce:scroll-auto" ref={thread} onScroll={onScroll}>
          {empty ? (
            <div className="mx-auto flex min-h-full w-full max-w-[640px] flex-col items-center justify-center px-4 py-10 text-center">
              <div className="grid size-12 place-items-center rounded-xl border border-border bg-surface"><Logo size={28} /></div>
              <h2 className="mt-5 text-2xl font-semibold tracking-tight text-balance">What can I help with?</h2>
              <p className="mt-2 max-w-[56ch] text-sm leading-relaxed text-muted-foreground text-pretty">Ask one thing or several at once. TraceGraph plans the steps, routes each to the right agent and merges the answers. Follow-ups keep the context.</p>
              <div className="mt-8 grid w-full grid-cols-1 gap-2 sm:grid-cols-2">
                {pickSamples(store.samples.length ? store.samples : ['What time is it in Tokyo?']).map(({ text, kind }) => (
                  <button type="button" data-slot="button" key={text} onClick={() => void send(text, null)}
                    className="flex items-start gap-2.5 rounded-lg border border-border bg-surface p-3 text-left text-[13px] leading-snug text-foreground transition-colors hover:border-edge hover:bg-subtle/40 focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
                    <span className="grid size-6 shrink-0 place-items-center rounded-md bg-subtle">
                      {kind === 'multi' ? <Icon name="planner" size={14} className="text-primary" /> : <AgentIcon agent={kind} size={14} tinted />}
                    </span>
                    <span className="min-w-0 pt-0.5 [overflow-wrap:anywhere]">{text}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="mx-auto flex w-full max-w-[760px] flex-col gap-8 px-4 py-6 sm:px-6 sm:py-8">
              <h2 className="sr-only">{title}</h2>
              {detailLoading && !turns.length && <div className="flex flex-col gap-4"><Skeleton height={36} width="45%" className="ml-auto" /><Skeleton lines={3} /></div>}
              {detailErr && !turns.length && !sending && <EmptyState icon="alert" title="Could not load this chat" text={detailErr} action={<Button variant="secondary" onClick={() => navigate('/?new=1')}>Start a new chat</Button>} />}
              {!detailLoading && !detailErr && sessionId && !turns.length && !sending && <EmptyState icon="chat" title="This chat is empty" text="Ask something below to start." />}
              {turns.map(run => (
                <Turn key={run.qid} run={run} selected={traceOpen && selected?.qid === run.qid} fileName={id => knownFiles.current.get(id) ?? 'file'}
                  onShowTrace={() => { setSelQid(run.qid); if (!traceOpen) toggleTrace() }} />
              ))}
              {sending && (
                <div className="flex flex-col gap-4">
                  <UserMessage text={sending.text} files={sending.files} />
                  <div className="border-l-2 border-transparent pl-4">
                    <BotHead />
                    <div className="mt-3 flex items-center gap-2.5 text-sm text-muted-foreground"><Spinner size={14} /> Sending…</div>
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {dragging && <div className="pointer-events-none absolute inset-2 z-10 rounded-lg border-2 border-dashed border-primary bg-primary/5" aria-hidden="true" />}

        <div className="shrink-0 px-3 pb-3 pt-2 sm:px-6 sm:pb-4">
          <div className="relative mx-auto w-full max-w-[760px]">
            {dragging && <div className="pointer-events-none absolute -top-11 left-1/2 z-20 flex -translate-x-1/2 items-center gap-1.5 whitespace-nowrap rounded-md bg-primary px-3 py-1.5 text-[13px] font-medium text-primary-foreground shadow-md"><Icon name="upload" size={16} /> Drop files to attach</div>}
            <form onSubmit={e => { e.preventDefault(); if (!running && !uploading && !sending) void send(text, sessionId) }}
              className={cn('rounded-xl border bg-surface p-2 shadow-sm transition-colors focus-within:border-edge',
                dragging ? 'border-dashed border-primary focus-within:border-primary' : 'border-border')}>
              {files.length > 0 && (
                <ul className="mb-2 flex flex-wrap gap-1.5 px-0.5 pt-0.5" aria-label="Attached files">
                  {files.map(f => (
                    <li key={f.key} title={f.error ?? f.name}
                      className={cn('inline-flex h-7 min-w-0 max-w-full items-center gap-1.5 rounded-md border bg-subtle/60 pl-2 pr-0.5 text-[13px]',
                        f.status === 'error' ? 'border-destructive/30' : 'border-border')}>
                      {f.status === 'uploading' ? <Spinner size={13} className="shrink-0 text-muted-foreground" />
                        : <Icon name={f.status === 'error' ? 'alert' : f.name.endsWith('.csv') ? 'file-csv' : f.name.endsWith('.json') ? 'file-json' : 'file-text'} size={14}
                          className={cn('shrink-0', f.status === 'error' ? 'text-destructive' : 'text-primary')} />}
                      <span className="min-w-0 max-w-[200px] truncate font-medium">{f.name}</span>
                      <span className={cn('whitespace-nowrap text-xs tabular-nums', f.status === 'error' ? 'text-destructive' : 'text-muted-foreground')}>
                        {f.status === 'error' ? 'failed' : f.status === 'uploading' ? 'uploading' : f.info?.rows != null ? `${f.info.rows} rows` : fmtSize(f.size)}
                      </span>
                      <button type="button" data-slot="button" aria-label={`Remove ${f.name}`} onClick={() => setFiles(fs => fs.filter(x => x.key !== f.key))}
                        className="grid size-6 shrink-0 place-items-center rounded-sm text-muted-foreground transition-colors hover:bg-subtle hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
                        <Icon name="close" size={12} />
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              <div className="flex items-end gap-1.5">
                <input ref={fileInput} type="file" accept={ACCEPT} multiple hidden onChange={e => { if (e.target.files) addFiles(e.target.files); e.target.value = '' }} />
                <IconButton icon="paperclip" label={filesEnabled ? 'Attach files (.txt .md .csv .json .pdf, up to 10 MB)' : 'File attachments are not enabled on this server'}
                  disabled={!filesEnabled} onClick={() => fileInput.current?.click()} className="mb-0.5 shrink-0" />
                <textarea ref={ta} value={text} rows={1} maxLength={500} onChange={e => setText(e.target.value)} onKeyDown={onComposerKey}
                  placeholder={sessionId && turns.length ? 'Ask a follow-up…' : 'Ask anything, or several things at once…'} aria-label="Message"
                  className="max-h-[220px] min-h-9 min-w-0 flex-1 resize-none bg-transparent px-1 py-2 font-sans text-[15px] leading-normal text-foreground outline-none placeholder:text-muted-foreground focus-visible:outline-none" />
                {running ? (
                  <Button variant="secondary" icon="stop" onClick={() => void stop()} className="mb-0.5 shrink-0" aria-label="Stop the running answer">Stop</Button>
                ) : (
                  <IconButton type="submit" variant="primary" icon="send" label="Send" disabled={!text.trim() || uploading || !!sending} className="mb-0.5 shrink-0" />
                )}
              </div>
            </form>
            <p className="mt-2 flex flex-wrap items-center justify-between gap-x-3 gap-y-1 px-1 text-xs text-muted-foreground">
              <span className="inline-flex items-center gap-1 max-sm:hidden"><Kbd>Enter</Kbd> send, <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> new line</span>
              <span className="min-w-0 truncate">
                {store.engine ? `Engine: ${store.engine.label}` : 'Keyless mode'}
                {text.length > 400 && <span className="tabular-nums max-sm:hidden"> · {text.length}/500</span>}
              </span>
            </p>
          </div>
        </div>
      </section>

      {/* trace: a column from xl, an overlay over the thread below it */}
      {showTrace && (
        <aside aria-label="Trace of the selected turn" className={cn('flex min-h-0 min-w-0 flex-col border-l border-border bg-surface',
          'max-xl:absolute max-xl:inset-y-0 max-xl:right-0 max-xl:z-20 max-xl:shadow-lg max-sm:w-full sm:max-xl:w-[420px]')}>
          <div className="flex h-12 shrink-0 items-center justify-between gap-2 border-b border-border pl-4 pr-2">
            <span className="flex min-w-0 items-center gap-2 text-sm font-semibold">
              <Icon name="graph" size={15} className="shrink-0 text-muted-foreground" />Trace
              {selected && <span className="truncate font-mono text-xs font-normal tabular-nums text-muted-foreground">Run #{selected.qid}</span>}
            </span>
            <span className="flex shrink-0 items-center gap-0.5">
              {selected && <a className={buttonClass('ghost', 'sm')} href={`#/runs/${selected.qid}`}><Icon name="external" size={14} />Open</a>}
              <IconButton icon="close" label="Hide trace panel" size="sm" onClick={toggleTrace} />
            </span>
          </div>
          {selected ? (
            <div className="min-h-0 flex-1 overflow-y-auto">
              <TraceView run={selected} store={store} compact legend={false} />
              <div className="border-t border-border px-4 py-4">
                <h3 className="mb-3 flex items-center gap-1.5 text-[13px] font-semibold"><Icon name="timeline" size={14} className="text-muted-foreground" />Timeline</h3>
                <Waterfall run={selected} />
              </div>
            </div>
          ) : (
            <EmptyState compact icon="graph" title="No trace yet" text="Send a message to watch it get planned, routed and answered live." />
          )}
        </aside>
      )}
    </div>
  )
}

function BotHead({ run }: { run?: Run }) {
  return (
    <div className="flex min-w-0 items-center gap-2">
      <Logo size={18} />
      <span className="text-[13px] font-semibold">TraceGraph</span>
      {run?.engine && <Badge tone="neutral" icon="engine" className="min-w-0">{run.engine}</Badge>}
    </div>
  )
}

function UserMessage({ text, files }: { text: string; files: string[] }) {
  return (
    <div className="flex flex-col items-end gap-1.5">
      <div className="max-w-[85%] rounded-2xl rounded-br-md bg-subtle px-4 py-2.5 text-[15px] leading-relaxed text-foreground">
        <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{text}</p>
      </div>
      {files.length > 0 && (
        <div className="flex max-w-[85%] flex-wrap justify-end gap-1.5">
          {files.map((n, i) => (
            <span key={i} className="inline-flex h-6 min-w-0 max-w-full items-center gap-1 rounded-md border border-border bg-surface px-2 text-xs text-muted-foreground">
              <Icon name="paperclip" size={12} className="shrink-0" /><span className="truncate">{n}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

const STEP_TONE: Record<string, string> = { done: 'text-ok', running: 'text-primary', warn: 'text-warn', error: 'text-warn', wait: 'text-muted-foreground' }
const STREAM_CURSOR = "[&>.md>:last-child]:after:ml-px [&>.md>:last-child]:after:text-primary [&>.md>:last-child]:after:content-['▍'] [&>.md>:last-child]:after:animate-[blink_1s_steps(2)_infinite]"

function Turn({ run, selected, onShowTrace, fileName }: { run: Run; selected: boolean; onShowTrace: () => void; fileName: (id: string) => string }) {
  const toast = useToast()
  const tasks = run.order.map(t => run.tasks[t]).filter(Boolean)
  const answer = answerOf(run)
  const streaming = !run.done && !run.merged
  const multi = tasks.length > 1
  const finalFail = run.done && run.status !== 'done' && run.status !== 'running'
  const phase = !run.plan ? 'Planning…' : tasks.some(t => !t.routed && !t.error) ? `Routing ${tasks.length} step${tasks.length === 1 ? '' : 's'}…`
    : tasks.some(t => !t.answered && !t.error) ? 'Agents working…' : multi ? 'Merging answers…' : 'Finishing…'
  const copy = async () => { (await copyText(answer)) ? toast.success('Answer copied') : toast.error('Could not copy') }
  // Steps start open while the run works and fold once it is done, like the old <details open={!run.done}>.
  const [stepsOpen, setStepsOpen] = useState(!run.done)
  useEffect(() => { setStepsOpen(!run.done) }, [run.done])
  const quiet = run.status === 'cancelled' || run.status === 'timeout'

  return (
    <div className="flex flex-col gap-4">
      <UserMessage text={run.text || '…'} files={run.files.map(fileName)} />
      <div className={cn('min-w-0 border-l-2 pl-4 transition-colors', selected ? 'border-primary' : 'border-transparent')}>
        <BotHead run={run} />
        {multi && (
          <Collapsible open={stepsOpen} onOpenChange={setStepsOpen} className="mt-3">
            <CollapsibleTrigger data-slot="button"
              className="group/steps -mx-1 inline-flex items-center gap-1.5 rounded-md px-1 py-0.5 text-[13px] text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
              <Icon name="subtasks" size={14} />{tasks.length} steps{run.plan ? `, ${run.plan.planner} plan` : ''}
              <Icon name="chevron-down" size={14} className="group-data-[state=open]/steps:rotate-180" />
            </CollapsibleTrigger>
            <CollapsibleContent>
              <ol className="mt-2 flex flex-col gap-1.5 border-l border-border pl-3">
                {tasks.map(t => {
                  const st = t.error ? 'error' : t.answered ? (t.answered.ok ? 'done' : 'warn') : t.routed ? 'running' : 'wait'
                  return (
                    <li key={t.tid} className="flex min-h-6 items-center gap-2 text-[13px] max-sm:flex-wrap">
                      <span className={cn('inline-flex w-4 shrink-0 justify-center', STEP_TONE[st])} aria-hidden="true">{st === 'running' ? <Spinner size={12} /> : st === 'done' ? <Icon name="check" size={13} /> : st === 'wait' ? <Icon name="clock" size={12} /> : <Icon name="alert" size={13} />}</span>
                      <span className={cn('min-w-0 flex-1 [overflow-wrap:anywhere]', st === 'wait' ? 'text-muted-foreground' : 'text-foreground')}>
                        {t.text}{t.depends_on.length > 0 && <span className="text-xs text-muted-foreground"> (after {t.depends_on.join(', ')})</span>}
                      </span>
                      {t.routed && <span className="max-sm:ml-6"><Chip agent={t.routed.agent} /></span>}
                    </li>
                  )
                })}
              </ol>
            </CollapsibleContent>
          </Collapsible>
        )}
        {answer ? (
          <div className={cn('mt-3 text-[15px] leading-relaxed text-foreground [overflow-wrap:anywhere]', streaming && STREAM_CURSOR)}><Markdown text={answer} /></div>
        ) : !run.done ? (
          <div className="mt-3 flex items-start gap-2.5 py-1 text-sm text-foreground" role="status">
            <Spinner size={14} className="mt-0.5 shrink-0 text-muted-foreground" />
            <span>{phase}<small className="mt-1 block text-xs text-muted-foreground">{!run.plan ? 'Breaking your request into clear steps.' : 'Your answer will appear here as it is ready.'}</small></span>
          </div>
        ) : !finalFail ? <p className="mt-3 text-sm text-muted-foreground">No answer.</p> : null}
        {run.error && <p className={cn('mt-3 flex items-start gap-2 rounded-md border p-3 text-sm text-foreground', quiet ? 'border-warn/25 bg-warn/10' : 'border-destructive/25 bg-destructive/10')}>
          <Icon name="alert" size={15} className={cn('mt-0.5 shrink-0', quiet ? 'text-warn' : 'text-destructive')} /><span className="min-w-0 [overflow-wrap:anywhere]">{run.error}</span></p>}
        {finalFail && !run.error && <p className={cn('mt-3 flex items-start gap-2 rounded-md border p-3 text-sm text-foreground', quiet ? 'border-warn/25 bg-warn/10' : 'border-destructive/25 bg-destructive/10')}>
          <Icon name={run.status === 'cancelled' ? 'cancelled' : 'timeout'} size={15} className={cn('mt-0.5 shrink-0', quiet ? 'text-warn' : 'text-destructive')} />
          {run.status === 'cancelled' ? 'Stopped before it finished.' : run.status === 'timeout' ? 'Timed out.' : 'Something went wrong.'}</p>}
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="flex min-w-0 flex-wrap gap-1.5">
            {tasks.filter(t => t.routed).map(t => (
              <AgentBadge key={t.tid} agent={t.routed!.agent} pct={pct(t.routed!.confidence)} title={`${t.routed!.agent}: ${pct(t.routed!.confidence)} confidence`} />
            ))}
          </span>
          <span className="ml-auto flex flex-wrap items-center gap-1">
            {run.done && run.status !== 'done' && <StatusBadge status={run.status} />}
            {run.total_ms != null && <span className="mx-1 inline-flex items-center gap-1 text-xs tabular-nums text-muted-foreground" title="Total time"><Icon name="latency" size={12} />{secs(run.total_ms)}</span>}
            {answer && run.done && <IconButton icon="copy" label="Copy answer" size="sm" onClick={() => void copy()} />}
            <Button variant="ghost" size="sm" icon="graph" iconRight="arrow-right" onClick={onShowTrace} aria-pressed={selected}
              className="aria-pressed:bg-primary/10 aria-pressed:text-primary">{selected ? 'Trace open' : 'View trace'}</Button>
          </span>
        </div>
      </div>
    </div>
  )
}

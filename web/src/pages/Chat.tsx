import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent as ReactKeyboardEvent } from 'react'
import { ask, cancelRun, deleteSession, errorText, getSession, listFiles, listSessions, uploadFile } from '../api'
import type { FileInfo, SessionSummary } from '../protocol'
import { useStore } from '../store'
import { fromRecord, type Run } from '../useEventStream'
import { Badge, Button, EmptyState, IconButton, Skeleton, Spinner, StatusBadge, copyText, navigate, timeAgo, useHashPath, useNow, useToast } from '../ui'
import { AgentIcon, Icon, Logo } from '../icons'
import { Chip } from '../components/Panels'
import Markdown from '../components/Markdown'
import { TraceView } from '../components/TraceView'
import { Waterfall } from '../components/Viz'
import { TopActions } from '../components/Shell'
import { pct } from '../lib'

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

function readTraceOpen() { try { return localStorage.getItem(TRACE_KEY) !== '0' } catch { return true } }

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
    if (!sessionId) { setDetail(null); setDetailErr(null); return }
    if (detail?.id === sessionId) return
    let alive = true
    setDetailLoading(true)
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

  const send = useCallback(async (raw: string, sid: string | null) => {
    const q = raw.trim().slice(0, 500)
    if (!q) return
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
      setText(q)
    } finally {
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

  return (
    <div className={'chat' + (traceOpen && !empty ? ' with-trace' : '') + (drawer ? ' drawer-open' : '')}>
      <TopActions>
        <IconButton icon="history" label="Show chats" className="chat-drawer-btn" onClick={() => setDrawer(d => !d)} active={drawer} />
        <Button variant="secondary" size="sm" icon="new" onClick={() => navigate('/?new=1')}>New chat</Button>
{!empty && (<IconButton icon="graph" label={traceOpen ? 'Hide trace panel' : 'Show trace panel'} active={traceOpen} onClick={toggleTrace} className="chat-trace-btn" />)}
      </TopActions>

      {/* sessions */}
      <aside className="chat-sessions" aria-label="Chats">
        <div className="cs-head">
          <span className="cs-title">Chats</span>
          <Button size="sm" variant="primary" icon="plus" onClick={() => { setDrawer(false); navigate('/?new=1') }}>New</Button>
        </div>
        <div className="cs-list">
          {sessions == null ? Array.from({ length: 5 }, (_, i) => <div key={i} className="cs-skel"><Skeleton height={13} width="80%" /><Skeleton height={10} width="45%" /></div>)
            : sessions.length === 0 ? <EmptyState compact icon="chat" title="No chats yet" text={sessErr ? `Could not load chats: ${sessErr}` : 'Your conversations will show up here.'} />
            : sessions.map(s => (
              <div key={s.id} className={'cs-item' + (s.id === sessionId ? ' on' : '')}>
                {confirmDel === s.id ? (
                  <div className="cs-confirm" role="group" aria-label={`Delete ${s.title}?`}>
                    <span>Delete this chat?</span>
                    <Button size="sm" variant="danger" onClick={() => void remove(s.id)} autoFocus>Delete</Button>
                    <Button size="sm" variant="ghost" onClick={() => setConfirmDel(null)}>Cancel</Button>
                  </div>
                ) : (
                  <>
                    <a href={'#/?s=' + encodeURIComponent(s.id)} className="cs-link" aria-current={s.id === sessionId ? 'page' : undefined} onClick={() => setDrawer(false)}>
                      <span className="cs-name">{s.title || 'Untitled chat'}</span>
                      <span className="cs-meta">{s.turns} turn{s.turns === 1 ? '' : 's'} · {timeAgo(s.updated, now)}</span>
                    </a>
                    <IconButton icon="trash" label={`Delete chat: ${s.title}`} size="sm" className="cs-del" onClick={() => setConfirmDel(s.id)} />
                  </>
                )}
              </div>
            ))}
        </div>
      </aside>
      {drawer && <div className="chat-scrim" onClick={() => setDrawer(false)} aria-hidden="true" />}

      {/* conversation */}
      <section className={'chat-main' + (dragging ? ' dragging' : '')} aria-label="Conversation"
        onDragOver={e => { if (filesEnabled && e.dataTransfer.types.includes('Files')) { e.preventDefault(); setDragging(true) } }}
        onDragLeave={e => { if (e.currentTarget === e.target) setDragging(false) }} onDrop={onDrop}>
        <div className="chat-thread" ref={thread} onScroll={onScroll}>
          {empty ? (
            <div className="chat-hero">
              <div className="hero-logo"><Logo size={44} /></div>
              <h2>What can I help with?</h2>
              <p className="muted">Ask one thing or several at once. TraceGraph plans the steps, routes each to the right agent and merges the answers. Follow-ups keep the context.</p>
              <div className="sample-grid">
                {pickSamples(store.samples.length ? store.samples : ['What time is it in Tokyo?']).map(({ text, kind }) => (
                  <button type="button" key={text} className="sample-card" onClick={() => void send(text, null)}>
                    {kind === 'multi' ? <Icon name="planner" size={16} /> : <AgentIcon agent={kind} size={16} />}
                    <span>{text}</span>
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <div className="turns">
              <h2 className="sr-only">{title}</h2>
              {detailLoading && !turns.length && <div className="turn-skel"><Skeleton height={36} width="45%" style={{ marginLeft: 'auto' }} /><Skeleton lines={3} /></div>}
              {detailErr && !turns.length && !sending && <EmptyState icon="alert" title="Could not load this chat" text={detailErr} action={<Button variant="secondary" onClick={() => navigate('/?new=1')}>Start a new chat</Button>} />}
              {!detailLoading && !detailErr && sessionId && !turns.length && !sending && <EmptyState icon="chat" title="This chat is empty" text="Ask something below to start." />}
              {turns.map(run => (
                <Turn key={run.qid} run={run} selected={traceOpen && selected?.qid === run.qid} fileName={id => knownFiles.current.get(id) ?? 'file'}
                  onShowTrace={() => { setSelQid(run.qid); if (!traceOpen) toggleTrace() }} />
              ))}
              {sending && (
                <div className="turn">
                  <div className="bubble-user"><p>{sending.text}</p>{sending.files.length > 0 && <FileRow names={sending.files} />}</div>
                  <div className="bubble-bot pending"><BotHead /><div className="thinking"><Spinner size={14} /> Sending…</div></div>
                </div>
              )}
            </div>
          )}
        </div>

        <div className="composer-wrap">
          {dragging && <div className="drop-hint"><Icon name="upload" size={18} /> Drop files to attach</div>}
          <form className="composer" onSubmit={e => { e.preventDefault(); if (!running) void send(text, sessionId) }}>
            {files.length > 0 && (
              <ul className="file-chips" aria-label="Attached files">
                {files.map(f => (
                  <li key={f.key} className={'file-chip ' + f.status} title={f.error ?? f.name}>
                    {f.status === 'uploading' ? <Spinner size={13} /> : <Icon name={f.status === 'error' ? 'alert' : f.name.endsWith('.csv') ? 'file-csv' : f.name.endsWith('.json') ? 'file-json' : 'file-text'} size={14} />}
                    <span className="fc-name">{f.name}</span>
                    <span className="fc-meta">{f.status === 'error' ? 'failed' : f.info?.rows != null ? `${f.info.rows} rows` : fmtSize(f.size)}</span>
                    <button type="button" className="fc-x" aria-label={`Remove ${f.name}`} onClick={() => setFiles(fs => fs.filter(x => x.key !== f.key))}><Icon name="close" size={12} /></button>
                  </li>
                ))}
              </ul>
            )}
            <div className="composer-row">
              <input ref={fileInput} type="file" accept={ACCEPT} multiple hidden onChange={e => { if (e.target.files) addFiles(e.target.files); e.target.value = '' }} />
              <IconButton icon="paperclip" label={filesEnabled ? 'Attach files (.txt .md .csv .json .pdf, up to 10 MB)' : 'File attachments are not enabled on this server'}
                disabled={!filesEnabled} onClick={() => fileInput.current?.click()} className="composer-attach" />
              <textarea ref={ta} value={text} rows={1} maxLength={500} onChange={e => setText(e.target.value)} onKeyDown={onComposerKey}
                placeholder={sessionId && turns.length ? 'Ask a follow-up…' : 'Ask anything, or several things at once…'} aria-label="Message" />
              {running ? (
                <Button variant="danger" icon="stop" onClick={() => void stop()} className="composer-send" aria-label="Stop the running answer">Stop</Button>
              ) : (
                <Button type="submit" icon="send" disabled={!text.trim() || uploading || !!sending} className="composer-send" aria-label="Send">
                  <span className="hide-xs">Send</span>
                </Button>
              )}
            </div>
          </form>
          <p className="composer-hint">
            <span><kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> new line</span>
            <span className="muted">{store.engine ? `Engine: ${store.engine.label}` : 'Keyless mode'}{text.length > 400 ? ` · ${text.length}/500` : ''}</span>
          </p>
        </div>
      </section>

      {/* trace */}
      {traceOpen && !empty && (
        <aside className="chat-trace" aria-label="Trace of the selected turn">
          <div className="ct-head">
            <span className="ct-title"><Icon name="graph" size={15} />Trace{selected && <span className="muted num"> · Run #{selected.qid}</span>}</span>
            <span className="ct-actions">
              {selected && <a className="btn btn-ghost btn-sm" href={`#/runs/${selected.qid}`}><Icon name="external" size={14} /><span className="btn-label">Open</span></a>}
              <IconButton icon="close" label="Hide trace panel" size="sm" onClick={toggleTrace} />
            </span>
          </div>
          {selected ? (
            <div className="ct-body">
              <TraceView run={selected} store={store} compact legend={false} />
              <div className="ct-section">
                <h3 className="ct-sub"><Icon name="timeline" size={13} />Timeline</h3>
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
    <div className="bot-head">
      <span className="bot-avatar"><Logo size={20} /></span>
      <span className="bot-name">TraceGraph</span>
      {run?.engine && <Badge tone="neutral" icon="engine">{run.engine}</Badge>}
    </div>
  )
}

function FileRow({ names }: { names: string[] }) {
  return <div className="bubble-files">{names.map((n, i) => <span key={i} className="bubble-file"><Icon name="paperclip" size={12} />{n}</span>)}</div>
}

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

  return (
    <div className={'turn' + (selected ? ' selected' : '')}>
      <div className="bubble-user">
        <p>{run.text || '…'}</p>
        {run.files.length > 0 && <FileRow names={run.files.map(fileName)} />}
      </div>
      <div className={'bubble-bot' + (finalFail ? ' failed' : '')}>
        <BotHead run={run} />
        {multi && (
          <details className="steps" open={!run.done}>
            <summary><Icon name="subtasks" size={13} />{tasks.length} steps{run.plan ? ` · ${run.plan.planner} plan` : ''}</summary>
            <ol>
              {tasks.map(t => {
                const st = t.error ? 'error' : t.answered ? (t.answered.ok ? 'done' : 'warn') : t.routed ? 'running' : 'wait'
                return (
                  <li key={t.tid} className={'step st-' + st}>
                    <span className="step-ico" aria-hidden="true">{st === 'running' ? <Spinner size={12} /> : st === 'done' ? <Icon name="check" size={13} /> : st === 'wait' ? <Icon name="clock" size={12} /> : <Icon name="alert" size={13} />}</span>
                    <span className="step-text">{t.text}{t.depends_on.length > 0 && <span className="muted small"> · after {t.depends_on.join(', ')}</span>}</span>
                    {t.routed && <Chip agent={t.routed.agent} />}
                  </li>
                )
              })}
            </ol>
          </details>
        )}
        {answer ? (
          <div className={'bot-answer' + (streaming ? ' streaming' : '')}><Markdown text={answer} /></div>
        ) : !run.done ? (
          <div className="thinking"><Spinner size={14} /><span>{phase}</span></div>
        ) : !finalFail ? <p className="muted">No answer.</p> : null}
        {run.error && <p className="bot-error"><Icon name="alert" size={14} />{run.error}</p>}
        {finalFail && !run.error && <p className="bot-error"><Icon name={run.status === 'cancelled' ? 'cancelled' : 'timeout'} size={14} />
          {run.status === 'cancelled' ? 'Stopped before it finished.' : run.status === 'timeout' ? 'Timed out.' : 'Something went wrong.'}</p>}
        <div className="bot-foot">
          <span className="bot-agents">
            {tasks.filter(t => t.routed).map(t => (
              <span key={t.tid} className="agent-conf" title={`${t.routed!.agent}: ${pct(t.routed!.confidence)} confidence`}>
                <AgentIcon agent={t.routed!.agent} size={12} tinted />{t.routed!.agent}<span className="num muted">{pct(t.routed!.confidence)}</span>
              </span>
            ))}
          </span>
          <span className="bot-meta">
            {run.done && run.status !== 'done' && <StatusBadge status={run.status} />}
            {run.total_ms != null && <span className="num muted" title="Total time"><Icon name="latency" size={12} /> {secs(run.total_ms)}</span>}
            {answer && run.done && <IconButton icon="copy" label="Copy answer" size="sm" onClick={() => void copy()} />}
            <IconButton icon="graph" label="Show trace in panel" size="sm" onClick={onShowTrace} active={selected} />
            <a className="trace-link" href={`#/runs/${run.qid}`}>View trace <Icon name="arrow-right" size={12} /></a>
          </span>
        </div>
      </div>
    </div>
  )
}

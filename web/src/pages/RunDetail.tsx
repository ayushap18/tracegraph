import { useEffect, useState } from 'react'
import { ask, cancelRun, errorText, getRun } from '../api'
import type { RunRecord } from '../protocol'
import { useRunState, useStore } from '../store'
import type { Run } from '../useEventStream'
import { Badge, Button, Card, EmptyState, Skeleton, StatusBadge, Tabs, copyText, navigate, timeAgo, useHashPath, useToast } from '../ui'
import { EngineIcon, Icon } from '../icons'
import { TaskCard } from '../components/Panels'
import Markdown from '../components/Markdown'
import { TraceView } from '../components/TraceView'
import { Waterfall } from '../components/Viz'
import { TopActions } from '../components/Shell'
import { pct } from '../lib'

// One run in full: header with actions, then Answer · Trace · Timeline · Subtasks · Raw.

const TABS = [
  { id: 'answer', label: 'Answer', icon: 'answer' as const },
  { id: 'trace', label: 'Trace', icon: 'graph' as const },
  { id: 'timeline', label: 'Timeline', icon: 'timeline' as const },
  { id: 'subtasks', label: 'Subtasks', icon: 'subtasks' as const },
  { id: 'raw', label: 'Raw', icon: 'code' as const },
]

const secs = (v: number | null) => (v == null ? '–' : v >= 1000 ? (v / 1000).toFixed(2) + ' s' : Math.round(v) + ' ms')

export default function RunDetail({ params }: { params: Record<string, string> }) {
  const qid = Number(params.qid)
  const { run, loading, error, notFound } = useRunState(Number.isFinite(qid) ? qid : null)
  const { store } = useStore()
  const toast = useToast()
  const { query } = useHashPath()
  const tab = TABS.some(t => t.id === query.get('tab')) ? query.get('tab')! : 'answer'
  const setTab = (id: string) => location.replace(`#/runs/${qid}${id === 'answer' ? '' : '?tab=' + id}`)
  const [busy, setBusy] = useState<'stop' | 'replay' | null>(null)

  if (!Number.isFinite(qid)) return <div className="wrap"><EmptyState icon="alert" title="Not a run id" text={`"${params.qid}" is not a number.`} action={<Button onClick={() => navigate('/runs')}>All runs</Button>} /></div>
  if (loading) return <DetailSkeleton />
  if (!run) {
    return (
      <div className="wrap">
        <Card><EmptyState icon={notFound ? 'runs' : 'alert'} title={notFound ? `Run #${qid} not found` : 'Could not load this run'}
          text={notFound ? 'It may have been removed, or the id is wrong.' : error ?? ''} action={<Button variant="secondary" icon="runs" onClick={() => navigate('/runs')}>All runs</Button>} /></Card>
      </div>
    )
  }

  const status = run.done ? run.status : 'running'
  const share = async () => { (await copyText(location.href)) ? toast.success('Link copied') : toast.error('Could not copy the link') }
  const stop = async () => {
    setBusy('stop')
    try { await cancelRun(run.qid); toast.info(`Stopping run #${run.qid}…`) } catch (e) { toast.error(`Could not stop: ${errorText(e)}`) } finally { setBusy(null) }
  }
  const replay = async () => {
    setBusy('replay')
    try {
      const res = await ask({ query: run.text, source: 'you', engine: run.engine ?? 'none', ...(run.files.length ? { files: run.files } : {}) })
      toast.success(`Replaying as run #${res.qid}`)
      navigate('/runs/' + res.qid)
    } catch (e) { toast.error(`Could not replay: ${errorText(e)}`) } finally { setBusy(null) }
  }
  const tasks = run.order.map(t => run.tasks[t]).filter(Boolean)

  return (
    <div className="wrap run-detail">
      <TopActions>
        <Button variant="ghost" size="sm" icon="prev" onClick={() => navigate('/runs')}>All runs</Button>
      </TopActions>
      <header className="rd-head card">
        <div className="rd-main">
          <div className="rd-kicker">
            <span className="num muted">Run #{run.qid}</span>
            <StatusBadge status={status} />
            <Badge tone="neutral">{run.source}</Badge>
            {run.engine && <Badge tone="accent"><EngineIcon name={run.engine} size={12} />{run.engine}</Badge>}
            {run.plan && <Badge tone="neutral" icon="planner">{run.plan.planner} plan</Badge>}
          </div>
          <h2 className="rd-query">{run.text || '…'}</h2>
          <dl className="rd-stats">
            <div><dt>Total</dt><dd className="num">{run.done ? secs(run.total_ms) : <span className="live-text">running…</span>}</dd></div>
            <div><dt>Steps</dt><dd className="num">{tasks.length || '–'}</dd></div>
            <div><dt>Agents</dt><dd>{[...new Set(tasks.map(t => t.routed?.agent).filter(Boolean))].join(', ') || '–'}</dd></div>
            <div><dt>Started</dt><dd title={new Date(run.at * 1000).toLocaleString()}>{timeAgo(run.at)}</dd></div>
          </dl>
          {(run.session_id || run.compare_id || run.files.length > 0) && (
            <div className="rd-links">
              {run.session_id && <a href={'#/?s=' + encodeURIComponent(run.session_id)}><Icon name="chat" size={13} />Open chat</a>}
              {run.compare_id && <a href={'#/compare/' + encodeURIComponent(run.compare_id)}><Icon name="compare" size={13} />Open comparison</a>}
              {run.files.length > 0 && <span><Icon name="paperclip" size={13} />{run.files.length} file{run.files.length === 1 ? '' : 's'} attached</span>}
            </div>
          )}
        </div>
        <div className="rd-actions">
          <Button variant="secondary" icon="share" onClick={() => void share()}>Share</Button>
          {!run.done && <Button variant="danger" icon="stop" loading={busy === 'stop'} onClick={() => void stop()}>Stop</Button>}
          <Button variant="primary" icon="replay" loading={busy === 'replay'} disabled={!run.text} onClick={() => void replay()}>Replay</Button>
        </div>
      </header>

      <Tabs tabs={TABS.map(t => (t.id === 'subtasks' ? { ...t, count: tasks.length } : t))} value={tab} onChange={setTab} aria-label="Run views" />

      <div className="rd-panel" role="tabpanel" aria-label={TABS.find(t => t.id === tab)?.label}>
        {tab === 'answer' && <AnswerTab run={run} />}
        {tab === 'trace' && <Card flush className="rd-trace"><TraceView run={run} store={store} /></Card>}
        {tab === 'timeline' && (
          <Card title="Pipeline timeline" icon="timeline" subtitle={run.marks.query == null ? 'Durations laid end to end (this run was loaded from history).' : 'When each stage started and finished in this browser.'}>
            <Waterfall run={run} />
          </Card>
        )}
        {tab === 'subtasks' && (
          tasks.length ? (
            <div className="subtask-grid">
              {tasks.map(t => (
                <div key={t.tid} className="card subtask-card">
                  {t.depends_on.length > 0 && <p className="dep-note"><Icon name="layers" size={12} />Runs after {t.depends_on.join(', ')}</p>}
                  <TaskCard task={t} />
                </div>
              ))}
            </div>
          ) : <Card><EmptyState icon="subtasks" title={run.done ? 'No subtasks' : 'Planning…'} text={run.done ? 'This run finished without a plan.' : 'Subtasks appear as soon as the planner splits the query.'} /></Card>
        )}
        {tab === 'raw' && <RawTab run={run} />}
      </div>
    </div>
  )
}

function AnswerTab({ run }: { run: Run }) {
  const toast = useToast()
  const tasks = run.order.map(t => run.tasks[t]).filter(Boolean)
  const answer = run.merged?.answer || run.mergeStream || tasks.map(t => t.answered?.answer ?? t.stream).filter(Boolean).join('\n\n')
  const copy = async () => { (await copyText(answer)) ? toast.success('Answer copied') : toast.error('Could not copy') }
  return (
    <div className="rd-answer-grid">
      <Card title="Answer" icon="answer" actions={answer ? <Button variant="ghost" size="sm" icon="copy" onClick={() => void copy()}>Copy</Button> : undefined}
        subtitle={run.merged ? `${run.merged.engine === 'single' ? 'direct' : run.merged.engine} merge${run.merged.ms != null ? ` · ${run.merged.ms} ms` : ''}` : undefined}>
        {answer ? <div className={'answer big' + (!run.done ? ' streaming' : '')}><Markdown text={answer} /></div>
          : run.done ? <EmptyState compact icon="answer" title="No answer" text={run.error ?? (run.status === 'cancelled' ? 'The run was stopped.' : 'Nothing was returned.')} />
          : <Skeleton lines={4} />}
        {run.error && answer && <p className="bot-error"><Icon name="alert" size={14} />{run.error}</p>}
      </Card>
      {tasks.length > 0 && (
        <Card title="Steps" icon="subtasks">
          <ol className="rd-steps">
            {tasks.map(t => (
              <li key={t.tid}>
                <span className="tid">{t.tid}</span>
                <span className="rd-step-text">{t.text}</span>
                <span className="rd-step-meta">
                  {t.routed ? <Badge tone="neutral">{t.routed.agent} · {pct(t.routed.confidence)}</Badge> : <span className="muted small">{t.error ? 'failed' : 'pending'}</span>}
                  {t.answered && <span className="muted small num">{t.answered.agent_ms} ms</span>}
                </span>
              </li>
            ))}
          </ol>
        </Card>
      )}
    </div>
  )
}

function RawTab({ run }: { run: Run }) {
  const toast = useToast()
  const [rec, setRec] = useState<RunRecord | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    let alive = true
    setRec(null); setErr(null)
    getRun(run.qid).then(r => alive && setRec(r), e => alive && setErr(errorText(e)))
    return () => { alive = false }
  }, [run.qid, run.done])
  // Without the server record, show the client's view of the run (minus browser timing marks).
  const { marks: _m, ...client } = run
  const json = JSON.stringify(rec ?? client, null, 2)
  const copy = async () => { (await copyText(json)) ? toast.success('JSON copied') : toast.error('Could not copy') }
  return (
    <Card title={rec ? 'Stored record' : 'Client state'} icon="code" subtitle={rec ? 'GET /api/runs/' + run.qid : err ? `Server record unavailable (${err}); showing what this browser knows.` : 'Loading the stored record…'}
      actions={<Button variant="ghost" size="sm" icon="copy" onClick={() => void copy()}>Copy JSON</Button>}>
      <pre className="raw-json"><code>{json}</code></pre>
    </Card>
  )
}

function DetailSkeleton() {
  return (
    <div className="wrap run-detail" aria-busy="true" aria-label="Loading run">
      <div className="card rd-head"><div className="rd-main"><Skeleton width={180} height={16} /><Skeleton height={28} width="70%" style={{ marginTop: 12 }} /><Skeleton height={14} width="50%" style={{ marginTop: 16 }} /></div></div>
      <Skeleton height={36} width={420} />
      <div className="card"><Skeleton lines={5} /></div>
    </div>
  )
}

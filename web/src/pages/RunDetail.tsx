import { useEffect, useState, type ReactNode } from 'react'
import { ask, cancelRun, errorText, getRun } from '../api'
import type { RunRecord } from '../protocol'
import { useRunState, useStore } from '../store'
import type { Run } from '../useEventStream'
import { Badge, Button, Card, EmptyState, Skeleton, StatusBadge, Tabs, copyText, navigate, timeAgo, useHashPath, useToast } from '../ui'
import { EngineIcon, Icon } from '../icons'
import { TaskCard } from '../components/Panels'
import Markdown from '../components/Markdown'
import { TraceView } from '../components/TraceView'
import { RouteFeedback, canLabel, useRunLabels } from '../components/RouteFeedback'
import { Waterfall } from '../components/Viz'
import { AgentBadge, BackLink, PageBody } from '../components/app'
import { cn } from '@/lib/utils'
import { pct } from '../lib'

// One run in full: header with actions, then Answer · Trace · Timeline · Subtasks · Raw.

const TABS = [
  { id: 'answer', label: 'Answer', icon: 'answer' as const },
  { id: 'trace', label: 'Trace', icon: 'graph' as const },
  { id: 'timeline', label: 'Timeline', icon: 'timeline' as const },
  { id: 'subtasks', label: 'Subtasks', icon: 'subtasks' as const },
  { id: 'raw', label: 'Raw', icon: 'code' as const },
]

// Same format as the Runs table: whole milliseconds under a second, one decimal above.
const secs = (v: number | null | undefined) => (v == null ? '-' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')

export default function RunDetail({ params }: { params: Record<string, string> }) {
  const qid = Number(params.qid)
  const { run, loading, error, notFound } = useRunState(Number.isFinite(qid) ? qid : null)
  const { store } = useStore()
  const toast = useToast()
  const { query } = useHashPath()
  const tab = TABS.some(t => t.id === query.get('tab')) ? query.get('tab')! : 'answer'
  const setTab = (id: string) => location.replace(`#/runs/${qid}${id === 'answer' ? '' : '?tab=' + id}`)
  const [busy, setBusy] = useState<'stop' | 'replay' | null>(null)

  if (!Number.isFinite(qid)) return <PageBody><Card><EmptyState icon="alert" title="Not a run id" text={`"${params.qid}" is not a number.`} action={<Button onClick={() => navigate('/runs')}>All runs</Button>} /></Card></PageBody>
  if (loading) return <DetailSkeleton />
  if (!run) {
    return (
      <PageBody>
        <BackLink href="#/runs">All runs</BackLink>
        <Card><EmptyState icon={notFound ? 'runs' : 'alert'} title={notFound ? `Run #${qid} not found` : 'Could not load this run'}
          text={notFound ? 'It may have been removed, or the id is wrong.' : error ?? ''} action={<Button variant="secondary" icon="runs" onClick={() => navigate('/runs')}>All runs</Button>} /></Card>
      </PageBody>
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

  const agentNames = [...new Set(tasks.map(t => t.routed?.agent).filter(Boolean))].join(', ')

  return (
    <PageBody>
      <div className="flex flex-col gap-4">
        <BackLink href="#/runs">All runs</BackLink>
        <header className="flex flex-col gap-5 lg:flex-row lg:items-start lg:justify-between lg:gap-8">
          <div className="flex min-w-0 flex-1 flex-col gap-3">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs tabular-nums text-muted-foreground">Run #{run.qid}</span>
              <StatusBadge status={status} />
              <Badge tone="neutral">{run.source}</Badge>
              {run.engine && <Badge tone="neutral"><EngineIcon name={run.engine} size={12} />{run.engine}</Badge>}
              {run.plan && <Badge tone="neutral" icon="planner">{run.plan.planner} plan</Badge>}
            </div>
            <h2 className="m-0 max-w-[70ch] text-xl font-semibold leading-snug tracking-[-0.015em] text-balance break-words text-foreground sm:text-2xl">{run.text || '…'}</h2>
            <dl className="m-0 flex flex-wrap gap-x-8 gap-y-3">
              <StatItem label="Total">{run.done ? <span className="tabular-nums">{secs(run.total_ms)}</span> : <span className="text-primary">running…</span>}</StatItem>
              <StatItem label="Steps"><span className="tabular-nums">{tasks.length || '-'}</span></StatItem>
              <StatItem label="Agents" className="max-w-full min-w-0"><span className="break-words">{agentNames || '-'}</span></StatItem>
              <StatItem label="Started"><span className="tabular-nums" title={new Date(run.at * 1000).toLocaleString()}>{timeAgo(run.at)}</span></StatItem>
            </dl>
            {(run.session_id || run.compare_id || run.files.length > 0) && (
              <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 text-[13px]">
                {run.session_id && <a className={LINK} href={'#/?s=' + encodeURIComponent(run.session_id)}><Icon name="chat" size={14} />Open chat</a>}
                {run.compare_id && <a className={LINK} href={'#/compare/' + encodeURIComponent(run.compare_id)}><Icon name="compare" size={14} />Open comparison</a>}
                {run.files.length > 0 && <span className="inline-flex items-center gap-1.5 text-muted-foreground"><Icon name="paperclip" size={14} /><span className="tabular-nums">{run.files.length}</span> file{run.files.length === 1 ? '' : 's'} attached</span>}
              </div>
            )}
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap lg:shrink-0 lg:justify-end [&>button]:w-full sm:[&>button]:w-auto">
            <Button variant="secondary" icon="share" onClick={() => void share()}>Share</Button>
            {!run.done && <Button variant="danger" icon="stop" loading={busy === 'stop'} onClick={() => void stop()}>Stop</Button>}
            <Button variant="primary" icon="replay" loading={busy === 'replay'} disabled={!run.text} onClick={() => void replay()}>Replay</Button>
          </div>
        </header>
      </div>

      <div className="flex min-w-0 flex-col gap-4">
        <Tabs tabs={TABS.map(t => (t.id === 'subtasks' ? { ...t, count: tasks.length } : t))} value={tab} onChange={setTab} aria-label="Run views" />

        <div className="min-w-0" role="tabpanel" aria-label={TABS.find(t => t.id === tab)?.label}>
          {tab === 'answer' && <AnswerTab run={run} />}
          {tab === 'trace' && <Card flush className="overflow-hidden"><TraceView run={run} store={store} /></Card>}
          {tab === 'timeline' && (
            <Card title="Pipeline timeline" icon="timeline" subtitle={run.marks.query == null ? 'Durations laid end to end (this run was loaded from history).' : 'When each stage started and finished in this browser.'}>
              <Waterfall run={run} />
            </Card>
          )}
          {tab === 'subtasks' && (
            tasks.length ? <SubtasksTab run={run} /> : <Card><EmptyState icon="subtasks" title={run.done ? 'No subtasks' : 'Planning…'} text={run.done ? 'This run finished without a plan.' : 'Subtasks appear as soon as the planner splits the query.'} /></Card>
          )}
          {tab === 'raw' && <RawTab run={run} />}
        </div>
      </div>
    </PageBody>
  )
}

// One card per subtask; saved runs get route feedback under each routing decision.
function SubtasksTab({ run }: { run: Run }) {
  const tasks = run.order.map(t => run.tasks[t]).filter(Boolean)
  const [labels, setLabel] = useRunLabels(run.qid, run.done && run.source !== 'sandbox')
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      {tasks.map(t => (
        <div key={t.tid} className="flex min-w-0 flex-col gap-3 overflow-hidden rounded-lg border border-border bg-surface p-4 sm:p-5">
          {t.depends_on.length > 0 && (
            <p className="m-0 flex items-center gap-1.5 text-xs text-muted-foreground">
              <Icon name="layers" size={12} />Runs after <span className="font-mono">{t.depends_on.join(', ')}</span>
            </p>
          )}
          <TaskCard task={t} bare />
          {run.done && canLabel(run.source, t) && (
            <RouteFeedback qid={run.qid} task={t} label={labels[t.tid]} onLabel={setLabel} className="border-t border-border pt-3" />
          )}
        </div>
      ))}
    </div>
  )
}

const LINK = 'inline-flex items-center gap-1.5 rounded-sm font-medium text-primary outline-none hover:underline hover:underline-offset-2 focus-visible:ring-[3px] focus-visible:ring-ring/35'

function StatItem({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn('flex flex-col gap-0.5', className)}>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="m-0 text-sm font-medium text-foreground">{children}</dd>
    </div>
  )
}

// Blinking caret after the last block while the answer streams in (opacity only).
const STREAMING = "[&>.md>:last-child]:after:ml-0.5 [&>.md>:last-child]:after:animate-pulse [&>.md>:last-child]:after:text-primary [&>.md>:last-child]:after:content-['▍']"

function AnswerTab({ run }: { run: Run }) {
  const toast = useToast()
  const tasks = run.order.map(t => run.tasks[t]).filter(Boolean)
  const answer = run.merged?.answer || run.mergeStream || tasks.map(t => t.answered?.answer ?? t.stream).filter(Boolean).join('\n\n')
  const copy = async () => { (await copyText(answer)) ? toast.success('Answer copied') : toast.error('Could not copy') }
  return (
    <div className="grid items-start gap-4 lg:grid-cols-3">
      <Card className={tasks.length > 0 ? 'lg:col-span-2' : 'lg:col-span-3'} title="Answer" icon="answer" actions={answer ? <Button variant="ghost" size="sm" icon="copy" onClick={() => void copy()}>Copy</Button> : undefined}
        subtitle={run.merged ? `${run.merged.engine === 'single' ? 'direct' : run.merged.engine} merge${run.merged.ms != null ? ` · ${secs(run.merged.ms)}` : ''}` : undefined}>
        {answer ? <div className={cn('min-w-0 max-w-[75ch] text-[14.5px] leading-relaxed break-words text-foreground', !run.done && STREAMING)}><Markdown text={answer} /></div>
          : run.done ? <EmptyState compact icon="answer" title="No answer" text={run.error ?? (run.status === 'cancelled' ? 'The run was stopped.' : 'Nothing was returned.')} />
          : <Skeleton lines={4} />}
        {run.error && answer && <p className="m-0 mt-3 flex items-start gap-1.5 text-[13px] text-warn"><Icon name="alert" size={14} className="mt-0.5 shrink-0" />{run.error}</p>}
      </Card>
      {tasks.length > 0 && (
        <Card title="Steps" icon="subtasks">
          <ol className="m-0 flex list-none flex-col p-0">
            {tasks.map(t => (
              <li key={t.tid} className="flex min-w-0 flex-col gap-1.5 border-b border-border py-2.5 first:pt-0 last:border-0 last:pb-0">
                <div className="flex min-w-0 items-baseline gap-2">
                  <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">{t.tid}</span>
                  <span className="min-w-0 text-[13px] font-medium break-words text-foreground">{t.text}</span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {t.routed ? <AgentBadge agent={t.routed.agent} pct={pct(t.routed.confidence)} /> : <span className="text-xs text-muted-foreground">{t.error ? 'failed' : 'pending'}</span>}
                  {t.answered && <span className="text-xs tabular-nums text-muted-foreground">{secs(t.answered.agent_ms)}</span>}
                </div>
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
      <pre className="m-0 max-h-[70vh] overflow-auto rounded-md bg-subtle p-4 font-mono text-[12.5px] leading-relaxed text-foreground"><code>{json}</code></pre>
    </Card>
  )
}

function DetailSkeleton() {
  return (
    <PageBody>
      <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading run">
        <Skeleton width="20%" height={14} />
        <div className="flex flex-col gap-3">
          <Skeleton width="45%" height={18} />
          <Skeleton width="80%" height={26} />
          <Skeleton width="60%" height={32} />
        </div>
        <Skeleton width="100%" height={40} />
        <div className="grid gap-4 lg:grid-cols-3">
          <div className="rounded-lg border border-border bg-surface p-4 sm:p-5 lg:col-span-2"><Skeleton lines={5} /></div>
          <div className="rounded-lg border border-border bg-surface p-4 sm:p-5"><Skeleton lines={4} /></div>
        </div>
      </div>
    </PageBody>
  )
}

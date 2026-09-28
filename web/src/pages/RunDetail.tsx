import { useEffect, useState, type ReactNode } from 'react'
import { ApiError, askOrConfirm, cancelRun, errorText, getRun, promoteRun } from '../api'
import type { AskBody, CheckpointInfo, CreatedFile, PromoteRunResponse, RunCost, RunRecord, RunTimings, SuspectCheck } from '../protocol'
import { useRunState, useStore } from '../store'
import type { Run, Task } from '../useEventStream'
import { Badge, Button, Card, EmptyState, Skeleton, StatusBadge, Tabs, copyText, navigate, timeAgo, useHashPath, useToast } from '../ui'
import { EngineIcon, Icon } from '../icons'
import { TaskCard } from '../components/Panels'
import Markdown from '../components/Markdown'
import { TraceView } from '../components/TraceView'
import { RouteFeedback, canLabel, useRunLabels } from '../components/RouteFeedback'
import { Waterfall } from '../components/Viz'
import { Timings } from '../components/Timings'
import { AgentBadge, BackLink, PageBody } from '../components/app'
import { CreatedFiles, FailedFiles, RunFiles, failedFileSteps, filesOfTasks, useResume } from '../components/chat/CreatedFiles'
import { aboutTokens, duration, useCostConfirm } from '../components/chat/CostDialog'
import { Assumptions, CaveatsBox, withoutAssumptions, withoutCaveats } from '../components/chat/Caveats'
import { assumptionsOf, caveatsOf } from '../components/chat/Turn'
import { StepPolicy } from '../components/StepPolicy'
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

// Files a step created (docs/PLAN-files.md): from the live `answered` event, else from the stored record, since a run
// loaded from history carries only the answer.
function filesOfTask(t: Task, rec: RunRecord | null): CreatedFile[] {
  return t.answered?.created_files?.length ? t.answered.created_files : filesOfTasks(rec?.tasks.filter(x => x.tid === t.tid) ?? [])
}

// Same format as the Runs table: whole milliseconds under a second, one decimal above.
const secs = (v: number | null | undefined) => (v == null ? '-' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')

// The stored record (GET /api/runs/:qid), fetched again when the run finishes. It carries what the live client state
// does not: stage timings, and the exact JSON for the Raw tab.
function useStoredRun(qid: number | null, done: boolean | undefined) {
  const [rec, setRec] = useState<RunRecord | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    if (qid == null) return
    let alive = true
    setErr(null)
    getRun(qid).then(r => alive && setRec(r), e => alive && setErr(errorText(e)))
    return () => { alive = false }
  }, [qid, done])
  return { rec: rec?.qid === qid ? rec : null, err }
}

export default function RunDetail({ params }: { params: Record<string, string> }) {
  const qid = Number(params.qid)
  const { run, loading, error, notFound } = useRunState(Number.isFinite(qid) ? qid : null)
  const { store } = useStore()
  const toast = useToast()
  const { query } = useHashPath()
  const tab = TABS.some(t => t.id === query.get('tab')) ? query.get('tab')! : 'answer'
  const setTab = (id: string) => location.replace(`#/runs/${qid}${id === 'answer' ? '' : '?tab=' + id}`)
  const [busy, setBusy] = useState<'stop' | 'replay' | null>(null)
  const stored = useStoredRun(Number.isFinite(qid) ? qid : null, run?.done)
  // A client that learns timings from the `done` event may carry them on the run; otherwise use the stored record.
  const timings: RunTimings | undefined = (run as { timings?: RunTimings } | null)?.timings ?? stored.rec?.timings
  const cost = useCostConfirm()

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
  // A costly replay comes back as 409 with an estimate (docs/PLAN-files-robust.md 5.4) and starts only on Continue.
  const replayWith = async (body: AskBody): Promise<void> => {
    setBusy('replay')
    try {
      const r = await askOrConfirm(body)
      if (r.kind === 'confirm') { cost.ask({ estimate: r.estimate, body, go: replayWith }); return }
      toast.success(`Replaying as run #${r.res.qid}`)
      navigate('/runs/' + r.res.qid)
    } catch (e) { toast.error(`Could not replay: ${errorText(e)}`) } finally { setBusy(null) }
  }
  const replay = () => replayWith({ query: run.text, source: 'you', engine: run.engine ?? 'none', ...(run.files.length ? { files: run.files } : {}) })
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
              {/* A run whose file step made no file gets the failure style, not "done". */}
              {run.done && status === 'done' && (run.file_failed || stored.rec?.file_failed)
                ? <Badge tone="bad" icon="error" title="The run finished, but its file step made no file">no file made</Badge>
                : <StatusBadge status={status} />}
              <Badge tone="neutral">{run.source}</Badge>
              {run.engine && <Badge tone="neutral"><EngineIcon name={run.engine} size={12} />{run.engine}</Badge>}
              {run.plan && <Badge tone="neutral" icon="planner">{run.plan.planner} plan</Badge>}
              {(stored.rec?.dry_run ?? run.dry_run) === 'route' && <Badge tone="info" icon="route" title="Planned and routed only; no agent ran">routing only</Badge>}
            </div>
            <h2 className="m-0 max-w-[70ch] text-xl font-semibold leading-snug tracking-[-0.015em] text-balance break-words text-foreground sm:text-2xl">{run.text || '…'}</h2>
            <dl className="m-0 flex flex-wrap gap-x-8 gap-y-3">
              <StatItem label="Total">{run.done ? <span className="tabular-nums">{secs(run.total_ms)}</span> : <span className="text-primary">running…</span>}</StatItem>
              <StatItem label="Steps"><span className="tabular-nums">{tasks.length || '-'}</span></StatItem>
              <StatItem label="Agents" className="max-w-full min-w-0"><span className="break-words">{agentNames || '-'}</span></StatItem>
              {timings?.first_token_ms != null && <StatItem label="First token"><span className="tabular-nums">{secs(timings.first_token_ms)}</span></StatItem>}
              <StatItem label="Started"><span className="tabular-nums" title={new Date(run.at * 1000).toLocaleString()}>{timeAgo(run.at)}</span></StatItem>
            </dl>
            {timings && run.done && (
              <button type="button" onClick={() => setTab('timeline')} aria-label="Stage timings, open the timeline"
                className="max-w-xl cursor-pointer rounded-md bg-transparent p-0 text-left outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
                <Timings timings={timings} totalMs={run.total_ms} compact />
              </button>
            )}
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
        <Suspects qid={run.qid} suspects={stored.rec?.suspects ?? run.suspects} />
        {cost.dialog}
      </div>

      <div className="flex min-w-0 flex-col gap-4">
        <Tabs tabs={TABS.map(t => (t.id === 'subtasks' ? { ...t, count: tasks.length } : t))} value={tab} onChange={setTab} aria-label="Run views" />

        <div className="min-w-0" role="tabpanel" aria-label={TABS.find(t => t.id === tab)?.label}>
          {tab === 'answer' && <AnswerTab run={run} rec={stored.rec} />}
          {tab === 'trace' && <Card flush className="overflow-hidden"><TraceView run={run} store={store} /></Card>}
          {tab === 'timeline' && (
            <div className="flex flex-col gap-4">
              <Card title="Stage timings" icon="latency"
                subtitle={timings ? 'Server-measured time per stage. Stages can overlap, so they may add up to more than the total.' : undefined}>
                {timings ? <Timings timings={timings} totalMs={run.total_ms} />
                  : <p className="m-0 text-[13px] text-muted-foreground">{!run.done ? 'Stage timings appear when the run finishes.' : stored.rec || stored.err ? 'This run has no stage timings. Runs saved before timings were recorded only have a total.' : 'Loading…'}</p>}
              </Card>
              <Card title="Pipeline timeline" icon="timeline" subtitle={run.marks.query == null ? 'Durations laid end to end (this run was loaded from history).' : 'When each stage started and finished in this browser.'}>
                <Waterfall run={run} />
              </Card>
            </div>
          )}
          {tab === 'subtasks' && (
            tasks.length ? <SubtasksTab run={run} rec={stored.rec} /> : <Card><EmptyState icon="subtasks" title={run.done ? 'No subtasks' : 'Planning…'} text={run.done ? 'This run finished without a plan.' : 'Subtasks appear as soon as the planner splits the query.'} /></Card>
          )}
          {tab === 'raw' && <RawTab run={run} rec={stored.rec} err={stored.err} />}
        </div>
      </div>
    </PageBody>
  )
}

// One card per subtask; saved runs get route feedback under each routing decision.
function SubtasksTab({ run, rec }: { run: Run; rec: RunRecord | null }) {
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
          <StepPolicy routed={t.routed} className="border-t border-border pt-3" />
          <StepFiles files={filesOfTask(t, rec)} />
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

function StepFiles({ files }: { files: CreatedFile[] }) {
  if (!files.length) return null
  return (
    <div className="flex min-w-0 flex-col gap-2 border-t border-border pt-3">
      <p className="m-0 flex items-center gap-1.5 text-xs font-medium text-muted-foreground"><Icon name="files" size={13} />Created {files.length === 1 ? 'a file' : `${files.length} files`}</p>
      <CreatedFiles files={files} />
    </div>
  )
}

function AnswerTab({ run, rec }: { run: Run; rec: RunRecord | null }) {
  const toast = useToast()
  const tasks = run.order.map(t => run.tasks[t]).filter(Boolean)
  const answer = run.merged?.answer || run.mergeStream || tasks.map(t => t.answered?.answer ?? t.stream).filter(Boolean).join('\n\n')
  const caveats = caveatsOf(run)
  const assumptions = assumptionsOf(run)
  const shown = withoutAssumptions(withoutCaveats(answer, caveats), assumptions)
  const copy = async () => { (await copyText(answer)) ? toast.success('Answer copied') : toast.error('Could not copy') }
  const made = tasks.flatMap(t => filesOfTask(t, rec)).filter((f, i, all) => all.findIndex(x => x.id === f.id) === i)
  const failed = run.done && failedFileSteps(tasks, rec?.checkpoints ?? run.checkpoints ?? []).length > 0
  return (
    <div className="grid items-start gap-4 lg:grid-cols-3">
      <Card className={tasks.length > 0 ? 'lg:col-span-2' : 'lg:col-span-3'} title="Answer" icon="answer" actions={answer ? <Button variant="ghost" size="sm" icon="copy" onClick={() => void copy()}>Copy</Button> : undefined}
        subtitle={run.merged ? `${run.merged.engine === 'single' ? 'direct' : run.merged.engine} merge${run.merged.ms != null ? ` · ${secs(run.merged.ms)}` : ''}` : undefined}>
        <Assumptions items={assumptions} className="mb-3" />
        {answer ? <div className={cn('min-w-0 max-w-[75ch] text-[14.5px] leading-relaxed break-words text-foreground', !run.done && STREAMING)}><Markdown text={shown} /></div>
          : run.done ? <EmptyState compact icon="answer" title="No answer" text={run.error ?? (run.status === 'cancelled' ? 'The run was stopped.' : 'Nothing was returned.')} />
          : <Skeleton lines={4} />}
        {run.error && answer && <p className="m-0 mt-3 flex items-start gap-1.5 text-[13px] text-warn"><Icon name="alert" size={14} className="mt-0.5 shrink-0" />{run.error}</p>}
        {run.done && <CaveatsBox caveats={caveats} className="mt-4" />}
      </Card>
      {made.length > 0 && (
        <Card className="lg:col-span-2 lg:row-start-2" title="Created files" icon="files" subtitle="Download, preview or convert. Converting reuses the stored content, so it costs no model tokens.">
          <RunFiles files={made} primary={run.merged?.primary_file} />
        </Card>
      )}
      {failed && (
        <Card className="lg:col-span-2" title="File not made" icon="files" subtitle="What was already written is kept. Resume writes only what is missing.">
          <FailedFiles tasks={tasks} checkpoints={rec?.checkpoints ?? run.checkpoints} />
        </Card>
      )}
      <CostCard cost={rec?.cost ?? run.cost ?? null} checkpoints={rec?.checkpoints ?? []} className="lg:col-span-2" />
      {tasks.length > 0 && (
        <Card title="Steps" icon="subtasks" className={made.length > 0 ? 'lg:row-span-2 lg:row-start-1 lg:col-start-3' : undefined}>
          <ol className="m-0 flex list-none flex-col p-0">
            {tasks.map(t => (
              <li key={t.tid} className="flex min-w-0 flex-col gap-1.5 border-b border-border py-2.5 first:pt-0 last:border-0 last:pb-0">
                <div className="flex min-w-0 items-baseline gap-2">
                  <span className="shrink-0 font-mono text-xs tabular-nums text-muted-foreground">{t.tid}</span>
                  <span className="min-w-0 text-[13px] font-medium break-words text-foreground">{t.text}</span>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {t.routed ? <AgentBadge agent={t.routed.agent} pct={pct(t.routed.confidence)} /> : <span className="text-xs text-muted-foreground">{t.error ? 'failed' : 'pending'}</span>}
                  {t.routed?.bound && <Badge tone="accent" icon="agent" title="This step took the agent picked with @">bound</Badge>}
                  {t.answered && <span className="text-xs tabular-nums text-muted-foreground">{secs(t.answered.agent_ms)}</span>}
                  {filesOfTask(t, rec).length > 0 && <Badge tone="neutral" icon="files">{filesOfTask(t, rec).length === 1 ? '1 file' : `${filesOfTask(t, rec).length} files`}</Badge>}
                </div>
              </li>
            ))}
          </ol>
        </Card>
      )}
    </div>
  )
}

const num = (v: number | null | undefined) => (v == null ? '-' : Math.round(v).toLocaleString())
const PHASE_TEXT: Record<CheckpointInfo['phase'], string> = { outline: 'outline', sections: 'writing sections', topup: 'top-up', render: 'render', done: 'done' }

/** Estimated vs used model calls, tokens and time (the estimate is made before the run without a model call), and
 *  the checkpoints of the run's file steps with Resume where something is missing. */
function CostCard({ cost, checkpoints, className }: { cost: RunCost | null; checkpoints: CheckpointInfo[]; className?: string }) {
  const est = cost?.estimate ?? null, act = cost?.actual ?? null
  if (!est && !act && !checkpoints.length) return null
  const rows: Array<[string, string, string]> = [
    ['Model calls', est ? `${num(est.calls)}${est.range.calls[1] > est.calls ? `, up to ${num(est.range.calls[1])}` : ''}` : '-', num(act?.calls)],
    ['Tokens in', est ? `about ${aboutTokens(est.tokens_in)}` : '-', num(act?.tokens_in)],
    ['Tokens out', est ? `about ${aboutTokens(est.tokens_out)}` : '-', num(act?.tokens_out)],
    ['Time', est ? duration(est.seconds) : '-', act ? duration(act.seconds) : '-'],
  ]
  return (
    <Card className={className} title="Cost" icon="spend"
      subtitle={est ? 'Estimated before the run without calling a model, beside what the run really used.' : 'What the run used.'}>
      <div className="flex flex-col gap-4">
        {(est || act) && (
          <div className="overflow-x-auto">
            <table className="w-full max-w-lg border-collapse text-[13px] tabular-nums">
              <thead>
                <tr className="text-xs text-muted-foreground">
                  <th scope="col" className="py-1.5 pr-4 text-left font-medium"><span className="sr-only">Measure</span></th>
                  <th scope="col" className="py-1.5 pr-4 text-right font-medium">Estimated</th>
                  <th scope="col" className="py-1.5 text-right font-medium">Used</th>
                </tr>
              </thead>
              <tbody>
                {rows.map(([label, e, a]) => (
                  <tr key={label} className="border-t border-border">
                    <th scope="row" className="py-1.5 pr-4 text-left font-normal text-muted-foreground">{label}</th>
                    <td className="py-1.5 pr-4 text-right text-foreground">{e}</td>
                    <td className="py-1.5 text-right text-foreground">{act ? a : (label === 'Model calls' ? 'not finished' : '-')}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {est?.engine_label && <p className="m-0 mt-2 text-xs text-muted-foreground">Estimated for {est.engine_label}.</p>}
          </div>
        )}
        {checkpoints.length > 0 && (
          <div className="flex flex-col gap-2">
            <h3 className="m-0 text-[13px] font-semibold text-foreground">Checkpoints</h3>
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {checkpoints.map(c => <li key={c.tid}><CheckpointRow c={c} /></li>)}
            </ul>
          </div>
        )}
      </div>
    </Card>
  )
}

function CheckpointRow({ c }: { c: CheckpointInfo }) {
  const { start, busy, dialog } = useResume()
  const unit = c.format === 'pptx' ? 'slides' : 'sections'
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 rounded-md border border-border px-3 py-2 text-[13px]">
      <span className="font-mono text-xs text-muted-foreground">step {c.tid}</span>
      <span className="text-foreground">{c.format.toUpperCase()}, {PHASE_TEXT[c.phase] ?? c.phase}</span>
      {c.planned > 0 && <span className="tabular-nums text-muted-foreground">{c.written} of {c.planned} {unit} written</span>}
      <span className="tabular-nums text-muted-foreground">{(c.tokens_in + c.tokens_out).toLocaleString()} tokens spent</span>
      {c.missing.length > 0 && <span className="min-w-0 basis-full text-xs text-muted-foreground [overflow-wrap:anywhere]">Missing: {c.missing.join(', ')}</span>}
      {c.resumable && (
        <Button variant="primary" size="sm" icon="replay" loading={busy} className="ml-auto" onClick={() => start({ qid: c.qid, tid: c.tid })}>
          {busy ? 'Starting' : 'Resume'}
        </Button>
      )}
      {dialog}
    </div>
  )
}

function RawTab({ run, rec, err }: { run: Run; rec: RunRecord | null; err: string | null }) {
  const toast = useToast()
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

const SUSPECT_LABEL: Record<string, string> = {
  pages_short: 'File too short', forced_non_file: '@create on a step with no file', reply_template_body: 'File holds a reply line',
  extra_format: 'File in a format not asked for', unfulfilled: 'Parts not done', dup_clarify: 'Repeated question',
  agent_label_leak: 'Agent label leaked', dead_end: 'Dead-end answer', cut_off: 'Context cut off',
}

/** Checks that flag this run as likely wrong, and "Make eval case", which drafts a case from it for the eval suite. */
function Suspects({ qid, suspects }: { qid: number; suspects: SuspectCheck[] | undefined }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [made, setMade] = useState<PromoteRunResponse | null>(null)
  useEffect(() => { setMade(null) }, [qid])
  if (!suspects?.length) return null
  const promote = async () => {
    setBusy(true)
    try {
      const res = await promoteRun(qid)
      setMade(res)
      toast.success(res.created ? `Drafted eval case ${res.case_id}` : `Eval case ${res.case_id} is already there`)
    } catch (e) {
      toast.error(e instanceof ApiError && e.status === 409 ? `An eval case for run #${qid} already exists` : `Could not make an eval case: ${errorText(e)}`)
    } finally { setBusy(false) }
  }
  return (
    <section aria-label="Suspect checks" className="flex flex-col gap-2 rounded-lg border border-warn/30 bg-warn/10 p-3 sm:p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="m-0 flex items-center gap-1.5 text-[13px] font-semibold text-foreground">
          <Icon name="flag" size={14} className="text-warn" />This run looks wrong in {suspects.length === 1 ? '1 way' : `${suspects.length} ways`}
        </h3>
        <Button variant="secondary" size="sm" icon="evals" loading={busy} disabled={!!made} onClick={() => void promote()}>
          {made ? 'Eval case made' : 'Make eval case'}
        </Button>
      </div>
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        {suspects.map((s, i) => (
          <li key={s.code + i} className="flex min-w-0 flex-wrap items-baseline gap-x-2 text-[13px] text-foreground">
            <span className="font-medium" title={s.code}>{SUSPECT_LABEL[s.code] ?? s.code.replace(/_/g, ' ')}</span>
            {s.note && <span className="min-w-0 text-muted-foreground [overflow-wrap:anywhere]">{s.note}</span>}
          </li>
        ))}
      </ul>
      {made && (
        <p className="m-0 text-xs text-muted-foreground [overflow-wrap:anywhere]">
          Case <span className="font-mono text-foreground">{made.case_id}</span> {made.created ? 'saved to' : 'is in'} <span className="font-mono text-foreground">{made.path}</span>. Edit its expectations there, then run the evals.
        </p>
      )}
    </section>
  )
}

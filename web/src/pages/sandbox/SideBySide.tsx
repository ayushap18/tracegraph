import { Badge, Button, Skeleton, StatusBadge } from '../../ui'
import { EngineIcon, Icon } from '../../icons'
import { AgentBadge } from '../../components/app'
import Markdown from '../../components/Markdown'
import { ms, pct } from '../../lib'
import { cn } from '@/lib/utils'
import { engineLabelOf } from './EngineSelect'
import type { EngineInfo, Run, SideBySideProps } from './types'

// A side-by-side turn: one question, one card per engine. Cards follow the width of the thread (container
// queries), so they stay readable next to the trace panel: one column when narrow, two from ~672px, then more.

const STREAM_CURSOR = "[&>.md>:last-child]:after:ml-px [&>.md>:last-child]:after:text-primary [&>.md>:last-child]:after:content-['▍'] [&>.md>:last-child]:after:animate-[blink_1s_steps(2)_infinite]"

const COLS: Record<number, string> = {
  1: '',
  2: '@2xl:grid-cols-2',
  3: '@2xl:grid-cols-2 @4xl:grid-cols-3',
  4: '@2xl:grid-cols-2 @5xl:grid-cols-4',
}

export function SideBySide({ group, byQid, engines, selectedQid, onShowTrace, onStop }: SideBySideProps) {
  const names = group.engines ?? []
  const cols = group.qids.map((qid, i) => ({ qid, engine: names[i] ?? byQid.get(qid)?.engine ?? KEYLESS_NAME, run: byQid.get(qid) }))
  const question = cols.find(c => c.run?.text)?.run?.text ?? ''

  const finished = cols.filter(c => c.run?.done && c.run.status === 'done' && c.run.total_ms != null)
  const fastest = finished.length > 1
    ? finished.reduce((a, b) => ((a.run!.total_ms ?? 0) <= (b.run!.total_ms ?? 0) ? a : b)).qid
    : null

  return (
    <div className="@container flex min-w-0 flex-col gap-4">
      <div className="flex flex-col items-end gap-1.5">
        <div className="max-w-[85%] rounded-2xl rounded-br-md bg-subtle px-4 py-2.5 text-[15px] leading-relaxed text-foreground">
          <p className="m-0 whitespace-pre-wrap [overflow-wrap:anywhere]">{question || '…'}</p>
        </div>
        <span className="flex items-center gap-1 text-xs tabular-nums text-muted-foreground">
          <Icon name="compare" size={12} />Side by side, {cols.length} engines
        </span>
      </div>
      <div className={cn('grid grid-cols-1 gap-3', COLS[Math.min(4, Math.max(1, cols.length))])}>
        {cols.map(c => (
          <EngineCard key={c.qid} qid={c.qid} engine={c.engine} run={c.run} engines={engines} fastest={c.qid === fastest}
            selected={selectedQid === c.qid} onShowTrace={() => onShowTrace(c.qid)} onStop={() => onStop(c.qid)} />
        ))}
      </div>
    </div>
  )
}

export default SideBySide

const KEYLESS_NAME = 'none'

function EngineCard({ qid, engine, run, engines, fastest, selected, onShowTrace, onStop }: {
  qid: number; engine: string; run: Run | undefined; engines: EngineInfo[]; fastest: boolean; selected: boolean
  onShowTrace: () => void; onStop: () => void
}) {
  const label = engineLabelOf(engines, engine)
  const status = run?.status ?? 'running'
  const running = !run || !run.done
  const failed = !!run?.done && status !== 'done'
  const tasks = run ? run.order.map(t => run.tasks[t]).filter(Boolean) : []
  const answer = run ? (run.merged?.answer ?? run.mergeStream) : ''
  const streaming = running && !run?.merged
  const tok = run?.tokens
  const totalTok = tok ? tok.jev_in + tok.llm_in + tok.llm_out : null
  const quiet = status === 'cancelled' || status === 'timeout'

  return (
    <article aria-label={`${label} answer`}
      className={cn('flex min-w-0 flex-col gap-3 rounded-lg border bg-surface p-4 transition-colors', selected ? 'border-primary/60' : 'border-border')}>
      <header className="flex min-h-7 items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-2 text-sm font-semibold text-foreground">
          <span className="inline-flex shrink-0 text-muted-foreground"><EngineIcon name={engine} size={16} /></span>
          <span className="truncate">{label}</span>
        </span>
        <span className="flex shrink-0 items-center gap-1.5">
          {fastest && <Badge tone="ok" icon="bolt">fastest</Badge>}
          {(running || failed) && <StatusBadge status={status} />}
        </span>
      </header>

      <dl className="m-0 grid grid-cols-2 gap-2 rounded-md bg-subtle px-3 py-2">
        <div className="flex min-w-0 flex-col gap-0.5">
          <dt className="text-xs text-muted-foreground">Time</dt>
          <dd className="m-0 truncate text-sm font-medium tabular-nums text-foreground">{running ? '…' : ms(run?.total_ms)}</dd>
        </div>
        <div className="flex min-w-0 flex-col gap-0.5">
          <dt className="text-xs text-muted-foreground">Tokens</dt>
          <dd className="m-0 truncate text-sm font-medium tabular-nums text-foreground"
            title={tok ? `Jev in ${tok.jev_in.toLocaleString()}, LLM in ${tok.llm_in.toLocaleString()}, LLM out ${tok.llm_out.toLocaleString()}` : undefined}>
            {totalTok != null ? totalTok.toLocaleString() : running ? '…' : 'n/a'}
          </dd>
        </div>
      </dl>

      {tasks.some(t => t.routed) && (
        <div className="flex flex-wrap gap-1.5">
          {tasks.filter(t => t.routed).map(t => (
            <AgentBadge key={t.tid} agent={t.routed!.agent} pct={pct(t.routed!.confidence)} title={`${t.routed!.agent}: ${pct(t.routed!.confidence)} confidence`} />
          ))}
        </div>
      )}

      <div className="min-w-0 text-sm leading-relaxed text-foreground [overflow-wrap:anywhere] md:max-h-[440px] md:overflow-y-auto">
        {answer ? <div className={cn(streaming && STREAM_CURSOR)}><Markdown text={answer} /></div>
          : running ? <div role="status" aria-label={`${label} is working`}><Skeleton lines={3} /></div>
          : !failed ? <span className="text-muted-foreground">No answer.</span> : null}
      </div>

      {failed && (
        <p className={cn('m-0 flex items-start gap-2 rounded-md border p-2.5 text-[13px] text-foreground',
          quiet ? 'border-warn/25 bg-warn/10' : 'border-destructive/25 bg-destructive/10')}>
          <Icon name={status === 'cancelled' ? 'cancelled' : status === 'timeout' ? 'timeout' : 'alert'} size={14}
            className={cn('mt-0.5 shrink-0', quiet ? 'text-warn' : 'text-destructive')} />
          <span className="min-w-0 [overflow-wrap:anywhere]">
            {run?.error ?? (status === 'cancelled' ? 'Stopped before it finished.' : status === 'timeout' ? 'Timed out.' : 'Something went wrong.')}
          </span>
        </p>
      )}

      <footer className="mt-auto flex items-center justify-end gap-1 pt-1">
        {running && <Button variant="ghost" size="sm" icon="stop" onClick={onStop} aria-label={`Stop ${label} run`}>Stop</Button>}
        <Button variant="ghost" size="sm" icon="graph" onClick={onShowTrace} aria-pressed={selected} disabled={!run}
          aria-label={`Trace for ${label}, run ${qid}`}
          className="aria-pressed:bg-primary/10 aria-pressed:text-primary">Trace</Button>
      </footer>
    </article>
  )
}

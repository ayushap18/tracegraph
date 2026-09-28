import type { AnswerChecks, RunTimings } from '../../protocol'
import type { Run } from '../../useEventStream'
import { Badge } from '../../ui'
import { Icon } from '../../icons'
import { modeInfo, styleInfo, type RunExtras } from './options'

// What was asked for and what happened on one answer: mode, style, @agent, cache hits, the verify step,
// and how long each stage took. Everything is optional: older runs and older servers show nothing extra.

export const secs = (v: number | null | undefined) => (v == null ? '' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')

interface Flags {
  forced: string | null; cached: boolean; checks: Array<AnswerChecks & { text: string }>
  bound: { step: number; of: number } | null // the step the @agent took, in a plan of several steps
}

function flagsOf(run: Run, x: RunExtras | undefined): Flags {
  let forced: string | null = x?.agent ?? null
  let cached = (x?.timings?.cache_hits ?? 0) > 0
  const checks: Flags['checks'] = []
  let bound: Flags['bound'] = null
  run.order.forEach((tid, i) => {
    if (run.tasks[tid]?.routed?.bound && run.order.length > 1) bound = { step: i + 1, of: run.order.length }
  })
  for (const tid of run.order) {
    const t = run.tasks[tid]
    if (!t) continue
    const saved = x?.tasks?.[tid]
    if (t.routed?.forced || saved?.forced) forced = forced ?? t.routed?.agent ?? null
    const c = t.answered?.checks ?? saved?.checks
    if (t.routed?.cached || saved?.cached || c?.cached) cached = true
    if (c) checks.push({ ...c, text: t.text })
  }
  return { forced, cached, checks, bound }
}

export function AnswerBadges({ run, extras }: { run: Run; extras?: RunExtras }) {
  const f = flagsOf(run, extras)
  const mode = extras?.mode && extras.mode !== 'balanced' ? modeInfo(extras.mode) : null
  const style = extras?.style && extras.style !== 'default' ? styleInfo(extras.style) : null
  const mismatch = f.checks.filter(c => c.verified === 'mismatch')
  const verified = f.checks.some(c => c.verified === 'ok')
  const effort = f.checks.find(c => c.effort)?.effort
  if (!mode && !style && !f.forced && !f.cached && !mismatch.length && !verified) return null
  return (
    <div className="mt-3 flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-1.5">
        {mode && <Badge tone="accent" icon={mode.icon} title={mode.help}>{mode.label}</Badge>}
        {style && <Badge tone="neutral" title={style.help}>{style.label}</Badge>}
        {f.forced && (f.bound
          ? <Badge tone="neutral" icon="agent" title={`You picked @${f.forced}. It took step ${f.bound.step}; the other steps were routed as usual.`}>@{f.forced} on step {f.bound.step} of {f.bound.of}</Badge>
          : <Badge tone="neutral" icon="agent" title="You picked this agent with @, so routing was skipped">@{f.forced}</Badge>)}
        {f.cached && <Badge tone="neutral" icon="database" title="Part of this answer came from a recent cached result">Cached</Badge>}
        {verified && !mismatch.length && <Badge tone="ok" icon="check" title={effort ? `Checked by a second pass (${effort} effort)` : 'Checked by a second pass'}>Checked</Badge>}
        {mismatch.length > 0 && <Badge tone="warn" icon="warning" title="A second check disagreed with this answer">Check failed</Badge>}
      </div>
      {mismatch.length > 0 && (
        <div role="note" className="flex items-start gap-2 rounded-md border border-warn/30 bg-warn/10 px-3 py-2 text-[13px] text-foreground">
          <Icon name="warning" size={14} className="mt-0.5 shrink-0 text-warn" />
          <span className="min-w-0 [overflow-wrap:anywhere]">
            A second check did not agree with this answer, so treat it with care.
            {mismatch.map((c, i) => c.verify_note ? <span key={i} className="mt-1 block text-muted-foreground">{mismatch.length > 1 ? `${c.text}: ` : ''}{c.verify_note}</span> : null)}
          </span>
        </div>
      )}
    </div>
  )
}

const PLANNER: Record<RunTimings['planner'], string> = { llm: 'LLM', heuristic: 'rules', single: 'one step' }
const MERGER: Record<RunTimings['merger'], string> = { llm: 'LLM', template: 'template', single: 'one answer' }

/** "Plan 3 ms (rules) · Route 120 ms · Agents 1.2 s · Merge 1 ms (template) · First text 0.4 s" */
export function TimingLine({ timings }: { timings?: RunTimings }) {
  if (!timings) return null
  const parts: string[] = []
  if (timings.plan_ms != null) parts.push(`Plan ${secs(timings.plan_ms)} (${PLANNER[timings.planner] ?? timings.planner})`)
  if (timings.route_ms != null) parts.push(`Route ${secs(timings.route_ms)}`)
  if (timings.agents_ms != null) parts.push(`Agents ${secs(timings.agents_ms)}`)
  if (timings.merge_ms != null) parts.push(`Merge ${secs(timings.merge_ms)} (${MERGER[timings.merger] ?? timings.merger})`)
  if (timings.first_token_ms != null) parts.push(`First text ${secs(timings.first_token_ms)}`)
  if (!parts.length) return null
  return (
    <p className="mt-2 flex items-start gap-1.5 text-xs leading-relaxed tabular-nums text-muted-foreground">
      <Icon name="latency" size={12} className="mt-0.5 shrink-0" />
      <span className="min-w-0">{parts.join(' · ')}</span>
    </p>
  )
}

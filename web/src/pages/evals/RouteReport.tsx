import type { FailStage, FileScore, ReasonCode, StepRoute, TagGate } from '../../protocol'
import { Badge } from '../../ui'
import { Icon } from '../../icons'
import { AgentBadge } from '../../components/app'
import { PolicyTrace } from '../../components/StepPolicy'
import { cn } from '@/lib/utils'
import { pctOf } from './shared'

// The routing report's smaller pieces (docs/PLAN-accuracy-v2.md D1-D3, D7): where failures happened, the CI gates, and
// per case the reason codes, the file score and each step's route.

export const STAGE_LABEL: Record<FailStage, string> = {
  plan_text: 'Plan text', plan_shape: 'Plan shape', jev_pick: "Jev's pick", clarity: 'Clarity veto', confidence: 'Low confidence',
  gate_missing_detail: 'Missing detail', gate_cant: "Can't-do rule", gate_wants_file: 'File rule', unsupported: 'Unsupported',
  forced: '@agent', frame: 'Follow-up frame', agent_answer: 'Agent answer', file: 'File', judge: 'Judge', budget: 'Budget',
}

/** Failure stages as one bar each, most common first. */
export function StageBars({ stages }: { stages: Partial<Record<FailStage, number>> }) {
  const rows = (Object.entries(stages) as Array<[FailStage, number]>).filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1])
  if (!rows.length) return <p className="m-0 text-[13px] text-muted-foreground">No failures to attribute.</p>
  const max = rows[0][1]
  const total = rows.reduce((n, [, v]) => n + v, 0)
  return (
    <ul className="m-0 grid list-none grid-cols-[minmax(0,9rem)_minmax(0,1fr)_2.5rem] items-center gap-x-3 gap-y-1.5 p-0 text-xs" aria-label="Failures by stage">
      {rows.map(([st, n]) => (
        <li key={st} className="contents" title={`${STAGE_LABEL[st] ?? st}: ${n} of ${total} failures`}>
          <span className="truncate text-foreground">{STAGE_LABEL[st] ?? st}</span>
          <span className="h-2 rounded-r-[4px] bg-subtle" aria-hidden="true">
            <span className="block h-full rounded-r-[4px] bg-primary" style={{ width: `${(n / max) * 100}%` }} />
          </span>
          <span className="text-right tabular-nums text-muted-foreground">{n}</span>
        </li>
      ))}
    </ul>
  )
}

/** One row per gated tag: the Wilson lower bound must reach the minimum, or the rate stay within max_drop of baseline. */
export function GatesTable({ gates }: { gates: TagGate[] }) {
  if (!gates.length) return null
  const failed = gates.filter(g => !g.ok).length
  return (
    <div className="flex flex-col gap-2">
      <p className={cn('m-0 flex items-center gap-1.5 text-[13px]', failed ? 'text-destructive' : 'text-foreground')}>
        <Icon name={failed ? 'error' : 'success'} size={14} />{failed ? `${failed} of ${gates.length} gates failed` : `All ${gates.length} gates passed`}
      </p>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[34rem] border-collapse text-[13px] tabular-nums">
          <thead>
            <tr className="text-xs text-muted-foreground">
              <th scope="col" className="py-1.5 pr-3 text-left font-medium">Tag</th>
              <th scope="col" className="py-1.5 pr-3 text-right font-medium">Min</th>
              <th scope="col" className="py-1.5 pr-3 text-right font-medium">Pass rate</th>
              <th scope="col" className="py-1.5 pr-3 text-right font-medium" title="Wilson 95% lower bound of the pass rate">Lower bound</th>
              <th scope="col" className="py-1.5 pr-3 text-right font-medium">Baseline</th>
              <th scope="col" className="py-1.5 text-right font-medium">Gate</th>
            </tr>
          </thead>
          <tbody>
            {[...gates].sort((a, b) => Number(a.ok) - Number(b.ok) || a.tag.localeCompare(b.tag)).map(g => (
              <tr key={g.tag} className="border-t border-border">
                <td className="py-1.5 pr-3 font-mono text-xs text-foreground">#{g.tag}</td>
                <td className="py-1.5 pr-3 text-right text-muted-foreground">{pctOf(g.min)}</td>
                <td className="py-1.5 pr-3 text-right text-foreground">{pctOf(g.rate)} <span className="text-xs text-muted-foreground">({g.passed}/{g.total})</span></td>
                <td className={cn('py-1.5 pr-3 text-right', g.lower < g.min ? 'text-warn' : 'text-foreground')}>{pctOf(g.lower)}</td>
                <td className="py-1.5 pr-3 text-right text-muted-foreground">{g.baseline == null ? 'none' : pctOf(g.baseline)}</td>
                <td className="py-1.5 text-right"><Badge tone={g.ok ? 'ok' : 'bad'} icon={g.ok ? 'check' : 'close'}>{g.ok ? 'pass' : 'fail'}</Badge></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const CODE_LABEL: Record<string, string> = {
  wrong_agent: 'wrong agent', extra_agent: 'extra agent', missing_agent: 'missing agent', forbidden_agent: 'forbidden agent',
  wrong_outcome: 'wrong outcome', plan_shape: 'plan shape', wrong_deps: 'wrong order', answer_regex: 'answer',
  forbidden_regex: 'forbidden text', file: 'file', judge: 'judge', budget: 'budget', unjudged: 'unjudged', run_status: 'run status',
  unrecorded: 'not recorded',
}

/** Reason codes as chips: "wrong agent, step 1: research, got create (@agent)". */
export function CaseCodes({ codes }: { codes: ReasonCode[] | undefined }) {
  if (!codes?.length) return null
  return (
    <span className="flex flex-wrap gap-1">
      {codes.map((c, i) => {
        const bits = [c.step != null ? `step ${c.step + 1}` : null, c.want ? `wanted ${c.want}` : null, c.got ? `got ${c.got}` : null,
          c.stage ? `at ${STAGE_LABEL[c.stage] ?? c.stage}` : null].filter(Boolean)
        return (
          <span key={i} title={bits.join(', ') || c.code}
            className={cn('inline-flex h-5 max-w-full items-center gap-1 rounded-sm px-1.5 font-mono text-[11px]',
              c.code === 'unjudged' || c.code === 'unrecorded' ? 'bg-subtle text-muted-foreground' : 'bg-destructive/10 text-destructive')}>
            <span className="truncate">{CODE_LABEL[c.code] ?? c.code}{c.step != null ? ` #${c.step + 1}` : ''}{c.got ? `: ${c.got}` : ''}</span>
          </span>
        )
      })}
    </span>
  )
}

/** "PDF, 12 pages, 3 images, 2 diagrams, greyscale, 1 file". */
export function FileScoreLine({ file }: { file: FileScore | null | undefined }) {
  if (!file) return null
  const parts = [
    file.format.toUpperCase(),
    file.pages != null ? `${file.pages} page${file.pages === 1 ? '' : 's'}` : null,
    file.slides != null ? `${file.slides} slide${file.slides === 1 ? '' : 's'}` : null,
    `${file.images} image${file.images === 1 ? '' : 's'}`,
    `${file.diagrams} diagram${file.diagrams === 1 ? '' : 's'}`,
    file.grayscale == null ? null : file.grayscale ? 'greyscale' : 'in colour',
    `${file.files} file${file.files === 1 ? '' : 's'}`,
  ].filter(Boolean)
  return (
    <span className="inline-flex max-w-full items-start gap-1 text-xs text-muted-foreground" title={file.fonts.length ? `Fonts: ${file.fonts.join(', ')}` : undefined}>
      <Icon name="files" size={12} className="mt-px shrink-0" /><span className="[overflow-wrap:anywhere]">{parts.join(', ')}</span>
    </span>
  )
}

// An expected agent may be a regex ("research|knowledge").
function agentMatches(expected: string, agent: string) {
  if (expected === agent) return true
  try { return new RegExp(`^(?:${expected})$`).test(agent) } catch { return false }
}

/** Each step: expected and final agent, the stage it went wrong at, and the policy trace. */
export function StepRoutes({ steps }: { steps: StepRoute[] }) {
  return (
    <ol className="m-0 flex list-none flex-col gap-3 p-0">
      {steps.map((s, i) => {
        const wrong = s.expected != null && !agentMatches(s.expected, s.agent)
        return (
          <li key={s.tid + i} className="flex min-w-0 flex-col gap-1.5">
            <div className="flex flex-wrap items-center gap-2 text-xs">
              <span className="font-mono tabular-nums text-muted-foreground">{s.tid}</span>
              {s.expected && <><span className="text-muted-foreground">expected</span><span className="font-mono text-foreground">{s.expected}</span></>}
              <span className="text-muted-foreground">got</span><AgentBadge agent={s.agent} pct={s.confidence} />
              <span className="tabular-nums text-muted-foreground">clear {pctOf(s.clear)}</span>
              {wrong && s.stage && <Badge tone="bad">at {STAGE_LABEL[s.stage] ?? s.stage}</Badge>}
            </div>
            <PolicyTrace trace={s.trace} className="pl-1" />
          </li>
        )
      })}
    </ol>
  )
}

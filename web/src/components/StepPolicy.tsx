import type { PolicyRule, PolicyStep, RouteSignals, RoutedFields } from '../protocol'
import { Badge } from '../ui'
import { Icon } from '../icons'
import { AgentBadge } from './app'
import { pct } from '../lib'
import { cn } from '@/lib/utils'

// How the decision policy (docs/PLAN-accuracy-v2.md A2) settled one step: the rules it applied in order, Jev's extra
// signals, and whether the step took the run's @agent or carried the previous turn's frame. Every field is optional,
// so steps from older runs show nothing here.

const RULE_LABEL: Record<PolicyRule, string> = {
  blocked: 'Unsafe request', blocked_dependency: 'Earlier step blocked', unsupported: "Can't do this here", cant_do: 'Known limit',
  forced: 'You picked the agent', create_demote: 'No file asked for', file_intent: 'File asked for', keyless_contract: 'Built-in parser check',
  confirmed: 'Parser confirmed', attached_file: 'Attached file', refers_back_file: 'Refers to a file', advice: 'Advice question',
  time_sensitive: 'Changes over time', clarify_policy: 'Answered with an assumption', missing_slot: 'Detail missing',
  mode_research: 'Research mode', ambiguous_term: 'Ambiguous term', frame: 'Follow-up of the last turn',
}
export const ruleLabel = (r: string) => RULE_LABEL[r as PolicyRule] ?? r.replace(/_/g, ' ')

const SIGNALS: Array<{ key: keyof RouteSignals; label: string; help: string }> = [
  { key: 'live', label: 'Live data', help: 'Needs live data or a prediction of the future' },
  { key: 'action', label: 'Action', help: 'Asks the assistant to do something in the world' },
  { key: 'personal', label: 'Personal', help: "Needs the user's or another person's private information" },
  { key: 'described', label: 'Described', help: 'Names something only by a description that must be looked up first' },
]

/** Badges for the step: took the @agent, forced, completed from the previous turn. */
export function StepFlags({ routed }: { routed: RoutedFields | undefined }) {
  if (!routed || (!routed.bound && !routed.forced && !routed.frame_used)) return null
  return (
    <span className="flex flex-wrap items-center gap-1.5">
      {routed.bound && <Badge tone="accent" icon="agent" title="This step took the agent picked with @ (one step per run)">bound @{routed.agent}</Badge>}
      {routed.forced && !routed.bound && <Badge tone="neutral" icon="agent" title="Routing was skipped for this step">forced</Badge>}
      {routed.frame_used && (
        <Badge tone="neutral" icon="history" title={`Completed from the previous turn: ${routed.frame_used.agent} ${slotsText(routed.frame_used.slots)}`}>
          follow-up of {routed.frame_used.agent}
        </Badge>
      )}
    </span>
  )
}

const slotsText = (slots: Record<string, string | number>) => Object.entries(slots).map(([k, v]) => `${k} ${v}`).join(', ')

/** The rules applied to the step, in order: rule, the agent after it, and why. The last row is the final agent. */
export function PolicyTrace({ trace, className }: { trace: PolicyStep[] | undefined; className?: string }) {
  if (!trace?.length) return null
  return (
    <section aria-label="Policy trace" className={cn('flex min-w-0 flex-col gap-1.5', className)}>
      <h4 className="m-0 flex items-center gap-1.5 text-xs font-medium text-muted-foreground"><Icon name="route" size={13} />Policy trace</h4>
      <ol className="m-0 flex list-none flex-col gap-1 p-0">
        {trace.map((t, i) => {
          const moved = i > 0 && trace[i - 1].agent !== t.agent
          return (
            <li key={i} className="flex min-w-0 gap-2 text-xs">
              <span className="w-4 shrink-0 pt-1 text-right tabular-nums text-muted-foreground">{i + 1}</span>
              <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                  <span className={cn('font-medium', moved ? 'text-foreground' : 'text-muted-foreground')} title={t.rule}>{ruleLabel(t.rule)}</span>
                  <Icon name="arrow-right" size={11} className="text-muted-foreground" />
                  <AgentBadge agent={t.agent} className={cn(!moved && i > 0 && 'opacity-70')} />
                </span>
                {t.why && <span className="text-muted-foreground [overflow-wrap:anywhere]">{t.why}</span>}
              </span>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

/** Jev's four extra scores as thin bars; the policy acts on them at 70% (60% for described). */
export function SignalBars({ signals, className }: { signals: RouteSignals | undefined; className?: string }) {
  if (!signals || !SIGNALS.some(s => signals[s.key] != null)) return null
  return (
    <section aria-label="Signals" className={cn('flex min-w-0 flex-col gap-1.5', className)}>
      <h4 className="m-0 flex items-center gap-1.5 text-xs font-medium text-muted-foreground"><Icon name="gauge" size={13} />Signals</h4>
      <dl className="m-0 grid grid-cols-[5.5rem_minmax(0,1fr)_2.75rem] items-center gap-x-2 gap-y-1.5 text-xs">
        {SIGNALS.map(s => {
          const v = signals[s.key]
          if (v == null) return null
          const at = s.key === 'described' ? 0.6 : 0.7
          return (
            <div key={s.key} className="contents" title={`${s.help}. The policy acts at ${pct(at)}.`}>
              <dt className="truncate text-muted-foreground">{s.label}</dt>
              <dd className="relative m-0 h-1.5 rounded-full bg-subtle">
                <span className={cn('absolute inset-y-0 left-0 rounded-full', v >= at ? 'bg-warn' : 'bg-muted-foreground/50')} style={{ width: `${Math.max(0, Math.min(1, v)) * 100}%` }} />
                <span className="absolute -top-0.5 h-2.5 w-px bg-foreground/40" style={{ left: `${at * 100}%` }} aria-hidden="true" />
              </dd>
              <span className={cn('text-right tabular-nums', v >= at ? 'font-medium text-warn' : 'text-muted-foreground')}>{pct(v)}</span>
            </div>
          )
        })}
      </dl>
    </section>
  )
}

/** Everything above for one step, stacked; renders nothing for steps without these fields. */
export function StepPolicy({ routed, className }: { routed: RoutedFields | undefined; className?: string }) {
  if (!routed || (!routed.trace?.length && !routed.signals && !routed.bound && !routed.forced && !routed.frame_used && !routed.assumption)) return null
  return (
    <div className={cn('flex min-w-0 flex-col gap-3', className)}>
      <StepFlags routed={routed} />
      {routed.assumption && <p className="m-0 text-[13px] leading-snug text-muted-foreground [overflow-wrap:anywhere]">{routed.assumption}</p>}
      <PolicyTrace trace={routed.trace} />
      <SignalBars signals={routed.signals} />
    </div>
  )
}

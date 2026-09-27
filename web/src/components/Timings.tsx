import type { RunTimings } from '../protocol'
import { cn } from '@/lib/utils'

// Stage timings of one run (speed plan A1): plan, route, agents and merge laid end to end as a stacked bar, with a
// marker where the first answer text appeared. Stages can overlap (routing may start while planning), so the bar
// is scaled to whichever is longer: the stages added up or the run's total.

export const STAGE_COLORS = {
  plan: 'var(--agent-time, #8b5cf6)', route: 'var(--accent)', agents: 'var(--agent-currency, #1f9d5a)', merge: 'var(--warn)',
} as const
type Stage = keyof typeof STAGE_COLORS
const STAGES: Array<{ key: Stage; field: 'plan_ms' | 'route_ms' | 'agents_ms' | 'merge_ms'; label: string }> = [
  { key: 'plan', field: 'plan_ms', label: 'Plan' },
  { key: 'route', field: 'route_ms', label: 'Route' },
  { key: 'agents', field: 'agents_ms', label: 'Agents' },
  { key: 'merge', field: 'merge_ms', label: 'Merge' },
]

// Whole milliseconds under a second, one decimal above (same as the Runs table).
export const secs = (v: number | null | undefined) => (v == null ? '-' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')

const PLANNER: Record<RunTimings['planner'], string> = { llm: 'LLM planner', heuristic: 'heuristic planner', single: 'no split' }
const MERGER: Record<RunTimings['merger'], string> = { llm: 'LLM merge', template: 'template merge', single: 'single answer' }

export function Timings({ timings, totalMs, compact, className }: {
  timings: RunTimings; totalMs?: number | null; compact?: boolean; className?: string
}) {
  const parts = STAGES.map(s => ({ ...s, ms: timings[s.field] })).filter(p => p.ms != null && p.ms > 0) as Array<(typeof STAGES)[number] & { ms: number }>
  const sum = parts.reduce((a, p) => a + p.ms, 0)
  const scale = Math.max(sum, totalMs ?? 0, timings.first_token_ms ?? 0, 1)
  const ft = timings.first_token_ms
  const summary = parts.map(p => `${p.label} ${secs(p.ms)}`).join(', ') + (ft != null ? `, first token at ${secs(ft)}` : '')

  return (
    <div className={cn('flex min-w-0 flex-col gap-2', className)}>
      <div className={cn('relative w-full', ft != null && 'pt-4')}>
        {ft != null && (
          <span className="absolute top-0 -translate-x-1/2 text-[10.5px] leading-none whitespace-nowrap text-muted-foreground tabular-nums"
            style={{ left: `clamp(1.5rem, ${(ft / scale) * 100}%, calc(100% - 1.5rem))` }} aria-hidden="true">
            first token
          </span>
        )}
        <div role="img" aria-label={summary || 'No stage timings'}
          className={cn('relative flex w-full overflow-hidden rounded-sm bg-subtle', compact ? 'h-2' : 'h-3.5')}>
          {parts.map(p => (
            <span key={p.key} title={`${p.label}: ${secs(p.ms)}`} className="h-full border-r border-surface last:border-r-0"
              style={{ width: `${(p.ms / scale) * 100}%`, background: STAGE_COLORS[p.key] }} />
          ))}
          {ft != null && (
            <span className="absolute inset-y-0 w-0.5 -translate-x-1/2 bg-foreground" style={{ left: `${Math.min(100, (ft / scale) * 100)}%` }}
              title={`First token at ${secs(ft)}`} />
          )}
        </div>
      </div>
      <ul className="m-0 flex list-none flex-wrap items-center gap-x-4 gap-y-1 p-0 text-xs text-muted-foreground">
        {STAGES.map(s => {
          const v = timings[s.field]
          const note = s.key === 'plan' ? PLANNER[timings.planner] : s.key === 'merge' ? MERGER[timings.merger] : null
          return (
            <li key={s.key} className={cn('inline-flex items-center gap-1.5', v == null && 'opacity-60')}>
              <i className="size-2 shrink-0 rounded-full" style={{ background: STAGE_COLORS[s.key] }} aria-hidden="true" />
              <span>{s.label}</span>
              <span className="font-medium tabular-nums text-foreground">{v == null ? 'skipped' : secs(v)}</span>
              {!compact && note && <span>({note})</span>}
            </li>
          )
        })}
        {ft != null && (
          <li className="inline-flex items-center gap-1.5">
            <i className="h-3 w-0.5 shrink-0 bg-foreground" aria-hidden="true" />
            <span>First token</span><span className="font-medium tabular-nums text-foreground">{secs(ft)}</span>
          </li>
        )}
        {timings.cache_hits > 0 && (
          <li className="inline-flex items-center rounded-sm bg-ok/10 px-1.5 font-medium text-ok" title="Routing or live data answered from the cache">
            {timings.cache_hits} cache hit{timings.cache_hits === 1 ? '' : 's'}
          </li>
        )}
      </ul>
      {compact && (
        <p className="m-0 text-xs text-muted-foreground">{PLANNER[timings.planner]}, {MERGER[timings.merger]}</p>
      )}
    </div>
  )
}

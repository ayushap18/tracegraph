import type { EvalCase, EvalTurnResult, JudgeScore, TagScore } from '../../protocol'
import { Badge } from '../../ui'
import { Icon } from '../../icons'
import { AgentBadge } from '../../components/app'
import { cn } from '@/lib/utils'
import { ms } from '../../lib'
import { accTone, pctOf } from './shared'
import { StepRoutes } from './RouteReport'

// The harder suite's extras on one eval: the per-tag scorecard, and per case the repeat attempts, the judge's rubric
// scores and multi-turn conversations turn by turn.

const BAR_TONE = { ok: 'bg-ok', warn: 'bg-warn', bad: 'bg-destructive' }

/** by_tag from the server, or counted from the cases for evals stored before it existed. */
export function tagScores(byTag: Record<string, TagScore> | undefined, cases: EvalCase[]): Record<string, TagScore> {
  if (byTag && Object.keys(byTag).length) return byTag
  const out: Record<string, TagScore> = {}
  for (const c of cases) for (const t of c.tags ?? []) {
    if (c.pass == null) continue // unjudged: neither passed nor failed
    const s = (out[t] ??= { passed: 0, total: 0 })
    s.total++
    if (c.pass) s.passed++
  }
  return out
}

/** Worst tag first, so the weak spots lead. Click a tag to filter the cases below. */
export function TagScorecard({ scores, active, onPick }: { scores: Record<string, TagScore>; active: string | null; onPick: (t: string | null) => void }) {
  const rows = Object.entries(scores).filter(([, s]) => s.total > 0)
    .sort(([a, x], [b, y]) => x.passed / x.total - y.passed / y.total || a.localeCompare(b))
  return (
    <ul className="m-0 grid list-none grid-cols-1 gap-x-8 gap-y-1 p-0 md:grid-cols-2">
      {rows.map(([t, s]) => {
        const v = s.passed / s.total
        const on = active === t
        return (
          <li key={t}>
            <button type="button" aria-pressed={on} onClick={() => onPick(on ? null : t)} title={on ? 'Show every tag' : `Show only #${t}`}
              className={cn('grid w-full cursor-pointer grid-cols-[minmax(0,8rem)_minmax(0,1fr)_3.5rem_2.75rem] items-center gap-2 rounded-md bg-transparent px-1.5 py-1.5 text-left outline-none transition-colors hover:bg-subtle focus-visible:ring-[3px] focus-visible:ring-ring/35',
                on && 'bg-primary/10 hover:bg-primary/15')}>
              <span className="truncate font-mono text-xs text-foreground">#{t}</span>
              <span className="h-1.5 rounded-full bg-subtle" aria-hidden="true">
                <span className={cn('block h-full rounded-full', BAR_TONE[accTone(v)])} style={{ width: `${v * 100}%` }} />
              </span>
              <span className="text-right text-xs tabular-nums text-muted-foreground">{s.passed}/{s.total}</span>
              <span className={cn('text-right text-xs font-medium tabular-nums', v < 0.7 ? 'text-destructive' : v < 0.9 ? 'text-warn' : 'text-foreground')}>{pctOf(v)}</span>
            </button>
          </li>
        )
      })}
    </ul>
  )
}

export const hasExtras = (c: EvalCase) => !!(c.turns?.length || c.judge || (c.attempts && c.attempts.length > 1) || c.steps?.some(s => s.trace?.length || s.expected))

/** Badges next to pass/fail: flaky, over the latency budget, judge mean or a judge that failed. */
export function CaseFlags({ c }: { c: EvalCase }) {
  return (
    <>
      {c.flaky && <Badge tone="warn" icon="refresh" title={attemptsTitle(c.attempts)}>flaky</Badge>}
      {c.over_budget && <Badge tone="warn" icon="timeout" title={`Took ${ms(c.ms)}, budget ${ms(c.max_ms)}`}>over budget</Badge>}
      {c.judge && <Badge tone={c.judge.mean >= 3.5 ? 'neutral' : 'bad'} title={`Judged by ${c.judge.engine}`}>judge {c.judge.mean.toFixed(1)}</Badge>}
      {c.judge_error && <Badge tone="bad" title={`The judge failed: ${c.judge_error}`}>not judged</Badge>}
      {c.split === 'holdout' && <Badge tone="info" title="Holdout case: scored only, never tuned against">holdout</Badge>}
      {c.pass === false && c.route_pass === false && <Badge tone="bad" icon="route" title="Agent, outcome, step or plan checks failed">route</Badge>}
      {c.pass === false && c.answer_pass === false && <Badge tone="bad" icon="answer" title="Answer, mention, file or judge checks failed">answer</Badge>}
    </>
  )
}

const attemptsTitle = (a: boolean[] | undefined) => (a?.length ? `Attempts: ${a.map(x => (x ? 'pass' : 'fail')).join(', ')}` : undefined)

/** Time with the budget beside it when the case has one. */
export function CaseTime({ c }: { c: EvalCase }) {
  return (
    <span className="inline-flex flex-col items-end">
      <span className={cn(c.over_budget && 'font-medium text-warn')}>{ms(c.ms)}</span>
      {c.max_ms != null && <span className="text-[11px] text-muted-foreground">of {ms(c.max_ms)}</span>}
    </span>
  )
}

/** What a case's kind adds, as a short label for the toggle. */
export function extrasLabel(c: EvalCase) {
  if (c.turns?.length) return `${c.turns.length} turns`
  if (c.judge) return 'Judge scores'
  if (c.steps?.length && !(c.attempts && c.attempts.length > 1)) return `${c.steps.length} step route${c.steps.length === 1 ? '' : 's'}`
  return `${c.attempts?.length ?? 0} attempts`
}

export function CaseExtras({ c }: { c: EvalCase }) {
  return (
    <div className="flex min-w-0 flex-col gap-4">
      {c.attempts && c.attempts.length > 1 && <Attempts attempts={c.attempts} flaky={!!c.flaky} />}
      {c.judge && <Judge j={c.judge} />}
      {!!c.turns?.length && <Turns turns={c.turns} />}
      {!!c.steps?.length && <StepRoutes steps={c.steps} />}
    </div>
  )
}

function Attempts({ attempts, flaky }: { attempts: boolean[]; flaky: boolean }) {
  const passed = attempts.filter(Boolean).length
  return (
    <div className="flex flex-wrap items-center gap-2 text-[13px]">
      <span className="text-xs text-muted-foreground">Attempts</span>
      <span className="flex gap-1" role="img" aria-label={attemptsTitle(attempts)}>
        {attempts.map((a, i) => (
          <span key={i} title={`Attempt ${i + 1}: ${a ? 'pass' : 'fail'}`}
            className={cn('grid size-5 place-items-center rounded-sm', a ? 'bg-ok/15 text-ok' : 'bg-destructive/15 text-destructive')}>
            <Icon name={a ? 'check' : 'close'} size={12} strokeWidth={2.4} />
          </span>
        ))}
      </span>
      <span className="tabular-nums text-muted-foreground">{passed} of {attempts.length} passed{flaky ? ', outcomes differ' : ''}</span>
    </div>
  )
}

const RUBRIC: Array<{ key: 'correct' | 'complete' | 'grounded' | 'concise'; label: string }> = [
  { key: 'correct', label: 'Correct' }, { key: 'complete', label: 'Complete' }, { key: 'grounded', label: 'Grounded' }, { key: 'concise', label: 'Concise' },
]

function Judge({ j }: { j: JudgeScore }) {
  return (
    <div className="flex min-w-0 flex-col gap-2">
      <span className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
        Judge scores <span className="text-sm font-semibold tabular-nums text-foreground">{j.mean.toFixed(1)}</span> mean of 5, by {j.engine}
      </span>
      <dl className="m-0 grid max-w-md grid-cols-[5rem_minmax(0,1fr)_1.5rem] items-center gap-x-3 gap-y-1.5">
        {RUBRIC.map(r => {
          const v = j[r.key]
          return (
            <div key={r.key} className="contents">
              <dt className="text-xs text-muted-foreground">{r.label}</dt>
              <dd className="m-0 flex gap-0.5" role="img" aria-label={`${r.label} ${v} of 5`}>
                {[1, 2, 3, 4, 5].map(n => <span key={n} className={cn('h-1.5 flex-1 rounded-full', n <= v ? (v >= 4 ? 'bg-ok' : v >= 3 ? 'bg-warn' : 'bg-destructive') : 'bg-subtle')} />)}
              </dd>
              <span className="m-0 text-right text-xs font-medium tabular-nums text-foreground">{v}</span>
            </div>
          )
        })}
      </dl>
      {j.note && <p className="m-0 max-w-[75ch] text-[13px] leading-snug text-foreground [overflow-wrap:anywhere]">{j.note}</p>}
    </div>
  )
}

function Turns({ turns }: { turns: EvalTurnResult[] }) {
  return (
    <ol className="m-0 flex list-none flex-col gap-0 p-0">
      {turns.map((t, i) => (
        <li key={i} className="relative flex min-w-0 gap-3 pb-3 last:pb-0">
          <span className="flex flex-col items-center">
            <span className={cn('grid size-5 shrink-0 place-items-center rounded-full text-[11px] font-semibold tabular-nums',
              t.pass ? 'bg-ok/15 text-ok' : 'bg-destructive/15 text-destructive')}>{i + 1}</span>
            {i < turns.length - 1 && <span className="w-px flex-1 bg-border" aria-hidden="true" />}
          </span>
          <div className="flex min-w-0 flex-1 flex-col gap-1.5">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <span className="min-w-0 text-[13px] font-medium leading-snug text-foreground [overflow-wrap:anywhere]">{t.query}</span>
              <span className="flex shrink-0 items-center gap-2">
                <span className="text-xs tabular-nums text-muted-foreground">{ms(t.ms)}</span>
                <Badge tone={t.pass ? 'ok' : 'bad'}>{t.pass ? 'pass' : 'fail'}</Badge>
                {t.qid != null && <a href={`#/runs/${t.qid}`} aria-label={`Open run ${t.qid}`} className="text-muted-foreground hover:text-foreground"><Icon name="external" size={13} /></a>}
              </span>
            </div>
            {t.agents.length > 0 && <span className="flex flex-wrap gap-1">{t.agents.map((a, k) => <AgentBadge key={k} agent={a} />)}</span>}
            {t.answer && <p className="m-0 line-clamp-4 text-[13px] leading-snug whitespace-pre-wrap text-muted-foreground [overflow-wrap:anywhere]">{t.answer}</p>}
            {t.reasons.length > 0 && (
              <ul className="m-0 list-disc pl-4 text-[13px] leading-snug text-destructive marker:text-muted-foreground">{t.reasons.map((r, k) => <li key={k}>{r}</li>)}</ul>
            )}
          </div>
        </li>
      ))}
    </ol>
  )
}

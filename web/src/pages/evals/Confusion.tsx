import type { EvalCase } from '../../protocol'
import { AgentIcon } from '../../icons'
import { cn } from '@/lib/utils'

// The routing confusion matrix (docs/PLAN-accuracy-v2.md D3): rows are the expected agent, columns the agent the step
// got. Shading is one hue by share of the row, so each row reads as "where did the steps that should go to X end up".
// Every non-empty cell links to the case list filtered to that pair.

/** Expected/got pairs a case contributes, the way the server counts them: steps with an expected agent, else the
 *  single-step case's first expected agent. */
export function casePairs(c: EvalCase): Array<[string, string]> {
  const steps = c.steps?.filter(s => s.expected) ?? []
  if (steps.length) return steps.map(s => [s.expected!, s.agent])
  if (!c.steps?.length && c.expect_agents?.length && (c.agents?.length ?? 0) <= 1) return [[c.expect_agents[0], c.agents?.[0] ?? 'none']]
  return []
}

export const pairHref = (id: string, expected: string, got: string) =>
  `#/evals/${id}?exp=${encodeURIComponent(expected)}&got=${encodeURIComponent(got)}`

export default function Confusion({ id, confusion, active }: {
  id: string; confusion: Record<string, Record<string, number>>; active?: [string, string] | null
}) {
  const rows = Object.keys(confusion).sort()
  const cols = [...new Set([...rows, ...rows.flatMap(r => Object.keys(confusion[r]))])]
    .sort((a, b) => Number(rows.includes(b)) - Number(rows.includes(a)) || a.localeCompare(b))
  if (!rows.length) return null
  const rowTotal = (r: string) => Object.values(confusion[r]).reduce((n, v) => n + v, 0)
  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-0.5 text-xs">
          <caption className="sr-only">Expected agent by row, agent the step got by column; each cell is a step count</caption>
          <thead>
            <tr>
              <th scope="col" className="px-1.5 pb-1 text-left align-bottom font-medium text-muted-foreground">expected ↓ got →</th>
              {cols.map(c => (
                <th key={c} scope="col" className="relative h-24 w-9 min-w-9 max-w-9 p-0 align-bottom font-medium text-muted-foreground">
                  <span className="absolute bottom-1.5 left-1/2 inline-flex origin-bottom-left -rotate-45 items-center gap-1 whitespace-nowrap">
                    <AgentIcon agent={c} size={12} tinted />{c}
                  </span>
                </th>
              ))}
              <th scope="col" className="px-2 pb-1 text-right align-bottom font-medium text-muted-foreground">Recall</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => {
              const total = rowTotal(r)
              const hit = confusion[r][r] ?? 0
              return (
                <tr key={r}>
                  <th scope="row" className="whitespace-nowrap px-1.5 text-left font-medium text-foreground">
                    <span className="inline-flex items-center gap-1"><AgentIcon agent={r} size={12} tinted />{r}</span>
                  </th>
                  {cols.map(c => {
                    const n = confusion[r][c] ?? 0
                    const share = total ? n / total : 0
                    const on = active?.[0] === r && active?.[1] === c
                    const label = `expected ${r}, got ${c}: ${n} step${n === 1 ? '' : 's'} (${Math.round(share * 100)}% of ${r})`
                    const cls = cn('grid size-9 place-items-center rounded-[4px] tabular-nums', r === c ? 'font-semibold' : '',
                      share >= 0.5 ? 'text-primary-foreground' : 'text-foreground', on && 'ring-2 ring-foreground')
                    const bg = n ? { background: `color-mix(in oklab, var(--accent) ${Math.round(12 + share * 88)}%, transparent)` } : undefined
                    return (
                      <td key={c} className="p-0">
                        {n ? (
                          <a href={pairHref(id, r, c)} title={`${label}. Show these cases`} aria-label={label} style={bg}
                            className={cn(cls, 'outline-none hover:ring-2 hover:ring-edge focus-visible:ring-[3px] focus-visible:ring-ring/35')}>{n}</a>
                        ) : <span className={cn(cls, 'bg-subtle/60 text-muted-foreground/50')} title={label}>·</span>}
                      </td>
                    )
                  })}
                  <td className="px-2 text-right tabular-nums text-muted-foreground">{total ? `${Math.round((hit / total) * 100)}%` : 'n/a'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="m-0 text-xs text-muted-foreground">Stronger cells hold more of the row's steps. The diagonal is right; anything off it went to the wrong agent. Click a cell to list its cases.</p>
    </div>
  )
}

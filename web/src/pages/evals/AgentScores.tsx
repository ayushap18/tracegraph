import type { AgentPR } from '../../protocol'
import { AgentBadge } from '../../components/app'
import { cn } from '@/lib/utils'

// Per-agent precision, recall and F1 over routed steps (docs/PLAN-accuracy-v2.md D3), weakest F1 first.

const num = (v: number | null) => (v == null ? 'n/a' : v.toFixed(2))
const tone = (v: number | null) => (v == null ? 'text-muted-foreground' : v >= 0.9 ? 'text-foreground' : v >= 0.7 ? 'text-warn' : 'text-destructive')

export default function AgentScores({ rows }: { rows: AgentPR[] }) {
  if (!rows.length) return null
  const sorted = [...rows].sort((a, b) => (a.f1 ?? 2) - (b.f1 ?? 2) || b.support - a.support || a.agent.localeCompare(b.agent))
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[34rem] border-collapse text-[13px]">
        <thead>
          <tr className="text-xs text-muted-foreground">
            <th scope="col" className="py-1.5 pr-3 text-left font-medium">Agent</th>
            <th scope="col" className="py-1.5 pr-3 text-left font-medium">F1</th>
            <th scope="col" className="py-1.5 pr-3 text-right font-medium" title="Of the steps routed here, the share that belonged here">Precision</th>
            <th scope="col" className="py-1.5 pr-3 text-right font-medium" title="Of the steps that belonged here, the share routed here">Recall</th>
            <th scope="col" className="py-1.5 pr-3 text-right font-medium" title="Right, wrongly routed here, and missed">TP / FP / FN</th>
            <th scope="col" className="py-1.5 text-right font-medium" title="Steps expected to go to this agent">Support</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map(r => (
            <tr key={r.agent} className="border-t border-border">
              <td className="py-1.5 pr-3"><AgentBadge agent={r.agent} /></td>
              <td className="py-1.5 pr-3">
                <span className="flex items-center gap-2" title={r.f1 == null ? 'No steps to score' : `F1 ${num(r.f1)}`}>
                  <span className="h-1.5 w-20 rounded-full bg-subtle" aria-hidden="true">
                    <span className={cn('block h-full rounded-full', r.f1 == null ? '' : r.f1 >= 0.9 ? 'bg-ok' : r.f1 >= 0.7 ? 'bg-warn' : 'bg-destructive')}
                      style={{ width: `${(r.f1 ?? 0) * 100}%` }} />
                  </span>
                  <span className={cn('tabular-nums font-medium', tone(r.f1))}>{num(r.f1)}</span>
                </span>
              </td>
              <td className={cn('py-1.5 pr-3 text-right tabular-nums', tone(r.precision))}>{num(r.precision)}</td>
              <td className={cn('py-1.5 pr-3 text-right tabular-nums', tone(r.recall))}>{num(r.recall)}</td>
              <td className="py-1.5 pr-3 text-right tabular-nums text-muted-foreground">{r.tp} / {r.fp} / {r.fn}</td>
              <td className="py-1.5 text-right tabular-nums text-muted-foreground">{r.support}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

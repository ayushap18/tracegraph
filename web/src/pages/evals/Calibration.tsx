import type { Calibration as Cal } from '../../protocol'
import { cn } from '@/lib/utils'

// Reliability diagrams for Jev's confidence and clarity scores (docs/PLAN-accuracy-v2.md D4). Each bar is one bin of
// steps: its height is how often those steps were right, its position how sure Jev was. On the dashed diagonal the
// score means what it says; bars below it are over-confident. ECE is the step-weighted gap.

const TITLE: Record<Cal['signal'], { label: string; right: string }> = {
  confidence: { label: 'Confidence', right: 'the final agent was the expected one' },
  clear: { label: 'Clear', right: 'the step was not sent to clarify when an answer was expected' },
}

const W = 240, H = 180, PAD = { l: 30, r: 8, t: 8, b: 26 }
const X = (v: number) => PAD.l + v * (W - PAD.l - PAD.r)
const Y = (v: number) => H - PAD.b - v * (H - PAD.t - PAD.b)

function Diagram({ cal }: { cal: Cal }) {
  const t = TITLE[cal.signal] ?? { label: cal.signal, right: 'right' }
  const total = cal.bins.reduce((n, b) => n + b.n, 0)
  return (
    <figure className="m-0 flex min-w-0 flex-col gap-1.5">
      <figcaption className="flex flex-wrap items-baseline justify-between gap-2 text-[13px]">
        <span className="font-medium text-foreground">{t.label}</span>
        <span className="text-xs tabular-nums text-muted-foreground">ECE <span className={cn('font-medium', cal.ece == null ? '' : cal.ece <= 0.05 ? 'text-foreground' : cal.ece <= 0.1 ? 'text-warn' : 'text-destructive')}>{cal.ece == null ? 'n/a' : cal.ece.toFixed(3)}</span> over {total} steps</span>
      </figcaption>
      <svg viewBox={`0 0 ${W} ${H}`} className="h-auto w-full max-w-[22rem]" role="img"
        aria-label={`${t.label} reliability: ${cal.bins.filter(b => b.n).map(b => `${b.lo.toFixed(1)} to ${b.hi.toFixed(1)}, ${b.n} steps, ${b.accuracy == null ? 'no' : Math.round(b.accuracy * 100) + '%'} right`).join('; ')}`}>
        {[0, 0.5, 1].map(v => (
          <g key={v}>
            <line x1={X(0)} x2={X(1)} y1={Y(v)} y2={Y(v)} stroke="var(--line)" strokeWidth={1} />
            <text x={PAD.l - 5} y={Y(v) + 3} textAnchor="end" fontSize={9} fill="var(--muted)">{v}</text>
            <text x={X(v)} y={H - PAD.b + 12} textAnchor="middle" fontSize={9} fill="var(--muted)">{v}</text>
          </g>
        ))}
        <text x={X(0.5)} y={H - 3} textAnchor="middle" fontSize={9} fill="var(--muted)">score</text>
        {cal.bins.map((b, i) => {
          if (!b.n || b.accuracy == null) return null
          const x0 = X(b.lo) + 1, x1 = X(b.hi) - 1
          const y = Y(b.accuracy)
          return (
            <g key={i}>
              <title>{`${b.lo.toFixed(1)} to ${b.hi.toFixed(1)}: ${b.n} step${b.n === 1 ? '' : 's'}, mean score ${b.mean.toFixed(2)}, ${Math.round(b.accuracy * 100)}% ${t.right}`}</title>
              <rect x={x0} y={y} width={Math.max(1, x1 - x0)} height={Math.max(0, Y(0) - y)} rx={2}
                fill="var(--accent)" fillOpacity={0.3 + 0.7 * Math.min(1, b.n / Math.max(1, total / 4))} />
              <rect x={X(b.lo)} y={PAD.t} width={X(b.hi) - X(b.lo)} height={Y(0) - PAD.t} fill="transparent" />
            </g>
          )
        })}
        <line x1={X(0)} y1={Y(0)} x2={X(1)} y2={Y(1)} stroke="var(--ink)" strokeOpacity={0.5} strokeWidth={1.5} strokeDasharray="4 3" />
      </svg>
      <table className="sr-only">
        <caption>{t.label} bins</caption>
        <thead><tr><th>From</th><th>To</th><th>Steps</th><th>Mean score</th><th>Right</th></tr></thead>
        <tbody>{cal.bins.map((b, i) => <tr key={i}><td>{b.lo}</td><td>{b.hi}</td><td>{b.n}</td><td>{b.mean}</td><td>{b.accuracy ?? 'n/a'}</td></tr>)}</tbody>
      </table>
    </figure>
  )
}

export default function Calibration({ items }: { items: Cal[] }) {
  if (!items.length) return null
  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-1 gap-6 sm:grid-cols-2">
        {items.map(c => <Diagram key={c.signal} cal={c} />)}
      </div>
      <p className="m-0 text-xs text-muted-foreground">Bars are how often steps in each score bin were right; fainter bars hold fewer steps. The dashed line is a perfectly calibrated score.</p>
    </div>
  )
}

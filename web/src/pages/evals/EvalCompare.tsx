import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { compareEvals, errorText, listEvals } from '../../api'
import type { EvalCompare as Compare, EvalCompareCase, EvalCompareSide, EvalSummary } from '../../protocol'
import { Badge, Button, Card, EmptyState, IconButton, Skeleton, navigate } from '../../ui'
import { BackLink, EngineBadge, PageBody } from '../../components/app'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'
import { CHIP, compareHref, pctOf, when } from './shared'

// Two eval runs side by side (GET /api/evals/compare): headline numbers with deltas, then every case with the
// flips highlighted. "Fixed" passes in B but failed in A; "broken" is the reverse. Usually A = examples off, B = on.

type Flip = 'fixed' | 'broken' | null
type CaseFilter = 'all' | 'flips' | 'fixed' | 'broken'
const flipOf = (c: EvalCompareCase): Flip => (c.a_pass === false && c.b_pass === true ? 'fixed' : c.a_pass === true && c.b_pass === false ? 'broken' : null)

export default function EvalCompare({ a, b }: { a: string; b: string }) {
  const [data, setData] = useState<Compare | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [evals, setEvals] = useState<EvalSummary[]>([])
  const [filter, setFilter] = useState<CaseFilter>('all')

  useEffect(() => {
    let alive = true
    setData(null); setError(null)
    compareEvals(a, b).then(d => { if (alive) setData(d) }, e => { if (alive) setError(errorText(e)) })
    return () => { alive = false }
  }, [a, b])
  useEffect(() => { listEvals().then(r => setEvals(r.evals.slice().sort((x, y) => y.at - x.at)), () => undefined) }, [])

  const flips = useMemo(() => {
    const cases = data?.cases ?? []
    const fixed = cases.filter(c => flipOf(c) === 'fixed').length
    const broken = cases.filter(c => flipOf(c) === 'broken').length
    return { fixed, broken, total: fixed + broken }
  }, [data])
  const shown = (data?.cases ?? []).filter(c => {
    const f = flipOf(c)
    return filter === 'all' || (filter === 'flips' ? f != null : f === filter)
  })
  const byId = useMemo(() => Object.fromEntries(evals.map(e => [e.eval_id, e])), [evals])

  return (
    <PageBody>
      <BackLink href="#/evals">All evals</BackLink>
      <Card title="Compare evals" icon="compare" subtitle="Pick two runs. B is compared against A.">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <RunSelect id="cmp-a" label="A" value={a} evals={evals} onChange={v => navigate(compareHref(v, b))} />
          <IconButton icon="compare" label="Swap A and B" onClick={() => navigate(compareHref(b, a))} className="self-center" />
          <RunSelect id="cmp-b" label="B" value={b} evals={evals} onChange={v => navigate(compareHref(a, v))} />
        </div>
      </Card>

      {error ? (
        <Card><EmptyState icon="compare" title="Could not compare these evals" text={error}
          action={<Button variant="secondary" onClick={() => navigate('/evals')}>Back to evals</Button>} /></Card>
      ) : !data ? (
        <div className="flex flex-col gap-6" aria-busy="true">
          <Skeleton height={180} radius={12} />
          <Skeleton height={240} radius={12} />
        </div>
      ) : (
        <>
          <Card flush title="Summary" icon="trend" aria-label="Summary">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[19rem] border-collapse text-[13px]">
                <thead>
                  <tr className="border-b border-border text-left text-xs text-muted-foreground">
                    <th className="px-3 py-2 sm:px-4 font-medium sm:pl-5">Metric</th>
                    <SideHead tag="A" side={data.a} at={byId[data.a.eval_id]?.at} />
                    <SideHead tag="B" side={data.b} at={byId[data.b.eval_id]?.at} />
                    <th className="px-3 py-2 sm:px-4 text-right font-medium sm:pr-5">Change</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  <MetricRow label="Accuracy" a={pctOf(data.a.accuracy)} b={pctOf(data.b.accuracy)}
                    delta={pts(data.b.accuracy - data.a.accuracy)} good={data.b.accuracy - data.a.accuracy} />
                  <MetricRow label="Passed" a={`${data.a.passed}/${data.a.total}`} b={`${data.b.passed}/${data.b.total}`}
                    delta={signed(data.b.passed - data.a.passed)} good={data.b.passed - data.a.passed} />
                  <MetricRow label="Silent wrong" a={data.a.silent_wrong} b={data.b.silent_wrong}
                    delta={signed(data.b.silent_wrong - data.a.silent_wrong)} good={data.a.silent_wrong - data.b.silent_wrong} />
                  <MetricRow label="Mean Jev tokens" a={Math.round(data.a.mean_jev_tokens).toLocaleString()} b={Math.round(data.b.mean_jev_tokens).toLocaleString()}
                    delta={tokenDelta(data.a.mean_jev_tokens, data.b.mean_jev_tokens)} good={tokenGood(data.a.mean_jev_tokens, data.b.mean_jev_tokens)} />
                </tbody>
              </table>
            </div>
            <p className="m-0 border-t border-border px-4 py-2.5 text-xs text-muted-foreground sm:px-5">
              {flips.total === 0 ? 'No case changed result.' : `${flips.fixed} fixed and ${flips.broken} broken in B.`} Token change above 30% is flagged.
            </p>
          </Card>

          <Card flush title="Cases" icon="evals" aria-label="Cases">
            <div className="flex flex-wrap items-center gap-2 border-b border-border px-4 py-3 sm:px-5">
              <ToggleGroup type="single" value={filter} onValueChange={v => { if (v) setFilter(v as CaseFilter) }} spacing={1} aria-label="Filter cases" className="flex-wrap">
                <ToggleGroupItem value="all" className={CHIP}>All<span className="tabular-nums text-muted-foreground">{data.cases.length}</span></ToggleGroupItem>
                <ToggleGroupItem value="flips" className={CHIP}>Flips only<span className="tabular-nums text-muted-foreground">{flips.total}</span></ToggleGroupItem>
                <ToggleGroupItem value="fixed" className={CHIP}>Fixed in B<span className="tabular-nums text-muted-foreground">{flips.fixed}</span></ToggleGroupItem>
                <ToggleGroupItem value="broken" className={CHIP}>Broken in B<span className="tabular-nums text-muted-foreground">{flips.broken}</span></ToggleGroupItem>
              </ToggleGroup>
            </div>
            {shown.length === 0
              ? <EmptyState icon="search" title={filter === 'all' ? 'No cases' : 'No flips here'} text={filter === 'all' ? 'Neither run recorded any cases.' : 'Both runs agree on every case in this filter.'} />
              : (
                <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">
                  <li className="hidden grid-cols-[minmax(0,1fr)_4.5rem_4.5rem_6.5rem] gap-3 px-5 py-2 text-xs font-medium text-muted-foreground md:grid" aria-hidden="true">
                    <span>Case</span><span>A</span><span>B</span><span className="text-right">Change</span>
                  </li>
                  {shown.map(c => <CaseLine key={c.id} c={c} />)}
                </ul>
              )}
          </Card>
        </>
      )}
    </PageBody>
  )
}

const signed = (n: number) => (n > 0 ? `+${n}` : n === 0 ? '0' : String(n))
const pts = (d: number) => `${d > 0 ? '+' : ''}${Math.round(d * 100)} pts`
const tokenDelta = (a: number, b: number) => (a > 0 ? `${b >= a ? '+' : ''}${Math.round(((b - a) / a) * 100)}%` : signed(Math.round(b - a)))
// Fewer tokens is fine; a small rise is neutral; more than 30% is the decision gate's limit, so it reads as bad.
const tokenGood = (a: number, b: number) => (a > 0 && (b - a) / a > 0.3 ? -1 : 0)

function SideHead({ tag, side, at }: { tag: string; side: EvalCompareSide; at?: number }) {
  return (
    <th className="px-3 py-2 sm:px-4 align-bottom font-medium">
      <span className="flex flex-col gap-1">
        <span className="flex flex-wrap items-center gap-1.5">
          <a href={`#/evals/${side.eval_id}`} className="font-semibold text-foreground underline-offset-2 hover:underline">{tag}</a>
          <Badge tone={side.examples ? 'accent' : 'neutral'}>examples {side.examples ? 'on' : 'off'}</Badge>
        </span>
        <EngineBadge name={side.engine ?? 'none'} label={side.engine ?? 'keyless'} className="text-xs" />
        {at != null && <span className="hidden text-xs font-normal tabular-nums sm:block">{when(at)}</span>}
      </span>
    </th>
  )
}

function MetricRow({ label, a, b, delta, good }: { label: string; a: ReactNode; b: ReactNode; delta: string; good: number }) {
  return (
    <tr>
      <td className="px-3 py-2.5 sm:px-4 text-muted-foreground sm:pl-5">{label}</td>
      <td className="px-3 py-2.5 sm:px-4 tabular-nums text-foreground">{a}</td>
      <td className="px-3 py-2.5 sm:px-4 tabular-nums text-foreground">{b}</td>
      <td className={cn('px-3 py-2.5 sm:px-4 text-right font-medium tabular-nums sm:pr-5',
        good > 0 ? 'text-ok' : good < 0 ? 'text-destructive' : 'text-muted-foreground')}>{delta}</td>
    </tr>
  )
}

function Result({ pass, side }: { pass: boolean | null; side: string }) {
  if (pass == null) return <Badge title={`Not in ${side}`}>none</Badge>
  return <Badge tone={pass ? 'ok' : 'bad'} icon={pass ? 'success' : 'error'}>{pass ? 'pass' : 'fail'}</Badge>
}

function CaseLine({ c }: { c: EvalCompareCase }) {
  const f = flipOf(c)
  return (
    <li className={cn('grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-2 px-4 py-2.5 sm:px-5 md:grid-cols-[minmax(0,1fr)_4.5rem_4.5rem_6.5rem] md:items-center',
      f === 'fixed' && 'bg-ok/[0.05]', f === 'broken' && 'bg-destructive/[0.05]')}>
      <span className="flex min-w-0 flex-col">
        <span className="text-[13px] font-medium leading-snug text-foreground [overflow-wrap:anywhere]">{c.query}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{c.id}</span>
      </span>
      <span className="col-start-1 flex items-center gap-2 md:contents">
        <span className="flex items-center gap-1 md:block"><span className="text-xs text-muted-foreground md:hidden">A</span><Result pass={c.a_pass} side="A" /></span>
        <span className="flex items-center gap-1 md:block"><span className="text-xs text-muted-foreground md:hidden">B</span><Result pass={c.b_pass} side="B" /></span>
      </span>
      <span className="col-start-2 row-span-2 row-start-1 self-start text-right md:col-auto md:row-auto md:self-center">
        {f === 'fixed' ? <Badge tone="ok" icon="arrow-up">fixed in B</Badge>
          : f === 'broken' ? <Badge tone="bad" icon="arrow-down">broken in B</Badge>
          : <span className="text-xs text-muted-foreground">same</span>}
      </span>
    </li>
  )
}

function RunSelect({ id, label, value, evals, onChange }: { id: string; label: string; value: string; evals: EvalSummary[]; onChange: (v: string) => void }) {
  const known = evals.some(e => e.eval_id === value)
  return (
    <div className="flex min-w-0 flex-1 items-center gap-2">
      <label htmlFor={id} className="w-4 shrink-0 text-sm font-semibold text-foreground">{label}</label>
      <Select value={value} onValueChange={onChange}>
        <SelectTrigger id={id} className="h-9 w-full min-w-0 bg-background"><SelectValue placeholder="Pick a run" /></SelectTrigger>
        <SelectContent position="popper">
          {!known && <SelectItem value={value}>{value}</SelectItem>}
          {evals.map(e => (
            <SelectItem key={e.eval_id} value={e.eval_id}>
              {when(e.at)}, {e.engine ?? 'keyless'}{e.examples ? ', examples' : ''}, {pctOf(e.accuracy)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  )
}

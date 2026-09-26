import { Icon } from '../../icons'
import { money } from '../../lib'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { engineLabelOf } from './EngineSelect'
import type { EngineInfo, Prices, Run, UsageMeterProps } from './types'

// Tokens and an estimated cost for the sandbox runs on screen. Jev tokens are always priced; LLM tokens only for
// engines billed per token ('api'). Subscription engines and keyless runs cost nothing per call.

export interface RunUsage {
  qid: number
  engine: string | null
  running: boolean
  jev_in: number
  llm_in: number
  llm_out: number
  billed: boolean // LLM tokens are priced (api engine)
  cost: number
}

export function runUsage(run: Run, prices: Prices, engines: EngineInfo[]): RunUsage {
  const t = run.tokens
  const info = run.engine ? engines.find(e => e.name === run.engine) : undefined
  const billed = info?.billing === 'api'
  const jev_in = t?.jev_in ?? 0, llm_in = t?.llm_in ?? 0, llm_out = t?.llm_out ?? 0
  const cost = jev_in * prices.jev_in / 1e6 + (billed ? llm_in * prices.claude_in / 1e6 + llm_out * prices.claude_out / 1e6 : 0)
  return { qid: run.qid, engine: run.engine, running: !run.done, jev_in, llm_in, llm_out, billed, cost }
}

export function usageTotals(runs: Run[], prices: Prices, engines: EngineInfo[]) {
  const rows = runs.map(r => runUsage(r, prices, engines))
  const sum = (k: 'jev_in' | 'llm_in' | 'llm_out' | 'cost') => rows.reduce((a, r) => a + r[k], 0)
  const jev_in = sum('jev_in'), llm_in = sum('llm_in'), llm_out = sum('llm_out')
  return { rows, jev_in, llm_in, llm_out, tokens: jev_in + llm_in + llm_out, cost: sum('cost'), running: rows.filter(r => r.running).length }
}

const n = (v: number) => v.toLocaleString()
const price = (v: number) => '$' + v.toLocaleString(undefined, { maximumFractionDigits: 4 })

export function UsageMeter({ runs, prices, engines }: UsageMeterProps) {
  const u = usageTotals(runs, prices, engines)

  return (
    <Popover>
      <PopoverTrigger asChild>
        <button type="button" data-slot="button" aria-label={`Usage: ${n(u.tokens)} tokens, estimated ${money(u.cost)}. Show breakdown`}
          className="inline-flex h-8 min-w-0 shrink-0 cursor-pointer items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 text-[13px] text-muted-foreground transition-colors outline-none hover:bg-subtle hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35">
          <Icon name="spend" size={14} className="shrink-0" />
          <span className="truncate tabular-nums">{n(u.tokens)} tokens, {money(u.cost)}</span>
          {u.running > 0 && <Icon name="spinner" size={12} className="shrink-0 animate-spin text-primary" aria-hidden="true" />}
        </button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-[min(420px,calc(100vw-32px))] rounded-lg p-0">
        <div className="flex flex-col">
          <div className="flex flex-col gap-0.5 border-b border-border px-4 py-3">
            <h3 className="m-0 text-sm font-semibold text-foreground">Usage in this sandbox</h3>
            <p className="m-0 text-xs tabular-nums text-muted-foreground">
              {u.rows.length} {u.rows.length === 1 ? 'run' : 'runs'}{u.running > 0 ? `, ${u.running} running` : ''}, estimated {money(u.cost)}
            </p>
          </div>
          {u.rows.length === 0 ? (
            <p className="m-0 px-4 py-6 text-center text-[13px] text-muted-foreground">No runs yet. Token counts appear when a run finishes.</p>
          ) : (
            <div className="max-h-[280px] overflow-auto">
              <table className="w-full border-collapse text-[13px] tabular-nums">
                <caption className="sr-only">Tokens and estimated cost per run</caption>
                <thead className="sticky top-0 bg-popover">
                  <tr className="border-b border-border text-xs text-muted-foreground">
                    <th scope="col" className="px-4 py-2 text-left font-medium">Run</th>
                    <th scope="col" className="px-2 py-2 text-right font-medium">Jev in</th>
                    <th scope="col" className="px-2 py-2 text-right font-medium">LLM in</th>
                    <th scope="col" className="px-2 py-2 text-right font-medium">LLM out</th>
                    <th scope="col" className="px-4 py-2 text-right font-medium">Cost</th>
                  </tr>
                </thead>
                <tbody>
                  {u.rows.map(r => (
                    <tr key={r.qid} className="border-b border-border last:border-b-0">
                      <th scope="row" className="max-w-[140px] px-4 py-1.5 text-left font-normal">
                        <span className="flex min-w-0 flex-col">
                          <span className="font-mono text-xs text-foreground">#{r.qid}</span>
                          <span className="truncate text-xs text-muted-foreground">
                            {engineLabelOf(engines, r.engine)}{r.running ? ', running' : ''}
                          </span>
                        </span>
                      </th>
                      <td className={cell(r.running)}>{n(r.jev_in)}</td>
                      <td className={cell(r.running)}>{n(r.llm_in)}</td>
                      <td className={cell(r.running)}>{n(r.llm_out)}</td>
                      <td className={cn(cell(r.running), 'pr-4')} title={!r.billed && (r.llm_in || r.llm_out) ? 'LLM tokens run on your plan: $0 per call' : undefined}>
                        {money(r.cost)}
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot className="sticky bottom-0 bg-popover">
                  <tr className="border-t border-border font-medium text-foreground">
                    <th scope="row" className="px-4 py-2 text-left">Total</th>
                    <td className="px-2 py-2 text-right">{n(u.jev_in)}</td>
                    <td className="px-2 py-2 text-right">{n(u.llm_in)}</td>
                    <td className="px-2 py-2 text-right">{n(u.llm_out)}</td>
                    <td className="px-4 py-2 text-right">{money(u.cost)}</td>
                  </tr>
                </tfoot>
              </table>
            </div>
          )}
          <p className="m-0 border-t border-border px-4 py-2.5 text-xs leading-relaxed text-muted-foreground">
            Estimate only. Jev tokens at {price(prices.jev_in)} per 1M; LLM tokens at {price(prices.claude_in)} in and {price(prices.claude_out)} out per 1M on
            pay-per-token engines. Subscription engines show $0 because they run on your plan, and keyless runs use no LLM.
            Running runs count as 0 until they finish.
          </p>
        </div>
      </PopoverContent>
    </Popover>
  )
}

export default UsageMeter

const cell = (running: boolean) => cn('px-2 py-1.5 text-right', running ? 'text-muted-foreground' : 'text-foreground')

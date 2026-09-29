import { useCallback, useRef, useState, type ReactNode } from 'react'
import type { Estimate, EstimateCall, EstimatePhase } from '../../protocol'
import { Button } from '../../ui'
import { Icon } from '../../icons'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { cn } from '@/lib/utils'

// Cost preflight (docs/PLAN-files-robust.md 6.1): the server answers a costly /ask or resume with 409 and an Estimate
// instead of starting it. This dialog shows that estimate and lets the user go ahead, switch to a cheaper engine or
// cancel. Nothing has started while it is open. The estimate is made without calling any model.

/** Tokens rounded to 2 significant figures: 243,700 gives "240,000". */
export function aboutTokens(n: number) {
  if (!Number.isFinite(n) || n <= 0) return '0'
  return Number(n.toPrecision(2)).toLocaleString()
}

/** "40 to 90 seconds" under two minutes, else "3 to 6 minutes". */
export function timeRange(lo: number, hi: number) {
  const a = Math.max(0, lo), b = Math.max(a, hi)
  if (b < 120) {
    const x = Math.max(1, Math.round(a)), y = Math.max(x, Math.round(b))
    return x === y ? `about ${x} seconds` : `${x} to ${y} seconds`
  }
  const x = Math.max(1, Math.round(a / 60)), y = Math.max(x, Math.ceil(b / 60))
  return x === y ? `about ${x} minute${x === 1 ? '' : 's'}` : `${x} to ${y} minutes`
}

/** One duration: "45 seconds" or "4 minutes". */
export function duration(s: number) {
  if (!Number.isFinite(s) || s < 0) return '-'
  if (s < 120) return `${Math.max(1, Math.round(s))} seconds`
  const m = Math.round(s / 60)
  return `${m} minute${m === 1 ? '' : 's'}`
}

const dollars = (v: number) => (v < 0.01 ? 'under $0.01' : '$' + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }))

/** Mid tokens of an estimate (in plus out). */
export const estTokens = (e: Pick<Estimate, 'tokens_in' | 'tokens_out'>) => (e.tokens_in || 0) + (e.tokens_out || 0)

const FILE_PHASES: ReadonlyArray<string> = ['outline', 'sections', 'topup', 'repair', 'critic']

/** What each estimated step is, in plain words (every EstimatePhase has one). */
export const PHASE_LABEL: Record<EstimatePhase, string> = {
  planner: 'Planning the steps', research: 'Research', answer: 'Answer', outline: 'File outline',
  sections: 'Writing sections', topup: 'Top-up if short', repair: 'Repair if a section fails', merge: 'Combining answers',
  critic: 'Design critic',
}

/** The breakdown lines, one per phase and engine, in the order the server sent them. */
function Steps({ calls }: { calls: EstimateCall[] }) {
  if (calls.length < 1) return null
  return (
    <div className="flex flex-col gap-1.5">
      <h3 className="m-0 text-[13px] font-semibold text-foreground">Steps</h3>
      <ul className="m-0 flex flex-col gap-1 pl-0 text-[13px] leading-relaxed text-muted-foreground">
        {calls.map((c, i) => (
          <li key={c.phase + c.engine + i} className="flex flex-wrap items-baseline justify-between gap-x-3">
            <span className="text-foreground">
              {PHASE_LABEL[c.phase] ?? c.phase}{c.calls > 1 ? ` (${c.calls} calls)` : ''}{c.optional ? ', only if needed' : ''}
            </span>
            <Sub>about {aboutTokens((c.tokens_in || 0) + (c.tokens_out || 0))} tokens</Sub>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** Mid tokens of the file's own calls (outline, sections, top-up, repair): what a file card compares with the tokens
 *  the file used. The planner, research and merge calls belong to the run, not the file. */
export function fileTokens(e: Estimate) {
  const rows = (e.breakdown ?? []).filter(c => !c.optional && FILE_PHASES.includes(c.phase))
  return rows.length ? rows.reduce((n, c) => n + (c.tokens_in || 0) + (c.tokens_out || 0), 0) : estTokens(e)
}

/** Several engines answering one ask side by side, as the server's estimate.combine: calls and tokens add up, time
 *  is the slowest run's. No cheaper engine is offered for a side-by-side ask. */
export function combineEstimates(list: Estimate[]): Estimate {
  if (list.length === 1) return { ...list[0], cheaper: [] }
  const sum = (f: (e: Estimate) => number) => list.reduce((n, e) => n + (f(e) || 0), 0)
  const max = (f: (e: Estimate) => number) => Math.max(...list.map(f))
  const engines = new Set(list.map(e => e.engine).filter(Boolean))
  const billing = new Set(list.map(e => e.billing).filter(Boolean))
  const labels = [...new Set(list.map(e => e.engine_label).filter((x): x is string => !!x))]
  const reasons = [...new Map(list.flatMap(e => e.reasons ?? []).map(r => [r.code, r])).values()]
  const out: Estimate = {
    version: 1, calls: sum(e => e.calls), tokens_in: sum(e => e.tokens_in), tokens_out: sum(e => e.tokens_out),
    seconds: max(e => e.seconds),
    range: {
      calls: [sum(e => e.range.calls[0]), sum(e => e.range.calls[1])],
      tokens_in: [sum(e => e.range.tokens_in[0]), sum(e => e.range.tokens_in[1])],
      tokens_out: [sum(e => e.range.tokens_out[0]), sum(e => e.range.tokens_out[1])],
      seconds: [max(e => e.range.seconds[0]), max(e => e.range.seconds[1])],
    },
    engine: engines.size === 1 ? [...engines][0] : null,
    engine_label: engines.size === 1 ? labels[0] ?? null : null,
    billing: billing.size === 1 ? [...billing][0] : null,
    dollars: list.some(e => e.dollars) ? [sum(e => e.dollars?.[0] ?? 0), sum(e => e.dollars?.[1] ?? 0)] : null,
    keyless: list.every(e => e.keyless), long_file: list.some(e => e.long_file),
    needs_confirmation: list.some(e => e.needs_confirmation),
    reasons: [...reasons, { code: 'several_engines', text: `${list.length} engines each answer the same question.` }],
    breakdown: list.flatMap(e => e.breakdown ?? []), deadline_s: max(e => e.deadline_s), cheaper: [], summary: '',
  }
  const tokens = aboutTokens(estTokens(out))
  out.summary = `These ${list.length} answers need about ${out.calls} model call${out.calls === 1 ? '' : 's'}${labels.length ? ' on ' + labels.join(', ') : ''}: roughly ${tokens} tokens and ${timeRange(out.range.seconds[0], out.range.seconds[1])}.`
  return out
}

/** A plain sentence for the dialog when the server sent none. */
function fallbackSummary(e: Estimate) {
  const calls = `${e.calls} model call${e.calls === 1 ? '' : 's'}`
  const on = e.engine_label ? ` on ${e.engine_label}` : ''
  return `This needs about ${calls}${on}: roughly ${aboutTokens(estTokens(e))} tokens and ${timeRange(e.range.seconds[0], e.range.seconds[1])}.`
}

export interface CostDialogProps {
  estimate: Estimate
  onContinue: () => void
  onCancel: () => void
  onSwitch: (engine: string) => void
  remember: boolean
  /** Omit to hide "Don't ask again in this chat" (pages with no chat to remember it for). */
  onRemember?: (v: boolean) => void
}

export function CostDialog({ estimate: e, onContinue, onCancel, onSwitch, remember, onRemember }: CostDialogProps) {
  const go = useRef<HTMLButtonElement>(null)
  const r = e.range
  const low = (r?.tokens_in?.[0] ?? e.tokens_in) + (r?.tokens_out?.[0] ?? e.tokens_out)
  const high = (r?.tokens_in?.[1] ?? e.tokens_in) + (r?.tokens_out?.[1] ?? e.tokens_out)
  const callsHigh = r?.calls?.[1] ?? e.calls
  const secLo = r?.seconds?.[0] ?? e.seconds, secHi = r?.seconds?.[1] ?? e.seconds
  const cheaper = e.cheaper?.[0]
  const nearDeadline = e.deadline_s > 0 && secHi >= 0.8 * e.deadline_s
  const billing = e.billing === 'subscription' ? 'uses your subscription'
    : e.billing === 'api' && e.dollars ? `${dollars(e.dollars[0])} to ${dollars(e.dollars[1])}` : null

  return (
    <Dialog open onOpenChange={o => { if (!o) onCancel() }}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-[520px]"
        onOpenAutoFocus={ev => { ev.preventDefault(); go.current?.focus() }}>
        <DialogHeader>
          <DialogTitle>This will take several model calls</DialogTitle>
          <DialogDescription className="text-pretty">{e.summary || fallbackSummary(e)}</DialogDescription>
        </DialogHeader>

        <dl className="m-0 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-2.5 rounded-md border border-border bg-subtle/40 p-3 text-sm">
          <Row label="Model calls">
            <span className="tabular-nums">{e.calls.toLocaleString()}</span>
            {callsHigh > e.calls && <Sub>up to {callsHigh.toLocaleString()}</Sub>}
          </Row>
          <Row label="Tokens">
            <span className="tabular-nums">about {aboutTokens(estTokens(e))}</span>
            {high > low && <Sub>from {aboutTokens(low)} to {aboutTokens(high)}</Sub>}
          </Row>
          <Row label="Time"><span className="tabular-nums">{timeRange(secLo, secHi)}</span></Row>
          <Row label="Engine">
            <span className="min-w-0 [overflow-wrap:anywhere]">{e.engine_label ?? (e.keyless ? 'Keyless' : 'Several engines')}</span>
            {billing && <Sub>{billing}</Sub>}
          </Row>
        </dl>

        {e.reasons?.length > 0 && (
          <div className="flex flex-col gap-1.5">
            <h3 className="m-0 text-[13px] font-semibold text-foreground">Why</h3>
            <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-[13px] leading-relaxed text-muted-foreground">
              {e.reasons.map((x, i) => <li key={x.code + i} className="[overflow-wrap:anywhere]">{x.text}</li>)}
            </ul>
          </div>
        )}

        <Steps calls={e.breakdown ?? []} />

        {cheaper && (
          <div className="flex flex-col gap-2 rounded-md border border-border p-3 sm:flex-row sm:items-center sm:justify-between">
            <p className="m-0 text-[13px] leading-relaxed text-foreground">
              {cheaper.label} would use about {aboutTokens(estTokens(cheaper))} tokens for the same work.
            </p>
            <Button variant="secondary" size="sm" icon="engine" className="shrink-0" onClick={() => onSwitch(cheaper.engine)}>
              Use {cheaper.label} instead
            </Button>
          </div>
        )}

        {nearDeadline && (
          <p className="m-0 flex items-start gap-2 rounded-md border border-warn/25 bg-warn/10 p-3 text-[13px] leading-relaxed text-foreground" role="note">
            <Icon name="timeout" size={15} className="mt-0.5 shrink-0 text-warn" />
            <span>This may come close to the {Math.max(1, Math.round(e.deadline_s / 60))} minute limit. If it runs out of time, what was written is kept and you can resume it.</span>
          </p>
        )}

        <DialogFooter className="items-stretch gap-3 sm:items-center sm:justify-between">
          {onRemember ? (
            <label className="inline-flex cursor-pointer items-center gap-2 text-[13px] text-muted-foreground">
              <input type="checkbox" checked={remember} onChange={ev => onRemember(ev.target.checked)}
                className="size-4 cursor-pointer accent-primary" />
              Don't ask again in this chat
            </label>
          ) : <span />}
          <span className="flex flex-col-reverse gap-2 sm:flex-row">
            <Button variant="ghost" onClick={onCancel}>Cancel</Button>
            <Button ref={go} variant="primary" onClick={onContinue}>Continue</Button>
          </span>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="m-0 flex min-w-0 flex-wrap items-baseline gap-x-2 font-medium text-foreground">{children}</dd>
    </>
  )
}
const Sub = ({ children, className }: { children: ReactNode; className?: string }) =>
  <span className={cn('text-xs font-normal text-muted-foreground tabular-nums', className)}>{children}</span>

// ---------- "Don't ask again in this chat" (sessionStorage, per chat or sandbox id) ----------

const KEY = 'tg-cost-ok:'
export function costRemembered(id: string | null | undefined) {
  if (!id) return false
  try { return sessionStorage.getItem(KEY + id) === '1' } catch { return false }
}
export function rememberCost(id: string | null | undefined) {
  if (!id) return
  try { sessionStorage.setItem(KEY + id, '1') } catch { /* private mode: ask again next time */ }
}

// ---------- the confirm flow, shared by chat, sandbox, resume, replay and the Live ask box ----------

export type CostBody = { confirm_cost?: boolean; engine?: string; engines?: string[] }

/** The body to send when the user picks "Use {label} instead": that engine, pinned. With several engines (Compare),
 *  it replaces the one the estimate says costs the most. */
export function switchBody<B extends CostBody>(body: B, estimate: Estimate, engine: string): B {
  if (Array.isArray(body.engines) && body.engines.length) {
    const cost = (name: string) => estimate.breakdown?.filter(c => c.engine === name).reduce((n, c) => n + c.tokens_in + c.tokens_out, 0) ?? 0
    const costliest = [...body.engines].sort((a, b) => cost(b) - cost(a))[0]
    // never the same engine twice: when it is already in the list, the costliest one is dropped instead
    const next = [...new Set(body.engines.includes(engine)
      ? body.engines.filter(n => n !== costliest)
      : body.engines.map(n => (n === costliest ? engine : n)))]
    return next.length >= 2 ? { ...body, engines: next, confirm_cost: true } : { ...body, engines: undefined, engine: next[0] ?? engine, confirm_cost: true }
  }
  return { ...body, engine, confirm_cost: true }
}

export interface CostPrompt<B extends CostBody> {
  estimate: Estimate
  body: B
  /** Sends the body again (with confirm_cost: true, and a new engine after "Use X instead"). */
  go: (body: B) => unknown
  /** Called when the user ticked "Don't ask again in this chat" and went ahead. Omit to hide the checkbox. */
  onRemember?: () => void
  onCancel?: () => void
}

/** `ask(prompt)` opens the dialog; render `dialog` somewhere in the tree. */
export function useCostConfirm() {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [prompt, setPrompt] = useState<CostPrompt<any> | null>(null)
  const [remember, setRemember] = useState(false)
  const ask = useCallback(<B extends CostBody>(p: CostPrompt<B>) => { setRemember(false); setPrompt(p) }, [])
  const done = (next: CostBody) => {
    const p = prompt
    if (!p) return
    setPrompt(null)
    if (remember) p.onRemember?.()
    void p.go(next)
  }
  const dialog = prompt ? (
    <CostDialog estimate={prompt.estimate} remember={remember} onRemember={prompt.onRemember ? setRemember : undefined}
      onCancel={() => { const p = prompt; setPrompt(null); p.onCancel?.() }}
      onContinue={() => done({ ...prompt.body, confirm_cost: true })}
      onSwitch={engine => done(switchBody(prompt.body, prompt.estimate, engine))} />
  ) : null
  return { ask, dialog, open: prompt != null }
}

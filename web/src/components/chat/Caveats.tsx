import { Icon } from '../../icons'
import { cn } from '@/lib/utils'

// What a run could not do (docs/PLAN-accuracy-v2.md B2, C8): unmet parts of a file brief, images not found, a font
// that could not be embedded, steps the assistant can't do here. Shown in an amber box that never folds away.

export function CaveatsBox({ caveats, className }: { caveats: string[] | undefined; className?: string }) {
  if (!caveats?.length) return null
  return (
    <section role="note" aria-label="What I couldn't do"
      className={cn('flex items-start gap-2.5 rounded-md border border-warn/30 bg-warn/10 px-3 py-2.5 text-[13px] text-foreground', className)}>
      <Icon name="warning" size={15} className="mt-0.5 shrink-0 text-warn" />
      <div className="flex min-w-0 flex-col gap-1">
        <h4 className="m-0 text-[13px] font-semibold">What I couldn't do</h4>
        <ul className="m-0 flex list-disc flex-col gap-0.5 pl-4 leading-snug marker:text-warn">
          {caveats.map((c, i) => <li key={i} className="[overflow-wrap:anywhere]">{c}</li>)}
        </ul>
      </div>
    </section>
  )
}

const CAVEATS_HEAD = /^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?What I couldn['’]t do:?(?:\*\*)?:?[ \t]*$/im

/** The answer without its closing "What I couldn't do:" list when the box shows the same caveats. Only a trailing
 *  heading followed by bullets is removed, so prose that merely mentions it stays. */
export function withoutCaveats(answer: string, caveats: string[] | undefined): string {
  if (!caveats?.length) return answer
  const m = CAVEATS_HEAD.exec(answer)
  if (!m) return answer
  const tail = answer.slice(m.index + m[0].length).split('\n')
  if (!tail.every(l => /^\s*(?:[-*•]\s+\S.*)?$/.test(l) || /^\s{2,}\S/.test(l))) return answer
  return answer.slice(0, m.index).trimEnd()
}

/** The answer without lines that repeat a step's stated assumption, which is shown above the answer instead. */
export function withoutAssumptions(answer: string, assumptions: string[]): string {
  if (!assumptions.length) return answer
  const left = new Set(assumptions.map(a => a.trim()))
  const lines = answer.split('\n').filter(l => {
    const t = l.trim().replace(/^_(.*)_$/, '$1').replace(/^\*(.*)\*$/, '$1').trim()
    if (left.has(t)) { left.delete(t); return false }
    return true
  })
  return lines.join('\n').replace(/^\s*\n/, '')
}

/** Stated assumptions in muted text, one line each. */
export function Assumptions({ items, className }: { items: string[]; className?: string }) {
  if (!items.length) return null
  return (
    <div className={cn('flex flex-col gap-1', className)}>
      {items.map((a, i) => (
        <p key={i} className="m-0 flex items-start gap-1.5 text-[13px] leading-snug text-muted-foreground">
          <Icon name="info" size={13} className="mt-0.5 shrink-0" /><span className="min-w-0 [overflow-wrap:anywhere]">{a}</span>
        </p>
      ))}
    </div>
  )
}

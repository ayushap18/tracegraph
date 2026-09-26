import { cn } from '@/lib/utils'

// Formatting and class names shared by the eval list, detail and compare views.

export const pctOf = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? 'n/a' : `${Math.round(v * 100)}%`)
export const when = (at: number) => new Date(at * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
export const accTone = (v: number): 'ok' | 'warn' | 'bad' => (v >= 0.9 ? 'ok' : v >= 0.7 ? 'warn' : 'bad')

export const HEAD = 'h-9 px-3 text-xs font-medium text-muted-foreground first:pl-4 last:pr-4 sm:first:pl-5 sm:last:pr-5'
export const CELL = 'px-3 py-2.5 first:pl-4 last:pr-4 sm:first:pl-5 sm:last:pr-5'
export const LINK_ICON = 'inline-grid size-7 place-items-center rounded-md text-muted-foreground outline-none transition-colors hover:bg-subtle hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35'
export const CHIP = cn(
  'h-8 rounded-md border border-border bg-surface px-2.5 text-[13px] font-medium text-muted-foreground shadow-none transition-colors',
  'hover:bg-subtle hover:text-foreground focus-visible:ring-ring/35',
  'data-[state=on]:border-primary/40 data-[state=on]:bg-primary/10 data-[state=on]:text-foreground',
)

/** `#/evals/<a>...<b>` opens the compare view; eval ids are hex, so the dots never clash. */
export const COMPARE_SEP = '...'
export const compareHref = (a: string, b: string) => `#/evals/${a}${COMPARE_SEP}${b}`
export function parseCompare(id: string | undefined): [string, string] | null {
  if (!id?.includes(COMPARE_SEP)) return null
  const [a, b] = id.split(COMPARE_SEP)
  return a && b ? [a, b] : null
}

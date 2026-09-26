import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'
import { AgentIcon, EngineIcon, Icon, type UiIconName } from '../../icons'
import { colorOf } from '../../protocol'

// App-level building blocks shared by every workspace page. Layout and rhythm live here so
// pages only describe content: one page container, one section header, one stat strip,
// one chart card, one way to show an agent, an engine or a status.

/** Page container: gutter, max width and vertical rhythm. `tg-scope` opts the page into the reset. */
export function PageBody({ children, width = 'default', className }: {
  children: ReactNode; width?: 'default' | 'wide' | 'narrow'; className?: string
}) {
  return (
    <div className={cn(
      'tg-scope mx-auto flex w-full min-w-0 flex-col gap-6 px-4 pt-4 pb-10 sm:px-6 sm:pt-6',
      width === 'wide' ? 'max-w-[1600px]' : width === 'narrow' ? 'max-w-[960px]' : 'max-w-[1280px]',
      className,
    )}>
      {children}
    </div>
  )
}

/** A titled group of content. Sentence case, optional muted count, description and actions. */
export function Section({ title, count, description, actions, icon, children, className, id, headingId }: {
  title: ReactNode; count?: ReactNode; description?: ReactNode; actions?: ReactNode; icon?: UiIconName
  children?: ReactNode; className?: string; id?: string; headingId?: string
}) {
  return (
    <section id={id} className={cn('flex min-w-0 flex-col gap-3', className)} aria-labelledby={headingId}>
      <header className="flex flex-wrap items-end justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h2 id={headingId} className="m-0 flex items-center gap-2 text-[15px] font-semibold tracking-[-0.01em] text-foreground">
            {icon && <Icon name={icon} size={16} strokeWidth={1.9} className="text-muted-foreground" />}
            {title}
            {count != null && <span className="text-sm font-normal tabular-nums text-muted-foreground">{count}</span>}
          </h2>
          {description != null && <p className="m-0 mt-1 max-w-[70ch] text-[13px] leading-relaxed text-muted-foreground text-pretty">{description}</p>}
        </div>
        {actions != null && <div className="flex shrink-0 flex-wrap items-center gap-2">{actions}</div>}
      </header>
      {children}
    </section>
  )
}

/** One bordered row of metrics separated by hairlines (never a row of separate cards). */
export function StatStrip({ children, className, cols = 4 }: { children: ReactNode; className?: string; cols?: 3 | 4 | 6 }) {
  return (
    <div className={cn(
      'grid gap-px overflow-hidden rounded-lg border border-border bg-border',
      'grid-cols-2', cols === 3 ? 'md:grid-cols-3' : cols === 6 ? 'md:grid-cols-3 xl:grid-cols-6' : 'lg:grid-cols-4',
      className,
    )}>
      {children}
    </div>
  )
}

export function Stat({ label, icon, value, note, tone, chart, title }: {
  label: ReactNode; icon?: UiIconName; value: ReactNode; note?: ReactNode; tone?: 'ok' | 'warn' | 'bad'; chart?: ReactNode; title?: string
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1 bg-surface px-4 py-3.5" title={title}>
      <span className="flex items-center gap-1.5 truncate text-[13px] text-muted-foreground">
        {icon && <Icon name={icon} size={14} strokeWidth={1.9} />}{label}
      </span>
      <span className={cn('truncate text-2xl font-semibold tracking-[-0.02em] tabular-nums text-foreground',
        tone === 'ok' && 'text-ok', tone === 'warn' && 'text-warn', tone === 'bad' && 'text-destructive')}>{value}</span>
      {chart != null ? <div className="h-7">{chart}</div> : note != null && <span className="truncate text-xs text-muted-foreground">{note}</span>}
      {chart != null && note != null && <span className="truncate text-xs text-muted-foreground">{note}</span>}
    </div>
  )
}

/** A chart with its headline number: title left, stat right, fixed-height body. */
export function ChartCard({ title, icon, stat, statNote, footer, children, className, height = 200 }: {
  title: ReactNode; icon?: UiIconName; stat?: ReactNode; statNote?: ReactNode; footer?: ReactNode
  children: ReactNode; className?: string; height?: number
}) {
  return (
    <section className={cn('flex min-w-0 flex-col rounded-lg border border-border bg-surface', className)}>
      <header className="flex items-start justify-between gap-3 px-4 pt-4 sm:px-5">
        <h2 className="m-0 flex items-center gap-2 text-sm font-semibold text-foreground">
          {icon && <Icon name={icon} size={15} strokeWidth={1.9} className="text-muted-foreground" />}{title}
        </h2>
        {stat != null && (
          <div className="text-right">
            <div className="text-lg font-semibold leading-tight tracking-[-0.02em] tabular-nums text-foreground">{stat}</div>
            {statNote != null && <div className="text-xs text-muted-foreground">{statNote}</div>}
          </div>
        )}
      </header>
      <div className="min-w-0 px-4 pt-3 sm:px-5" style={{ height }}>{children}</div>
      {footer != null && <footer className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border px-4 py-2.5 text-xs text-muted-foreground sm:px-5">{footer}</footer>}
    </section>
  )
}

/** An agent: coloured icon (or dot), name, optional confidence. Replaces the old solid chips. */
export function AgentBadge({ agent, pct, dot, className, title }: {
  agent: string | undefined; pct?: number | string; dot?: boolean; className?: string; title?: string
}) {
  const c = colorOf(agent)
  return (
    <span title={title} className={cn(
      'inline-flex h-6 max-w-full shrink-0 items-center gap-1.5 rounded-md border border-border bg-surface px-1.5 text-xs font-medium text-foreground',
      className,
    )}>
      {dot || !agent
        ? <i className="size-1.5 shrink-0 rounded-full" style={{ background: c }} aria-hidden="true" />
        : <AgentIcon agent={agent} size={13} strokeWidth={2} style={{ color: c }} />}
      <span className="truncate">{agent ?? '…'}</span>
      {pct != null && <span className="tabular-nums text-muted-foreground">{typeof pct === 'number' ? `${Math.round(pct * 100)}%` : pct}</span>}
    </span>
  )
}

/** An engine by name: glyph + label, muted. */
export function EngineBadge({ name, label, className }: { name: string | null | undefined; label?: string; className?: string }) {
  return (
    <span className={cn('inline-flex min-w-0 items-center gap-1.5 text-[13px] text-muted-foreground', className)}>
      <EngineIcon name={name ?? 'none'} size={14} strokeWidth={1.8} />
      <span className="truncate">{label ?? (name || 'keyless')}</span>
    </span>
  )
}

const DOT_TONE: Record<string, string> = {
  done: 'bg-ok', ok: 'bg-ok', running: 'bg-primary', info: 'bg-primary', warn: 'bg-warn', cancelled: 'bg-warn', timeout: 'bg-warn',
  error: 'bg-destructive', bad: 'bg-destructive', idle: 'bg-edge',
}

/** A status as a small dot plus label (quieter than a badge, for dense rows). */
export function StatusDot({ status, label, className }: { status: string; label?: ReactNode; className?: string }) {
  return (
    <span className={cn('inline-flex items-center gap-1.5 text-[13px] text-muted-foreground', className)}>
      <span className="relative flex size-2 shrink-0">
        {status === 'running' && <span className="absolute inset-0 animate-ping rounded-full bg-primary/50 motion-reduce:hidden" />}
        <span className={cn('relative size-2 rounded-full', DOT_TONE[status] ?? 'bg-destructive')} />
      </span>
      {label ?? status}
    </span>
  )
}

/** Muted inline metadata items separated by thin gaps (never middle dots). */
export function Meta({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted-foreground', className)}>{children}</div>
}

/** A back link for detail pages. */
export function BackLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} className="inline-flex w-fit items-center gap-1.5 rounded-md text-[13px] font-medium text-muted-foreground transition-colors hover:text-foreground">
      <Icon name="prev" size={15} strokeWidth={2} />{children}
    </a>
  )
}

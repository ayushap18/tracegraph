import {
  createContext, forwardRef, useContext, useEffect, useId, useMemo, useRef, useState,
  type ButtonHTMLAttributes, type ChangeEvent, type CSSProperties, type InputHTMLAttributes, type KeyboardEvent, type ReactNode,
} from 'react'
import { toast as sonner } from 'sonner'
import { cn } from '@/lib/utils'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { Skeleton as SkeletonBase } from '@/components/ui/skeleton'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Toaster } from '@/components/ui/sonner'
import { Icon, type UiIconName } from './icons'

// Shared primitives. Every page builds from these so the product reads as one system.
// Built on shadcn/ui (src/components/ui) and Tailwind; the exported API predates the
// migration and is kept stable so call sites don't change.

// ---------- hash routing ----------

/** Go to an in-app route: navigate('/runs/12') or navigate('#/runs/12'). */
export function navigate(path: string) {
  const p = path.replace(/^#/, '')
  const hash = '#' + (p.startsWith('/') ? p : '/' + p)
  if (location.hash === hash) window.dispatchEvent(new HashChangeEvent('hashchange'))
  else location.hash = hash
}

/** The current route path without the leading '#', e.g. '/runs/12'. Query strings after '?' are split off. */
export function useHashPath() {
  const read = () => {
    const raw = location.hash.replace(/^#/, '') || '/'
    const split = raw.indexOf('?')
    const path = split < 0 ? raw : raw.slice(0, split)
    const query = split < 0 ? '' : raw.slice(split + 1)
    return { path: path.startsWith('/') ? path : '/' + path, query: new URLSearchParams(query) }
  }
  const [loc, setLoc] = useState(read)
  useEffect(() => {
    const on = () => setLoc(read())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return loc
}

// ---------- buttons ----------

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'subtle'
export type ButtonSize = 'sm' | 'md' | 'lg'

const BTN_BASE =
  'inline-flex shrink-0 cursor-pointer select-none items-center justify-center gap-2 whitespace-nowrap rounded-md border border-transparent ' +
  'font-medium outline-none transition-[background-color,border-color,color,box-shadow,scale] duration-150 ' +
  'focus-visible:ring-[3px] focus-visible:ring-ring/35 active:scale-[0.98] disabled:pointer-events-none disabled:opacity-50 [&_svg]:shrink-0'
const BTN_VARIANT: Record<ButtonVariant, string> = {
  primary: 'bg-primary text-primary-foreground shadow-xs hover:bg-primary/90',
  secondary: 'border-border bg-surface text-foreground shadow-xs hover:bg-subtle',
  ghost: 'text-muted-foreground hover:bg-subtle hover:text-foreground',
  danger: 'bg-destructive text-[var(--on-bad)] shadow-xs hover:bg-destructive/90',
  subtle: 'bg-subtle text-foreground hover:bg-subtle/60',
}
const BTN_SIZE: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-[13px] gap-1.5',
  md: 'h-9 px-3.5 text-sm',
  lg: 'h-10 px-4 text-sm',
}

/** Button look for things that aren't <button> (links styled as buttons). */
export const buttonClass = (variant: ButtonVariant = 'primary', size: ButtonSize = 'md', className?: string) =>
  cn(BTN_BASE, BTN_VARIANT[variant], BTN_SIZE[size], className)

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  icon?: UiIconName
  iconRight?: UiIconName
  loading?: boolean
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'primary', size = 'md', icon, iconRight, loading, className, children, disabled, type = 'button', ...rest }, ref,
) {
  const is = size === 'sm' ? 14 : 16
  return (
    <button ref={ref} type={type} data-slot="button" className={buttonClass(variant, size, className)} disabled={disabled || loading} aria-busy={loading || undefined} {...rest}>
      {loading ? <Spinner size={is} /> : icon && <Icon name={icon} size={is} strokeWidth={2} />}
      {children != null && children !== false && <span className="btn-label truncate">{children}</span>}
      {iconRight && <Icon name={iconRight} size={is} strokeWidth={2} />}
    </button>
  )
})

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  icon: UiIconName
  label: string // accessible name and tooltip
  size?: ButtonSize
  variant?: 'ghost' | 'secondary' | 'danger' | 'primary'
  active?: boolean
}

const ICON_SIZE: Record<ButtonSize, string> = { sm: 'size-7', md: 'size-8', lg: 'size-9' }

export function iconButtonClass(variant: IconButtonProps['variant'] = 'ghost', size: ButtonSize = 'md', active?: boolean, className?: string) {
  return cn(BTN_BASE, 'p-0', ICON_SIZE[size],
    variant === 'ghost' ? BTN_VARIANT.ghost : variant === 'secondary' ? BTN_VARIANT.secondary : variant === 'danger' ? 'text-destructive hover:bg-destructive/10' : BTN_VARIANT.primary,
    active && 'bg-subtle text-foreground', className)
}

export const IconButton = forwardRef<HTMLButtonElement, IconButtonProps>(function IconButton(
  { icon, label, size = 'md', variant = 'ghost', active, className, type = 'button', title, ...rest }, ref,
) {
  const is = size === 'sm' ? 15 : size === 'lg' ? 18 : 16
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button ref={ref} type={type} data-slot="icon-button" className={iconButtonClass(variant, size, active, className)} aria-label={label}
          aria-pressed={active === undefined ? undefined : active} {...rest}>
          <Icon name={icon} size={is} strokeWidth={1.9} />
        </button>
      </TooltipTrigger>
      <TooltipContent>{title ?? label}</TooltipContent>
    </Tooltip>
  )
})

// ---------- surfaces ----------

export interface CardProps {
  title?: ReactNode
  icon?: UiIconName
  subtitle?: ReactNode
  actions?: ReactNode
  children?: ReactNode
  className?: string
  flush?: boolean // no body padding (tables, graphs)
  id?: string
  style?: CSSProperties
  'aria-label'?: string
}

/** A surface for one group of content: sentence-case title, optional subtitle and actions. */
export function Card({ title, icon, subtitle, actions, children, className, flush, id, style, ...aria }: CardProps) {
  const hasHead = title != null || actions != null
  return (
    <section data-slot="card" className={cn('min-w-0 rounded-lg border border-border bg-surface text-foreground', className)} id={id} style={style} aria-label={aria['aria-label']}>
      {hasHead && (
        <header className={cn('flex items-start justify-between gap-3 px-4 pt-4 sm:px-5', flush ? 'border-b border-border pb-3' : 'pb-1')}>
          <div className="min-w-0">
            {title != null && (
              <h2 className="m-0 flex items-center gap-2 text-sm font-semibold tracking-[-0.01em] text-foreground">
                {icon && <Icon name={icon} size={15} strokeWidth={1.9} className="text-muted-foreground" />}{title}
              </h2>
            )}
            {subtitle != null && <p className="m-0 mt-0.5 text-[13px] text-muted-foreground">{subtitle}</p>}
          </div>
          {actions != null && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
        </header>
      )}
      <div className={cn(flush ? '' : 'px-4 pt-3 pb-4 sm:px-5 sm:pb-5', !hasHead && !flush && 'pt-4 sm:pt-5')}>{children}</div>
    </section>
  )
}

export type BadgeTone = 'neutral' | 'muted' | 'accent' | 'info' | 'ok' | 'success' | 'warn' | 'bad' | 'danger' | 'error'

const BADGE_TONE: Record<string, string> = {
  neutral: 'border-border bg-subtle text-muted-foreground',
  accent: 'border-primary/25 bg-primary/10 text-primary',
  info: 'border-primary/25 bg-primary/10 text-primary',
  ok: 'border-ok/25 bg-ok/10 text-ok',
  warn: 'border-warn/30 bg-warn/10 text-warn',
  bad: 'border-destructive/25 bg-destructive/10 text-destructive',
}
const BADGE_BASE = 'inline-flex h-5 shrink-0 items-center gap-1 whitespace-nowrap rounded-md border px-1.5 text-xs font-medium leading-none tabular-nums [&_svg]:shrink-0'

export function badgeClass(tone: BadgeTone | string = 'neutral', className?: string) {
  const t = tone === 'success' ? 'ok' : tone === 'danger' || tone === 'error' ? 'bad' : tone === 'muted' ? 'neutral' : tone
  return cn(BADGE_BASE, BADGE_TONE[t] ?? BADGE_TONE.neutral, className)
}

export function Badge({ tone = 'neutral', icon, children, className, title, dot }: {
  tone?: BadgeTone | string; icon?: UiIconName; children?: ReactNode; className?: string; title?: string; dot?: boolean
}) {
  return (
    <span className={badgeClass(tone, className)} title={title}>
      {dot && <i className="size-1.5 rounded-full bg-current" aria-hidden="true" />}
      {icon && <Icon name={icon} size={12} strokeWidth={2.1} />}
      {children}
    </span>
  )
}

/** Status of a run as a coloured badge. */
export function StatusBadge({ status }: { status: string }) {
  const tone = status === 'done' ? 'ok' : status === 'running' ? 'info' : status === 'cancelled' || status === 'timeout' ? 'warn' : 'bad'
  const icon: UiIconName = status === 'done' ? 'success' : status === 'running' ? 'spinner' : status === 'cancelled' ? 'cancelled' : status === 'timeout' ? 'timeout' : 'error'
  return (
    <span className={badgeClass(tone)}>
      <Icon name={icon} size={12} strokeWidth={2.1} className={status === 'running' ? 'animate-spin' : undefined} />{status}
    </span>
  )
}

export function Skeleton({ width, height = 14, lines, radius, className, style }: {
  width?: number | string; height?: number | string; lines?: number; radius?: number; className?: string; style?: CSSProperties
}) {
  if (lines && lines > 1) {
    return (
      <div className={cn('flex flex-col gap-2', className)} aria-hidden="true">
        {Array.from({ length: lines }, (_, i) => (
          <SkeletonBase key={i} className="max-w-full" style={{ height, width: i === lines - 1 ? '62%' : '100%', borderRadius: radius }} />
        ))}
      </div>
    )
  }
  return <SkeletonBase aria-hidden="true" className={cn('block max-w-full', className)} style={{ width: width ?? '100%', height, borderRadius: radius, ...style }} />
}

export function EmptyState({ icon = 'sparkles', title, text, action, compact, className }: {
  icon?: UiIconName; title: ReactNode; text?: ReactNode; action?: ReactNode; compact?: boolean; className?: string
}) {
  return (
    <div className={cn('flex flex-col items-center text-center', compact ? 'gap-1.5 px-3 py-6' : 'gap-2 px-4 py-12', className)}>
      <span className={cn('mb-1 grid place-items-center rounded-lg border border-border bg-surface text-muted-foreground shadow-xs', compact ? 'size-8' : 'size-10')}>
        <Icon name={icon} size={compact ? 16 : 18} strokeWidth={1.8} />
      </span>
      <p className="m-0 text-sm font-medium text-foreground">{title}</p>
      {text != null && <p className="m-0 max-w-sm text-[13px] leading-relaxed text-muted-foreground text-pretty">{text}</p>}
      {action != null && <div className="mt-3 flex flex-wrap items-center justify-center gap-2">{action}</div>}
    </div>
  )
}

// ---------- tabs ----------

export interface TabItem { id: string; label: ReactNode; icon?: UiIconName; count?: number | string; disabled?: boolean }

/** Underline tabs. Panels are rendered by the caller, so this keeps its own roving focus. */
export function Tabs({ tabs, value, onChange, className, 'aria-label': ariaLabel = 'Sections' }: {
  tabs: TabItem[]; value: string; onChange: (id: string) => void; className?: string; 'aria-label'?: string
}) {
  const refs = useRef<Record<string, HTMLButtonElement | null>>({})
  const baseId = useId()
  const onKey = (e: KeyboardEvent) => {
    const enabled = tabs.filter(t => !t.disabled)
    const i = enabled.findIndex(t => t.id === value)
    let next: TabItem | undefined
    if (e.key === 'ArrowRight') next = enabled[(i + 1) % enabled.length]
    else if (e.key === 'ArrowLeft') next = enabled[(i - 1 + enabled.length) % enabled.length]
    else if (e.key === 'Home') next = enabled[0]
    else if (e.key === 'End') next = enabled[enabled.length - 1]
    if (!next) return
    e.preventDefault()
    onChange(next.id)
    refs.current[next.id]?.focus()
  }
  return (
    <div className={cn('flex gap-1 overflow-x-auto border-b border-border [scrollbar-width:none]', className)} role="tablist" aria-label={ariaLabel} onKeyDown={onKey}>
      {tabs.map(t => (
        <button key={t.id} ref={el => { refs.current[t.id] = el }} type="button" role="tab" data-slot="tab" id={`${baseId}-${t.id}`}
          aria-selected={t.id === value} tabIndex={t.id === value ? 0 : -1} disabled={t.disabled}
          className={cn(
            'relative -mb-px inline-flex h-10 shrink-0 cursor-pointer items-center gap-1.5 border-b-2 border-transparent bg-transparent px-3 text-sm font-medium',
            'text-muted-foreground outline-none transition-colors hover:text-foreground focus-visible:text-foreground disabled:pointer-events-none disabled:opacity-50',
            t.id === value && 'border-primary text-foreground',
          )}
          onClick={() => onChange(t.id)}>
          {t.icon && <Icon name={t.icon} size={15} strokeWidth={1.9} />}
          <span>{t.label}</span>
          {t.count != null && <span className="rounded-sm bg-subtle px-1.5 text-xs tabular-nums text-muted-foreground">{t.count}</span>}
        </button>
      ))}
    </div>
  )
}

// ---------- small controls ----------

export function Spinner({ size = 16, label, className }: { size?: number; label?: string; className?: string }) {
  return (
    <span className={cn('inline-block shrink-0 animate-spin rounded-full border-2 border-current/20 border-t-current', className)}
      role={label ? 'status' : undefined} aria-label={label} aria-hidden={label ? undefined : true}
      style={{ width: size, height: size }} />
  )
}

export function Toggle({ checked, onChange, label, disabled, className, hint }: {
  checked: boolean; onChange: (v: boolean) => void; label?: ReactNode; disabled?: boolean; className?: string; hint?: ReactNode
}) {
  const id = useId()
  return (
    <div className={cn('flex items-start gap-3', disabled && 'opacity-60', className)}>
      <Switch id={id} checked={checked} onCheckedChange={onChange} disabled={disabled} className="mt-0.5" />
      {(label != null || hint != null) && (
        <Label htmlFor={id} className="flex cursor-pointer flex-col items-start gap-0.5 text-sm font-medium">
          {label}{hint != null && <span className="text-xs font-normal text-muted-foreground">{hint}</span>}
        </Label>
      )}
    </div>
  )
}

type FieldBase = Omit<InputHTMLAttributes<HTMLInputElement>, 'onChange' | 'children'>
export interface FieldProps extends FieldBase {
  label: ReactNode
  hint?: ReactNode
  error?: ReactNode
  textarea?: boolean
  multiline?: boolean
  rows?: number
  onChange?: (e: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => void
  children?: ReactNode // custom control instead of the built-in input
  className?: string
}

export function Field({ label, hint, error, textarea, multiline, rows = 4, children, className, id, maxLength, value, ...rest }: FieldProps) {
  const auto = useId()
  const fid = id ?? auto
  const describedBy = [hint != null ? fid + '-hint' : '', error ? fid + '-err' : ''].filter(Boolean).join(' ') || undefined
  const common = { id: fid, maxLength, value, 'aria-invalid': error ? true : undefined, 'aria-describedby': describedBy }
  const len = typeof value === 'string' ? value.length : null
  return (
    <div className={cn('flex flex-col gap-1.5', className)}>
      <div className="flex items-baseline justify-between gap-2">
        <Label htmlFor={fid}>{label}</Label>
        {maxLength != null && len != null && (
          <span className={cn('text-xs tabular-nums text-muted-foreground', len > maxLength * 0.9 && 'text-warn')}>{len}/{maxLength}</span>
        )}
      </div>
      {children ?? (textarea || multiline
        ? <Textarea {...(rest as unknown as InputHTMLAttributes<HTMLTextAreaElement>)} {...common} rows={rows} className="min-h-0 resize-y"
            onChange={rest.onChange as ((e: ChangeEvent<HTMLTextAreaElement>) => void) | undefined} />
        : <Input {...rest} {...common} />)}
      {hint != null && !error && <p className="m-0 text-xs text-muted-foreground" id={fid + '-hint'}>{hint}</p>}
      {error && <p className="m-0 text-xs text-destructive" id={fid + '-err'} role="alert">{error}</p>}
    </div>
  )
}

const PROGRESS_TONE = { accent: 'bg-primary', ok: 'bg-ok', warn: 'bg-warn', bad: 'bg-destructive' }

export function ProgressBar({ value, max = 1, label, tone = 'accent', indeterminate, showValue, className }: {
  value?: number; max?: number; label?: string; tone?: 'accent' | 'ok' | 'warn' | 'bad'; indeterminate?: boolean; showValue?: boolean; className?: string
}) {
  const frac = value == null || max <= 0 ? 0 : Math.max(0, Math.min(1, value / max))
  return (
    <div className={cn('flex items-center gap-3', className)}>
      <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-subtle" role="progressbar" aria-label={label}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={indeterminate ? undefined : Math.round(frac * 100)}>
        <span className={cn('absolute inset-y-0 left-0 rounded-full transition-[width] duration-500', PROGRESS_TONE[tone], indeterminate && 'w-1/3 animate-indeterminate')}
          style={indeterminate ? undefined : { width: `${frac * 100}%` }} />
      </div>
      {showValue && <span className="text-xs tabular-nums text-muted-foreground">{Math.round(frac * 100)}%</span>}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return (
    <kbd className="inline-flex h-5 min-w-5 items-center justify-center rounded-sm border border-border bg-surface px-1 font-mono text-[11px] font-medium text-muted-foreground shadow-xs">
      {children}
    </kbd>
  )
}

// ---------- toasts (Sonner) ----------

export type ToastKind = 'success' | 'error' | 'info'

export interface ToastApi {
  (text: ReactNode, kind?: ToastKind): void
  success: (text: ReactNode) => void
  error: (text: ReactNode) => void
  info: (text: ReactNode) => void
}

const ToastCtx = createContext<ToastApi | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const api = useMemo(() => {
    const push = (text: ReactNode, kind: ToastKind = 'info') => {
      if (kind === 'success') sonner.success(text)
      else if (kind === 'error') sonner.error(text, { duration: 7000 })
      else sonner.info(text)
    }
    const fn = push as ToastApi
    fn.success = t => push(t, 'success')
    fn.error = t => push(t, 'error')
    fn.info = t => push(t, 'info')
    return fn
  }, [])
  return (
    <ToastCtx.Provider value={api}>
      {children}
      <Toaster position="bottom-right" visibleToasts={4} closeButton
        mobileOffset={{ bottom: 'calc(var(--bottombar-h) + 12px + env(safe-area-inset-bottom, 0px))' }} />
    </ToastCtx.Provider>
  )
}

const fallbackToast: ToastApi = Object.assign((t: ReactNode) => console.info(t), {
  success: (t: ReactNode) => console.info(t), error: (t: ReactNode) => console.error(t), info: (t: ReactNode) => console.info(t),
})

/** toast('text'), toast.success('Saved'), toast.error('Failed: …'). */
export function useToast(): ToastApi {
  return useContext(ToastCtx) ?? fallbackToast
}

// ---------- helpers ----------

/** Copy text to the clipboard; resolves false when the browser refuses. */
export async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true } catch { /* fall through */ }
  try {
    const ta = document.createElement('textarea')
    ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0'
    document.body.appendChild(ta); ta.select()
    const ok = document.execCommand('copy')
    ta.remove()
    return ok
  } catch { return false }
}

/** "just now", "5m ago", "3h ago", "2d ago", then a date. `at` is in seconds. */
export function timeAgo(at: number, now = Date.now() / 1000) {
  const s = Math.max(0, now - at)
  if (s < 45) return 'just now'
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`
  if (s < 86400) return `${Math.round(s / 3600)}h ago`
  if (s < 86400 * 7) return `${Math.round(s / 86400)}d ago`
  return new Date(at * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' })
}

/** Re-render every `ms` so relative times stay fresh. */
export function useNow(ms = 30000) {
  const [now, setNow] = useState(() => Date.now() / 1000)
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now() / 1000), ms)
    return () => clearInterval(id)
  }, [ms])
  return now
}

import {
  createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState,
  type ButtonHTMLAttributes, type ChangeEvent, type CSSProperties, type InputHTMLAttributes, type KeyboardEvent, type ReactNode,
} from 'react'
import { Icon, type UiIconName } from './icons'

// Small shared primitives (PLAN-v4 §2.1). Every page builds from these so the product reads as one system.

const cx = (...c: Array<string | false | null | undefined>) => c.filter(Boolean).join(' ')

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

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  icon?: UiIconName
  iconRight?: UiIconName
  loading?: boolean
}

export function Button({ variant = 'primary', size = 'md', icon, iconRight, loading, className, children, disabled, type = 'button', ...rest }: ButtonProps) {
  const is = size === 'sm' ? 14 : size === 'lg' ? 18 : 16
  return (
    <button type={type} className={cx('btn', `btn-${variant}`, `btn-${size}`, className)} disabled={disabled || loading} aria-busy={loading || undefined} {...rest}>
      {loading ? <Spinner size={is} /> : icon && <Icon name={icon} size={is} strokeWidth={2} />}
      {children != null && children !== false && <span className="btn-label">{children}</span>}
      {iconRight && <Icon name={iconRight} size={is} strokeWidth={2} />}
    </button>
  )
}

export interface IconButtonProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  icon: UiIconName
  label: string // accessible name and tooltip
  size?: ButtonSize
  variant?: 'ghost' | 'secondary' | 'danger' | 'primary'
  active?: boolean
}

export function IconButton({ icon, label, size = 'md', variant = 'ghost', active, className, type = 'button', title, ...rest }: IconButtonProps) {
  const is = size === 'sm' ? 14 : size === 'lg' ? 19 : 16
  return (
    <button type={type} className={cx('icon-btn', `icon-btn-${size}`, `icon-btn-${variant}`, active && 'on', className)} aria-label={label} title={title ?? label}
      aria-pressed={active === undefined ? undefined : active} {...rest}>
      <Icon name={icon} size={is} strokeWidth={1.9} />
    </button>
  )
}

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

export function Card({ title, icon, subtitle, actions, children, className, flush, id, style, ...aria }: CardProps) {
  const hasHead = title != null || actions != null
  return (
    <section className={cx('card', flush && 'card-flush', className)} id={id} style={style} aria-label={aria['aria-label']}>
      {hasHead && (
        <header className="card-head">
          <div className="card-titles">
            {title != null && <h2 className="card-title">{icon && <Icon name={icon} size={15} strokeWidth={2} />}{title}</h2>}
            {subtitle != null && <p className="card-sub">{subtitle}</p>}
          </div>
          {actions != null && <div className="card-actions">{actions}</div>}
        </header>
      )}
      <div className="card-body">{children}</div>
    </section>
  )
}

export type BadgeTone = 'neutral' | 'muted' | 'accent' | 'info' | 'ok' | 'success' | 'warn' | 'bad' | 'danger' | 'error'

export function Badge({ tone = 'neutral', icon, children, className, title, dot }: {
  tone?: BadgeTone | string; icon?: UiIconName; children?: ReactNode; className?: string; title?: string; dot?: boolean
}) {
  const t = tone === 'success' ? 'ok' : tone === 'danger' || tone === 'error' ? 'bad' : tone === 'muted' ? 'neutral' : tone
  return (
    <span className={cx('badge', `badge-${t}`, className)} title={title}>
      {dot && <i className="badge-dot" aria-hidden="true" />}
      {icon && <Icon name={icon} size={12} strokeWidth={2.2} />}
      {children}
    </span>
  )
}

/** Status of a run as a coloured badge. */
export function StatusBadge({ status }: { status: string }) {
  const tone = status === 'done' ? 'ok' : status === 'running' ? 'info' : status === 'cancelled' || status === 'timeout' ? 'warn' : 'bad'
  const icon: UiIconName = status === 'done' ? 'success' : status === 'running' ? 'spinner' : status === 'cancelled' ? 'cancelled' : status === 'timeout' ? 'timeout' : 'error'
  return (
    <span className={cx('badge', `badge-${tone}`, status === 'running' && 'badge-live')}>
      <Icon name={icon} size={12} strokeWidth={2.2} className={status === 'running' ? 'spin' : undefined} />{status}
    </span>
  )
}

export function Skeleton({ width, height = 14, lines, radius, className, style }: {
  width?: number | string; height?: number | string; lines?: number; radius?: number; className?: string; style?: CSSProperties
}) {
  if (lines && lines > 1) {
    return (
      <div className={cx('skel-lines', className)} aria-hidden="true">
        {Array.from({ length: lines }, (_, i) => <span key={i} className="skel" style={{ height, width: i === lines - 1 ? '62%' : '100%', borderRadius: radius }} />)}
      </div>
    )
  }
  return <span className={cx('skel', className)} aria-hidden="true" style={{ width: width ?? '100%', height, borderRadius: radius, ...style }} />
}

export function EmptyState({ icon = 'sparkles', title, text, action, compact, className }: {
  icon?: UiIconName; title: ReactNode; text?: ReactNode; action?: ReactNode; compact?: boolean; className?: string
}) {
  return (
    <div className={cx('empty-state', compact && 'compact', className)}>
      <span className="empty-icon"><Icon name={icon} size={compact ? 18 : 22} strokeWidth={1.8} /></span>
      <p className="empty-title">{title}</p>
      {text != null && <p className="empty-text">{text}</p>}
      {action != null && <div className="empty-action">{action}</div>}
    </div>
  )
}

// ---------- tabs ----------

export interface TabItem { id: string; label: ReactNode; icon?: UiIconName; count?: number | string; disabled?: boolean }

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
    <div className={cx('tabs', className)} role="tablist" aria-label={ariaLabel} onKeyDown={onKey}>
      {tabs.map(t => (
        <button key={t.id} ref={el => { refs.current[t.id] = el }} type="button" role="tab" id={`${baseId}-${t.id}`}
          aria-selected={t.id === value} tabIndex={t.id === value ? 0 : -1} disabled={t.disabled}
          className={cx('tab', t.id === value && 'on')} onClick={() => onChange(t.id)}>
          {t.icon && <Icon name={t.icon} size={14} strokeWidth={2} />}
          <span>{t.label}</span>
          {t.count != null && <span className="tab-count">{t.count}</span>}
        </button>
      ))}
    </div>
  )
}

// ---------- small controls ----------

export function Spinner({ size = 16, label, className }: { size?: number; label?: string; className?: string }) {
  return (
    <span className={cx('spinner', className)} role={label ? 'status' : undefined} aria-label={label} aria-hidden={label ? undefined : true}
      style={{ width: size, height: size }} />
  )
}

export function Toggle({ checked, onChange, label, disabled, className, hint }: {
  checked: boolean; onChange: (v: boolean) => void; label?: ReactNode; disabled?: boolean; className?: string; hint?: ReactNode
}) {
  const id = useId()
  return (
    <label className={cx('toggle', disabled && 'disabled', className)} htmlFor={id}>
      <button id={id} type="button" role="switch" aria-checked={checked} disabled={disabled} className={cx('switch', checked && 'on')}
        onClick={() => onChange(!checked)}><span className="knob" /></button>
      {(label != null || hint != null) && (
        <span className="toggle-text">{label}{hint != null && <span className="toggle-hint">{hint}</span>}</span>
      )}
    </label>
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
    <div className={cx('field', error ? 'has-error' : '', className)}>
      <div className="field-top">
        <label htmlFor={fid} className="field-label">{label}</label>
        {maxLength != null && len != null && <span className={cx('field-count', len > maxLength * 0.9 && 'near')}>{len}/{maxLength}</span>}
      </div>
      {children ?? (textarea || multiline
        ? <textarea {...(rest as unknown as InputHTMLAttributes<HTMLTextAreaElement>)} {...common} rows={rows}
            onChange={rest.onChange as ((e: ChangeEvent<HTMLTextAreaElement>) => void) | undefined} />
        : <input {...rest} {...common} />)}
      {hint != null && !error && <p className="field-hint" id={fid + '-hint'}>{hint}</p>}
      {error && <p className="field-error" id={fid + '-err'} role="alert">{error}</p>}
    </div>
  )
}

export function ProgressBar({ value, max = 1, label, tone = 'accent', indeterminate, showValue, className }: {
  value?: number; max?: number; label?: string; tone?: 'accent' | 'ok' | 'warn' | 'bad'; indeterminate?: boolean; showValue?: boolean; className?: string
}) {
  const frac = value == null || max <= 0 ? 0 : Math.max(0, Math.min(1, value / max))
  return (
    <div className={cx('progress-wrap', className)}>
      <div className={cx('progress', `progress-${tone}`, indeterminate && 'indeterminate')} role="progressbar" aria-label={label}
        aria-valuemin={0} aria-valuemax={100} aria-valuenow={indeterminate ? undefined : Math.round(frac * 100)}>
        <span className="progress-fill" style={{ width: indeterminate ? undefined : `${frac * 100}%` }} />
      </div>
      {showValue && <span className="progress-val num">{Math.round(frac * 100)}%</span>}
    </div>
  )
}

export function Kbd({ children }: { children: ReactNode }) {
  return <kbd className="kbd">{children}</kbd>
}

// ---------- toasts ----------

export type ToastKind = 'success' | 'error' | 'info'
interface ToastItem { id: number; kind: ToastKind; text: ReactNode }

export interface ToastApi {
  (text: ReactNode, kind?: ToastKind): void
  success: (text: ReactNode) => void
  error: (text: ReactNode) => void
  info: (text: ReactNode) => void
}

const ToastCtx = createContext<ToastApi | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const seq = useRef(0)
  const dismiss = useCallback((id: number) => setItems(t => t.filter(x => x.id !== id)), [])
  const api = useMemo(() => {
    const push = (text: ReactNode, kind: ToastKind = 'info') => {
      const id = ++seq.current
      setItems(t => [...t.slice(-3), { id, kind, text }])
      window.setTimeout(() => dismiss(id), kind === 'error' ? 7000 : 4000)
    }
    const fn = push as ToastApi
    fn.success = t => push(t, 'success')
    fn.error = t => push(t, 'error')
    fn.info = t => push(t, 'info')
    return fn
  }, [dismiss])
  return (
    <ToastCtx.Provider value={api}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {items.map(t => (
          <div key={t.id} className={cx('toast', `toast-${t.kind}`)}>
            <Icon name={t.kind === 'success' ? 'success' : t.kind === 'error' ? 'alert' : 'info'} size={16} strokeWidth={2} />
            <div className="toast-text">{t.text}</div>
            <button type="button" className="toast-x" aria-label="Dismiss" onClick={() => dismiss(t.id)}><Icon name="close" size={14} /></button>
          </div>
        ))}
      </div>
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

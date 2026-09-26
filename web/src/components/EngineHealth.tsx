import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { engineHealth, errorText } from '../api'
import type { EngineHealth as Health } from '../protocol'
import { Button, EmptyState, IconButton, Skeleton } from '../ui'
import { EngineIcon } from '../icons'
import { StatusDot } from './app'
import { cn } from '@/lib/utils'

// Per-engine counters since the server started (GET /api/engines/health): success rate, latency, fallbacks and
// cooldowns. Refreshes every 15 s while the tab is visible. `compact` is the small card used on the Live page.

const REFRESH_MS = 15000

type Tone = 'ok' | 'warn' | 'bad' | 'idle'
const TONE_TEXT: Record<Tone, string> = { ok: 'text-ok', warn: 'text-warn', bad: 'text-destructive', idle: 'text-muted-foreground' }

/** Wall-clock seconds, or null when the server sent nothing usable (a monotonic clock would read as 1970). */
const coolingAt = (h: Health) => (h.cooling_until != null && h.cooling_until > 1e9 && h.cooling_until * 1000 > Date.now() ? h.cooling_until : null)
const hhmm = (at: number) => new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
const msText = (v: number | null) => (v == null ? '-' : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`)
const rateOf = (h: Health) => (h.calls ? h.ok / h.calls : null)

export function healthStatus(h: Health): { tone: Tone; label: string } {
  const cool = coolingAt(h)
  if (cool) return { tone: 'warn', label: `cooling until ${hhmm(cool)}` }
  if (h.cooling_until != null && h.cooling_until * 1000 > Date.now()) return { tone: 'warn', label: 'cooling down' }
  const r = rateOf(h)
  if (r == null) return { tone: 'idle', label: 'no calls yet' }
  return r >= 0.9 ? { tone: 'ok', label: 'healthy' } : r >= 0.5 ? { tone: 'warn', label: 'flaky' } : { tone: 'bad', label: 'failing' }
}

/** Loads engine health and reloads on an interval while the page is visible. */
export function useEngineHealth() {
  const [engines, setEngines] = useState<Health[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const load = useCallback(async () => {
    setLoading(true)
    try { const r = await engineHealth(); setEngines(r.engines); setError(null) } catch (e) { setError(errorText(e)) } finally { setLoading(false) }
  }, [])
  useEffect(() => {
    let timer = 0
    const start = () => { if (!timer) timer = window.setInterval(() => void load(), REFRESH_MS) }
    const stop = () => { clearInterval(timer); timer = 0 }
    const onVis = () => { if (document.hidden) stop(); else { void load(); start() } }
    void load()
    if (!document.hidden) start()
    document.addEventListener('visibilitychange', onVis)
    return () => { stop(); document.removeEventListener('visibilitychange', onVis) }
  }, [load])
  return { engines, error, loading, reload: load }
}

export default function EngineHealth({ compact, className }: { compact?: boolean; className?: string }) {
  const { engines, error, loading, reload } = useEngineHealth()
  const refresh = <IconButton icon="refresh" label="Refresh engine health" size="sm" onClick={() => void reload()} disabled={loading} />

  let body
  if (engines == null && !error) {
    body = <div className="flex flex-col gap-2 p-4" aria-busy="true">{[0, 1, 2].map(i => <Skeleton key={i} height={compact ? 20 : 28} radius={6} />)}</div>
  } else if (engines == null) {
    body = <EmptyState compact icon="alert" title="Could not load engine health" text={error ?? ''}
      action={<Button variant="secondary" size="sm" icon="retry" onClick={() => void reload()}>Retry</Button>} />
  } else if (engines.length === 0) {
    body = <EmptyState compact icon="engine" title="No engines" text="Keyless mode uses only built-in agents, so there is nothing to track." />
  } else {
    body = compact
      ? <ul className="m-0 grid list-none grid-cols-1 gap-x-8 px-4 py-2 sm:grid-cols-2 sm:px-5">{engines.map(h => <CompactRow key={h.name} h={h} />)}</ul>
      : <ul className="m-0 flex list-none flex-col divide-y divide-border p-0">{engines.map(h => <FullRow key={h.name} h={h} />)}</ul>
  }

  return (
    <section aria-label="Engine health" className={cn('min-w-0 rounded-lg border border-border bg-surface', className)}>
      <header className="flex items-center justify-between gap-3 border-b border-border px-4 py-2.5 sm:px-5">
        {compact
          ? <h2 className="m-0 text-sm font-semibold text-foreground">Engine health</h2>
          : <p className="m-0 text-xs text-muted-foreground">Since the server started. Updates every 15 s.</p>}
        {refresh}
      </header>
      {body}
    </section>
  )
}

function Rate({ h }: { h: Health }) {
  const r = rateOf(h)
  const tone: Tone = r == null ? 'idle' : r >= 0.9 ? 'ok' : r >= 0.5 ? 'warn' : 'bad'
  return <span className={cn('tabular-nums', TONE_TEXT[tone])}>{r == null ? '-' : `${Math.round(r * 100)}%`}</span>
}

function CompactRow({ h }: { h: Health }) {
  const s = healthStatus(h)
  return (
    <li className="flex min-h-8 min-w-0 items-center gap-3 py-1">
      <EngineIcon name={h.name} size={14} className="shrink-0 text-muted-foreground" />
      <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-foreground">{h.label}</span>
      <span className="shrink-0" title={s.label}><StatusDot status={s.tone} label={<span className="hidden max-w-[9rem] truncate text-xs min-[420px]:inline">{s.label}</span>} /></span>
      <span className="w-10 shrink-0 text-right text-[13px]"><Rate h={h} /></span>
      <span className="w-14 shrink-0 text-right text-xs tabular-nums text-muted-foreground" title="p95 latency">{msText(h.p95_ms)}</span>
    </li>
  )
}

function FullRow({ h }: { h: Health }) {
  const s = healthStatus(h)
  return (
    <li className="flex min-w-0 flex-col gap-2 px-4 py-3 sm:px-5 md:flex-row md:items-center md:gap-4">
      <div className="flex min-w-0 items-center gap-3 md:w-56 md:shrink-0">
        <EngineIcon name={h.name} size={16} className="shrink-0 text-muted-foreground" />
        <div className="flex min-w-0 flex-col">
          <span className="truncate text-sm font-medium text-foreground">{h.label}</span>
          <StatusDot status={s.tone} label={<span className={cn('text-xs', s.tone === 'idle' ? '' : TONE_TEXT[s.tone])}>{s.label}</span>} />
        </div>
      </div>
      <dl className="m-0 grid min-w-0 flex-1 grid-cols-2 gap-x-4 gap-y-1.5 text-[13px] min-[400px]:grid-cols-3 sm:grid-cols-5">
        <Metric label="Success"><Rate h={h} /></Metric>
        <Metric label="Calls">{h.ok}/{h.calls}</Metric>
        <Metric label="p50 / p95">{msText(h.p50_ms)} / {msText(h.p95_ms)}</Metric>
        <Metric label="Fell back" title="Calls that moved on from this engine to the next one">{h.fallbacks_from}</Metric>
        <Metric label="Took over" title="Calls this engine answered after another one failed">{h.fallbacks_to}</Metric>
      </dl>
      {h.last_error && (
        <p className="m-0 min-w-0 truncate rounded-md bg-destructive/5 px-2 py-1 text-xs text-destructive md:max-w-[16rem]" title={h.last_error}>
          {h.last_error}
        </p>
      )}
    </li>
  )
}

function Metric({ label, title, children }: { label: string; title?: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col" title={title}>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="m-0 truncate tabular-nums text-foreground">{children}</dd>
    </div>
  )
}

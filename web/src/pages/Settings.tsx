import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { agentExamples, deleteFile, errorText, listAgents, listEvals, listFiles, listSessions, setEngine, setRouteExamples, testEngine } from '../api'
import type { EngineInfo, EngineTestResult, FileInfo } from '../protocol'
import { useStore } from '../store'
import { useTheme, THEMES, THEME_ICON, THEME_LABEL, type Theme } from '../theme'
import { Badge, Button, EmptyState, IconButton, Kbd, Skeleton, Spinner, Toggle, useToast } from '../ui'
import { EngineIcon, Icon, type UiIconName } from '../icons'
import { MOD_KEY } from '../components/Shell'
import EngineHealth from '../components/EngineHealth'
import { PageBody, Section, Stat, StatStrip } from '../components/app'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { cn } from '@/lib/utils'

// Engines (status, billing, test, set active), theme, limits, keyboard shortcuts and data counts.

const KEYLESS: EngineInfo = { name: 'none', label: 'Keyless', billing: 'api', web: false, available: true, why: '' }

const SECTIONS: Array<{ id: string; label: string; icon: UiIconName }> = [
  { id: 'settings-engines', label: 'Engines', icon: 'engine' },
  { id: 'settings-health', label: 'Engine health', icon: 'activity' },
  { id: 'settings-routing', label: 'Routing', icon: 'jev' },
  { id: 'settings-appearance', label: 'Appearance', icon: 'sun' },
  { id: 'settings-limits', label: 'Limits', icon: 'latency' },
  { id: 'settings-shortcuts', label: 'Shortcuts', icon: 'keyboard' },
  { id: 'settings-data', label: 'Data', icon: 'database' },
  { id: 'settings-features', label: 'Server features', icon: 'blocks' },
]

export default function Settings() {
  const { store } = useStore()
  const { theme, setTheme } = useTheme()
  const active = store.engine?.name ?? 'none'
  const engines = [...store.engines, KEYLESS]

  return (
    <PageBody>
      <div className="grid min-w-0 grid-cols-1 gap-8 lg:grid-cols-[180px_minmax(0,960px)] lg:gap-10">
        <SectionNav />

        <div className="flex min-w-0 flex-col gap-10">
          <Section id="settings-engines" headingId="engines-h" icon="engine" title="Engines" className="scroll-mt-20"
            description="The LLM that plans, answers open questions and merges results. Subscription CLIs run on your own plan; the API bills per token; Keyless uses only built-in agents.">
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {!store.ready
                ? Array.from({ length: 4 }, (_, i) => (
                    <li key={i} className="flex items-start gap-3 rounded-lg border border-border bg-surface p-4" aria-hidden="true">
                      <Skeleton width={36} height={36} radius={8} />
                      <div className="flex min-w-0 flex-1 flex-col gap-2"><Skeleton width="40%" height={16} /><Skeleton lines={2} /></div>
                    </li>
                  ))
                : engines.map(e => <li key={e.name}><EngineRow engine={e} active={e.name === active} /></li>)}
            </ul>
          </Section>

          <Section id="settings-health" headingId="health-h" icon="activity" title="Engine health" className="scroll-mt-20"
            description="How each engine has behaved since the server started. A failing engine can make routing look wrong when it is not.">
            <EngineHealth />
          </Section>

          <RoutingSection />

          <Section id="settings-appearance" headingId="appearance-h" icon="sun" title="Appearance" className="scroll-mt-20">
            <div className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4 sm:p-5">
              <ToggleGroup type="single" variant="outline" aria-label="Theme" value={theme}
                onValueChange={(v: string) => { if (v) setTheme(v as Theme) }} className="max-w-full">
                {THEMES.map(t => (
                  <ToggleGroupItem key={t} value={t} aria-label={THEME_LABEL[t]}
                    className="gap-1.5 px-3 text-[13px] data-[state=on]:bg-subtle data-[state=on]:text-foreground">
                    <Icon name={THEME_ICON[t]} size={15} />{THEME_LABEL[t]}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
              <p className="m-0 text-[13px] text-muted-foreground">System follows your OS setting. Press <Kbd>t</Kbd> anywhere to cycle.</p>
            </div>
          </Section>

          <Section id="settings-limits" headingId="limits-h" icon="latency" title="Limits and timeouts" className="scroll-mt-20"
            description="Set on the server; shown here for reference.">
            <DefList>
              <DefRow label="Run timeout">300 s <span className="text-xs text-muted-foreground">(TG_RUN_TIMEOUT)</span></DefRow>
              <DefRow label="Engine test timeout">60 s</DefRow>
              <DefRow label="Query length">500 characters</DefRow>
              <DefRow label="Subtasks per query">up to 4</DefRow>
              <DefRow label="File uploads">10 MB · .txt .md .csv .json .pdf</DefRow>
              <DefRow label="Custom agents">up to 12</DefRow>
              <DefRow label="Autopilot interval">
                <span className="inline-flex items-center gap-2">{store.state.interval} s {store.state.autopilot ? <Badge tone="warn">on</Badge> : <Badge>off</Badge>}</span>
              </DefRow>
            </DefList>
          </Section>

          <Section id="settings-shortcuts" headingId="shortcuts-h" icon="keyboard" title="Keyboard shortcuts" className="scroll-mt-20">
            <DefList>
              <DefRow label="Command palette"><Kbd>{MOD_KEY}</Kbd><Kbd>K</Kbd></DefRow>
              <DefRow label="Focus the message box (Chat, Live)"><Kbd>/</Kbd></DefRow>
              <DefRow label="Send"><Kbd>Enter</Kbd></DefRow>
              <DefRow label="New line"><Kbd>Shift</Kbd>+<Kbd>Enter</Kbd></DefRow>
              <DefRow label="Cycle theme"><Kbd>t</Kbd></DefRow>
              <DefRow label="Collapse or expand the sidebar"><Kbd>[</Kbd></DefRow>
              <DefRow label="Previous or next run (Live)"><Kbd>←</Kbd><Kbd>→</Kbd></DefRow>
              <DefRow label="Follow the newest run (Live)"><Kbd>l</Kbd></DefRow>
              <DefRow label="Toggle autopilot (Live)"><Kbd>a</Kbd></DefRow>
              <DefRow label="Fullscreen trace graph (Live)"><Kbd>f</Kbd></DefRow>
              <DefRow label="Reset graph zoom (Live)"><Kbd>r</Kbd></DefRow>
              <DefRow label="Close dialogs and panels"><Kbd>Esc</Kbd></DefRow>
            </DefList>
          </Section>

          <DataSection />

          <Section id="settings-features" headingId="features-h" icon="blocks" title="Server features" className="scroll-mt-20"
            description="Reported by the server in its config.">
            <div className="flex flex-wrap gap-2">
              {(Object.entries(store.features) as Array<[string, boolean]>).map(([k, v]) => (
                <Badge key={k} tone={v ? 'ok' : 'neutral'} icon={v ? 'check' : 'close'}>{k.replace('_', ' ')}</Badge>
              ))}
            </div>
          </Section>
        </div>
      </div>
    </PageBody>
  )
}

/** Sticky in-page navigation on large screens. Pages use hash routing, so this scrolls instead of linking to #ids. */
function SectionNav() {
  const [current, setCurrent] = useState(SECTIONS[0].id)
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return
    const els = SECTIONS.map(s => document.getElementById(s.id)).filter((x): x is HTMLElement => !!x)
    const io = new IntersectionObserver(entries => {
      const hit = entries.filter(e => e.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0]
      if (hit) setCurrent(hit.target.id)
    }, { rootMargin: '-15% 0px -70% 0px' })
    els.forEach(el => io.observe(el))
    return () => io.disconnect()
  }, [])
  const go = (id: string) => {
    const el = document.getElementById(id)
    if (!el) return
    const reduce = typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches
    el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
    setCurrent(id)
  }
  return (
    <nav aria-label="Settings sections" className="hidden lg:block">
      <ul className="sticky top-6 m-0 flex list-none flex-col gap-0.5 p-0">
        {SECTIONS.map(s => (
          <li key={s.id}>
            <button type="button" data-slot="button" onClick={() => go(s.id)} aria-current={current === s.id ? 'true' : undefined}
              className={cn(
                'flex h-8 w-full items-center gap-2 rounded-md border-0 bg-transparent px-2.5 text-left text-[13px] font-medium text-muted-foreground transition-colors',
                'hover:bg-subtle hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35',
                current === s.id && 'bg-subtle text-foreground',
              )}>
              <Icon name={s.icon} size={15} strokeWidth={1.9} />{s.label}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  )
}

function DefList({ children }: { children: ReactNode }) {
  return <dl className="m-0 divide-y divide-border rounded-lg border border-border bg-surface">{children}</dl>
}

function DefRow({ label, children }: { label: ReactNode; children: ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-4 px-4 py-2.5 sm:px-5">
      <dt className="min-w-0 text-[13px] text-muted-foreground">{label}</dt>
      <dd className="m-0 flex shrink-0 items-center gap-1 text-right text-sm tabular-nums text-foreground">{children}</dd>
    </div>
  )
}

function EngineRow({ engine: e, active }: { engine: EngineInfo; active: boolean }) {
  const toast = useToast()
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<EngineTestResult | null>(null)
  const [switching, setSwitching] = useState(false)
  const keyless = e.name === 'none'

  const test = async () => {
    setTesting(true); setResult(null)
    try {
      const r = await testEngine(e.name)
      setResult(r)
      if (r.ok) toast.success(`${e.label} replied in ${r.ms} ms`)
      else toast.error(`${e.label} test failed: ${r.error ?? 'no reply'}`)
    } catch (err) {
      setResult({ ok: false, ms: 0, error: errorText(err) })
      toast.error(`${e.label} test failed: ${errorText(err)}`)
    } finally { setTesting(false) }
  }
  const activate = async () => {
    setSwitching(true)
    try { await setEngine(e.name); toast.success(`${e.label} is now the active engine`) }
    catch (err) { toast.error(`Could not switch: ${errorText(err)}`) } finally { setSwitching(false) }
  }

  return (
    <article aria-label={`${e.label} engine`} aria-current={active ? 'true' : undefined}
      className={cn('flex flex-col gap-3 rounded-lg border p-4 transition-colors sm:flex-row sm:items-start',
        active ? 'border-primary/50 bg-primary/5' : 'border-border bg-surface')}>
      <div className="flex min-w-0 flex-1 items-start gap-3">
        <span className={cn('grid size-9 shrink-0 place-items-center rounded-md border',
          active ? 'border-primary/30 bg-primary/10 text-primary' : 'border-border bg-subtle text-muted-foreground',
          !e.available && 'opacity-60')}>
          <EngineIcon name={e.name} size={18} />
        </span>
        <div className="flex min-w-0 flex-1 flex-col gap-2">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className={cn('m-0 text-sm font-semibold', e.available ? 'text-foreground' : 'text-muted-foreground')}>{e.label}</h3>
              {active && <Badge tone="accent" icon="check">Active</Badge>}
            </div>
            <span className={cn('block truncate text-xs text-muted-foreground', !keyless && 'font-mono')}>{keyless ? 'built-in agents only' : e.name}</span>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {keyless ? <Badge tone="ok">free</Badge> : e.billing === 'subscription' ? <Badge tone="ok" icon="lock">your subscription</Badge> : <Badge tone="warn" icon="spend">pay per token</Badge>}
            {e.web && <Badge tone="neutral" icon="web">web search</Badge>}
            {e.name === 'codex' && <Badge tone="neutral" icon="code">can run code</Badge>}
            {e.available ? <Badge tone="ok" dot>available</Badge> : <Badge tone="bad" dot>unavailable</Badge>}
          </div>
          {!e.available && <p className="m-0 max-w-[70ch] text-[13px] text-muted-foreground">{e.why}</p>}
          {result && (
            <p role="status" className={cn('m-0 flex min-w-0 items-start gap-2 rounded-md border px-3 py-2 text-[13px] text-foreground',
              result.ok ? 'border-ok/30 bg-ok/5' : 'border-destructive/30 bg-destructive/5')}>
              <Icon name={result.ok ? 'success' : 'error'} size={14} className={cn('mt-0.5 shrink-0', result.ok ? 'text-ok' : 'text-destructive')} />
              <span className="min-w-0 break-words">
                {result.ok ? <>Replied in <b className="font-semibold tabular-nums">{result.ms} ms</b>{result.text ? <>: <q>{result.text.slice(0, 60)}</q></> : null}</> : result.error ?? 'Failed'}
              </span>
            </p>
          )}
        </div>
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2 pl-12 sm:pl-0">
        {!keyless && <Button variant="secondary" size="sm" icon="bolt" loading={testing} disabled={!e.available} onClick={() => void test()}>Test</Button>}
        <Button variant={active ? 'ghost' : 'secondary'} size="sm" icon={active ? 'check' : 'play'} loading={switching} disabled={active || !e.available} onClick={() => void activate()}>
          {active ? 'Active' : 'Set active'}
        </Button>
      </div>
    </article>
  )
}

/** The route examples switch. The live value comes from hello/config events; agentExamples() seeds it on load. */
function RoutingSection() {
  const { subscribe } = useStore()
  const toast = useToast()
  const [on, setOn] = useState<boolean | null>(null)
  const [count, setCount] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    agentExamples().then(r => {
      setOn(v => v ?? r.enabled)
      setCount(r.agents.reduce((n, a) => n + a.examples.length + a.not.length, 0))
    }, () => setOn(v => v ?? false))
  }, [])
  useEffect(() => subscribe(e => {
    if ((e.type === 'hello' || e.type === 'config') && typeof e.route_examples === 'boolean') setOn(e.route_examples)
  }), [subscribe])
  const flip = async (v: boolean) => {
    setBusy(true)
    try { await setRouteExamples(v); setOn(v); toast.success(v ? 'Route examples are on for new runs' : 'Route examples are off for new runs') }
    catch (err) { toast.error(`Could not change route examples: ${errorText(err)}`) } finally { setBusy(false) }
  }
  return (
    <Section id="settings-routing" headingId="routing-h" icon="jev" title="Routing" className="scroll-mt-20">
      <div className="flex flex-col gap-2 rounded-lg border border-border bg-surface p-4 sm:p-5">
        <Toggle checked={!!on} disabled={on == null || busy} onChange={v => void flip(v)} label="Route examples"
          hint="Shows Jev your corrected routes as examples for each agent, so similar questions go to the right place." />
        <p className="m-0 pl-11 text-xs text-muted-foreground">
          {count == null ? '' : `${count} example${count === 1 ? '' : 's'} from your labels. `}Applies to new runs only.{' '}
          <a href="#/agents" className="font-medium text-primary underline-offset-2 hover:underline">See them on Agents</a>
        </p>
      </div>
    </Section>
  )
}

function DataSection() {
  const { store } = useStore()
  const toast = useToast()
  const [counts, setCounts] = useState<{ sessions?: number; files?: FileInfo[]; agents?: number; evals?: number }>({})
  const [loading, setLoading] = useState(true)
  const load = useCallback(() => {
    setLoading(true)
    const settle = <T,>(p: Promise<T>) => p.then(v => v, () => undefined)
    Promise.all([settle(listSessions(100)), settle(listFiles()), settle(listAgents()), settle(listEvals())]).then(([s, f, a, e]) => {
      setCounts({ sessions: s?.sessions.length, files: f?.files, agents: a?.agents.filter(x => x.kind === 'custom').length, evals: e?.evals.length })
      setLoading(false)
    })
  }, [])
  useEffect(() => { load() }, [load])
  const [confirm, setConfirm] = useState<string | null>(null)
  const remove = async (f: FileInfo) => {
    setConfirm(null)
    try { await deleteFile(f.id); setCounts(c => ({ ...c, files: c.files?.filter(x => x.id !== f.id) })); toast.success(`Deleted ${f.name}`) }
    catch (e) { toast.error(`Could not delete ${f.name}: ${errorText(e)}`) }
  }
  const n = (v: number | undefined) => (loading ? <Spinner size={12} /> : v == null ? '-' : v.toLocaleString())
  const failed = !loading && (counts.sessions == null || counts.files == null || counts.agents == null || counts.evals == null)

  return (
    <Section id="settings-data" headingId="data-h" icon="database" title="Data" className="scroll-mt-20"
      actions={<IconButton icon="refresh" label="Refresh counts" size="sm" onClick={load} />}>
      <StatStrip cols={6}>
        <Stat label="Queries" value={store.stats.queries.toLocaleString()} note="since start" />
        <Stat label="Runs" value={store.runs.length} note="in memory" />
        <Stat label="Chats" value={n(counts.sessions)} />
        <Stat label="Files" value={n(counts.files?.length)} />
        <Stat label="Custom agents" value={n(counts.agents)} />
        <Stat label="Eval runs" value={n(counts.evals)} />
      </StatStrip>
      {failed && (
        <p role="status" className="m-0 flex items-center gap-2 text-[13px] text-warn">
          <Icon name="warning" size={14} className="shrink-0" />Some counts could not be loaded. Refresh to try again.
        </p>
      )}

      <div className="flex flex-col gap-2 pt-2">
        <h3 className="m-0 flex items-center gap-2 text-sm font-medium text-foreground">
          <Icon name="file" size={14} className="text-muted-foreground" />Uploaded files
        </h3>
        {loading ? (
          <div className="flex flex-col gap-3 rounded-lg border border-border bg-surface p-4" aria-hidden="true"><Skeleton lines={2} /></div>
        ) : !counts.files?.length ? (
          <div className="rounded-lg border border-border bg-surface">
            {counts.files == null
              ? <EmptyState compact icon="alert" title="Could not load files" text="Refresh to try again." />
              : <EmptyState compact icon="file" title="No files" text="Attach files in Chat to ask about documents or tables." />}
          </div>
        ) : (
          <ul className="m-0 list-none divide-y divide-border rounded-lg border border-border bg-surface p-0">
            {counts.files.map(f => (
              <li key={f.id} className="flex min-h-12 items-center gap-3 px-4 py-2">
                <Icon name={f.kind === 'csv' ? 'file-csv' : f.kind === 'json' ? 'file-json' : 'file-text'} size={15} className="shrink-0 text-muted-foreground" />
                <div className="flex min-w-0 flex-1 flex-col sm:flex-row sm:items-center sm:gap-3">
                  <span className="min-w-0 truncate text-sm text-foreground">{f.name}</span>
                  <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{f.kind}{f.rows != null ? ` · ${f.rows} rows` : ` · ${f.chars.toLocaleString()} chars`}</span>
                </div>
                {confirm === f.id
                  ? <span className="flex shrink-0 items-center gap-1.5"><Button size="sm" variant="danger" onClick={() => void remove(f)} autoFocus>Delete</Button><Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>Cancel</Button></span>
                  : <IconButton icon="trash" size="sm" label={`Delete ${f.name}`} onClick={() => setConfirm(f.id)} />}
              </li>
            ))}
          </ul>
        )}
      </div>
    </Section>
  )
}

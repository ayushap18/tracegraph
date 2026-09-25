import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useEventStream, post } from './useEventStream'
import type { ControlState } from './protocol'
import { inFlight, latency, rows } from './lib'
import Graph, { type AgentStat } from './components/Graph'
import { ConfidenceChart, LatencyChart, LATENCY_COLORS, type ConfPoint, type LatencyPoint } from './components/Charts'
import { AskBox, Inspector, Kpis, LatestRun, RoutingLog } from './components/Panels'
import { Donut, Heatmap, Waterfall } from './components/Viz'

type Theme = 'auto' | 'light' | 'dark'
const THEMES: Theme[] = ['auto', 'dark', 'light']
const THEME_ICON: Record<Theme, string> = { auto: '◐', dark: '☾', light: '☀' }

function useTheme() {
  const [theme, setTheme] = useState<Theme>(() => {
    try { const t = localStorage.getItem('jr-theme'); return THEMES.includes(t as Theme) ? (t as Theme) : 'auto' } catch { return 'auto' }
  })
  useEffect(() => {
    const el = document.documentElement
    if (theme === 'auto') delete el.dataset.theme
    else el.dataset.theme = theme
    try { localStorage.setItem('jr-theme', theme) } catch { /* private mode: keep it for this visit only */ }
  }, [theme])
  const cycle = useCallback(() => setTheme(t => THEMES[(THEMES.indexOf(t) + 1) % THEMES.length]), [])
  return [theme, cycle] as const
}

interface Toast { id: number; text: string }

export default function App() {
  const { store } = useEventStream('/events')
  const [selected, setSelected] = useState<string | null>(null)
  const [theme, cycleTheme] = useTheme()
  const [resetKey, setResetKey] = useState(0)
  const [toasts, setToasts] = useState<Toast[]>([])
  const [full, setFull] = useState(false)
  const [viewQid, setViewQid] = useState<number | null>(null) // null follows the newest run
  const [zoomBy, setZoomBy] = useState({ n: 0, k: 1 })
  const graphPanel = useRef<HTMLElement>(null)
  const askRef = useRef<HTMLInputElement>(null)
  const { runs, rev } = store

  // Everything below changes only on structural events (store.rev), never per streamed token.
  const allAgents = useMemo(() => {
    const list = [...Object.keys(store.agents), ...store.guards]
    for (const a of Object.keys(store.stats.by_agent)) if (!list.includes(a)) list.push(a) // e.g. research seen in history
    return list
  }, [store.agents, store.guards, store.stats.by_agent])

  const allRows = useMemo(() => rows(runs), [rev])

  // The graph, run card and timeline all show one run: the newest (live) or one picked with ◀ ▶.
  const shownIdx = viewQid == null ? runs.length - 1 : runs.findIndex(r => r.qid === viewQid)
  const shown = runs[shownIdx >= 0 ? shownIdx : runs.length - 1]
  const following = viewQid == null || shownIdx < 0
  const step = (d: number) => {
    const i = (shownIdx >= 0 ? shownIdx : runs.length - 1) + d
    if (i < 0) return
    setViewQid(i >= runs.length - 1 ? null : runs[i].qid)
  }

  const agentStats = useMemo(() => {
    const out: Record<string, AgentStat> = {}
    for (const a of allAgents) {
      const mine = allRows.filter(r => r.task.routed?.agent === a)
      const timed = mine.filter(r => r.task.answered)
      out[a] = {
        count: store.stats.by_agent[a] ?? 0,
        avgMs: timed.length ? timed.reduce((s, r) => s + (r.task.answered?.agent_ms ?? 0), 0) / timed.length : null,
        avgConf: mine.length ? mine.reduce((s, r) => s + (r.task.routed?.confidence ?? 0), 0) / mine.length : null,
      }
    }
    return out
  }, [allRows, allAgents, store.stats.by_agent])
  const jevInfo = useMemo(() => {
    const routed = allRows.map(r => r.task.routed).filter((r): r is NonNullable<typeof r> => !!r)
    const avg = (f: (r: (typeof routed)[number]) => number) => (routed.length ? routed.reduce((s, r) => s + f(r), 0) / routed.length : null)
    return { model: routed[routed.length - 1]?.model ?? '', avgMs: avg(r => r.jev_ms), avgConf: avg(r => r.confidence) }
  }, [allRows])
  const conf = useMemo<ConfPoint[]>(() => allRows.filter(r => r.task.routed).slice(-60)
    .map(({ run, task }) => ({ key: task.tid, confidence: task.routed!.confidence, agent: task.routed!.agent, label: task.text || run.text })), [allRows])
  const traffic = useMemo(() => allAgents.map(a => ({ agent: a, count: store.stats.by_agent[a] ?? 0 })), [allAgents, store.stats.by_agent])
  const lat = useMemo<LatencyPoint[]>(() => runs.filter(r => r.done && r.order.length).slice(-40)
    .map(r => ({ key: String(r.qid), label: r.text, ...latency(r) })), [rev])
  const heatAgents = useMemo(() => Object.keys(store.agents), [store.agents])

  // Errors surface as toasts for a few seconds, besides living in the run cards.
  const lastErr = useRef<string | null>(null)
  useEffect(() => {
    if (!store.lastError || store.lastError === lastErr.current) return
    lastErr.current = store.lastError
    const id = Date.now()
    setToasts(t => [...t.slice(-3), { id, text: store.lastError! }])
    const timer = window.setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 6000)
    return () => clearTimeout(timer)
  }, [store.lastError])

  const toggleFull = () => {
    const el = graphPanel.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else if (el.requestFullscreen) void el.requestFullscreen().catch(() => setFull(f => !f))
    else setFull(f => !f) // no Fullscreen API (iOS Safari): fall back to a CSS overlay
  }
  useEffect(() => {
    const on = () => setFull(!!document.fullscreenElement)
    document.addEventListener('fullscreenchange', on)
    return () => document.removeEventListener('fullscreenchange', on)
  }, [])

  const state: ControlState = store.state
  const stepRef = useRef(step)
  stepRef.current = step
  const autopilotRef = useRef(state.autopilot)
  autopilotRef.current = state.autopilot
  // Single-key shortcuts, ignored while typing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      const typing = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable
      if (e.key === 'Escape') { setSelected(null); if (typing) el.blur(); return }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key === '/') { e.preventDefault(); askRef.current?.focus() }
      else if (e.key === 'a') void post('/control', { autopilot: !autopilotRef.current })
      else if (e.key === 'f') toggleFull()
      else if (e.key === 'r') setResetKey(k => k + 1)
      else if (e.key === 't') cycleTheme()
      else if (e.key === 'ArrowLeft') stepRef.current(-1)
      else if (e.key === 'ArrowRight') stepRef.current(1)
      else if (e.key === 'l') setViewQid(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cycleTheme])

  const flying = inFlight(runs).length

  return (
    <>
      <header className="top">
        <div className="brand">
          <span className={'dot' + (store.connected ? ' on' : '')} />
          <h1>Jev Router</h1>
          <span className="ver">v2.2</span>
        </div>
        <span className="conn">{store.connected ? (flying ? `live · ${flying} in flight` : 'live') : store.ready ? 'server offline, retrying…' : 'connecting…'}</span>
        <span className={'tag' + (store.claude ? ' ok' : '')} title={store.claude ? 'Claude writes and merges answers' : 'Add ANTHROPIC_API_KEY to .env to enable Claude agents'}>
          {store.claude ? 'Claude on' : 'keyless'}
        </span>
        {state.autopilot && <span className="tag warn">autopilot · {state.interval}s</span>}
        <div className="head-actions">
          <span className="keys" aria-hidden="true"><kbd>/</kbd> ask <kbd>a</kbd> autopilot <kbd>←</kbd><kbd>→</kbd> runs <kbd>f</kbd> fullscreen <kbd>t</kbd> theme</span>
          <button type="button" className="icon" onClick={cycleTheme} title={`Theme: ${theme}`} aria-label={`Theme: ${theme}`}>{THEME_ICON[theme]}</button>
        </div>
      </header>
      <main className="wrap">
        <Kpis store={store} />
        <AskBox samples={store.samples} state={state} inputRef={askRef} />
        <section ref={graphPanel} className={'panel graph-panel' + (full ? ' full' : '')}>
          <div className="graph-bar">
            <div className="graph-title">
              <h2>Trace graph</h2>
              <div className="run-nav" role="group" aria-label="Browse runs">
                <button type="button" className="mini" onClick={() => step(-1)} disabled={!runs.length || shownIdx === 0} title="Previous run (←)" aria-label="Previous run">‹</button>
                <span className="run-id num">{shown ? `Run #${shown.qid}` : 'No runs'}{shown && <span className="muted"> · {shown.done ? 'finished' : 'running'}</span>}</span>
                <button type="button" className="mini" onClick={() => step(1)} disabled={following} title="Next run (→)" aria-label="Next run">›</button>
                <button type="button" className={'mini live' + (following ? ' on' : '')} onClick={() => setViewQid(null)} title="Follow the newest run (l)">
                  <span className="live-dot" />Live
                </button>
              </div>
            </div>
            <div className="graph-tools">
              <span className="st-legend" aria-hidden="true">
                <span><i className="st-running" />running</span><span><i className="st-done" />done</span><span><i className="st-warn" />no answer</span><span><i className="st-error" />failed</span>
              </span>
              <div className="btn-group" role="group" aria-label="Zoom">
                <button type="button" className="mini" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1 / 1.25 }))} aria-label="Zoom out">−</button>
                <button type="button" className="mini" onClick={() => setResetKey(k => k + 1)} title="Fit (r)">fit</button>
                <button type="button" className="mini" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1.25 }))} aria-label="Zoom in">+</button>
              </div>
              <button type="button" className="mini" onClick={toggleFull} title="Fullscreen (f)">{full ? 'exit' : '⤢'}</button>
            </div>
          </div>
          <Graph run={shown} agents={allAgents} agentStats={agentStats} jev={jevInfo} selected={selected} onSelect={setSelected} resetKey={resetKey} zoomBy={zoomBy} />
          {selected && <Inspector id={selected} store={store} onClose={() => setSelected(null)} />}
          <p className="graph-hint muted small">Hover a node to trace its connections · click for details · drag to pan · ⌘/Ctrl + scroll to zoom · ← → browse runs</p>
        </section>
        <div className="grid-even">
          <LatestRun run={shown} claude={store.claude} />
          <section className="panel">
            <h2>Pipeline timeline <span>{shown ? `#${shown.qid} · ${shown.done ? 'finished' : 'running'}` : ''}</span></h2>
            <Waterfall run={shown} />
          </section>
        </div>
        <div className="grid-2">
          <section className="panel">
            <h2>Routing heatmap <span>Jev's probability per agent · each column is a subtask</span></h2>
            <Heatmap rows={allRows} agents={heatAgents} onPick={tid => setSelected('t:' + tid)} />
          </section>
          <section className="panel">
            <h2>Traffic by agent <span>click to inspect</span></h2>
            <Donut data={traffic} onPick={a => setSelected('a:' + a)} />
          </section>
        </div>
        <div className="grid-even">
          <section className="panel"><h2>Route confidence <span>last 60 · 45% threshold</span></h2><ConfidenceChart data={conf} /></section>
          <section className="panel"><h2>Latency per query <span>ms</span></h2><LatencyChart data={lat} />
            <div className="legend">{Object.entries(LATENCY_COLORS).map(([k, c]) => <span key={k}><i style={{ background: c }} />{k}</span>)}</div>
          </section>
        </div>
        <RoutingLog runs={runs} />
      </main>
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map(t => <div key={t.id} className="toast">{t.text}</div>)}
      </div>
    </>
  )
}

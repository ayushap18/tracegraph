import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useEventStream, post } from './useEventStream'
import { colorOf, type ControlState } from './protocol'
import { inFlight, latency, rows } from './lib'
import Graph, { type GraphModel } from './components/Graph'
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
  const { store, subscribe } = useEventStream('/events')
  const [selected, setSelected] = useState<string | null>(null)
  const [theme, cycleTheme] = useTheme()
  const [resetKey, setResetKey] = useState(0)
  const [toasts, setToasts] = useState<Toast[]>([])
  const [full, setFull] = useState(false)
  const graphPanel = useRef<HTMLElement>(null)
  const askRef = useRef<HTMLInputElement>(null)
  const { runs, rev } = store

  // Everything below changes only on structural events (store.rev), never per streamed token.
  const allAgents = useMemo(() => {
    const list = [...Object.keys(store.agents), ...store.guards]
    for (const a of Object.keys(store.stats.by_agent)) if (!list.includes(a)) list.push(a) // e.g. research seen in history
    return list
  }, [store.agents, store.guards, store.stats.by_agent])

  const model = useMemo<GraphModel>(() => {
    const live = inFlight(runs)
    const shown = live.length ? live : runs.slice(-1)
    const subtasks = shown.flatMap(r => r.order.map(tid => {
      const t = r.tasks[tid]
      return { tid, text: t?.text ?? '', agent: t?.routed?.agent, done: !!(t?.answered || t?.error || r.done) }
    })).slice(-8)
    // Every subtask on screen lights its own route, not just the most recent one.
    const probs: Record<string, number> = {}
    const routed = new Set<string>()
    for (const r of shown) for (const tid of r.order) {
      const rt = r.tasks[tid]?.routed
      if (!rt) continue
      routed.add(rt.agent)
      for (const [a, p] of Object.entries(rt.probabilities)) probs[a] = Math.max(probs[a] ?? 0, p)
      if (!(rt.agent in rt.probabilities)) probs[rt.agent] = Math.max(probs[rt.agent] ?? 0, 0.6) // guards carry no probability of their own
    }
    const busy = new Set<string>()
    let jevBusy = false, plannerBusy = false
    for (const r of live) {
      if (!r.plan) plannerBusy = true
      for (const tid of r.order) {
        const t = r.tasks[tid]
        if (t.error) continue // routing failed: nothing is working on it
        if (!t.routed) jevBusy = true
        else if (!t.answered) busy.add(t.routed.agent)
      }
    }
    return { agents: allAgents, counts: store.stats.by_agent, subtasks, probs, routed: [...routed], busy: [...busy], jevBusy, plannerBusy }
  }, [rev, allAgents])

  const allRows = useMemo(() => rows(runs), [rev])
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
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cycleTheme])

  const flying = inFlight(runs).length
  const latest = runs[runs.length - 1]

  return (
    <>
      <header className="top">
        <div className="brand">
          <span className={'dot' + (store.connected ? ' on' : '')} />
          <h1>Jev Router</h1>
          <span className="ver">v2.1</span>
        </div>
        <span className="conn">{store.connected ? (flying ? `live · ${flying} in flight` : 'live') : store.ready ? 'server offline, retrying…' : 'connecting…'}</span>
        <span className={'tag' + (store.claude ? ' ok' : '')} title={store.claude ? 'Claude writes and merges answers' : 'Add ANTHROPIC_API_KEY to .env to enable Claude agents'}>
          {store.claude ? 'Claude on' : 'keyless'}
        </span>
        {state.autopilot && <span className="tag warn">autopilot · {state.interval}s</span>}
        <div className="head-actions">
          <span className="keys" aria-hidden="true"><kbd>/</kbd> ask <kbd>a</kbd> autopilot <kbd>f</kbd> fullscreen <kbd>t</kbd> theme</span>
          <button type="button" className="icon" onClick={cycleTheme} title={`Theme: ${theme}`} aria-label={`Theme: ${theme}`}>{THEME_ICON[theme]}</button>
        </div>
      </header>
      <main className="wrap">
        <Kpis store={store} />
        <AskBox samples={store.samples} state={state} inputRef={askRef} />
        <div className="grid-top">
          <section ref={graphPanel} className={'panel graph-panel' + (full ? ' full' : '')}>
            <h2>Live routing graph
              <span className="panel-tools">
                <span>{flying ? `${flying} in flight` : 'idle'} · drag · zoom · click</span>
                <button type="button" className="mini" onClick={() => setResetKey(k => k + 1)} title="Reset view (r)">reset</button>
                <button type="button" className="mini" onClick={toggleFull} title="Fullscreen (f)">{full ? 'exit' : 'fullscreen'}</button>
              </span>
            </h2>
            <Graph model={model} subscribe={subscribe} selected={selected} onSelect={setSelected} resetKey={resetKey} />
            {selected && <Inspector id={selected} store={store} onClose={() => setSelected(null)} />}
            <div className="legend">{allAgents.map(a => (
              <button type="button" key={a} className="legend-item" onClick={() => setSelected('a:' + a)}>
                <i style={{ background: colorOf(a) }} />{a}
              </button>
            ))}</div>
          </section>
          <LatestRun run={latest} claude={store.claude} />
        </div>
        <div className="grid-2">
          <section className="panel">
            <h2>Pipeline timeline <span>{latest ? `#${latest.qid} · ${latest.done ? 'finished' : 'running'}` : ''}</span></h2>
            <Waterfall run={latest} />
          </section>
          <section className="panel">
            <h2>Traffic by agent <span>click to inspect</span></h2>
            <Donut data={traffic} onPick={a => setSelected('a:' + a)} />
          </section>
        </div>
        <div className="grid-2">
          <section className="panel">
            <h2>Routing heatmap <span>Jev's probability per agent · each column is a subtask</span></h2>
            <Heatmap rows={allRows} agents={heatAgents} onPick={tid => setSelected('t:' + tid)} />
          </section>
          <section className="panel"><h2>Route confidence <span>last 60 · 45% threshold</span></h2><ConfidenceChart data={conf} /></section>
        </div>
        <div className="grid-2 wide-left">
          <RoutingLog runs={runs} />
          <section className="panel"><h2>Latency per query <span>ms</span></h2><LatencyChart data={lat} />
            <div className="legend">{Object.entries(LATENCY_COLORS).map(([k, c]) => <span key={k}><i style={{ background: c }} />{k}</span>)}</div>
          </section>
        </div>
      </main>
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map(t => <div key={t.id} className="toast">{t.text}</div>)}
      </div>
    </>
  )
}

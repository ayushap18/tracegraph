import { useEffect, useMemo, useRef, useState } from 'react'
import { post } from '../useEventStream'
import type { ControlState } from '../protocol'
import { inFlight, latency } from '../lib'
import { colorOf, MIN_CONFIDENCE } from '../protocol'
import Graph from '../components/Graph'
import { ConfidenceChart, LatencyChart, LATENCY_COLORS, type ConfPoint, type LatencyPoint } from '../components/Charts'
import { AskBox, ChartPanel, Inspector, Kpis, LatestRun, RoutingLog } from '../components/Panels'
import { Donut, Heatmap, Waterfall } from '../components/Viz'
import { Icon } from '../icons'
import { useStore } from '../store'
import { useTrace } from '../components/useTrace'
import { Badge } from '../ui'
import { TopActions } from '../components/Shell'

// The original real-time dashboard (v2/v3), now one page of the app: KPIs, ask box, trace graph, latest run,
// pipeline timeline, analytics and the routing log. Single-key shortcuts work here only.
export default function Live() {
  const { store } = useStore()
  const [selected, setSelected] = useState<string | null>(null)
  const [resetKey, setResetKey] = useState(0)
  const [full, setFull] = useState(false)
  const [viewQid, setViewQid] = useState<number | null>(null) // null follows the newest run
  const [zoomBy, setZoomBy] = useState({ n: 0, k: 1 })
  const graphPanel = useRef<HTMLElement>(null)
  const askRef = useRef<HTMLInputElement>(null)
  const { runs, rev } = store

  const { allAgents, allRows, agentStats, jev: jevInfo } = useTrace(store)

  // The graph, run card and timeline all show one run: the newest (live) or one picked with ◀ ▶.
  const shownIdx = viewQid == null ? runs.length - 1 : runs.findIndex(r => r.qid === viewQid)
  const shown = runs[shownIdx >= 0 ? shownIdx : runs.length - 1]
  const following = viewQid == null || shownIdx < 0
  const step = (d: number) => {
    const i = (shownIdx >= 0 ? shownIdx : runs.length - 1) + d
    if (i < 0) return
    setViewQid(i >= runs.length - 1 ? null : runs[i].qid)
  }

  const conf = useMemo<ConfPoint[]>(() => allRows.filter(r => r.task.routed).slice(-60)
    .map(({ run, task }) => ({ key: task.tid, confidence: task.routed!.confidence, agent: task.routed!.agent, label: task.text || run.text })), [allRows])
  const traffic = useMemo(() => allAgents.map(a => ({ agent: a, count: store.stats.by_agent[a] ?? 0 })), [allAgents, store.stats.by_agent])
  const lat = useMemo<LatencyPoint[]>(() => runs.filter(r => r.done && r.order.length).slice(-40)
    .map(r => ({ key: String(r.qid), label: r.text, ...latency(r) })), [rev])
  const heatAgents = useMemo(() => Object.keys(store.agents), [store.agents])

  // Headline numbers for the analytics panels.
  const summary = useMemo(() => {
    const confs = conf.map(c => c.confidence)
    const totals = lat.map(l => l.jev + l.agent + l.merge).sort((a, b) => a - b)
    const q = (p: number) => (totals.length ? totals[Math.min(totals.length - 1, Math.floor(p * totals.length))] : null)
    const top = [...traffic].sort((a, b) => b.count - a.count)[0]
    const routedCount = allRows.filter(r => r.task.routed).length
    return {
      avgConf: confs.length ? confs.reduce((a, b) => a + b, 0) / confs.length : null,
      low: confs.filter(c => c < MIN_CONFIDENCE).length,
      p50: q(0.5), p95: q(0.95),
      top: top && top.count ? top : null,
      total: traffic.reduce((a, b) => a + b.count, 0),
      routedCount,
    }
  }, [conf, lat, traffic, allRows])
  const secs = (v: number | null) => (v == null ? '–' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')

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
      else if (e.key === 'ArrowLeft') stepRef.current(-1)
      else if (e.key === 'ArrowRight') stepRef.current(1)
      else if (e.key === 'l') setViewQid(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const flying = inFlight(runs).length

  return (
    <>
      <TopActions>
        {state.autopilot && <Badge tone="warn" icon="play">autopilot · {state.interval}s</Badge>}
        {flying > 0 && <Badge tone="info" dot>{flying} in flight</Badge>}
        <span className="keys" aria-hidden="true"><kbd>/</kbd> ask <kbd>a</kbd> autopilot <kbd>←</kbd><kbd>→</kbd> runs <kbd>f</kbd> fullscreen</span>
      </TopActions>
      <div className="wrap live-page">
        <Kpis store={store} />
        <AskBox samples={store.samples} state={state} inputRef={askRef} />
        <section ref={graphPanel} className={'panel graph-panel' + (full ? ' full' : '')}>
          <div className="graph-bar">
            <div className="graph-title">
              <h2><span className="h-title"><Icon name="graph" size={14} strokeWidth={2} />Trace graph</span></h2>
              <div className="run-nav" role="group" aria-label="Browse runs">
                <button type="button" className="mini" onClick={() => step(-1)} disabled={!runs.length || shownIdx === 0} title="Previous run (←)" aria-label="Previous run"><Icon name="prev" size={15} strokeWidth={2} /></button>
                <span className="run-id num">{shown ? `Run #${shown.qid}` : 'No runs'}{shown && <span className="muted"> · {shown.done ? 'finished' : 'running'}</span>}</span>
                <button type="button" className="mini" onClick={() => step(1)} disabled={following} title="Next run (→)" aria-label="Next run"><Icon name="next" size={15} strokeWidth={2} /></button>
                <button type="button" className={'mini live' + (following ? ' on' : '')} onClick={() => setViewQid(null)} title="Follow the newest run (l)">
                  <Icon name="live" size={13} strokeWidth={2} />Live
                </button>
              </div>
            </div>
            <div className="graph-tools">
              <span className="st-legend" aria-hidden="true">
                <span><i className="st-running" />running</span><span><i className="st-done" />done</span><span><i className="st-warn" />no answer</span><span><i className="st-error" />failed</span>
              </span>
              <div className="btn-group" role="group" aria-label="Zoom">
                <button type="button" className="mini" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1 / 1.25 }))} aria-label="Zoom out" title="Zoom out"><Icon name="zoom-out" size={15} /></button>
                <button type="button" className="mini" onClick={() => setResetKey(k => k + 1)} title="Fit to view (r)" aria-label="Fit to view"><Icon name="fit" size={15} /></button>
                <button type="button" className="mini" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1.25 }))} aria-label="Zoom in" title="Zoom in"><Icon name="zoom-in" size={15} /></button>
              </div>
              <button type="button" className="mini" onClick={toggleFull} title={full ? 'Exit fullscreen (f)' : 'Fullscreen (f)'} aria-label={full ? 'Exit fullscreen' : 'Fullscreen'}><Icon name={full ? 'minimize' : 'maximize'} size={15} /></button>
            </div>
          </div>
          <Graph run={shown} agents={allAgents} agentStats={agentStats} jev={jevInfo} selected={selected} onSelect={setSelected} resetKey={resetKey} zoomBy={zoomBy} />
          {selected && <Inspector id={selected} store={store} onClose={() => setSelected(null)} />}
          <p className="graph-hint muted small">Hover a node to trace its connections · click for details · drag to pan · ⌘/Ctrl + scroll to zoom · ← → browse runs</p>
        </section>
        <div className="grid-even">
          <LatestRun run={shown} engine={store.engine} />
          <section className="panel">
            <h2><span className="h-title"><Icon name="timeline" size={14} strokeWidth={2} />Pipeline timeline</span> <span>{shown ? `#${shown.qid} · ${shown.done ? 'finished' : 'running'}` : ''}</span></h2>
            <Waterfall run={shown} />
          </section>
        </div>
        <div className="grid-2 chart-row">
          <ChartPanel icon="heatmap" title="Routing heatmap" stat={`${summary.routedCount} subtasks`} note="Jev's probability for each agent, per subtask"
            legend={<><span className="scale"><span>0%</span><i className="ramp" /><span>100%</span></span><span>older → newer · click a column</span></>}>
            <Heatmap rows={allRows} agents={heatAgents} onPick={tid => setSelected('t:' + tid)} />
          </ChartPanel>
          <ChartPanel icon="traffic" title="Traffic by agent" stat={summary.top ? `${summary.top.agent} ${Math.round((summary.top.count / Math.max(1, summary.total)) * 100)}%` : '–'}
            note="busiest agent" legend={<span>click a slice or row to inspect</span>}>
            <Donut data={traffic} onPick={a => setSelected('a:' + a)} />
          </ChartPanel>
        </div>
        <div className="grid-even chart-row">
          <ChartPanel icon="confidence" title="Route confidence" stat={summary.avgConf == null ? '–' : `${Math.round(summary.avgConf * 100)}% avg`}
            note={summary.low ? `${summary.low} below threshold` : 'none below threshold'}
            legend={<><span><i style={{ background: colorOf('weather') }} />subtask, colored by agent</span><span><i className="dash" />{Math.round(MIN_CONFIDENCE * 100)}% clarify threshold</span><span>last 60</span></>}>
            <ConfidenceChart data={conf} />
          </ChartPanel>
          <ChartPanel icon="latency" title="Latency per query" stat={`${secs(summary.p50)} median`} note={`p95 ${secs(summary.p95)}`}
            legend={<>{Object.entries(LATENCY_COLORS).map(([k, c]) => <span key={k}><i style={{ background: c }} />{k}</span>)}<span>last 40 queries</span></>}>
            <LatencyChart data={lat} />
          </ChartPanel>
        </div>
        <RoutingLog runs={runs} />
      </div>
    </>
  )
}

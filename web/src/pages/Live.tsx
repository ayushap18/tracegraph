import { useEffect, useMemo, useRef, useState } from 'react'
import { post } from '../useEventStream'
import type { ControlState } from '../protocol'
import { CHART_H, inFlight, latency } from '../lib'
import { colorOf, MIN_CONFIDENCE } from '../protocol'
import Graph from '../components/Graph'
import EngineHealth from '../components/EngineHealth'
import { ConfidenceChart, LatencyChart, LATENCY_COLORS, type ConfPoint, type LatencyPoint } from '../components/Charts'
import { AskBox, Inspector, Kpis, LatestRun, RoutingLog } from '../components/Panels'
import { ChartCard, PageBody } from '../components/app'
import { Donut, Heatmap, Waterfall } from '../components/Viz'
import { Icon, type UiIconName } from '../icons'
import { useStore } from '../store'
import { useTrace } from '../components/useTrace'
import { Badge, Button, Card, EmptyState, IconButton, Kbd } from '../ui'
import { cn } from '@/lib/utils'
import { MOD_KEY, TopActions } from '../components/Shell'

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
  const secs = (v: number | null) => (v == null ? '-' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')

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
      const typing = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable
      if (e.key === 'Escape') { setFull(false); setSelected(null); if (typing) el.blur(); return }
      if (typing || el.closest('button, a, [role=dialog]') || e.metaKey || e.ctrlKey || e.altKey) return
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
  const runState = shown ? (shown.done ? 'finished' : 'running') : ''

  return (
    <>
      <TopActions>
        {state.autopilot && <Badge tone="warn" icon="play">autopilot · {state.interval}s</Badge>}
        {flying > 0 && <Badge tone="info" dot>{flying} in flight</Badge>}
        <span className="hidden items-center gap-3 text-xs text-muted-foreground xl:flex" aria-hidden="true">
          <span className="inline-flex items-center gap-1.5"><Kbd>/</Kbd>ask</span>
          <span className="inline-flex items-center gap-1.5"><Kbd>a</Kbd>autopilot</span>
          <span className="inline-flex items-center gap-1.5"><Kbd>←</Kbd><Kbd>→</Kbd>runs</span>
          <span className="inline-flex items-center gap-1.5"><Kbd>f</Kbd>fullscreen</span>
        </span>
      </TopActions>
      <PageBody width="wide">
        <Kpis store={store} />
        <AskBox samples={store.samples} state={state} inputRef={askRef} />
        <section ref={graphPanel} aria-label="Trace graph"
          className={cn('relative min-w-0 overflow-hidden rounded-lg border border-border bg-surface [&:fullscreen]:overflow-auto [&:fullscreen]:rounded-none',
            full && 'fixed inset-0 z-50 overflow-auto rounded-none')}>
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border px-3 py-2.5 sm:px-4">
            <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-2">
              <h2 className="m-0 flex items-center gap-2 text-sm font-semibold text-foreground">
                <Icon name="graph" size={15} strokeWidth={1.9} className="text-muted-foreground" />Trace graph
              </h2>
              <div className="flex items-center gap-1" role="group" aria-label="Browse runs">
                <IconButton icon="prev" size="sm" label="Previous run" title="Previous run (←)" onClick={() => step(-1)} disabled={!runs.length || shownIdx === 0} />
                <span className="min-w-[9rem] px-1 text-center text-[13px] tabular-nums text-foreground">
                  {shown ? `Run #${shown.qid}` : 'No runs'}{shown && <span className="text-muted-foreground">, {runState}</span>}
                </span>
                <IconButton icon="next" size="sm" label="Next run" title="Next run (→)" onClick={() => step(1)} disabled={following} />
                <Button variant="ghost" size="sm" icon="live" onClick={() => setViewQid(null)} title="Follow the newest run (l)" aria-pressed={following}
                  className={cn('ml-1', following && 'bg-ok/10 text-ok hover:bg-ok/15 hover:text-ok')}>
                  Live
                </Button>
              </div>
            </div>
            <div className="flex items-center gap-3">
              <span className="hidden items-center gap-3 text-xs text-muted-foreground md:flex" aria-hidden="true">
                <span className="inline-flex items-center gap-1.5"><i className="size-1.5 rounded-full bg-primary" />Running</span>
                <span className="inline-flex items-center gap-1.5"><i className="size-1.5 rounded-full bg-ok" />Done</span>
                <span className="inline-flex items-center gap-1.5"><i className="size-1.5 rounded-full bg-warn" />No answer</span>
                <span className="inline-flex items-center gap-1.5"><i className="size-1.5 rounded-full bg-destructive" />Failed</span>
              </span>
              <div className="flex items-center gap-0.5 rounded-md border border-border p-0.5" role="group" aria-label="Zoom">
                <IconButton icon="zoom-out" size="sm" label="Zoom out" className="size-6 rounded-sm" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1 / 1.25 }))} />
                <IconButton icon="fit" size="sm" label="Fit to view" title="Fit to view (r)" className="size-6 rounded-sm" onClick={() => setResetKey(k => k + 1)} />
                <IconButton icon="zoom-in" size="sm" label="Zoom in" className="size-6 rounded-sm" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1.25 }))} />
              </div>
              <IconButton icon={full ? 'minimize' : 'maximize'} size="sm" label={full ? 'Exit fullscreen' : 'Fullscreen'} title={full ? 'Exit fullscreen (f)' : 'Fullscreen (f)'} onClick={toggleFull} />
            </div>
          </div>
          <Graph run={shown} agents={allAgents} agentStats={agentStats} jev={jevInfo} selected={selected} onSelect={setSelected} resetKey={resetKey} zoomBy={zoomBy} />
          {selected && <Inspector id={selected} store={store} onClose={() => setSelected(null)} />}
          <p className="m-0 border-t border-border px-4 py-2.5 text-xs text-muted-foreground">
            Hover a node to trace its connections, click for details, drag to pan, {MOD_KEY} + scroll to zoom, ← → to browse runs
          </p>
        </section>
        <div className="grid min-w-0 items-start gap-6 lg:grid-cols-2">
          <LatestRun run={shown} engine={store.engine} />
          <Card title="Pipeline timeline" icon="timeline"
            actions={shown ? <span className="text-xs tabular-nums text-muted-foreground">#{shown.qid}, {runState}</span> : undefined}>
            <Waterfall run={shown} />
          </Card>
        </div>
        <div className="grid min-w-0 gap-6 lg:grid-cols-2">
          <ChartCard height={CHART_H + 12} icon="heatmap" title="Routing heatmap" stat={`${summary.routedCount} subtasks`} statNote="Jev's probability for each agent, per subtask"
            footer={<>
              <span className="inline-flex items-center gap-1.5 tabular-nums">0%<i className="h-2 w-16 rounded-sm bg-linear-to-r from-subtle to-primary" />100%</span>
              <span>Older to newer, click a column</span>
            </>}>
            {summary.routedCount
              ? <Heatmap rows={allRows} agents={heatAgents} onPick={tid => setSelected('t:' + tid)} />
              : <ChartEmpty icon="heatmap" title="No routed subtasks yet" text="Each column appears here as Jev routes a subtask." />}
          </ChartCard>
          <ChartCard height={CHART_H + 12} icon="traffic" title="Traffic by agent" stat={summary.top ? `${summary.top.agent} ${Math.round((summary.top.count / Math.max(1, summary.total)) * 100)}%` : '-'}
            statNote="Busiest agent" footer={<span>Click a slice or row to inspect</span>}>
            {summary.total
              ? <Donut data={traffic} onPick={a => setSelected('a:' + a)} />
              : <ChartEmpty icon="traffic" title="No traffic yet" text="Subtasks per agent show up once queries are routed." />}
          </ChartCard>
          <ChartCard height={CHART_H + 12} icon="confidence" title="Route confidence" stat={summary.avgConf == null ? '-' : `${Math.round(summary.avgConf * 100)}% avg`}
            statNote={summary.low ? `${summary.low} below threshold` : 'None below threshold'}
            footer={<>
              <span className="inline-flex items-center gap-1.5"><i className="size-2 rounded-full" style={{ background: colorOf('weather') }} />Subtask, colored by agent</span>
              <span className="inline-flex items-center gap-1.5"><i className="w-4 border-t-2 border-dashed border-warn" />{Math.round(MIN_CONFIDENCE * 100)}% clarify threshold</span>
              <span>Last 60</span>
            </>}>
            {conf.length
              ? <ConfidenceChart data={conf} />
              : <ChartEmpty icon="confidence" title="No routed subtasks yet" text="Confidence for each routed subtask is plotted here." />}
          </ChartCard>
          <ChartCard height={CHART_H + 12} icon="latency" title="Latency per query" stat={`${secs(summary.p50)} median`} statNote={`p95 ${secs(summary.p95)}`}
            footer={<>
              {Object.entries(LATENCY_COLORS).map(([k, c]) => (
                <span key={k} className="inline-flex items-center gap-1.5"><i className="size-2 rounded-full" style={{ background: c }} />{k === 'jev' ? 'Jev' : k.charAt(0).toUpperCase() + k.slice(1)}</span>
              ))}
              <span>Last 40 queries</span>
            </>}>
            {lat.length
              ? <LatencyChart data={lat} />
              : <ChartEmpty icon="latency" title="No finished queries yet" text="Jev, agent and merge time for each finished query stack up here." />}
          </ChartCard>
        </div>
        {store.engines.some(e => e.name !== 'none') && <EngineHealth compact />}
        <RoutingLog runs={runs} />
      </PageBody>
    </>
  )
}

function ChartEmpty({ icon, title, text }: { icon: UiIconName; title: string; text: string }) {
  return <div className="grid h-full place-items-center"><EmptyState compact icon={icon} title={title} text={text} /></div>
}

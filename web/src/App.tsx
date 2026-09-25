import { useMemo, useState } from 'react'
import { useEventStream } from './useEventStream'
import { colorOf, type ControlState } from './protocol'
import { inFlight, latency, rows } from './lib'
import Graph, { type GraphModel } from './components/Graph'
import { ConfidenceChart, LatencyChart, LATENCY_COLORS, TrafficChart, type ConfPoint, type LatencyPoint } from './components/Charts'
import { AskBox, Inspector, Kpis, LatestRun, RoutingLog } from './components/Panels'

export default function App() {
  const { store, subscribe } = useEventStream('/events')
  const [selected, setSelected] = useState<string | null>(null)
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
    const subtasks = shown.flatMap(r => r.order.map(tid => ({ tid, text: r.tasks[tid]?.text ?? '', agent: r.tasks[tid]?.routed?.agent }))).slice(-8)
    let probs: Record<string, number> = {}
    for (const r of runs) for (const tid of r.order) if (r.tasks[tid]?.routed) probs = r.tasks[tid].routed!.probabilities
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
    return { agents: allAgents, counts: store.stats.by_agent, subtasks, probs, busy: [...busy], jevBusy, plannerBusy }
  }, [rev, allAgents])

  const conf = useMemo<ConfPoint[]>(() => rows(runs).filter(r => r.task.routed).slice(-60)
    .map(({ run, task }) => ({ key: task.tid, confidence: task.routed!.confidence, agent: task.routed!.agent, label: task.text || run.text })), [rev])
  const traffic = useMemo(() => allAgents.map(a => ({ agent: a, count: store.stats.by_agent[a] ?? 0 })), [allAgents, store.stats.by_agent])
  const lat = useMemo<LatencyPoint[]>(() => runs.filter(r => r.done && r.order.length).slice(-40)
    .map(r => ({ key: String(r.qid), label: r.text, ...latency(r) })), [rev])

  const flying = inFlight(runs).length
  const state: ControlState = store.state

  return (
    <>
      <header className="top">
        <h1><span className={'dot' + (store.connected ? ' on' : '')} />Jev Router <small>v2 · plan, route, run, merge</small></h1>
        <span className="conn">{store.connected ? 'live' : store.ready ? 'server offline, retrying…' : 'connecting…'}</span>
        <span className={'tag' + (store.claude ? ' ok' : '')}>{store.claude ? 'Claude on' : 'keyless'}</span>
        <Kpis store={store} />
      </header>
      <main className="wrap">
        <AskBox samples={store.samples} state={state} />
        <div className="grid-top">
          <section className="panel graph-panel">
            <h2>Live routing graph <span>{flying ? `${flying} in flight` : 'idle'} · drag, zoom, click a node</span></h2>
            <Graph model={model} subscribe={subscribe} selected={selected} onSelect={setSelected} />
            {selected && <Inspector id={selected} store={store} onClose={() => setSelected(null)} />}
            <div className="legend">{allAgents.map(a => <span key={a}><i style={{ background: colorOf(a) }} />{a}</span>)}</div>
          </section>
          <LatestRun run={runs[runs.length - 1]} claude={store.claude} />
        </div>
        <div className="grid-row">
          <section className="panel"><h2>Route confidence <span>last 60 subtasks · 45% threshold</span></h2><ConfidenceChart data={conf} /></section>
          <section className="panel"><h2>Traffic by agent <span>subtasks</span></h2><TrafficChart data={traffic} /></section>
          <section className="panel"><h2>Latency per query <span>ms</span></h2><LatencyChart data={lat} />
            <div className="legend">{Object.entries(LATENCY_COLORS).map(([k, c]) => <span key={k}><i style={{ background: c }} />{k}</span>)}</div>
          </section>
        </div>
        {store.lastError && <p className="err">Last error: {store.lastError}</p>}
        <RoutingLog runs={runs} />
      </main>
    </>
  )
}

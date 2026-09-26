import { useEffect, useMemo, useRef, useState } from 'react'
import Graph from './Graph'
import { Inspector } from './Panels'
import { useTrace } from './useTrace'
import { Icon } from '../icons'
import type { Run, Store } from '../useEventStream'

// The trace graph for one run with its own zoom toolbar and node inspector. Used by Chat and Run detail;
// the Live page keeps its richer toolbar (run browsing) around the same Graph.
export function TraceView({ run, store, compact, legend = true }: { run: Run | undefined; store: Store; compact?: boolean; legend?: boolean }) {
  const { allAgents, agentStats, jev } = useTrace(store)
  const [selected, setSelected] = useState<string | null>(null)
  const [resetKey, setResetKey] = useState(0)
  const [zoomBy, setZoomBy] = useState({ n: 0, k: 1 })
  const [full, setFull] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  // Agents this run used but that are not active now (custom agents deleted, files agents) still get a node.
  // Compact (side panel): only agents this run used or seriously considered, so the graph stays legible.
  const agents = useMemo(() => {
    const extra = run ? run.order.map(t => run.tasks[t]?.routed?.agent).filter((a): a is string => !!a && !allAgents.includes(a)) : []
    const all = extra.length ? [...allAgents, ...new Set(extra)] : allAgents
    if (!compact || !run) return all
    const keep = new Set<string>()
    for (const tid of run.order) {
      const r = run.tasks[tid]?.routed
      if (!r) continue
      keep.add(r.agent)
      for (const [a, p] of Object.entries(r.probabilities)) if (p >= 0.08) keep.add(a)
    }
    return keep.size ? all.filter(a => keep.has(a)) : all
  }, [allAgents, run, compact])

  useEffect(() => { setSelected(null) }, [run?.qid])
  useEffect(() => {
    const on = () => setFull(!!document.fullscreenElement && document.fullscreenElement === box.current)
    document.addEventListener('fullscreenchange', on)
    return () => document.removeEventListener('fullscreenchange', on)
  }, [])
  const toggleFull = () => {
    const el = box.current
    if (!el) return
    if (document.fullscreenElement) void document.exitFullscreen()
    else if (el.requestFullscreen) void el.requestFullscreen().catch(() => setFull(f => !f))
    else setFull(f => !f)
  }

  return (
    <div ref={box} className={'trace-view' + (compact ? ' compact' : '') + (full ? ' full' : '')}>
      <div className="trace-tools">
        {legend && (
          <span className="st-legend" aria-hidden="true">
            <span><i className="st-running" />running</span><span><i className="st-done" />done</span><span><i className="st-warn" />no answer</span><span><i className="st-error" />failed</span>
          </span>
        )}
        <div className="btn-group" role="group" aria-label="Zoom">
          <button type="button" className="mini" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1 / 1.25 }))} aria-label="Zoom out" title="Zoom out"><Icon name="zoom-out" size={15} /></button>
          <button type="button" className="mini" onClick={() => setResetKey(k => k + 1)} aria-label="Fit to view" title="Fit to view"><Icon name="fit" size={15} /></button>
          <button type="button" className="mini" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1.25 }))} aria-label="Zoom in" title="Zoom in"><Icon name="zoom-in" size={15} /></button>
        </div>
        <button type="button" className="mini" onClick={toggleFull} aria-label={full ? 'Exit fullscreen' : 'Fullscreen'} title={full ? 'Exit fullscreen' : 'Fullscreen'}>
          <Icon name={full ? 'minimize' : 'maximize'} size={15} />
        </button>
      </div>
      <Graph run={run} agents={agents} agentStats={agentStats} jev={jev} selected={selected} onSelect={setSelected} resetKey={resetKey} zoomBy={zoomBy} />
      {selected && <Inspector id={selected} store={store} run={run} onClose={() => setSelected(null)} />}
    </div>
  )
}

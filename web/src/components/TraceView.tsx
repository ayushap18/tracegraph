import { useEffect, useMemo, useRef, useState } from 'react'
import Graph from './Graph'
import { Inspector } from './Panels'
import { useTrace } from './useTrace'
import { AgentIcon, Icon } from '../icons'
import { Spinner } from '../ui'
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
    return all.filter(a => keep.has(a))
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
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => setFull(false))
    else if (full) setFull(false)
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
        {compact && !full && <span className="trace-summary-label">Execution steps</span>}
        {(!compact || full) && <div className="btn-group" role="group" aria-label="Zoom">
          <button type="button" className="mini" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1 / 1.25 }))} aria-label="Zoom out" title="Zoom out"><Icon name="zoom-out" size={15} /></button>
          <button type="button" className="mini" onClick={() => setResetKey(k => k + 1)} aria-label="Fit to view" title="Fit to view"><Icon name="fit" size={15} /></button>
          <button type="button" className="mini" onClick={() => setZoomBy(z => ({ n: z.n + 1, k: 1.25 }))} aria-label="Zoom in" title="Zoom in"><Icon name="zoom-in" size={15} /></button>
        </div>}
        <button type="button" className="mini" onClick={toggleFull} aria-label={full ? 'Exit fullscreen' : 'Fullscreen'} title={full ? 'Exit fullscreen' : 'Fullscreen'}>
          <Icon name={full ? 'minimize' : 'maximize'} size={15} />
        </button>
      </div>
      {compact && !full ? <CompactTrace run={run} onSelect={setSelected} /> : <Graph run={run} agents={agents} agentStats={agentStats} jev={jev} selected={selected} onSelect={setSelected} resetKey={resetKey} zoomBy={zoomBy} />}
      {selected && <Inspector id={selected} store={store} run={run} onClose={() => setSelected(null)} />}
    </div>
  )
}


function CompactTrace({ run, onSelect }: { run: Run | undefined; onSelect: (id: string) => void }) {
  if (!run) return <p className="trace-empty">Send a message to follow its progress.</p>
  const tasks = run.order.map(id => run.tasks[id]).filter(Boolean)
  const finished = tasks.filter(t => t.answered || t.error).length
  return (
    <div className="trace-flow">
      <button type="button" className="trace-stage" onClick={() => onSelect('query')}>
        <span className="trace-stage-icon"><Icon name="query" size={16} /></span>
        <span><strong>Your request</strong><small>{run.text || 'Receiving request…'}</small></span>
      </button>
      <div className={'trace-stage' + (!run.plan && !run.done ? ' active' : '')} role="status">
        <span className="trace-stage-icon">{!run.plan && !run.done ? <Spinner size={16} /> : <Icon name={run.plan ? 'check' : 'alert'} size={16} />}</span>
        <span><strong>{run.plan ? `${tasks.length} step${tasks.length === 1 ? '' : 's'} planned` : run.done ? 'Planning stopped' : 'Planning your request'}</strong><small>{run.plan ? `${finished} of ${tasks.length} completed` : run.done ? run.error || 'This run ended before a plan was ready.' : 'Finding the right steps and agents.'}</small></span>
      </div>
      {tasks.map((task, index) => {
        const settled = !!task.answered || !!task.error
        const waiting = task.depends_on.some(id => !run.tasks[id]?.answered && !run.tasks[id]?.error)
        const state = task.error ? 'Routing failed' : task.answered ? task.answered.ok ? 'Completed' : 'No answer' : run.done ? 'Stopped' : waiting ? 'Waiting for dependencies' : task.routed ? 'Working' : 'Selecting an agent'
        return <button type="button" key={task.tid} className={'trace-stage trace-task' + (!settled && !waiting && !run.done ? ' active' : '')} onClick={() => onSelect('t:' + task.tid)}>
          <span className="trace-stage-icon">{task.routed ? <AgentIcon agent={task.routed.agent} size={16} tinted /> : <span>{index + 1}</span>}</span>
          <span><strong>{task.text || `Step ${index + 1}`}</strong><small>{task.routed && <b>{task.routed.agent} · </b>}{state}</small></span>
          {task.answered?.ok && <Icon name="check" size={14} />}
        </button>
      })}
      <button type="button" className={'trace-stage trace-result' + (run.done ? ' complete' : '')} onClick={() => onSelect('answer')}>
        <span className="trace-stage-icon"><Icon name={run.done && run.status === 'done' ? 'check' : 'answer'} size={16} /></span>
        <span><strong>{run.done ? run.status === 'done' ? 'Answer ready' : `Run ${run.status}` : 'Response'}</strong><small>{run.done ? run.total_ms == null ? 'Run finished' : `${(run.total_ms / 1000).toFixed(1)} seconds total` : tasks.length && finished === tasks.length ? 'Combining the results…' : 'Waiting for the steps above.'}</small></span>
      </button>
    </div>
  )
}

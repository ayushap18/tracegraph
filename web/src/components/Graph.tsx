import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import * as d3 from 'd3'
import { colorOf } from '../protocol'
import type { Run } from '../useEventStream'
import { useSize } from '../lib'
import { AgentIcon, Icon, type UiIconName } from '../icons'

// A layered trace graph: Query → Subtasks → Router → Agents → Response.
// Positions are computed, not simulated, so the picture is stable and every run reads the same way.

export interface AgentStat { count: number; avgMs: number | null; avgConf: number | null }
export interface GraphProps {
  run: Run | undefined
  agents: string[] // active agents + guards, display order
  agentStats: Record<string, AgentStat>
  jev: { model: string; avgMs: number | null; avgConf: number | null }
  selected: string | null
  onSelect: (id: string | null) => void
  resetKey: number
  zoomBy: { n: number; k: number } // toolbar zoom requests
}

type Status = 'idle' | 'running' | 'done' | 'warn' | 'error'
interface Card { id: string; x: number; y: number; w: number; h: number; color: string; icon: string; title: string; lines: string[]; status: Status; badge?: string; dim?: boolean }
interface Edge { id: string; from: string; to: string; d: string; x2: number; y2: number; color: string | null; width: number; state: 'idle' | 'done' | 'running'; label?: string; dep?: boolean }

// Card icons: 'u:<ui icon>' for pipeline nodes, 'a:<agent>' for agents, anything else is drawn as text (subtask numbers).
const ICONS = { query: 'u:query', jev: 'u:jev', answer: 'u:answer' }
const LAYOUT_MIN_W = 760 // below this the graph lays out at this width and scales down to fit
const PAD = 16, HEAD = 34, AGENT_H = 40, AGENT_GAP = 10, SUB_H = 48, SUB_GAP = 12

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, Math.max(1, n - 1)).trimEnd() + '…' : s)
const pct = (v: number | null | undefined) => (v == null ? '–' : Math.round(v * 100) + '%')
const msf = (v: number | null | undefined) => (v == null ? '–' : v >= 1000 ? (v / 1000).toFixed(1) + ' s' : Math.round(v) + ' ms')
const curve = (x1: number, y1: number, x2: number, y2: number) => {
  const dx = Math.max(24, (x2 - x1) * 0.5)
  return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`
}

function layout(width: number, p: GraphProps) {
  const { run, agents, agentStats, jev } = p
  const W = Math.max(LAYOUT_MIN_W, width)
  // Column widths scale with the canvas; the router → agents gap gets the most room because that is where edges fan out.
  const f = Math.max(0.82, Math.min(1.35, W / 1130))
  const colW = [150, 175, 150, 165, 140].map(v => Math.round(v * f))
  const gapW = [0.9, 0.75, 1.55, 0.8]
  const free = W - PAD * 2 - colW.reduce((a, b) => a + b, 0)
  const unit = free / gapW.reduce((a, b) => a + b, 0)
  const xs: number[] = []
  colW.forEach((_, i) => xs.push(i ? xs[i - 1] + colW[i - 1] + gapW[i - 1] * unit : PAD))
  const colX = (i: number) => xs[i]
  const order = run?.order ?? []
  const agentsH = agents.length * AGENT_H + Math.max(0, agents.length - 1) * AGENT_GAP
  const subsH = Math.max(1, order.length) * SUB_H + Math.max(0, order.length - 1) * SUB_GAP
  const bodyH = Math.max(agentsH, subsH, 200)
  const H = HEAD + bodyH + PAD * 2
  const midY = HEAD + PAD + bodyH / 2
  const chars = (i: number) => Math.floor((colW[i] - 50) / 6.4)

  // Per-run state for every node.
  const tasks = order.map(tid => run!.tasks[tid]).filter(Boolean)
  const used = new Map<string, Status>() // agent -> status in this run
  for (const t of tasks) {
    const a = t.routed?.agent
    if (!a) continue
    const s: Status = t.error ? 'error' : !t.answered ? (run?.done ? 'warn' : 'running') : t.answered.ok ? 'done' : 'warn'
    const prev = used.get(a)
    used.set(a, prev === 'running' || s === 'running' ? 'running' : prev === 'error' || s === 'error' ? 'error' : prev === 'warn' || s === 'warn' ? 'warn' : 'done')
  }
  const probs: Record<string, number> = {}
  for (const t of tasks) for (const [a, v] of Object.entries(t.routed?.probabilities ?? {})) probs[a] = Math.max(probs[a] ?? 0, v)
  const anyUnrouted = tasks.some(t => !t.routed && !t.error)
  const multi = order.length > 1

  const cards: Card[] = []
  const add = (c: Card) => { cards.push(c); return c }

  const qStatus: Status = !run ? 'idle' : run.plan ? 'done' : run.done ? 'error' : 'running'
  add({
    id: 'query', x: colX(0), y: midY - 38, w: colW[0], h: 76, color: 'var(--accent)', icon: ICONS.query, title: run ? `Query #${run.qid}` : 'Query',
    lines: run ? [clip(run.text || '…', chars(0) + 5), run.plan ? `${run.plan.planner} plan · ${order.length} subtask${order.length === 1 ? '' : 's'}${run.plan.ms != null ? ' · ' + msf(run.plan.ms) : ''}` : run.done ? 'planning stopped' : 'planning…']
      : ['waiting for a query', ''],
    status: qStatus,
  })

  const subY0 = midY - subsH / 2
  // v4 DAG plans: a step whose dependencies have not answered yet is waiting, not routing.
  const waitingOn = (t: (typeof tasks)[number]) => t.depends_on.filter(d => { const dt = run!.tasks[d]; return dt && !dt.answered && !dt.error })
  tasks.forEach((t, i) => {
    const r = t.routed
    const waits = !r && !t.error ? waitingOn(t) : []
    add({
      id: 't:' + t.tid, x: colX(1), y: subY0 + i * (SUB_H + SUB_GAP), w: colW[1], h: SUB_H,
      color: r ? colorOf(r.agent) : 'var(--muted)', icon: t.tid.split('.')[1] ?? '·', title: clip(t.text || t.tid, chars(1) - 1),
      lines: [t.error ? 'routing failed' : r ? `→ ${r.agent} · ${pct(r.confidence)}` : waits.length ? `waits for ${waits.join(', ')}` : 'routing…'],
      status: t.error ? 'error' : run?.done && !t.answered ? 'warn' : waits.length ? 'idle' : !r || !t.answered ? 'running' : t.answered.ok ? 'done' : 'warn',
    })
  })
  if (!tasks.length) add({ id: 'none', x: colX(1), y: midY - SUB_H / 2, w: colW[1], h: SUB_H, color: 'var(--line)', icon: '·', title: run ? run.done ? 'no plan' : 'planning…' : 'no subtasks', lines: [''], status: 'idle', dim: true })

  add({
    id: 'jev', x: colX(2), y: midY - 46, w: colW[2], h: 92, color: 'var(--accent)', icon: ICONS.jev, title: 'Jev router',
    lines: [jev.model || 'typesafe system_one', `avg ${msf(jev.avgMs)} · ${pct(jev.avgConf)} conf`, anyUnrouted ? 'routing…' : run ? `${tasks.filter(t => t.routed).length}/${tasks.length} routed` : 'idle'],
    status: !run || !tasks.length ? 'idle' : anyUnrouted ? (run.done ? 'error' : 'running') : 'done',
  })

  const agY0 = midY - agentsH / 2
  agents.forEach((a, i) => {
    const st = agentStats[a]
    const s = used.get(a)
    add({
      id: 'a:' + a, x: colX(3), y: agY0 + i * (AGENT_H + AGENT_GAP), w: colW[3], h: AGENT_H, color: colorOf(a), icon: 'a:' + a,
      title: a, lines: [st?.count ? `${msf(st.avgMs)}${st.avgConf != null ? ' · ' + pct(st.avgConf) : ''}` : 'no runs yet'],
      badge: String(st?.count ?? 0), status: s ?? 'idle', dim: !!run && !s,
    })
  })

  const merged = run?.merged
  add({
    id: 'answer', x: colX(4), y: midY - 38, w: colW[4], h: 76, color: 'var(--ok)', icon: ICONS.answer, title: 'Response',
    lines: !run ? ['', ''] : run.done
      ? [merged ? `${merged.engine === 'single' ? 'direct' : merged.engine} merge` : 'finished', `total ${msf(run.total_ms)}`]
      : [multi ? `merging ${tasks.filter(t => t.answered).length}/${tasks.length}` : 'waiting for agent', 'running…'],
    status: !run ? 'idle' : run.done ? (run.error || (tasks.length > 0 && tasks.every(t => t.error)) ? 'error' : 'done') : tasks.length && tasks.every(t => t.answered || t.error) ? 'running' : 'idle',
  })

  // Edges run from a card's right port to the next card's left port.
  const byId = new Map(cards.map(c => [c.id, c]))
  const edges: Edge[] = []
  const edge = (from: string, to: string, o: Partial<Edge> = {}) => {
    const a = byId.get(from), b = byId.get(to)
    if (!a || !b) return
    const x1 = a.x + a.w, y1 = a.y + a.h / 2, x2 = b.x, y2 = b.y + b.h / 2
    edges.push({ id: `${from}>${to}`, from, to, d: curve(x1, y1, x2, y2), x2, y2, color: null, width: 1.25, state: 'idle', ...o })
  }
  // Dependency arcs between steps bulge out to the left of the subtask column.
  for (const t of tasks) for (const d of t.depends_on) {
    const a = byId.get('t:' + d), b = byId.get('t:' + t.tid)
    if (!a || !b) continue
    const x1 = a.x, y1 = a.y + a.h / 2 + 6, x2 = b.x, y2 = b.y + b.h / 2 - 6
    const bulge = Math.min(46, 18 + Math.abs(y2 - y1) * 0.25)
    const dt = run!.tasks[d]
    edges.push({ id: `dep:${d}>${t.tid}`, from: 't:' + d, to: 't:' + t.tid, d: `M${x1},${y1} C${x1 - bulge},${y1} ${x2 - bulge},${y2} ${x2},${y2}`,
      x2, y2, color: 'var(--accent2)', width: 1.4, state: dt?.answered || run?.done ? 'done' : 'running', dep: true })
  }
  if (tasks.length) for (const t of tasks) {
    const sid = 't:' + t.tid
    edge('query', sid, { state: 'done', color: 'var(--accent)', width: 1.5 })
    edge(sid, 'jev', { state: t.routed || t.error || run?.done ? 'done' : 'running', color: t.routed ? colorOf(t.routed.agent) : 'var(--accent)', width: 1.5 })
  } else {
    edge('query', 'none', { state: run && !run.done ? 'running' : 'idle', color: run ? 'var(--accent)' : null })
    edge('none', 'jev')
  }
  for (const a of agents) {
    const p = probs[a] ?? 0, s = used.get(a)
    edge('jev', 'a:' + a, {
      state: s === 'running' ? 'running' : s ? 'done' : 'idle',
      color: s || p >= 0.05 ? colorOf(a) : null,
      width: s ? 1.5 + p * 3.5 : p >= 0.05 ? 1 + p * 2 : 1,
      label: run && (s || p >= 0.05) && a in probs ? pct(p) : undefined,
    })
    if (s) edge('a:' + a, 'answer', { state: s === 'running' ? 'idle' : run?.done ? 'done' : 'running', color: colorOf(a), width: 1.5 })
  }
  const heads = ['query', 'subtasks', 'route', 'agents', 'response']
  const headers = heads.map((t, i) => [t, colX(i) + colW[i] / 2] as [string, number])
  return { W, H, cards, edges, headers }
}

export default function Graph(props: GraphProps) {
  const { selected, onSelect, resetKey, zoomBy } = props
  const [wrapRef, size] = useSize<HTMLDivElement>()
  const svgRef = useRef<SVGSVGElement>(null)
  const gRef = useRef<SVGGElement>(null)
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null)
  const [hover, setHover] = useState<string | null>(null)

  const L = useMemo(() => layout(size.width, props), [size.width, props.run, props.agents, props.agentStats, props.jev])
  const scale = size.width ? Math.min(1, size.width / L.W) : 1

  // Pan by dragging; zoom only with ⌘/Ctrl + wheel or the toolbar, so the page still scrolls normally.
  useEffect(() => {
    const svg = d3.select(svgRef.current!)
    const zoom = d3.zoom<SVGSVGElement, unknown>().scaleExtent([0.5, 2.5])
      .filter(ev => (ev.type === 'wheel' ? ev.ctrlKey || ev.metaKey : !ev.button))
      .on('zoom', ev => d3.select(gRef.current).attr('transform', ev.transform.toString()))
    svg.call(zoom).on('dblclick.zoom', null)
    zoomRef.current = zoom
    return () => { svg.on('.zoom', null) }
  }, [])
  useEffect(() => {
    if (resetKey && svgRef.current && zoomRef.current) d3.select(svgRef.current).transition().duration(350).call(zoomRef.current.transform, d3.zoomIdentity)
  }, [resetKey])
  useEffect(() => {
    setHover(null)
    if (svgRef.current && zoomRef.current) d3.select(svgRef.current).call(zoomRef.current.transform, d3.zoomIdentity)
  }, [props.run?.qid])
  useEffect(() => {
    if (zoomBy.n && svgRef.current && zoomRef.current) d3.select(svgRef.current).transition().duration(250).call(zoomRef.current.scaleBy, zoomBy.k)
  }, [zoomBy])

  // Hover or selection highlights a node's own edges and fades the rest.
  const focus = hover ?? selected
  const linked = useMemo(() => {
    if (!focus) return null
    const set = new Set([focus])
    for (const e of L.edges) if (e.from === focus || e.to === focus) { set.add(e.from); set.add(e.to) }
    return set
  }, [focus, L.edges])

  const cardEl = (c: Card): ReactNode => {
    const faded = linked ? !linked.has(c.id) : c.dim
    const cls = ['card', 'st-' + c.status, c.id === selected ? 'sel' : '', faded ? 'faded' : '', c.id === 'none' ? 'ghost' : ''].join(' ')
    const big = c.h >= 70
    const iconY = big ? 10 : (c.h - 24) / 2
    return (
      <g key={c.id} className={cls} transform={`translate(${c.x},${c.y})`} tabIndex={c.id === 'none' ? -1 : 0} role="button"
        aria-label={`${c.title}: ${c.lines.filter(Boolean).join(', ')}`}
        onMouseEnter={() => setHover(c.id)} onMouseLeave={() => setHover(null)}
        onClick={ev => { ev.stopPropagation(); if (c.id !== 'none') onSelect(c.id) }}
        onKeyDown={ev => { if ((ev.key === 'Enter' || ev.key === ' ') && c.id !== 'none') { ev.preventDefault(); onSelect(c.id) } }}>
        <rect className="card-bg" width={c.w} height={c.h} rx={8} />
        <rect className="card-accent" width={3} height={c.h - 14} x={0} y={7} rx={1.5} style={{ fill: c.color }} />
        <rect className="card-icon" x={10} y={iconY} width={24} height={24} rx={6}
          style={{ fill: `color-mix(in srgb, ${c.color} 15%, transparent)`, stroke: `color-mix(in srgb, ${c.color} 38%, transparent)` }} />
        {c.icon.startsWith('u:') ? <Icon name={c.icon.slice(2) as UiIconName} x={15} y={iconY + 5} size={14} strokeWidth={2} style={{ color: c.color }} />
          : c.icon.startsWith('a:') ? <AgentIcon agent={c.icon.slice(2)} x={15} y={iconY + 5} size={14} strokeWidth={2} style={{ color: c.color }} />
          : <text className="card-glyph" x={22} y={iconY + 16} textAnchor="middle" style={{ fill: c.color }}>{c.icon}</text>}
        <text className="card-title" x={42} y={big ? 26 : c.lines[0] ? c.h / 2 - 3 : c.h / 2 + 4}>{c.title}</text>
        {c.lines.map((l, i) => l && <text key={i} className="card-line" x={big ? 12 : 42} y={big ? 50 + i * 16 : c.h / 2 + 12 + i * 14}>{l}</text>)}
        {c.badge != null && <text className="card-badge" x={c.w - 22} y={c.h / 2 + 4} textAnchor="end">{c.badge}</text>}
        <circle className="st-dot" cx={c.w - 11} cy={big ? 15 : c.h / 2} r={3.5} />
        <title>{[c.title, ...c.lines.filter(Boolean)].join('\n')}</title>
      </g>
    )
  }

  return (
    <div ref={wrapRef} className="dag-wrap">
      <svg ref={svgRef} className="dag" width={size.width} height={L.H * scale} viewBox={`0 0 ${L.W} ${L.H}`} preserveAspectRatio="xMinYMin meet"
        role="img" aria-label="Trace graph: query, subtasks, Jev router, agents, response" onClick={() => onSelect(null)}>
        <g ref={gRef}>
          {L.headers.map(([t, x]) => <text key={t} className="col-head" x={x} y={PAD + 8} textAnchor="middle">{t}</text>)}
          <g className="edges">
            {L.edges.map(e => {
              const faded = linked ? !(e.from === focus || e.to === focus) : false
              return (
                <g key={e.id} className={`edge e-${e.state}${e.color ? ' colored' : ''}${faded ? ' faded' : ''}${e.dep ? ' dep' : ''}`}>
                  <path className="wire" d={e.d} strokeWidth={e.width} style={e.color ? { stroke: e.color } : undefined} />
                  <path className="arrow" d={`M${e.x2 - 6},${e.y2 - 3.5} L${e.x2},${e.y2} L${e.x2 - 6},${e.y2 + 3.5} Z`} style={e.color ? { fill: e.color } : undefined} />
                  {e.state === 'running' && !e.dep && (
                    <circle className="packet" r={3.2} style={{ fill: e.color ?? 'var(--accent)' }}>
                      <animateMotion dur="1.1s" repeatCount="indefinite" path={e.d} keyPoints="0;1" keyTimes="0;1" calcMode="spline" keySplines="0.4 0 0.2 1" />
                    </circle>
                  )}
                  {e.label && (
                    <g className="plabel" transform={`translate(${e.x2 - 26},${e.y2})`}>
                      <rect x={-17} y={-8} width={34} height={16} rx={8} style={e.color ? { stroke: e.color } : undefined} />
                      <text y={3.5} textAnchor="middle">{e.label}</text>
                    </g>
                  )}
                </g>
              )
            })}
          </g>
          <g className="cards">{L.cards.map(cardEl)}</g>
        </g>
      </svg>
    </div>
  )
}

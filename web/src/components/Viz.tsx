import { useEffect, useMemo, useRef, useState } from 'react'
import * as d3 from 'd3'
import { colorOf } from '../protocol'
import type { Run } from '../useEventStream'
import { CHART_H, useSize, type Row } from '../lib'
import { AgentIcon } from '../icons'

// ---------- count-up numbers and sparklines for the KPI cards ----------

export function useCountUp(target: number, ms = 600) {
  const [v, setV] = useState(target)
  const from = useRef(target)
  useEffect(() => {
    const start = performance.now(), a = from.current
    if (a === target) return
    let raf = 0
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / ms)
      const cur = a + (target - a) * d3.easeCubicOut(t)
      setV(cur)
      from.current = cur
      if (t < 1) raf = requestAnimationFrame(step)
    }
    raf = requestAnimationFrame(step)
    return () => cancelAnimationFrame(raf)
  }, [target, ms])
  return v
}

export function Sparkline({ values, color = 'var(--accent)', height = 26, min }: { values: number[]; color?: string; height?: number; min?: number }) {
  const [ref, size] = useSize<HTMLDivElement>()
  const w = size.width
  const { line, area, last } = useMemo(() => {
    if (values.length < 2 || w < 10) return { line: '', area: '', last: null as null | [number, number] }
    const x = d3.scaleLinear().domain([0, values.length - 1]).range([2, w - 3])
    const lo = min ?? (d3.min(values) ?? 0), hi = d3.max(values) ?? 1
    const y = d3.scaleLinear().domain([lo, hi === lo ? lo + 1 : hi]).range([height - 3, 3])
    const pts = values.map((v, i) => [x(i), y(v)] as [number, number])
    return {
      line: d3.line().curve(d3.curveMonotoneX)(pts) ?? '',
      area: d3.area().curve(d3.curveMonotoneX).y0(height)(pts) ?? '',
      last: pts[pts.length - 1],
    }
  }, [values, w, height, min])
  return (
    <div ref={ref} className="spark">
      <svg width={w} height={height} aria-hidden="true">
        {area && <path d={area} fill={color} opacity={0.12} />}
        {line && <path d={line} fill="none" stroke={color} strokeWidth={1.6} />}
        {last && <circle cx={last[0]} cy={last[1]} r={2.6} fill={color} />}
      </svg>
    </div>
  )
}

// ---------- pipeline waterfall: when each stage of one run started and finished ----------

interface Span { key: string; label: string; start: number; end: number | null; color: string; kind: 'plan' | 'route' | 'agent' | 'merge' }

// Live runs use browser receipt times; runs from history only have durations, so they are laid end to end.
function spans(run: Run): { list: Span[]; total: number | null; live: boolean } {
  const m = run.marks
  const list: Span[] = []
  if (m.query != null) {
    const t0 = m.query
    const rel = (v: number | undefined) => (v == null ? null : v - t0)
    const terminal = run.done ? rel(m.done) ?? run.total_ms ?? 0 : null
    const planEnd = rel(m.plan) ?? terminal
    list.push({ key: 'plan', label: `plan · ${run.plan?.planner ?? '…'}`, start: 0, end: planEnd, color: 'var(--accent)', kind: 'plan' })
    for (const tid of run.order) {
      const t = run.tasks[tid], r = rel(m.routed[tid]), a = rel(m.answered[tid])
      const agent = t?.routed?.agent
      list.push({ key: 'r' + tid, label: `${tid} route`, start: planEnd ?? 0, end: r ?? terminal ?? (t?.error ? planEnd : null), color: 'var(--accent)', kind: 'route' })
      if (r != null) list.push({ key: 'a' + tid, label: `${tid} ${agent ?? ''}`, start: r, end: a ?? terminal, color: colorOf(agent), kind: 'agent' })
    }
    const answered = run.order.map(tid => rel(m.answered[tid])).filter((v): v is number => v != null)
    if (run.merged || m.merged != null || (run.order.length > 1 && answered.length === run.order.length)) {
      const s = answered.length ? Math.max(...answered) : planEnd ?? 0
      list.push({ key: 'merge', label: `merge · ${run.merged?.engine ?? '…'}`, start: s, end: rel(m.merged) ?? terminal, color: 'var(--warn)', kind: 'merge' })
    }
    return { list, total: rel(m.done), live: !run.done }
  }
  const planMs = run.plan?.ms ?? 0
  list.push({ key: 'plan', label: `plan · ${run.plan?.planner ?? '–'}`, start: 0, end: planMs, color: 'var(--accent)', kind: 'plan' })
  let last = planMs
  for (const tid of run.order) {
    const t = run.tasks[tid], jev = t?.routed?.jev_ms ?? 0, ag = t?.answered?.agent_ms ?? 0
    list.push({ key: 'r' + tid, label: `${tid} route`, start: planMs, end: planMs + jev, color: 'var(--accent)', kind: 'route' })
    if (t?.routed) list.push({ key: 'a' + tid, label: `${tid} ${t.routed.agent}`, start: planMs + jev, end: planMs + jev + ag, color: colorOf(t.routed.agent), kind: 'agent' })
    last = Math.max(last, planMs + jev + ag)
  }
  if (run.merged && run.merged.engine !== 'single') list.push({ key: 'merge', label: `merge · ${run.merged.engine}`, start: last, end: last + (run.merged.ms ?? 0), color: 'var(--warn)', kind: 'merge' })
  return { list, total: run.total_ms, live: false }
}

export function Waterfall({ run }: { run: Run | undefined }) {
  const [ref, size] = useSize<HTMLDivElement>()
  const [, tick] = useState(0)
  const live = !!run && !run.done && run.marks.query != null
  // While a run is in flight, redraw ~12 times a second so open bars grow toward "now".
  useEffect(() => {
    if (!live) return
    const id = window.setInterval(() => tick(n => n + 1), 80)
    return () => clearInterval(id)
  }, [live])
  // The measured wrapper must mount on the first render (useSize observes once), so the empty state lives inside it.
  if (!run) return <div ref={ref} className="waterfall"><p className="muted small empty">No runs yet.</p></div>
  const { list, total } = spans(run)
  const now = run.marks.query != null ? performance.now() - run.marks.query : 0
  const end = (s: Span) => s.end ?? now
  const maxT = Math.max(total ?? 0, ...list.map(end), 400)
  const w = size.width, rowH = 22, labelW = Math.min(150, w * 0.34), top = 18
  const h = top + list.length * rowH + 6
  const x = d3.scaleLinear().domain([0, maxT * 1.04]).range([labelW, Math.max(labelW + 10, w - 8)])
  return (
    <div ref={ref} className="waterfall">
      {w > 10 && (
        <svg width={w} height={h} role="img" aria-label={`Pipeline timeline for query ${run.qid}`}>
          {x.ticks(4).map(v => (
            <g key={v} className="axis">
              <line className="grid" x1={x(v)} x2={x(v)} y1={top - 4} y2={h} />
              <text x={x(v)} y={10} textAnchor="middle">{v >= 1000 ? (v / 1000).toFixed(1) + 's' : Math.round(v) + 'ms'}</text>
            </g>
          ))}
          {list.map((s, i) => {
            const y = top + i * rowH, open = s.end == null
            const bw = Math.max(3, x(end(s)) - x(s.start))
            return (
              <g key={s.key} className={'wf-row' + (open ? ' open' : '')}>
                <text className="wf-lbl" x={0} y={y + rowH / 2 + 4}><title>{s.label}</title>{s.label.length > Math.floor(labelW / 6.5) ? s.label.slice(0, Math.max(1, Math.floor(labelW / 6.5) - 2)) + '…' : s.label}</text>
                <rect x={x(s.start)} y={y + 4} width={bw} height={rowH - 8} rx={4} fill={s.color} opacity={s.kind === 'route' ? 0.55 : 0.9} />
                {!open && s.end! - s.start >= 1 && (() => {
                  // No room after the bar near the right edge: write the duration inside its end instead.
                  const inside = x(s.start) + bw + 46 > w && bw > 46
                  return <text className={'wf-ms' + (inside ? ' inside' : '')} x={inside ? x(s.start) + bw - 6 : x(s.start) + bw + 5}
                    y={y + rowH / 2 + 4} textAnchor={inside ? 'end' : 'start'}>{Math.round(s.end! - s.start)}ms</text>
                })()}
              </g>
            )
          })}
          {live && <line className="wf-now" x1={x(now)} x2={x(now)} y1={top - 4} y2={h} />}
        </svg>
      )}
    </div>
  )
}

// ---------- routing heatmap: Jev's probability for every agent on each recent subtask ----------

export function Heatmap({ rows, agents, onPick }: { rows: Row[]; agents: string[]; onPick?: (tid: string) => void }) {
  const [ref, size] = useSize<HTMLDivElement>()
  const w = size.width
  const cols = rows.filter(r => r.task.routed)
  const labelW = 74, top = 2, cellH = (CHART_H - top - 4) / (agents.length + 1)
  const maxCols = Math.max(1, Math.floor((w - labelW) / 12))
  const shown = cols.slice(-maxCols)
  const cw = shown.length ? (w - labelW) / Math.max(shown.length, 30) : 0 // fills the width once 30 subtasks exist
  const h = top + (agents.length + 1) * cellH + 4
  return (
    <div ref={ref} className="heatmap">
      {w > 10 && (
        <svg width={w} height={h} role="img" aria-label="Routing probability heatmap, agents by recent subtasks">
          <text className="hm-lbl" x={0} y={top + cellH / 2 + 3}>chosen</text>
          {agents.map((a, i) => <text key={a} className="hm-lbl" x={0} y={top + (i + 1) * cellH + cellH / 2 + 3}>{a}</text>)}
          {shown.map(({ run, task }, j) => {
            const r = task.routed!, x0 = labelW + j * cw
            return (
              <g key={task.tid} className="hm-col" onClick={() => onPick?.(task.tid)}>
                <title>{`${task.text || run.text} → ${r.agent} (${Math.round(r.confidence * 100)}%)`}</title>
                <rect x={x0 + 1} y={top + 2} width={cw - 2} height={cellH - 4} rx={3} fill={colorOf(r.agent)} />
                {agents.map((a, i) => {
                  const p = r.probabilities[a] ?? 0
                  return <rect key={a} x={x0 + 1} y={top + (i + 1) * cellH + 1} width={cw - 2} height={cellH - 2} rx={2}
                    fill={p > 0.005 ? colorOf(a) : 'var(--soft)'} opacity={p > 0.005 ? 0.12 + p * 0.88 : 1} />
                })}
              </g>
            )
          })}
          {!shown.length && <text className="hm-lbl" x={labelW} y={top + cellH / 2 + 3}>waiting for routed subtasks…</text>}
        </svg>
      )}
    </div>
  )
}

// ---------- traffic donut ----------

export function Donut({ data, onPick }: { data: Array<{ agent: string; count: number }>; onPick?: (agent: string) => void }) {
  const size = CHART_H - 16, r = size / 2
  const total = data.reduce((s, d) => s + d.count, 0)
  const live = data.filter(d => d.count > 0)
  const arcs = d3.pie<{ agent: string; count: number }>().value(d => d.count).sort(null).padAngle(0.02)(live)
  const arc = d3.arc<d3.PieArcDatum<{ agent: string; count: number }>>().innerRadius(r * 0.62).outerRadius(r - 2).cornerRadius(3)
  const shown = useCountUp(total)
  return (
    <div className="donut">
      <svg width={size} height={size} viewBox={`${-r} ${-r} ${size} ${size}`} role="img" aria-label={`Traffic by agent, ${total} subtasks`}>
        <circle r={r * 0.81} fill="none" stroke="var(--soft)" strokeWidth={r * 0.38} />
        {arcs.map(a => (
          <path key={a.data.agent} d={arc(a) ?? ''} fill={colorOf(a.data.agent)} className="slice" onClick={() => onPick?.(a.data.agent)}>
            <title>{`${a.data.agent}: ${a.data.count} (${Math.round((a.data.count / total) * 100)}%)`}</title>
          </path>
        ))}
        <text className="donut-v" y={4} textAnchor="middle">{Math.round(shown)}</text>
        <text className="donut-l" y={20} textAnchor="middle">subtasks</text>
      </svg>
      <ul className="donut-legend">
        {data.map(d => (
          <li key={d.agent} className={d.count ? '' : 'zero'} onClick={() => onPick?.(d.agent)}>
            <AgentIcon agent={d.agent} size={14} strokeWidth={2} style={{ color: colorOf(d.agent) }} />{d.agent}
            <span className="num">{d.count}</span>
            <span className="num muted">{total ? Math.round((d.count / total) * 100) + '%' : ''}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

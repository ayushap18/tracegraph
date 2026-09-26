import { useEffect, useRef } from 'react'
import * as d3 from 'd3'
import { MIN_CONFIDENCE, colorOf } from '../protocol'
import { CHART_H, useSize } from '../lib'

const H = CHART_H

function useChart<D>(data: D, draw: (svg: d3.Selection<SVGSVGElement, unknown, null, undefined>, w: number, h: number, data: D) => void) {
  const [wrapRef, size] = useSize<HTMLDivElement>()
  const svgRef = useRef<SVGSVGElement>(null)
  useEffect(() => {
    if (!svgRef.current || size.width < 10) return
    const svg = d3.select(svgRef.current)
    svg.selectAll('*').remove()
    draw(svg, size.width, H, data)
  }, [data, size.width]) // draw is recreated each render; data and width are what matter
  return { wrapRef, svgRef, width: size.width }
}

export interface ConfPoint { key: string; confidence: number; agent: string; label: string }

export function ConfidenceChart({ data }: { data: ConfPoint[] }) {
  const { wrapRef, svgRef, width } = useChart(data, (svg, w, h, pts) => {
    const m = { l: 34, r: 8, t: 8, b: 8 }
    const x = d3.scalePoint<string>().domain(pts.map(p => p.key)).range([m.l, w - m.r]).padding(0.5)
    const y = d3.scaleLinear().domain([0, 1]).range([h - m.b, m.t])
    const grid = svg.append('g').attr('class', 'axis')
    for (const v of [0, 0.5, 1]) {
      grid.append('line').attr('x1', m.l).attr('x2', w - m.r).attr('y1', y(v)).attr('y2', y(v)).attr('class', 'grid')
      grid.append('text').attr('x', m.l - 6).attr('y', y(v) + 3).attr('text-anchor', 'end').text(v * 100 + '%')
    }
    svg.append('line').attr('class', 'threshold').attr('x1', m.l).attr('x2', w - m.r).attr('y1', y(MIN_CONFIDENCE)).attr('y2', y(MIN_CONFIDENCE))
    if (!pts.length) return
    svg.append('path').datum(pts).attr('class', 'trend')
      .attr('d', d3.line<ConfPoint>().x(p => x(p.key) ?? 0).y(p => y(p.confidence)).curve(d3.curveMonotoneX))
    svg.append('g').selectAll('circle').data(pts).join('circle')
      .attr('cx', p => x(p.key) ?? 0).attr('cy', p => y(p.confidence))
      .attr('r', (_, i) => (i === pts.length - 1 ? 5 : 3.5)).attr('fill', p => colorOf(p.agent))
      .append('title').text(p => `${p.label} → ${p.agent} (${Math.round(p.confidence * 100)}%)`)
  })
  return <div ref={wrapRef} className="chart"><svg ref={svgRef} width={width} height={H} role="img" aria-label="Route confidence per subtask" /></div>
}

export function TrafficChart({ data }: { data: Array<{ agent: string; count: number }> }) {
  const h = Math.max(H, data.length * 22 + 8)
  const [wrapRef, size] = useSize<HTMLDivElement>()
  const svgRef = useRef<SVGSVGElement>(null)
  useEffect(() => {
    if (!svgRef.current || size.width < 10) return
    const svg = d3.select(svgRef.current)
    svg.selectAll('*').remove()
    const w = size.width, m = { l: 74, r: 36 }
    const y = d3.scaleBand<string>().domain(data.map(d => d.agent)).range([4, h - 4]).padding(0.3)
    const x = d3.scaleLinear().domain([0, Math.max(1, d3.max(data, d => d.count) ?? 1)]).range([m.l, w - m.r])
    const row = svg.selectAll('g').data(data).join('g').attr('transform', d => `translate(0,${y(d.agent) ?? 0})`)
    row.append('text').attr('class', 'axis-lbl').attr('x', 0).attr('y', y.bandwidth() / 2 + 4).text(d => d.agent)
    row.append('rect').attr('class', 'track').attr('x', m.l).attr('width', Math.max(0, w - m.r - m.l)).attr('height', y.bandwidth()).attr('rx', 4)
    row.append('rect').attr('x', m.l).attr('height', y.bandwidth()).attr('rx', 4).attr('fill', d => colorOf(d.agent))
      .attr('width', d => Math.max(0, x(d.count) - m.l))
    row.append('text').attr('class', 'axis-lbl num').attr('x', w).attr('text-anchor', 'end').attr('y', y.bandwidth() / 2 + 4).text(d => d.count)
  }, [data, size.width, h])
  return <div ref={wrapRef} className="chart"><svg ref={svgRef} width={size.width} height={h} role="img" aria-label="Subtasks per agent" /></div>
}

export interface LatencyPoint { key: string; label: string; jev: number; agent: number; merge: number }
const PARTS = ['jev', 'agent', 'merge'] as const
export const LATENCY_COLORS: Record<(typeof PARTS)[number], string> = { jev: 'var(--accent)', agent: 'var(--muted)', merge: 'var(--warn)' }

export function LatencyChart({ data }: { data: LatencyPoint[] }) {
  const { wrapRef, svgRef, width } = useChart(data, (svg, w, h, pts) => {
    const m = { l: 44, r: 4, t: 8, b: 4 }
    const max = d3.max(pts, p => p.jev + p.agent + p.merge) ?? 0
    const y = d3.scaleLinear().domain([0, Math.max(100, max) * 1.08]).range([h - m.b, m.t]).nice()
    const x = d3.scaleBand<string>().domain(pts.map(p => p.key)).range([m.l, w - m.r]).padding(0.2)
    const ax = svg.append('g').attr('class', 'axis')
    for (const v of y.ticks(3)) {
      ax.append('line').attr('class', 'grid').attr('x1', m.l).attr('x2', w - m.r).attr('y1', y(v)).attr('y2', y(v))
      ax.append('text').attr('x', m.l - 6).attr('y', y(v) + 3).attr('text-anchor', 'end').text(v >= 1000 ? v / 1000 + 's' : String(v))
    }
    const series = d3.stack<LatencyPoint, (typeof PARTS)[number]>().keys(PARTS)(pts)
    svg.append('g').selectAll('g').data(series).join('g').style('fill', s => LATENCY_COLORS[s.key])
      .selectAll('rect').data(s => s).join('rect')
      .attr('x', d => x(d.data.key) ?? 0).attr('width', x.bandwidth())
      .attr('y', d => y(d[1])).attr('height', d => Math.max(0, y(d[0]) - y(d[1])))
      .append('title').text(d => `${d.data.label}: Jev ${Math.round(d.data.jev)} · agent ${Math.round(d.data.agent)} · merge ${Math.round(d.data.merge)} ms`)
  })
  return <div ref={wrapRef} className="chart"><svg ref={svgRef} width={width} height={H} role="img" aria-label="Latency per query, stacked Jev, agent and merge" /></div>
}

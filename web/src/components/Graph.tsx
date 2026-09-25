import { useEffect, useRef } from 'react'
import * as d3 from 'd3'
import { colorOf, type ServerEvent } from '../protocol'
import type { Listener } from '../useEventStream'
import { useSize } from '../lib'

export interface GraphModel {
  agents: string[] // active agents + guards, in display order
  counts: Record<string, number>
  subtasks: Array<{ tid: string; text: string; agent?: string; done: boolean }>
  probs: Record<string, number> // max routing probability per agent across the subtasks on screen
  routed: string[] // agents chosen for the subtasks on screen
  busy: string[] // agents currently working
  jevBusy: boolean
  plannerBusy: boolean
}

type Kind = 'query' | 'planner' | 'jev' | 'agent' | 'subtask' | 'merger' | 'answer'
interface GNode extends d3.SimulationNodeDatum { id: string; kind: Kind; label: string; sub: string; color: string; r: number; tx: number; ty: number; title: string; share: number }
interface GLink extends d3.SimulationLinkDatum<GNode> { id: string; width: number; color: string | null; opacity: number; flow: boolean }
interface Particle { from: string; to: string; t0: number; dur: number; color: string; loop: boolean; key: string; born: number; size: number; alpha: number }
interface Ripple { node: string; born: number; color: string }

const MAX_WAIT = 8000 // a particle whose endpoint never appears is dropped after this
const MAX_LOOP = 45000
const RIPPLE_MS = 1100
const TRAIL = [0.03, 0.06, 0.09, 0.12]

const nid = { agent: (a: string) => 'a:' + a, sub: (tid: string) => 't:' + tid }
const qidOf = (tid: string) => tid.split('.')[0]

// Links are horizontal S-curves; particles ride the exact same cubic so they stay on the line.
const ends = (a: GNode, b: GNode) => {
  const sx = a.x ?? 0, sy = a.y ?? 0, tx = b.x ?? 0, ty = b.y ?? 0
  return { sx, sy, tx, ty, mx: (sx + tx) / 2 }
}
const pathOf = (a: GNode, b: GNode) => {
  const { sx, sy, tx, ty, mx } = ends(a, b)
  return `M${sx},${sy} C${mx},${sy} ${mx},${ty} ${tx},${ty}`
}
const pointOn = (a: GNode, b: GNode, t: number) => {
  const { sx, sy, tx, ty, mx } = ends(a, b), u = 1 - t
  return {
    x: u * u * u * sx + 3 * u * u * t * mx + 3 * u * t * t * mx + t * t * t * tx,
    y: u * u * u * sy + 3 * u * u * t * sy + 3 * u * t * t * ty + t * t * t * ty,
  }
}

export default function Graph({ model, subscribe, selected, onSelect, resetKey }: {
  model: GraphModel
  subscribe: (fn: Listener) => () => void
  selected: string | null
  onSelect: (id: string | null) => void
  resetKey: number
}) {
  const [wrapRef, size] = useSize<HTMLDivElement>()
  const svgRef = useRef<SVGSVGElement>(null)
  const nodes = useRef(new Map<string, GNode>())
  const particles = useRef<Particle[]>([])
  const ripples = useRef<Ripple[]>([])
  const sim = useRef<d3.Simulation<GNode, GLink> | null>(null)
  const layers = useRef<{ stages: SVGGElement; links: SVGGElement; flows: SVGGElement; parts: SVGGElement; nodes: SVGGElement } | null>(null)
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null)
  const fitK = useRef(0)
  const dragging = useRef(new Set<string>())
  const selectRef = useRef(onSelect)
  selectRef.current = onSelect

  // Build the simulation, zoom and animation loop once; later renders only update data joins.
  useEffect(() => {
    const svg = d3.select(svgRef.current!)
    const defs = svg.append('defs')
    const glow = defs.append('filter').attr('id', 'jr-glow').attr('x', '-100%').attr('y', '-100%').attr('width', '300%').attr('height', '300%')
    glow.append('feGaussianBlur').attr('stdDeviation', 3.2).attr('result', 'b')
    const merge = glow.append('feMerge')
    merge.append('feMergeNode').attr('in', 'b')
    merge.append('feMergeNode').attr('in', 'SourceGraphic')

    const root = svg.append('g')
    const stages = root.append('g').attr('class', 'g-stages')
    const links = root.append('g').attr('class', 'g-links')
    const flows = root.append('g').attr('class', 'g-flows')
    const nodeLayer = root.append('g').attr('class', 'g-nodes')
    const parts = root.append('g').attr('class', 'g-parts').attr('filter', 'url(#jr-glow)')
    layers.current = { stages: stages.node()!, links: links.node()!, flows: flows.node()!, parts: parts.node()!, nodes: nodeLayer.node()! }

    const zoom = d3.zoom<SVGSVGElement, unknown>().scaleExtent([0.35, 3]).on('zoom', ev => root.attr('transform', ev.transform.toString()))
    svg.call(zoom).on('dblclick.zoom', null)
    zoomRef.current = zoom
    fitK.current = 0
    svg.on('click', ev => { if (ev.target === svg.node()) selectRef.current(null) })

    const drawLinks = () => {
      const L = layers.current!
      for (const layer of [L.links, L.flows]) {
        d3.select(layer).selectAll<SVGPathElement, GLink>('path')
          .attr('d', d => pathOf(d.source as GNode, d.target as GNode))
      }
    }
    const s = d3.forceSimulation<GNode, GLink>([])
      .force('link', d3.forceLink<GNode, GLink>([]).id(d => d.id).distance(70).strength(0.02))
      .force('charge', d3.forceManyBody<GNode>().strength(-70).distanceMax(150))
      .force('collide', d3.forceCollide<GNode>(d => d.r + 12))
      .force('x', d3.forceX<GNode>(d => d.tx).strength(d => (d.kind === 'subtask' ? 0.45 : d.kind === 'agent' ? 0.4 : 0.7)))
      .force('y', d3.forceY<GNode>(d => d.ty).strength(d => (d.kind === 'subtask' ? 0.45 : d.kind === 'agent' ? 0.4 : 0.7)))
      .alphaDecay(0.035)
      .on('tick', () => {
        d3.select(layers.current!.nodes).selectAll<SVGGElement, GNode>('g.node').attr('transform', d => `translate(${d.x ?? 0},${d.y ?? 0})`)
        drawLinks()
      })
    sim.current = s

    let raf = 0
    const frame = (now: number) => {
      const map = nodes.current
      const dots: Array<{ x: number; y: number; r: number; o: number; color: string }> = []
      particles.current = particles.current.filter(p => {
        const a = map.get(p.from), b = map.get(p.to)
        if (!a || !b) { p.t0 = Math.max(p.t0, now); return now - p.born < MAX_WAIT } // endpoint not rendered yet: hold the particle
        if (now < p.t0) return true
        let t = (now - p.t0) / p.dur
        if (p.loop) { if (now - p.born > MAX_LOOP) return false; t %= 1 } else if (t >= 1) return false
        const fade = p.alpha * (t > 0.85 ? (1 - t) / 0.15 : 1)
        const head = pointOn(a, b, d3.easeCubicInOut(t))
        dots.push({ ...head, r: p.size, o: fade, color: p.color })
        // A short comet tail: the same curve sampled a little earlier, smaller and fainter.
        TRAIL.forEach((lag, i) => {
          const tt = t - lag
          if (tt <= 0) return
          const q = pointOn(a, b, d3.easeCubicInOut(tt))
          dots.push({ ...q, r: p.size * (1 - (i + 1) * 0.18), o: fade * (0.5 - i * 0.1), color: p.color })
        })
        return true
      })
      d3.select(layers.current!.parts).selectAll<SVGCircleElement, (typeof dots)[number]>('circle.dot').data(dots).join('circle')
        .attr('class', 'dot').attr('r', d => d.r).attr('cx', d => d.x).attr('cy', d => d.y).attr('fill', d => d.color).attr('opacity', d => d.o)

      ripples.current = ripples.current.filter(r => now - r.born < RIPPLE_MS && map.has(r.node))
      const rings = ripples.current.map(r => {
        const n = map.get(r.node)!, t = (now - r.born) / RIPPLE_MS
        return { x: n.x ?? 0, y: n.y ?? 0, r: n.r + 4 + d3.easeCubicOut(t) * 46, o: (1 - t) * 0.8, color: r.color }
      })
      d3.select(layers.current!.parts).selectAll<SVGCircleElement, (typeof rings)[number]>('circle.ripple').data(rings).join('circle')
        .attr('class', 'ripple').attr('cx', d => d.x).attr('cy', d => d.y).attr('r', d => d.r)
        .attr('stroke', d => d.color).attr('opacity', d => d.o)
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)

    return () => {
      cancelAnimationFrame(raf)
      s.stop()
      svg.on('.zoom', null).on('click', null)
      root.remove()
      defs.remove()
      sim.current = null
      layers.current = null
    }
  }, [])

  // Event-driven particles: each protocol step moves a comet along the matching edge.
  useEffect(() => subscribe((e: ServerEvent) => {
    const now = performance.now()
    if (document.hidden && e.type !== 'hello') return // the frame loop that prunes particles is paused while hidden
    const add = (from: string, to: string, key: string, color: string, o: Partial<Particle> & { delay?: number } = {}) => {
      const { delay = 0, ...rest } = o
      particles.current.push({ from, to, key, color, born: now, dur: 750, loop: false, size: 5, alpha: 1, ...rest, t0: now + delay })
    }
    const kill = (pred: (p: Particle) => boolean) => { particles.current = particles.current.filter(p => !pred(p)) }
    const ripple = (node: string, color: string) => ripples.current.push({ node, born: now, color })
    const accent = 'var(--accent)'
    switch (e.type) {
      case 'hello': particles.current = []; ripples.current = []; break
      case 'query': ripple('query', accent); add('query', 'planner', `q:${e.qid}`, accent, { loop: true, dur: 950 }); break
      case 'plan':
        kill(p => p.key === `q:${e.qid}`)
        ripple('planner', accent)
        for (const s of e.subtasks) {
          add('planner', nid.sub(s.tid), `p:${s.tid}`, accent, { dur: 600 })
          add(nid.sub(s.tid), 'jev', `s:${s.tid}`, accent, { loop: true, dur: 850, delay: 600 })
        }
        break
      case 'routed': {
        kill(p => p.key === `s:${e.tid}`)
        ripple('jev', colorOf(e.agent))
        add('jev', nid.agent(e.agent), `r:${e.tid}`, colorOf(e.agent), { loop: true, dur: 950, size: 5.5 })
        // Faint ghosts on runner-up edges show the alternatives Jev weighed.
        for (const [a, v] of Object.entries(e.probabilities)) if (a !== e.agent && v >= 0.08) add('jev', nid.agent(a), `g:${e.tid}`, colorOf(a), { dur: 1100, alpha: 0.35, size: 3.5 })
        break
      }
      case 'answered':
        kill(p => p.key === `r:${e.tid}`)
        ripple(nid.agent(e.agent), colorOf(e.agent))
        add(nid.agent(e.agent), 'merger', `d:${e.tid}`, colorOf(e.agent), { dur: 700 })
        break
      case 'merged': add('merger', 'answer', `m:${e.qid}`, accent, { dur: 700, size: 7 }); break
      case 'done': case 'error':
        if (e.type === 'done') ripple('answer', 'var(--ok)')
        if (e.type === 'error' && e.tid) { const t = e.tid; kill(p => p.loop && p.key.slice(2) === t) }
        else if (e.qid != null) { const q = String(e.qid); kill(p => p.loop && qidOf(p.key.slice(2)) === q) }
        break
    }
  }), [subscribe])

  // Data join: reuse node objects by id so positions survive, then nudge the simulation.
  useEffect(() => {
    const s = sim.current, L = layers.current
    if (!s || !L || size.width < 10) return
    // Narrow screens lay out on a 680px-wide canvas and zoom out to fit, rather than squashing nodes together.
    const k = Math.min(1, size.width / 680)
    const w = size.width / k, h = size.height / k, cy = h / 2 + 10
    if (k !== fitK.current && svgRef.current && zoomRef.current) {
      fitK.current = k
      d3.select(svgRef.current).call(zoomRef.current.transform, d3.zoomIdentity.scale(k))
    }
    const X = { query: 0.06, planner: 0.19, sub: 0.33, jev: 0.46, agent: 0.62, merger: 0.87, answer: 0.96 }
    d3.select(L.stages).selectAll<SVGTextElement, [string, number]>('text')
      .data([['input', X.query], ['plan', (X.planner + X.sub) / 2], ['route', X.jev], ['run', X.agent + 0.03], ['merge', (X.merger + X.answer) / 2]] as Array<[string, number]>)
      .join('text').attr('class', 'stage').attr('x', d => d[1] * w).attr('y', 22).attr('text-anchor', 'middle').text(d => d[0])

    const map = nodes.current
    const want: GNode[] = []
    const node = (id: string, kind: Kind, tx: number, ty: number, r: number, label: string, sub = '', color = '', title = label, share = 0) => {
      let n = map.get(id)
      if (!n) {
        const seed = kind === 'subtask' ? map.get('planner') : undefined
        n = { id, kind, x: seed?.x ?? tx, y: seed?.y ?? ty } as GNode
        map.set(id, n)
      }
      Object.assign(n, { kind, tx, ty, r, label, sub, color, title, share })
      // Hubs are pinned so the query → answer spine stays a straight line; agents and subtasks float.
      const hub = kind !== 'agent' && kind !== 'subtask'
      if (hub && !dragging.current.has(id)) { n.fx = tx; n.fy = ty }
      want.push(n)
      return n
    }
    node('query', 'query', w * X.query, cy, 22, 'query')
    node('planner', 'planner', w * X.planner, cy, 24, 'planner')
    node('jev', 'jev', w * X.jev, cy, 40, 'Jev', 'router')
    node('merger', 'merger', w * X.merger, cy, 24, 'merger')
    node('answer', 'answer', w * X.answer, cy, 22, 'answer')
    const n = model.agents.length
    const total = Object.values(model.counts).reduce((a, b) => a + b, 0) || 1
    // Even vertical spacing with a rightward bulge in the middle: an arc without bunching at the ends.
    model.agents.forEach((a, i) => {
      const u = n < 2 ? 0 : (i / (n - 1)) * 2 - 1
      const c = model.counts[a] ?? 0
      node(nid.agent(a), 'agent', w * (X.agent + 0.08 * (1 - u * u)), cy + u * (h - 70) * 0.46, 13 + Math.min(13, Math.sqrt(c) * 2.2), a, String(c), colorOf(a), `${a}: ${c} subtasks`, c / total)
    })
    const m = model.subtasks.length
    model.subtasks.forEach((t, i) => {
      node(nid.sub(t.tid), 'subtask', w * X.sub, cy + (i - (m - 1) / 2) * Math.min(66, (h * 0.75) / Math.max(1, m)), 13, t.tid, '', t.agent ? colorOf(t.agent) : '', t.text)
    })
    const keep = new Set(want.map(d => d.id))
    for (const id of [...map.keys()]) if (!keep.has(id)) map.delete(id)

    const links: GLink[] = []
    const link = (a: string, b: string, width = 1.5, color: string | null = null, opacity = 1, flow = false) =>
      links.push({ id: `${a}>${b}`, source: a, target: b, width, color, opacity, flow })
    const live = model.plannerBusy || model.jevBusy || model.busy.length > 0 || model.subtasks.some(t => !t.done)
    link('query', 'planner', 2.5, null, 1, model.plannerBusy)
    if (m) for (const t of model.subtasks) {
      link('planner', nid.sub(t.tid), 2, null, 1, !t.done)
      link(nid.sub(t.tid), 'jev', t.agent ? 3 : 2, t.agent ? colorOf(t.agent) : null, 0.85, !t.done)
    }
    else link('planner', 'jev', 2.5)
    for (const a of model.agents) {
      const p = model.probs[a] ?? 0, chosen = model.routed.includes(a), busy = model.busy.includes(a)
      link('jev', nid.agent(a), 1 + p * 11, p > 0.02 ? colorOf(a) : null, p > 0.02 ? Math.max(0.3, Math.min(0.9, p + 0.2)) : 1, chosen && live)
      link(nid.agent(a), 'merger', busy || chosen ? 3 : 1.2, busy || chosen ? colorOf(a) : null, busy || chosen ? 0.8 : 1, busy)
    }
    link('merger', 'answer', 2.5, null, 1, live)

    const pathAttrs = (sel: d3.Selection<SVGPathElement, GLink, SVGGElement, unknown>) => sel
      .attr('stroke-width', d => d.width).style('stroke', d => d.color ?? 'var(--edge)').style('stroke-opacity', d => d.opacity)
    pathAttrs(d3.select(L.links).selectAll<SVGPathElement, GLink>('path').data(links, d => d.id)
      .join(enter => enter.append('path').attr('class', 'edge')))
    // Moving dashes over the edges that currently carry work.
    d3.select(L.flows).selectAll<SVGPathElement, GLink>('path').data(links.filter(l => l.flow), d => d.id)
      .join(enter => enter.append('path').attr('class', 'flow'))
      .attr('stroke-width', d => Math.max(1.5, Math.min(3, d.width * 0.5))).style('stroke', d => d.color ?? 'var(--accent)')

    const drag = d3.drag<SVGGElement, GNode>()
      .on('start', (ev, d) => { if (!ev.active) s.alphaTarget(0.3).restart(); dragging.current.add(d.id); d.fx = d.x; d.fy = d.y })
      .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y })
      .on('end', (ev, d) => {
        if (!ev.active) s.alphaTarget(0)
        dragging.current.delete(d.id)
        const hub = d.kind !== 'agent' && d.kind !== 'subtask'
        d.fx = hub ? d.tx : null; d.fy = hub ? d.ty : null // hubs spring back to their slot
      })

    const arc = d3.arc<GNode>().innerRadius(d => d.r + 3).outerRadius(d => d.r + 6).startAngle(0).cornerRadius(2)
    const g = d3.select(L.nodes).selectAll<SVGGElement, GNode>('g.node').data(want, d => d.id)
      .join(enter => {
        const e = enter.append('g').attr('class', d => `node k-${d.kind}`).attr('tabindex', 0).attr('role', 'button')
          .attr('transform', d => `translate(${d.x ?? 0},${d.y ?? 0})`)
        e.append('title')
        e.append('circle').attr('class', 'halo')
        e.filter(d => d.kind === 'jev').append('circle').attr('class', 'ring')
        e.filter(d => d.kind === 'agent').append('circle').attr('class', 'arc-track')
        e.filter(d => d.kind === 'agent').append('path').attr('class', 'arc')
        e.append('circle').attr('class', 'body')
        e.append('text').attr('class', 'lbl').attr('text-anchor', 'middle')
        e.append('text').attr('class', 'sub').attr('text-anchor', 'middle')
        e.on('click', (ev, d) => { ev.stopPropagation(); selectRef.current(d.id) })
          .on('keydown', (ev: KeyboardEvent, d) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectRef.current(d.id) } })
        e.call(drag)
        return e
      })
    g.classed('busy', d => (d.kind === 'agent' && model.busy.includes(d.label)) || (d.kind === 'jev' && model.jevBusy) || (d.kind === 'planner' && model.plannerBusy))
      .classed('chosen', d => d.kind === 'agent' && model.routed.includes(d.label))
      .style('--c', d => d.color || 'var(--accent)')
    g.select('title').text(d => d.title)
    g.select<SVGCircleElement>('circle.halo').attr('r', d => d.r + 4)
    g.select<SVGCircleElement>('circle.body').attr('r', d => d.r)
      .style('stroke', d => d.color || null)
      .style('fill', d => (d.color && d.kind !== 'jev' ? `color-mix(in srgb, ${d.color} 24%, var(--panel))` : null))
    g.select<SVGCircleElement>('circle.ring').attr('r', d => d.r + 8)
    g.select<SVGCircleElement>('circle.arc-track').attr('r', d => d.r + 4.5)
    g.select<SVGPathElement>('path.arc').attr('d', d => arc.endAngle(Math.max(0.001, d.share) * Math.PI * 2)(d)).style('fill', d => d.color)
    // Agent and subtask names sit under the node; hubs carry their name inside.
    // Agent names sit to the right (the arc is dense vertically), subtask ids underneath, hub names inside.
    g.select<SVGTextElement>('text.lbl').text(d => d.label)
      .attr('text-anchor', d => (d.kind === 'agent' ? 'start' : 'middle'))
      .attr('x', d => (d.kind === 'agent' ? d.r + 10 : 0))
      .attr('y', d => (d.kind === 'subtask' ? d.r + 16 : d.kind === 'jev' ? -2 : 4))
    g.select<SVGTextElement>('text.sub').text(d => d.sub).attr('y', d => (d.kind === 'jev' ? 15 : 4))

    s.nodes(want)
    s.force<d3.ForceLink<GNode, GLink>>('link')!.links(links)
    s.alpha(Math.max(s.alpha(), 0.3)).restart()
  }, [model, size])

  useEffect(() => {
    if (layers.current) d3.select(layers.current.nodes).selectAll<SVGGElement, GNode>('g.node').classed('sel', d => d.id === selected)
  }, [selected, model])

  useEffect(() => {
    if (!resetKey || !svgRef.current || !zoomRef.current) return
    d3.select(svgRef.current).transition().duration(450).call(zoomRef.current.transform, d3.zoomIdentity.scale(fitK.current || 1))
  }, [resetKey])

  return (
    <div ref={wrapRef} className="graph-wrap">
      <svg ref={svgRef} className="graph" width={size.width} height={size.height} role="img"
        aria-label="Routing graph: query, planner, subtasks, Jev, agents, merger, answer" />
    </div>
  )
}

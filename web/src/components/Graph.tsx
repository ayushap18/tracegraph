import { useEffect, useRef } from 'react'
import * as d3 from 'd3'
import { colorOf, type ServerEvent } from '../protocol'
import type { Listener } from '../useEventStream'
import { useSize } from '../lib'

export interface GraphModel {
  agents: string[] // active agents + guards, in display order
  counts: Record<string, number>
  subtasks: Array<{ tid: string; text: string; agent?: string }>
  probs: Record<string, number> // latest routing probabilities
  busy: string[] // agents currently working
  jevBusy: boolean
  plannerBusy: boolean
}

type Kind = 'query' | 'planner' | 'jev' | 'agent' | 'subtask' | 'merger' | 'answer'
interface GNode extends d3.SimulationNodeDatum { id: string; kind: Kind; label: string; sub: string; color: string; r: number; tx: number; ty: number; title: string }
interface GLink extends d3.SimulationLinkDatum<GNode> { id: string; width: number; color: string | null; opacity: number }
interface Particle { from: string; to: string; t0: number; dur: number; color: string; loop: boolean; key: string; born: number; size: number; alpha: number }

const MAX_WAIT = 8000 // a particle whose endpoint never appears is dropped after this
const MAX_LOOP = 45000

const nid = { agent: (a: string) => 'a:' + a, sub: (tid: string) => 't:' + tid }
const qidOf = (tid: string) => tid.split('.')[0]

export default function Graph({ model, subscribe, selected, onSelect }: {
  model: GraphModel
  subscribe: (fn: Listener) => () => void
  selected: string | null
  onSelect: (id: string | null) => void
}) {
  const [wrapRef, size] = useSize<HTMLDivElement>()
  const svgRef = useRef<SVGSVGElement>(null)
  const nodes = useRef(new Map<string, GNode>())
  const particles = useRef<Particle[]>([])
  const sim = useRef<d3.Simulation<GNode, GLink> | null>(null)
  const layers = useRef<{ links: SVGGElement; parts: SVGGElement; nodes: SVGGElement } | null>(null)
  const zoomRef = useRef<d3.ZoomBehavior<SVGSVGElement, unknown> | null>(null)
  const fitK = useRef(0)
  const selectRef = useRef(onSelect)
  selectRef.current = onSelect

  // Build the simulation, zoom and particle loop once; later renders only update data joins.
  useEffect(() => {
    const svg = d3.select(svgRef.current!)
    const root = svg.append('g')
    const links = root.append('g').attr('class', 'g-links')
    const parts = root.append('g').attr('class', 'g-parts')
    const nodeLayer = root.append('g').attr('class', 'g-nodes')
    layers.current = { links: links.node()!, parts: parts.node()!, nodes: nodeLayer.node()! }

    const zoom = d3.zoom<SVGSVGElement, unknown>().scaleExtent([0.4, 3]).on('zoom', ev => root.attr('transform', ev.transform.toString()))
    svg.call(zoom).on('dblclick.zoom', null)
    zoomRef.current = zoom
    fitK.current = 0
    svg.on('click', ev => { if (ev.target === svg.node()) selectRef.current(null) })

    const s = d3.forceSimulation<GNode, GLink>([])
      .force('link', d3.forceLink<GNode, GLink>([]).id(d => d.id).distance(70).strength(0.03))
      .force('charge', d3.forceManyBody<GNode>().strength(-60).distanceMax(160))
      .force('collide', d3.forceCollide<GNode>(d => d.r + 8))
      .force('x', d3.forceX<GNode>(d => d.tx).strength(d => (d.kind === 'subtask' ? 0.2 : 0.35)))
      .force('y', d3.forceY<GNode>(d => d.ty).strength(d => (d.kind === 'subtask' ? 0.2 : 0.35)))
      .alphaDecay(0.04)
      .on('tick', () => {
        d3.select(layers.current!.nodes).selectAll<SVGGElement, GNode>('g.node').attr('transform', d => `translate(${d.x ?? 0},${d.y ?? 0})`)
        d3.select(layers.current!.links).selectAll<SVGLineElement, GLink>('line')
          .attr('x1', d => (d.source as GNode).x ?? 0).attr('y1', d => (d.source as GNode).y ?? 0)
          .attr('x2', d => (d.target as GNode).x ?? 0).attr('y2', d => (d.target as GNode).y ?? 0)
      })
    sim.current = s

    let raf = 0
    const frame = (now: number) => {
      const map = nodes.current
      const live: Array<Particle & { x: number; y: number; o: number }> = []
      particles.current = particles.current.filter(p => {
        const a = map.get(p.from), b = map.get(p.to)
        if (!a || !b) { p.t0 = Math.max(p.t0, now); return now - p.born < MAX_WAIT } // endpoint not rendered yet: hold the particle
        if (now < p.t0) return true
        let t = (now - p.t0) / p.dur
        if (p.loop) { if (now - p.born > MAX_LOOP) return false; t %= 1 } else if (t >= 1) return false
        const e = d3.easeCubicInOut(t)
        live.push({ ...p, x: (a.x ?? 0) + ((b.x ?? 0) - (a.x ?? 0)) * e, y: (a.y ?? 0) + ((b.y ?? 0) - (a.y ?? 0)) * e, o: p.alpha * (t > 0.85 ? (1 - t) / 0.15 : 1) })
        return true
      })
      d3.select(layers.current!.parts).selectAll('circle').data(live).join('circle')
        .attr('r', d => d.size).attr('cx', d => d.x).attr('cy', d => d.y).attr('fill', d => d.color).attr('opacity', d => d.o)
      raf = requestAnimationFrame(frame)
    }
    raf = requestAnimationFrame(frame)

    return () => {
      cancelAnimationFrame(raf)
      s.stop()
      svg.on('.zoom', null).on('click', null)
      root.remove()
      sim.current = null
      layers.current = null
    }
  }, [])

  // Event-driven particles: each protocol step moves a dot along the matching edge.
  useEffect(() => subscribe((e: ServerEvent) => {
    const now = performance.now()
    if (document.hidden && e.type !== 'hello') return // the frame loop that prunes particles is paused while hidden
    const add = (from: string, to: string, key: string, color: string, o: Partial<Particle> & { delay?: number } = {}) => {
      const { delay = 0, ...rest } = o
      particles.current.push({ from, to, key, color, born: now, dur: 700, loop: false, size: 5.5, alpha: 1, ...rest, t0: now + delay })
    }
    const kill = (pred: (p: Particle) => boolean) => { particles.current = particles.current.filter(p => !pred(p)) }
    const accent = 'var(--accent)'
    switch (e.type) {
      case 'hello': particles.current = []; break
      case 'query': add('query', 'planner', `q:${e.qid}`, accent, { loop: true, dur: 900 }); break
      case 'plan':
        kill(p => p.key === `q:${e.qid}`)
        if (!e.subtasks.length) break
        for (const s of e.subtasks) {
          add('planner', nid.sub(s.tid), `p:${s.tid}`, accent, { dur: 600 })
          add(nid.sub(s.tid), 'jev', `s:${s.tid}`, accent, { loop: true, dur: 800, delay: 600 })
        }
        break
      case 'routed': {
        kill(p => p.key === `s:${e.tid}`)
        add('jev', nid.agent(e.agent), `r:${e.tid}`, colorOf(e.agent), { loop: true, dur: 900 })
        // Faint ghosts on runner-up edges show the alternatives Jev weighed.
        for (const [a, v] of Object.entries(e.probabilities)) if (a !== e.agent && v >= 0.08) add('jev', nid.agent(a), `g:${e.tid}`, colorOf(a), { dur: 1000, alpha: 0.35, size: 4 })
        break
      }
      case 'answered':
        kill(p => p.key === `r:${e.tid}`)
        add(nid.agent(e.agent), 'merger', `d:${e.tid}`, colorOf(e.agent), { dur: 650 })
        break
      case 'merged': add('merger', 'answer', `m:${e.qid}`, accent, { dur: 650, size: 7 }); break
      case 'done': case 'error':
        if (e.type === 'error' && e.tid) { const t = e.tid; kill(p => p.loop && p.key.slice(2) === t) }
        else if (e.qid != null) { const q = String(e.qid); kill(p => p.loop && qidOf(p.key.slice(2)) === q) }
        break
    }
  }), [subscribe])

  // Data join: reuse node objects by id so positions survive, then nudge the simulation.
  useEffect(() => {
    const s = sim.current, L = layers.current
    if (!s || !L || size.width < 10) return
    // Narrow screens lay out on a 640px-wide canvas and zoom out to fit, rather than squashing nodes together.
    const k = Math.min(1, size.width / 640)
    const w = size.width / k, h = size.height / k, cy = h / 2
    if (k !== fitK.current && svgRef.current && zoomRef.current) {
      fitK.current = k
      d3.select(svgRef.current).call(zoomRef.current.transform, d3.zoomIdentity.scale(k))
    }
    const map = nodes.current
    const want: GNode[] = []
    const node = (id: string, kind: Kind, tx: number, ty: number, r: number, label: string, sub = '', color = '', title = label) => {
      let n = map.get(id)
      if (!n) {
        const seed = kind === 'subtask' ? map.get('planner') : undefined
        n = { id, kind, x: seed?.x ?? tx, y: seed?.y ?? ty } as GNode
        map.set(id, n)
      }
      Object.assign(n, { kind, tx, ty, r, label, sub, color, title })
      want.push(n)
      return n
    }
    node('query', 'query', w * 0.06, cy, 22, 'query')
    node('planner', 'planner', w * 0.2, cy, 22, 'planner')
    node('jev', 'jev', w * 0.47, cy, 38, 'Jev', 'router')
    node('merger', 'merger', w * 0.84, cy, 22, 'merger')
    node('answer', 'answer', w * 0.95, cy, 22, 'answer')
    const n = model.agents.length
    // Even vertical spacing with a rightward bulge in the middle: an arc without bunching at the ends.
    model.agents.forEach((a, i) => {
      const u = n < 2 ? 0 : (i / (n - 1)) * 2 - 1
      const c = model.counts[a] ?? 0
      node(nid.agent(a), 'agent', w * (0.6 + 0.09 * (1 - u * u)), cy + u * h * 0.43, 12 + Math.min(14, Math.sqrt(c) * 2.2), a, String(c), colorOf(a))
    })
    const m = model.subtasks.length
    model.subtasks.forEach((t, i) => {
      node(nid.sub(t.tid), 'subtask', w * 0.335, cy + (i - (m - 1) / 2) * Math.min(64, (h * 0.8) / Math.max(1, m)), 12, t.tid, '', t.agent ? colorOf(t.agent) : '', t.text)
    })
    const keep = new Set(want.map(d => d.id))
    for (const id of [...map.keys()]) if (!keep.has(id)) map.delete(id)

    const links: GLink[] = []
    const link = (a: string, b: string, width = 1.5, color: string | null = null, opacity = 1) => links.push({ id: `${a}>${b}`, source: a, target: b, width, color, opacity })
    link('query', 'planner', 2.5)
    if (m) for (const t of model.subtasks) { link('planner', nid.sub(t.tid), 2); link(nid.sub(t.tid), 'jev', 2) }
    else link('planner', 'jev', 2.5)
    for (const a of model.agents) {
      const p = model.probs[a] ?? 0
      link('jev', nid.agent(a), 1 + p * 11, p > 0.02 ? colorOf(a) : null, p > 0.02 ? Math.max(0.25, Math.min(0.9, p + 0.2)) : 1)
      link(nid.agent(a), 'merger', model.busy.includes(a) ? 3 : 1.2, model.busy.includes(a) ? colorOf(a) : null)
    }
    link('merger', 'answer', 2.5)

    d3.select(L.links).selectAll<SVGLineElement, GLink>('line').data(links, d => d.id)
      .join(enter => enter.append('line').attr('class', 'edge'))
      .attr('stroke-width', d => d.width)
      .style('stroke', d => d.color ?? 'var(--edge)')
      .style('stroke-opacity', d => d.opacity)

    const drag = d3.drag<SVGGElement, GNode>()
      .on('start', (ev, d) => { if (!ev.active) s.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y })
      .on('drag', (ev, d) => { d.fx = ev.x; d.fy = ev.y })
      .on('end', (ev, d) => { if (!ev.active) s.alphaTarget(0); d.fx = null; d.fy = null })

    const g = d3.select(L.nodes).selectAll<SVGGElement, GNode>('g.node').data(want, d => d.id)
      .join(enter => {
        const e = enter.append('g').attr('class', d => `node k-${d.kind}`).attr('tabindex', 0).attr('role', 'button')
          .attr('transform', d => `translate(${d.x ?? 0},${d.y ?? 0})`)
        e.append('title')
        e.filter(d => d.kind === 'jev').append('circle').attr('class', 'ring')
        e.append('circle').attr('class', 'body')
        e.append('text').attr('class', 'lbl').attr('text-anchor', 'middle')
        e.append('text').attr('class', 'sub').attr('text-anchor', 'middle')
        e.on('click', (ev, d) => { ev.stopPropagation(); selectRef.current(d.id) })
          .on('keydown', (ev: KeyboardEvent, d) => { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); selectRef.current(d.id) } })
        e.call(drag)
        return e
      })
    g.classed('busy', d => (d.kind === 'agent' && model.busy.includes(d.label)) || (d.kind === 'jev' && model.jevBusy) || (d.kind === 'planner' && model.plannerBusy))
    g.select('title').text(d => d.title)
    g.select<SVGCircleElement>('circle.body').attr('r', d => d.r)
      .style('stroke', d => d.color || null)
      .style('fill', d => (d.kind === 'agent' ? `color-mix(in srgb, ${d.color} 22%, var(--panel))` : null))
    g.select<SVGCircleElement>('circle.ring').attr('r', d => d.r + 7)
    // Agent names sit under the node; hubs carry their name inside.
    g.select<SVGTextElement>('text.lbl').text(d => d.label)
      .attr('y', d => (d.kind === 'agent' || d.kind === 'subtask' ? d.r + 13 : d.kind === 'jev' ? -2 : 4))
    g.select<SVGTextElement>('text.sub').text(d => d.sub).attr('y', d => (d.kind === 'jev' ? 14 : 4))

    s.nodes(want)
    s.force<d3.ForceLink<GNode, GLink>>('link')!.links(links)
    s.alpha(Math.max(s.alpha(), 0.35)).restart()
  }, [model, size])

  useEffect(() => {
    if (layers.current) d3.select(layers.current.nodes).selectAll<SVGGElement, GNode>('g.node').classed('sel', d => d.id === selected)
  }, [selected, model])

  return (
    <div ref={wrapRef} className="graph-wrap">
      <svg ref={svgRef} className="graph" width={size.width} height={size.height} role="img"
        aria-label="Routing graph: query, planner, subtasks, Jev, agents, merger, answer" />
    </div>
  )
}

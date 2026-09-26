import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from 'react'
import { AnimatePresence, motion, useInView, useReducedMotion } from 'motion/react'
import { cn } from '@/lib/utils'
import { AgentIcon, Icon, type UiIconName } from '../../icons'
import { colorOf } from '../../protocol'

/*
  A designed, theme-aware replay of a real run (#29 from the live view): one question planned into
  three subtasks, routed by Jev, answered by three agents in parallel and merged. It is drawn with
  the workspace's own node language (tinted icon tile, colour bar, status dot) instead of a
  screenshot, so it reads correctly in light and dark and stays sharp at any size.

  Desktop: a fixed 1100 x 420 canvas scaled to fit its column. Phones: a vertical version.
*/

const W = 1100
const H = 420
const CY = 228

const QUERY = 'What time is it in Seoul, convert 300 USD to KRW, and who was Nikola Tesla?'

type Agent = { name: string; cy: number; conf?: string; ms?: string; sub?: number }
const AGENTS: Agent[] = [
  { name: 'math', cy: CY - 140 },
  { name: 'time', cy: CY - 70, conf: '100%', ms: '703 ms', sub: 0 },
  { name: 'currency', cy: CY, conf: '100%', ms: '858 ms', sub: 1 },
  { name: 'knowledge', cy: CY + 70, conf: '100%', ms: '293 ms', sub: 2 },
  { name: 'clarify', cy: CY + 140 },
]
const SUBTASKS = [
  { text: 'Time in Seoul', agent: 'time', cy: CY - 88 },
  { text: '300 USD to KRW', agent: 'currency', cy: CY },
  { text: 'Who was Nikola Tesla?', agent: 'knowledge', cy: CY + 88 },
]

// Geometry of each column (x, width, height).
const G = {
  query: { x: 12, w: 176, h: 76 },
  sub: { x: 236, w: 222, h: 56 },
  jev: { x: 508, w: 172, h: 86 },
  agent: { x: 752, w: 156, h: 50 },
  resp: { x: 950, w: 140, h: 76 },
}
const COLUMNS = [
  { label: 'query', x: G.query.x + G.query.w / 2 },
  { label: 'subtasks', x: G.sub.x + G.sub.w / 2 },
  { label: 'route', x: G.jev.x + G.jev.w / 2 },
  { label: 'agents', x: G.agent.x + G.agent.w / 2 },
  { label: 'response', x: G.resp.x + G.resp.w / 2 },
]

const curve = (x1: number, y1: number, x2: number, y2: number) => {
  const k = (x2 - x1) * 0.5
  return `M${x1},${y1} C${x1 + k},${y1} ${x2 - k},${y2} ${x2},${y2}`
}

/* The run plays as phases. Each phase says which stages are running vs done. */
// 0 query · 1 plan · 2 route · 3 agents · 4 merge · 5 finished
const PHASE_AT = [0, 900, 1800, 2700, 4100, 4900]
const LOOP = 9000

function usePhase(active: boolean, reduce: boolean | null) {
  const [phase, setPhase] = useState(reduce ? 5 : 0)
  useEffect(() => {
    if (reduce) { setPhase(5); return }
    if (!active) return
    let timers: number[] = []
    const run = () => {
      timers.forEach(clearTimeout)
      timers = PHASE_AT.map((t, i) => window.setTimeout(() => setPhase(i), t))
    }
    run()
    const loop = window.setInterval(run, LOOP)
    return () => { timers.forEach(clearTimeout); clearInterval(loop) }
  }, [active, reduce])
  return phase
}

type State = 'pending' | 'running' | 'done' | 'idle'
const stateOf = (phase: number, stage: number): State => (phase < stage ? 'pending' : phase === stage ? 'running' : 'done')

function Dot({ state }: { state: State }) {
  if (state === 'idle') return null
  return (
    <span className="relative grid size-2 place-items-center">
      {state === 'running' && <span className="absolute inset-0 animate-ping rounded-full bg-primary/60 motion-reduce:hidden" />}
      <span className={cn('size-2 rounded-full transition-colors duration-300', state === 'done' ? 'bg-ok' : state === 'running' ? 'bg-primary' : 'bg-edge')} />
    </span>
  )
}

function Node({ x, y, w, h, color, icon, title, meta, state, className }: {
  x: number; y: number; w: number; h: number; color: string; icon: ReactNode; title: ReactNode; meta?: ReactNode; state: State; className?: string
}) {
  const dim = state === 'pending' || state === 'idle'
  return (
    <div
      className={cn(
        'absolute flex items-center gap-2.5 rounded-lg border bg-card pr-5 pl-2.5 transition-[opacity,border-color,box-shadow] duration-500',
        dim ? 'border-border opacity-45' : 'border-border opacity-100 shadow-tile',
        className,
      )}
      style={{
        left: x, top: y, width: w, height: h,
        boxShadow: `inset 2px 0 0 ${color}${state === 'running' ? `, 0 0 0 4px color-mix(in srgb, ${color} 14%, transparent)` : ''}`,
        borderColor: state === 'running' ? `color-mix(in srgb, ${color} 55%, var(--line))` : undefined,
      }}
    >
      <span className="grid size-8 shrink-0 place-items-center rounded-md" style={{ background: `color-mix(in srgb, ${color} 15%, transparent)`, color }}>{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-semibold tracking-[-0.01em] text-foreground">{title}</span>
        {meta && <span className="mt-0.5 block truncate font-mono text-[10.5px] text-muted-foreground">{meta}</span>}
      </span>
      <span className="absolute top-2 right-2"><Dot state={state} /></span>
    </div>
  )
}

/** One wire: a quiet base line drawn in once its source finishes, and a light packet while its target works. */
function Wire({ d, color, drawn, flowing, dashed }: { d: string; color: string; drawn: boolean; flowing: boolean; dashed?: boolean }) {
  return (
    <g>
      <path d={d} fill="none" stroke="var(--edge)" strokeWidth={1.25} strokeDasharray={dashed ? '3 5' : undefined} opacity={dashed ? 0.8 : 0.9} />
      {!dashed && (
        <motion.path
          d={d} fill="none" stroke={color} strokeWidth={2} strokeLinecap="round"
          initial={false}
          animate={{ pathLength: drawn ? 1 : 0, opacity: drawn ? 0.75 : 0 }}
          transition={{ duration: drawn ? 0.7 : 0.25, ease: [0.16, 1, 0.3, 1] }}
        />
      )}
      {flowing && (
        <motion.path
          d={d} fill="none" stroke={color} strokeWidth={3} strokeLinecap="round" pathLength={1}
          strokeDasharray="0.14 1.3"
          initial={{ strokeDashoffset: 0.14 }}
          animate={{ strokeDashoffset: -1 }}
          transition={{ duration: 0.9, repeat: Infinity, ease: 'linear' }}
          style={{ filter: `drop-shadow(0 0 4px ${color})` }}
        />
      )}
    </g>
  )
}

function Pill({ x, y, color, children, show }: { x: number; y: number; color: string; children: ReactNode; show: boolean }) {
  return (
    <motion.span
      initial={false}
      animate={{ opacity: show ? 1 : 0, scale: show ? 1 : 0.8 }}
      transition={{ duration: 0.35, ease: [0.16, 1, 0.3, 1] }}
      className="absolute -translate-x-1/2 -translate-y-1/2 rounded-full border bg-card px-1.5 py-px font-mono text-[10.5px] font-medium"
      style={{ left: x, top: y, color, borderColor: `color-mix(in srgb, ${color} 45%, var(--line))` }}
    >
      {children}
    </motion.span>
  )
}

function Canvas({ phase }: { phase: number }) {
  const q = G.query, s = G.sub, j = G.jev, a = G.agent, r = G.resp
  const qState = stateOf(phase, 0)
  const planState = stateOf(phase, 1)
  const jevState = stateOf(phase, 2)
  const agentState = stateOf(phase, 3)
  const respState: State = phase >= 5 ? 'done' : phase === 4 ? 'running' : 'pending'

  return (
    <div className="relative" style={{ width: W, height: H }}>
      {COLUMNS.map(c => (
        <span key={c.label} className="absolute top-1 -translate-x-1/2 font-mono text-[11px] tracking-wide text-muted-foreground" style={{ left: c.x }}>{c.label}</span>
      ))}

      <svg className="absolute inset-0 overflow-visible" width={W} height={H} aria-hidden>
        {SUBTASKS.map((st, i) => (
          <Wire key={'q' + i} d={curve(q.x + q.w, CY, s.x, st.cy)} color="var(--accent)" drawn={phase >= 1} flowing={phase === 1} />
        ))}
        {SUBTASKS.map((st, i) => (
          <Wire key={'s' + i} d={curve(s.x + s.w, st.cy, j.x, CY)} color={colorOf(st.agent)} drawn={phase >= 2} flowing={phase === 2} />
        ))}
        {AGENTS.map(ag => (
          <Wire key={'j' + ag.name} d={curve(j.x + j.w, CY, a.x, ag.cy)} color={colorOf(ag.name)} drawn={phase >= 3 && !!ag.conf} flowing={phase === 3 && !!ag.conf} dashed={!ag.conf} />
        ))}
        {AGENTS.filter(ag => ag.conf).map(ag => (
          <Wire key={'a' + ag.name} d={curve(a.x + a.w, ag.cy, r.x, CY)} color={colorOf(ag.name)} drawn={phase >= 4} flowing={phase === 4} />
        ))}
      </svg>

      <Node x={q.x} y={CY - q.h / 2} w={q.w} h={q.h} color="var(--accent)" icon={<Icon name="query" size={16} />}
        title="Query #29" meta={phase >= 1 ? '3 subtasks' : 'planning…'} state={qState} />

      {SUBTASKS.map((st, i) => (
        <motion.div key={st.text} initial={false} animate={{ opacity: phase >= 1 ? 1 : 0, x: phase >= 1 ? 0 : -8 }} transition={{ duration: 0.45, delay: phase >= 1 ? i * 0.08 : 0, ease: [0.16, 1, 0.3, 1] }}>
          <Node x={s.x} y={st.cy - s.h / 2} w={s.w} h={s.h} color={colorOf(st.agent)}
            icon={<span className="font-mono text-[12px] font-semibold">{i + 1}</span>}
            title={st.text} meta={phase >= 3 ? `→ ${st.agent}` : 'waiting'} state={planState === 'pending' ? 'pending' : phase >= 3 ? 'done' : 'running'} />
        </motion.div>
      ))}

      <Node x={j.x} y={CY - j.h / 2} w={j.w} h={j.h} color="var(--accent)" icon={<Icon name="jev" size={17} />}
        title="Jev router" meta={<>jev-1.13.0<br />{phase >= 3 ? '3/3 · 99% conf' : 'routing…'}</>} state={jevState} />

      {AGENTS.map(ag => (
        <Node key={ag.name} x={a.x} y={ag.cy - a.h / 2} w={a.w} h={a.h} color={colorOf(ag.name)} icon={<AgentIcon agent={ag.name} size={16} />}
          title={ag.name}
          meta={ag.conf ? (phase >= 4 ? ag.ms : phase === 3 ? 'answering…' : 'idle') : 'not chosen'}
          state={ag.conf ? agentState : 'idle'} />
      ))}
      {AGENTS.filter(ag => ag.conf).map(ag => (
        <Pill key={'p' + ag.name} x={a.x - 30} y={ag.cy} color={colorOf(ag.name)} show={phase >= 3}>{ag.conf}</Pill>
      ))}

      <Node x={r.x} y={CY - r.h / 2} w={r.w} h={r.h} color="var(--ok)" icon={<Icon name="answer" size={16} />}
        title="Response" meta={phase >= 5 ? '1.9 s' : 'merging…'} state={respState} />
    </div>
  )
}

/** Scales the fixed canvas to its column without re-laying it out (so the wires stay exact). */
function Fit({ children }: { children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null)
  const [scale, setScale] = useState(1)
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    const ro = new ResizeObserver(([e]) => setScale(Math.min(1, e.contentRect.width / W)))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return (
    <div ref={box} className="relative w-full" style={{ height: H * scale }}>
      <div className="absolute top-0 left-0 origin-top-left" style={{ transform: `scale(${scale})` }}>{children}</div>
    </div>
  )
}

// Timeline rows in ms from the start of the run (real timings from run #29).
const TOTAL = 1900
const TIMELINE: Array<{ label: string; start: number; ms: number; color: string; phase: number }> = [
  { label: 'plan', start: 0, ms: 511, color: 'var(--accent)', phase: 1 },
  { label: 'route', start: 511, ms: 379, color: 'color-mix(in srgb, var(--accent) 60%, var(--muted))', phase: 2 },
  { label: 'time', start: 890, ms: 703, color: colorOf('time'), phase: 3 },
  { label: 'currency', start: 890, ms: 858, color: colorOf('currency'), phase: 3 },
  { label: 'knowledge', start: 890, ms: 293, color: colorOf('knowledge'), phase: 3 },
]

function Timeline({ phase }: { phase: number }) {
  return (
    <div className="grid grid-cols-[64px_minmax(0,1fr)_52px] items-center gap-x-3 gap-y-2 sm:grid-cols-[88px_minmax(0,1fr)_60px] sm:gap-x-4">
      {TIMELINE.map(t => {
        const shown = phase >= t.phase
        return (
          <div key={t.label} className="contents">
            <span className="truncate font-mono text-[11px] text-muted-foreground">{t.label}</span>
            <div className="relative h-3.5">
              <motion.span
                initial={false}
                animate={{ scaleX: shown ? 1 : 0, opacity: shown ? 1 : 0 }}
                transition={{ duration: 0.8, ease: [0.16, 1, 0.3, 1] }}
                className="absolute inset-y-0 origin-left rounded-[4px]"
                style={{ left: `${(t.start / TOTAL) * 100}%`, width: `${(t.ms / TOTAL) * 100}%`, background: t.color }}
              />
            </div>
            <motion.span
              initial={false}
              animate={{ opacity: shown ? 1 : 0 }}
              transition={{ duration: 0.3, delay: shown ? 0.5 : 0 }}
              className="text-right font-mono text-[10.5px] whitespace-nowrap text-muted-foreground tabular-nums"
            >
              {t.ms} ms
            </motion.span>
          </div>
        )
      })}
    </div>
  )
}

/** Phone layout: the same run as a vertical story. */
function Stack({ phase }: { phase: number }) {
  const row = (key: string, color: string, icon: ReactNode, title: string, meta: string, state: State) => (
    <div key={key} className={cn('flex items-center gap-3 rounded-lg border border-border bg-card px-3 py-2.5 transition-opacity duration-500', state === 'pending' ? 'opacity-45' : 'opacity-100')} style={{ boxShadow: `inset 2px 0 0 ${color}` }}>
      <span className="grid size-8 shrink-0 place-items-center rounded-md" style={{ background: `color-mix(in srgb, ${color} 15%, transparent)`, color }}>{icon}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-semibold text-foreground">{title}</span>
        <span className="block truncate font-mono text-[10.5px] text-muted-foreground">{meta}</span>
      </span>
      <Dot state={state} />
    </div>
  )
  return (
    <div className="flex flex-col gap-2">
      {row('q', 'var(--accent)', <Icon name="query" size={16} />, 'Query #29', 'heuristic plan · 3 subtasks', stateOf(phase, 0))}
      <div className="ml-4 flex flex-col gap-2 border-l border-dashed border-edge pl-4">
        {SUBTASKS.map((st, i) => row('s' + i, colorOf(st.agent), <AgentIcon agent={st.agent} size={16} />, st.text, phase >= 3 ? `${st.agent} · 100%` : 'routing…', phase < 2 ? 'pending' : phase < 4 ? 'running' : 'done'))}
      </div>
      {row('r', 'var(--ok)', <Icon name="answer" size={16} />, 'Response', phase >= 5 ? 'merged · total 1.9 s' : 'merging…', phase >= 5 ? 'done' : phase === 4 ? 'running' : 'pending')}
    </div>
  )
}

export function TraceCanvas() {
  const reduce = useReducedMotion()
  const ref = useRef<HTMLDivElement>(null)
  const inView = useInView(ref, { margin: '0px 0px -10% 0px' })
  const phase = usePhase(inView, reduce)
  const finished = phase >= 5

  return (
    <div ref={ref} className="overflow-hidden rounded-xl border border-border bg-card shadow-frame">
      <div className="flex items-center gap-3 border-b border-border px-4 py-3 sm:px-5">
        <Icon name="graph" size={16} className="shrink-0 text-muted-foreground" />
        <p className="min-w-0 flex-1 truncate text-[13px] text-foreground"><span className="hidden text-muted-foreground sm:inline">Example run · </span>{QUERY}</p>
        <AnimatePresence mode="wait" initial={false}>
          <motion.span
            key={finished ? 'done' : 'run'}
            initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.2 }}
            className={cn('flex shrink-0 items-center gap-1.5 rounded-full border px-2 py-0.5 font-mono text-[11px]',
              finished ? 'border-ok/40 text-ok' : 'border-primary/40 text-primary')}
          >
            <Icon name={(finished ? 'check' : 'live') as UiIconName} size={12} />{finished ? 'finished · 1.9 s' : 'running'}
          </motion.span>
        </AnimatePresence>
      </div>

      <div className="bg-[radial-gradient(var(--dots)_1px,transparent_1px)] [background-size:20px_20px] px-4 py-5 sm:px-5 sm:py-6">
        <div className="hidden sm:block"><Fit><Canvas phase={phase} /></Fit></div>
        <div className="sm:hidden"><Stack phase={phase} /></div>
      </div>

      <div className="border-t border-border bg-background/40 px-4 py-4 sm:px-5">
        <Timeline phase={phase} />
      </div>
    </div>
  )
}

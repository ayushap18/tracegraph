import { forwardRef, useEffect, useRef, useState } from 'react'
import { motion, useInView } from 'motion/react'
import { AnimatedBeam } from '@/components/ui/animated-beam'
import { cn } from '@/lib/utils'
import { AgentIcon, Icon, type UiIconName } from '../../icons'
import { colorOf } from '../../protocol'
import { PIPELINE as P } from './content'
import { Container, SectionHeading } from './primitives'

const STEP_ICON: Record<string, UiIconName> = { ask: 'query', plan: 'planner', route: 'jev', merge: 'merger' }
const AGENTS = ['weather', 'currency', 'knowledge']

// What the diagram shows under each stage, for the example question.
const STAGE_NOTE = [
  P.example,
  'Three steps, none waiting on another, so all three can start at once.',
  'weather, currency and knowledge run in parallel, each with its confidence score.',
  'Three answers, one reply. Every decision above stays inspectable in the trace.',
]

type NodeProps = { label: string; active: boolean; color?: string; children: React.ReactNode; size?: 'md' | 'lg' }
const Node = forwardRef<HTMLDivElement, NodeProps>(({ label, active, color = 'var(--accent)', children, size = 'md' }, ref) => (
  <div className="relative z-10 flex flex-col items-center gap-2">
    <div
      ref={ref}
      className={cn(
        'grid place-items-center rounded-lg border bg-card transition-[border-color,box-shadow,color] duration-500',
        size === 'lg' ? 'size-12 sm:size-14' : 'size-10 sm:size-11',
        active ? 'text-foreground' : 'border-border text-muted-foreground',
      )}
      style={active ? { borderColor: color, color, boxShadow: `0 0 0 4px color-mix(in srgb, ${color} 14%, transparent)` } : undefined}
    >
      {children}
    </div>
    <span className={cn('font-mono text-[10.5px] transition-colors duration-500 sm:text-[11px]', active ? 'text-foreground' : 'text-muted-foreground')}>{label}</span>
  </div>
))
Node.displayName = 'Node'

function Diagram({ active }: { active: number }) {
  const box = useRef<HTMLDivElement>(null)
  const query = useRef<HTMLDivElement>(null)
  const plan = useRef<HTMLDivElement>(null)
  const jev = useRef<HTMLDivElement>(null)
  const answer = useRef<HTMLDivElement>(null)
  const a0 = useRef<HTMLDivElement>(null), a1 = useRef<HTMLDivElement>(null), a2 = useRef<HTMLDivElement>(null)
  const agentRefs = [a0, a1, a2]
  const beam = { duration: 3.2, pathWidth: 1.5, pathOpacity: 0.35, pathColor: 'var(--edge)' }

  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card shadow-frame">
      <div
        ref={box}
        className="relative flex items-center justify-between px-5 py-10 sm:px-8 sm:py-14 bg-[radial-gradient(var(--dots)_1px,transparent_1px)] [background-size:18px_18px]"
      >
        <Node ref={query} label="query" active={active === 0}><Icon name="query" size={19} /></Node>
        <Node ref={plan} label="plan" active={active === 1}><Icon name="planner" size={19} /></Node>
        <Node ref={jev} label="jev" active={active === 2} size="lg"><Icon name="jev" size={22} /></Node>
        <div className="flex flex-col gap-4 sm:gap-5">
          {AGENTS.map((a, i) => (
            <Node key={a} ref={agentRefs[i]} label={a} active={active === 2} color={colorOf(a)}><AgentIcon agent={a} size={18} /></Node>
          ))}
        </div>
        <Node ref={answer} label="answer" active={active === 3} color="var(--ok)"><Icon name="answer" size={19} /></Node>

        <AnimatedBeam containerRef={box} fromRef={query} toRef={plan} {...beam} gradientStartColor="var(--accent)" gradientStopColor="var(--accent)" />
        <AnimatedBeam containerRef={box} fromRef={plan} toRef={jev} {...beam} delay={0.35} gradientStartColor="var(--accent)" gradientStopColor="var(--accent)" />
        {AGENTS.map((a, i) => (
          <AnimatedBeam key={'in' + a} containerRef={box} fromRef={jev} toRef={agentRefs[i]} {...beam} delay={0.7} curvature={(i - 1) * -18} gradientStartColor="var(--accent)" gradientStopColor={colorOf(a)} />
        ))}
        {AGENTS.map((a, i) => (
          <AnimatedBeam key={'out' + a} containerRef={box} fromRef={agentRefs[i]} toRef={answer} {...beam} delay={1.2 + i * 0.1} curvature={(i - 1) * 18} gradientStartColor={colorOf(a)} gradientStopColor="var(--ok)" />
        ))}
      </div>

      <div className="flex min-h-[92px] items-start gap-3 border-t border-border bg-background/40 px-5 py-4 sm:px-6">
        <span className="mt-0.5 grid size-7 shrink-0 place-items-center rounded-md bg-primary/12 text-primary">
          <Icon name={STEP_ICON[P.steps[active].key]} size={15} />
        </span>
        <div className="min-w-0">
          <p className="font-mono text-[11px] text-muted-foreground">Example run, {P.steps[active].title.toLowerCase()}</p>
          {/* Every note sits in the same grid cell, so the box is always as tall as the longest
              one and the page never shifts while the story advances. */}
          <div className="mt-1 grid">
            {STAGE_NOTE.map((note, i) => (
              <motion.p
                key={i}
                aria-hidden={i !== active}
                initial={false}
                animate={{ opacity: i === active ? 1 : 0, y: i === active ? 0 : i < active ? -6 : 6 }}
                transition={{ duration: 0.3, ease: [0.16, 1, 0.3, 1] }}
                className="col-start-1 row-start-1 text-[14px] leading-relaxed text-foreground"
              >
                {note}
              </motion.p>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

function Step({ index, active, onActive }: { index: number; active: boolean; onActive: (i: number) => void }) {
  const ref = useRef<HTMLLIElement>(null)
  // A step becomes active when it crosses the middle band of the viewport.
  const inView = useInView(ref, { margin: '-45% 0px -45% 0px' })
  useEffect(() => { if (inView) onActive(index) }, [inView, index, onActive])
  const s = P.steps[index]
  return (
    <li ref={ref} className="relative flex gap-5 py-8 lg:min-h-[44vh] lg:items-center lg:py-0">
      <span
        aria-hidden
        className={cn('absolute top-8 bottom-8 left-0 w-px transition-colors duration-500 lg:top-1/2 lg:bottom-auto lg:h-24 lg:-translate-y-1/2', active ? 'bg-primary' : 'bg-border')}
      />
      <div className={cn('pl-6 transition-opacity duration-500', active ? 'opacity-100' : 'opacity-45')}>
        <div className="flex items-center gap-3">
          <Icon name={STEP_ICON[s.key]} size={20} className={cn('transition-colors duration-500', active ? 'text-primary' : 'text-muted-foreground')} />
          <h3 className="text-[22px] font-semibold tracking-[-0.025em] text-foreground">{s.title}</h3>
        </div>
        <p className="mt-2.5 max-w-[42ch] text-[15px] leading-relaxed text-muted-foreground">{s.text}</p>
      </div>
    </li>
  )
}

export function Pipeline() {
  const [active, setActive] = useState(0)
  return (
    <section id="about-how" tabIndex={-1} aria-labelledby="how-title" className="scroll-mt-20 border-y border-border bg-card/40 py-20 outline-none sm:py-28">
      <Container>
        <SectionHeading id="how-title" title={P.title} lede={P.lede} />
        <div className="mt-10 grid gap-10 lg:mt-4 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)] lg:gap-16">
          <ol className="order-2 lg:order-1 lg:py-[8vh]">
            {P.steps.map((s, i) => <Step key={s.key} index={i} active={active === i} onActive={setActive} />)}
          </ol>
          <div className="order-1 lg:order-2">
            <div className="lg:sticky lg:top-[calc(50vh-210px)]">
              <Diagram active={active} />
            </div>
          </div>
        </div>
      </Container>
    </section>
  )
}

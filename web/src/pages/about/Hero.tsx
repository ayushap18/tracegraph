import { useRef } from 'react'
import { motion, useReducedMotion, useScroll, useTransform } from 'motion/react'
import { Button } from '@/components/ui/button'
import { GithubMark, Icon } from '../../icons'
import { CTA, HERO, LINKS } from './content'
import { Container, rise, stagger } from './primitives'
import { TraceCanvas } from './TraceCanvas'

export function Hero() {
  const reduce = useReducedMotion()
  const frame = useRef<HTMLDivElement>(null)
  // The product shot starts tilted away and settles flat as it scrolls into view:
  // the page literally brings the trace into focus.
  const { scrollYProgress } = useScroll({ target: frame, offset: ['start end', 'start 0.35'] })
  const rotateX = useTransform(scrollYProgress, [0, 1], [10, 0])
  const scale = useTransform(scrollYProgress, [0, 1], [0.96, 1])

  return (
    <section aria-labelledby="hero-title" className="relative overflow-hidden pt-10 sm:pt-20 lg:pt-24">
      {/* A single, soft wash of the accent behind the fold. No blobs, no second hue. */}
      <div aria-hidden className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-[680px] bg-[radial-gradient(60%_60%_at_50%_0%,color-mix(in_srgb,var(--accent)_13%,transparent),transparent_70%)]" />
      <div aria-hidden className="pointer-events-none absolute inset-x-0 top-0 -z-10 h-[680px] opacity-60 [mask-image:radial-gradient(70%_60%_at_50%_0%,#000,transparent)] bg-[radial-gradient(var(--dots)_1px,transparent_1px)] [background-size:22px_22px]" />

      <Container>
        <motion.div variants={stagger} initial="hidden" animate="show" className="max-w-6xl">
          <motion.a
            variants={rise}
            href={LINKS.github}
            target="_blank"
            rel="noopener noreferrer"
            className="group inline-flex items-center gap-2 rounded-full border border-border bg-card/70 py-1 pr-3 pl-1.5 text-[13px] font-medium text-muted-foreground backdrop-blur transition-colors hover:border-edge hover:text-foreground"
          >
            <span className="grid size-6 place-items-center rounded-full bg-muted text-foreground"><GithubMark size={13} /></span>
            {HERO.kicker}
            <Icon name="arrow-right" size={13} className="transition-transform duration-300 group-hover:translate-x-0.5" />
          </motion.a>

          <motion.h1
            variants={rise}
            id="hero-title"
            className="mt-7 text-[clamp(36px,5vw,68px)] leading-[1.02] font-semibold tracking-[-0.045em] text-foreground text-balance"
          >
            {HERO.title} <br className="hidden md:block" /><span className="text-muted-foreground">{HERO.titleMuted}</span>
          </motion.h1>

          <motion.p variants={rise} className="mt-6 max-w-[34rem] text-[17px] leading-relaxed text-muted-foreground text-pretty sm:text-lg">
            {HERO.lede}
          </motion.p>

          <motion.div variants={rise} className="mt-9 flex flex-col gap-3 sm:flex-row">
            <Button asChild size="lg">
              <a href={LINKS.app}><Icon name="chat" size={17} />{CTA.open}</a>
            </Button>
            <Button asChild size="lg" variant="outline">
              <a href={LINKS.live}><Icon name="live" size={17} />{CTA.live}</a>
            </Button>
          </motion.div>
        </motion.div>

        <div className="mt-12 [perspective:1600px] sm:mt-20">
          <motion.div
            ref={frame}
            style={reduce ? undefined : { rotateX, scale, transformOrigin: '50% 0%' }}
            initial={{ opacity: 0, y: 40 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 1.1, delay: 0.35, ease: [0.16, 1, 0.3, 1] }}
          >
            <figure aria-label={HERO.shotAlt}><TraceCanvas /></figure>
          </motion.div>
        </div>
      </Container>
    </section>
  )
}

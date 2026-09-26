import { useRef, type ComponentProps, type PointerEvent, type ReactNode } from 'react'
import { motion, type Variants } from 'motion/react'
import { cn } from '@/lib/utils'

// Shared layout + motion building blocks for the About page.

export const EASE = [0.16, 1, 0.3, 1] as const

/** Page gutter and max width, identical for every section so edges line up down the page. */
export function Container({ className, ...props }: ComponentProps<'div'>) {
  return <div className={cn('mx-auto w-full max-w-[1200px] px-4 sm:px-6 lg:px-8', className)} {...props} />
}

/** Fades content up once as it enters the viewport. Motivation: pace the read, one block at a time. */
export function Reveal({ children, className, delay = 0, as = 'div' }: { children: ReactNode; className?: string; delay?: number; as?: 'div' | 'li' }) {
  const M = as === 'li' ? motion.li : motion.div
  return (
    <M
      className={className}
      initial={{ opacity: 0, y: 18 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true, margin: '0px 0px -12% 0px' }}
      transition={{ duration: 0.7, delay, ease: EASE }}
    >
      {children}
    </M>
  )
}

export const stagger: Variants = { hidden: {}, show: { transition: { staggerChildren: 0.08, delayChildren: 0.05 } } }
export const rise: Variants = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0, transition: { duration: 0.75, ease: EASE } },
}

/**
 * Bento tile with a cursor-following spotlight on its border and surface.
 * Pointer position is written straight to CSS variables (no React state, no re-render per move).
 */
export function SpotlightCard({ className, children, ...props }: ComponentProps<'article'>) {
  const ref = useRef<HTMLElement>(null)
  const onMove = (e: PointerEvent<HTMLElement>) => {
    const el = ref.current
    if (!el || e.pointerType !== 'mouse') return
    const r = el.getBoundingClientRect()
    el.style.setProperty('--x', `${e.clientX - r.left}px`)
    el.style.setProperty('--y', `${e.clientY - r.top}px`)
  }
  return (
    <article
      ref={ref}
      onPointerMove={onMove}
      className={cn(
        'group/tile relative isolate flex flex-col overflow-hidden rounded-lg border border-border bg-card shadow-tile',
        'transition-[border-color] duration-300 hover:border-edge',
        className,
      )}
      {...props}
    >
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 -z-10 opacity-0 transition-opacity duration-500 group-hover/tile:opacity-100 bg-[radial-gradient(420px_circle_at_var(--x,50%)_var(--y,0%),color-mix(in_srgb,var(--accent)_9%,transparent),transparent_65%)]"
      />
      {children}
    </article>
  )
}

/** Tile copy block: title + one sentence, always the same rhythm. */
export function TileCopy({ title, text, className }: { title: string; text: string; className?: string }) {
  return (
    <div className={cn('p-6', className)}>
      <h3 className="text-[15px] font-semibold tracking-[-0.015em] text-foreground">{title}</h3>
      <p className="mt-1.5 max-w-[46ch] text-[13.5px] leading-relaxed text-muted-foreground">{text}</p>
    </div>
  )
}

/** Section heading: headline over one line of body, stacked (never split left/right). */
export function SectionHeading({ id, title, lede, className }: { id: string; title: string; lede?: string; className?: string }) {
  return (
    <Reveal className={cn('max-w-2xl', className)}>
      <h2 id={id} className="text-[clamp(28px,3.6vw,42px)] leading-[1.08] font-semibold tracking-[-0.035em] text-foreground text-balance">{title}</h2>
      {lede && <p className="mt-4 max-w-[58ch] text-base leading-relaxed text-muted-foreground text-pretty">{lede}</p>}
    </Reveal>
  )
}

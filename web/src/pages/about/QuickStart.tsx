import { useEffect, useRef, useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { Button } from '@/components/ui/button'
import { GithubMark, Icon } from '../../icons'
import { CTA, LINKS, QUICKSTART as Q } from './content'
import { Container, Reveal } from './primitives'

type CopyState = 'idle' | 'copied' | 'failed'

function CodeBlock() {
  const [state, setState] = useState<CopyState>('idle')
  const timer = useRef<number>()
  useEffect(() => () => window.clearTimeout(timer.current), [])
  const text = Q.code.filter(l => l.t).map(l => l.t).join('\n')

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text)
      setState('copied')
    } catch {
      setState('failed')
    }
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => setState('idle'), 2000)
  }

  const label = state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'
  return (
    <div className="overflow-hidden rounded-xl border border-border bg-card shadow-frame">
      <div className="flex items-center justify-between border-b border-border px-4 py-2.5">
        <span className="flex items-center gap-2 font-mono text-[12px] text-muted-foreground"><Icon name="code" size={14} />Quick start</span>
        <Button variant="ghost" size="xs" onClick={copy} aria-label="Copy the quick start commands" className="font-mono">
          <AnimatePresence mode="wait" initial={false}>
            <motion.span key={state} initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.18 }} className="flex items-center gap-1.5">
              <Icon name={state === 'copied' ? 'check' : state === 'failed' ? 'warning' : 'copy'} size={12} />{label}
            </motion.span>
          </AnimatePresence>
        </Button>
      </div>
      <pre className="px-4 py-5 font-mono text-[12px] leading-[1.8] sm:overflow-x-auto sm:px-5 sm:text-[12.5px] sm:leading-[1.9]">
        <code>
          {Q.code.map((l, i) => (
            <span key={i} className="block pl-[2ch] -indent-[2ch] whitespace-pre-wrap [overflow-wrap:anywhere] sm:pl-0 sm:indent-0 sm:whitespace-pre">
              {l.t && <><span className="text-primary select-none">$ </span><span className="text-foreground">{l.t}</span></>}
              {l.c && <span className={l.t ? 'block text-muted-foreground sm:inline' : 'text-muted-foreground'}>{l.t ? '  ' : ''}{l.c}</span>}
            </span>
          ))}
        </code>
      </pre>
    </div>
  )
}

/** The page's single marquee: the stack is breadth, not something to read item by item. */
function StackMarquee() {
  const items = [...Q.stack, ...Q.stack]
  return (
    <div className="relative mt-16 overflow-hidden [mask-image:linear-gradient(90deg,transparent,#000_12%,#000_88%,transparent)] sm:mt-20" aria-label="Built with" role="region">
      <ul className="flex w-max animate-marquee gap-3 motion-reduce:animate-none motion-reduce:flex-wrap motion-reduce:w-full motion-reduce:justify-center hover:[animation-play-state:paused]">
        {items.map((s, i) => (
          <li key={i} aria-hidden={i >= Q.stack.length} className="rounded-full border border-border bg-card px-4 py-1.5 font-mono text-[12.5px] whitespace-nowrap text-muted-foreground">{s}</li>
        ))}
      </ul>
    </div>
  )
}

export function QuickStart() {
  return (
    <section aria-labelledby="qs-title" className="py-20 sm:py-28">
      <Container className="grid items-center gap-10 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] lg:gap-16">
        <Reveal>
          <h2 id="qs-title" className="text-[clamp(28px,3.6vw,42px)] leading-[1.08] font-semibold tracking-[-0.035em] text-foreground text-balance">{Q.title}</h2>
          <p className="mt-4 max-w-[46ch] text-base leading-relaxed text-muted-foreground text-pretty">{Q.lede}</p>
          <Button asChild variant="outline" size="lg" className="mt-8">
            <a href={LINKS.github} target="_blank" rel="noopener noreferrer"><GithubMark size={16} />{CTA.source}<Icon name="external" size={14} /></a>
          </Button>
        </Reveal>
        <Reveal delay={0.1} className="min-w-0"><CodeBlock /></Reveal>
      </Container>
      <StackMarquee />
    </section>
  )
}

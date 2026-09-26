import { Button } from '@/components/ui/button'
import { GithubMark, Icon, Logo } from '../../icons'
import { CLOSING, CTA, FOOTER, LINKS } from './content'
import { Container, Reveal } from './primitives'
import { scrollToSection } from './Nav'

// Footer links share one shape: a 44px-tall hit area on touch, compact on desktop.
const LINK = 'inline-flex min-h-11 items-center gap-1.5 rounded-md text-[14px] text-muted-foreground transition-colors hover:text-foreground focus-visible:text-foreground sm:min-h-8 sm:text-[13.5px]'

export function Closing() {
  return (
    <>
      <section aria-labelledby="closing-title" className="relative overflow-hidden border-t border-border py-20 sm:py-32">
        <div aria-hidden className="pointer-events-none absolute inset-x-0 bottom-0 -z-10 h-[420px] bg-[radial-gradient(55%_70%_at_50%_100%,color-mix(in_srgb,var(--accent)_12%,transparent),transparent_70%)]" />
        <Container className="flex flex-col items-start gap-8 sm:gap-10 lg:flex-row lg:items-end lg:justify-between">
          <Reveal>
            <h2 id="closing-title" className="text-[clamp(32px,5vw,60px)] leading-[1.04] font-semibold tracking-[-0.04em] text-foreground text-balance">
              {CLOSING.title} <br className="hidden sm:block" /><span className="text-muted-foreground">{CLOSING.titleMuted}</span>
            </h2>
          </Reveal>
          <Reveal delay={0.1} className="flex w-full flex-col gap-3 sm:w-auto sm:flex-row">
            <Button asChild size="lg" className="w-full sm:w-auto"><a href={LINKS.app}>{CTA.open}<Icon name="arrow-right" size={16} /></a></Button>
            <Button asChild size="lg" variant="outline" className="w-full sm:w-auto"><a href={LINKS.live}><Icon name="live" size={17} />{CTA.live}</a></Button>
          </Reveal>
        </Container>
      </section>

      <footer className="border-t border-border pt-12 pb-[max(32px,env(safe-area-inset-bottom))] sm:pt-16">
        <Container className="grid gap-10 sm:grid-cols-2 lg:grid-cols-[minmax(0,1.4fr)_repeat(3,minmax(0,1fr))] lg:gap-8">
          <div className="max-w-xs">
            <a href="#/about" className="inline-flex items-center gap-2.5 text-[15px] font-semibold tracking-[-0.02em] text-foreground">
              <Logo size={24} />TraceGraph
            </a>
            <p className="mt-3 text-[13.5px] leading-relaxed text-muted-foreground">{FOOTER.tagline}</p>
          </div>

          {/* Two columns of groups on phones keeps the footer short without shrinking tap targets. */}
          <div className="grid grid-cols-2 gap-x-6 gap-y-8 sm:contents">
            {FOOTER.groups.map(g => (
              <nav key={g.title} aria-label={g.title} className={g.title === 'Source' ? 'col-span-2 sm:col-span-1' : undefined}>
                <h3 className="text-[13px] font-medium text-foreground">{g.title}</h3>
                <ul className="mt-2 sm:mt-3">
                  {g.links.map(l => (
                    <li key={l.label}>
                      {l.section ? (
                        <button type="button" onClick={() => scrollToSection(l.section!)} className={LINK}>{l.label}</button>
                      ) : l.external ? (
                        <a href={l.href} target="_blank" rel="noopener noreferrer" className={LINK}>
                          {l.label === 'GitHub' && <GithubMark size={14} />}{l.label}
                          <Icon name="external" size={12} className="opacity-60" />
                          <span className="sr-only">(opens in a new tab)</span>
                        </a>
                      ) : (
                        <a href={l.href} className={LINK}>{l.label}</a>
                      )}
                    </li>
                  ))}
                </ul>
              </nav>
            ))}
          </div>
        </Container>
      </footer>
    </>
  )
}

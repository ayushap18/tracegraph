import { motion, useScroll, useTransform } from 'motion/react'
import { Button } from '@/components/ui/button'
import { GithubMark, Icon, Logo } from '../../icons'
import { THEME_ICON, THEME_LABEL, useTheme } from '../../theme'
import { CTA, LINKS, SECTIONS } from './content'
import { MobileMenu } from './MobileMenu'
import { Container } from './primitives'

// Hash routing owns location.hash, so in-page links scroll with JS instead of #anchors.
export function scrollToSection(id: string) {
  const el = document.getElementById(id)
  if (!el) return
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
  // Focus first: Chrome cancels an in-flight smooth scroll when focus moves.
  el.focus({ preventScroll: true })
  el.scrollIntoView({ behavior: reduce ? 'auto' : 'smooth', block: 'start' })
}

export function Nav() {
  const { theme, cycle } = useTheme()
  // The bar is transparent over the hero and gains a surface once the page moves.
  const { scrollY } = useScroll()
  const surface = useTransform(scrollY, [0, 48], [0, 1])

  return (
    <header className="sticky top-0 z-30">
      <motion.div
        aria-hidden
        style={{ opacity: surface }}
        className="absolute inset-0 border-b border-border bg-background/75 backdrop-blur-xl backdrop-saturate-150"
      />
      <Container className="relative flex h-16 items-center justify-between gap-4">
        <a href="#/about" className="flex shrink-0 items-center gap-2.5 text-[15px] font-semibold tracking-[-0.02em] text-foreground">
          <Logo size={26} />
          TraceGraph
        </a>

        <nav aria-label="About sections" className="hidden items-center gap-1 md:flex">
          {SECTIONS.map(s => (
            <Button key={s.id} variant="ghost" size="sm" onClick={() => scrollToSection(s.id)}>{s.label}</Button>
          ))}
        </nav>

        <div className="flex items-center gap-1.5">
          <Button asChild variant="ghost" size="icon-sm" className="hidden md:inline-flex">
            <a href={LINKS.github} target="_blank" rel="noopener noreferrer" aria-label="TraceGraph on GitHub"><GithubMark size={17} /></a>
          </Button>
          <Button variant="ghost" size="icon-sm" className="hidden md:inline-flex" onClick={cycle} aria-label={`Theme: ${THEME_LABEL[theme]}`} title={`Theme: ${THEME_LABEL[theme]}`}>
            <Icon name={THEME_ICON[theme]} size={17} />
          </Button>
          <Button asChild size="sm" className="ml-1.5 hidden min-[420px]:inline-flex">
            <a href={LINKS.app}>{CTA.open}<Icon name="arrow-right" size={14} /></a>
          </Button>
          <MobileMenu onNavigate={scrollToSection} />
        </div>
      </Container>
    </header>
  )
}

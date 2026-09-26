import { useEffect, useRef, useState } from 'react'
import { Dialog } from 'radix-ui'
import { AnimatePresence, motion } from 'motion/react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { GithubMark, Icon, Logo } from '../../icons'
import { THEMES, THEME_ICON, THEME_LABEL, useTheme } from '../../theme'
import { CTA, LINKS, SECTIONS } from './content'

const EASE = [0.16, 1, 0.3, 1] as const

/**
 * Phone / small-tablet navigation (below md). A Radix Dialog gives focus trap, Escape,
 * scroll lock and labelling; Motion handles enter/exit. Picking a section closes the menu
 * first and scrolls once the scroll lock has been released.
 */
export function MobileMenu({ onNavigate }: { onNavigate: (id: string) => void }) {
  const [open, setOpen] = useState(false)
  const pending = useRef<string | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const { theme, setTheme } = useTheme()

  // Close if the viewport grows past the breakpoint where the inline nav takes over.
  useEffect(() => {
    const mq = matchMedia('(min-width: 768px)')
    const onChange = () => mq.matches && setOpen(false)
    mq.addEventListener('change', onChange)
    return () => mq.removeEventListener('change', onChange)
  }, [])

  const go = (id: string) => {
    pending.current = id
    setOpen(false)
  }

  return (
    <Dialog.Root open={open} onOpenChange={setOpen}>
      <Dialog.Trigger asChild>
        <Button ref={trigger} variant="ghost" size="icon" aria-label="Open menu" className="md:hidden">
          <Icon name="menu" size={20} />
        </Button>
      </Dialog.Trigger>

      <AnimatePresence>
        {open && (
          <Dialog.Portal forceMount>
            <Dialog.Overlay asChild forceMount>
              <motion.div
                className="fixed inset-0 z-40 bg-[var(--scrim)] backdrop-blur-[2px]"
                initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }}
              />
            </Dialog.Overlay>
            <Dialog.Content
              asChild
              forceMount
              onCloseAutoFocus={e => {
                e.preventDefault()
                const id = pending.current
                pending.current = null
                // Plain close: return focus to the menu button explicitly (Safari never focuses a
                // tapped button, so Radix would otherwise fall back to <body>).
                if (!id) { trigger.current?.focus(); return }
                // Navigating: wait for the scroll lock to be released, then scroll and focus the section.
                window.setTimeout(() => onNavigate(id), 50)
              }}
            >
              <motion.div
                className="fixed inset-x-0 top-0 z-50 max-h-[100dvh] overflow-y-auto rounded-b-xl border-b border-border bg-background text-foreground shadow-frame outline-none"
                style={{ paddingTop: 'env(safe-area-inset-top)' }}
                initial={{ y: '-100%' }} animate={{ y: 0 }} exit={{ y: '-100%' }}
                transition={{ duration: 0.4, ease: EASE }}
              >
                <div className="flex h-16 items-center justify-between px-4 sm:px-6">
                  <span className="flex items-center gap-2.5 text-[15px] font-semibold tracking-[-0.02em]"><Logo size={26} />TraceGraph</span>
                  <Dialog.Close asChild>
                    <Button variant="ghost" size="icon" aria-label="Close menu"><Icon name="close" size={20} /></Button>
                  </Dialog.Close>
                </div>
                <Dialog.Title className="sr-only">Menu</Dialog.Title>
                <Dialog.Description className="sr-only">Jump to a section of the page, open the app, or change the theme.</Dialog.Description>

                <nav aria-label="About sections" className="px-2 sm:px-4">
                  <ul>
                    {SECTIONS.map((s, i) => (
                      <motion.li key={s.id} initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.08 + i * 0.05, duration: 0.35, ease: EASE }}>
                        <button
                          type="button"
                          onClick={() => go(s.id)}
                          className="flex min-h-14 w-full items-center justify-between rounded-lg px-3 text-left text-[22px] font-semibold tracking-[-0.025em] transition-colors hover:bg-muted active:bg-muted"
                        >
                          {s.label}
                          <Icon name="arrow-right" size={18} className="text-muted-foreground" />
                        </button>
                      </motion.li>
                    ))}
                  </ul>
                </nav>

                <div className="mx-4 my-3 border-t border-border sm:mx-6" />

                <div className="flex flex-col gap-1 px-2 sm:px-4">
                  <a href={LINKS.live} onClick={() => setOpen(false)} className="flex min-h-12 items-center gap-3 rounded-lg px-3 text-[15px] font-medium transition-colors hover:bg-muted">
                    <Icon name="live" size={18} className="text-muted-foreground" />{CTA.live}
                  </a>
                  <a href={LINKS.github} target="_blank" rel="noopener noreferrer" className="flex min-h-12 items-center gap-3 rounded-lg px-3 text-[15px] font-medium transition-colors hover:bg-muted">
                    <span className="text-muted-foreground"><GithubMark size={18} /></span>GitHub
                    <Icon name="external" size={14} className="ml-auto text-muted-foreground" />
                  </a>
                </div>

                <div className="flex items-center justify-between gap-3 px-5 py-4 sm:px-7">
                  <span className="text-[14px] text-muted-foreground" id="menu-theme">Theme</span>
                  <div role="radiogroup" aria-labelledby="menu-theme" className="flex rounded-full border border-border bg-card p-1">
                    {THEMES.map(t => (
                      <button
                        key={t}
                        type="button"
                        role="radio"
                        aria-checked={theme === t}
                        aria-label={THEME_LABEL[t]}
                        onClick={() => setTheme(t)}
                        className={cn('grid size-9 place-items-center rounded-full transition-colors', theme === t ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground')}
                      >
                        <Icon name={THEME_ICON[t]} size={16} />
                      </button>
                    ))}
                  </div>
                </div>

                <div className="px-4 pb-[max(20px,env(safe-area-inset-bottom))] sm:px-6">
                  <Button asChild size="lg" className="w-full">
                    <a href={LINKS.app} onClick={() => setOpen(false)}>{CTA.open}<Icon name="arrow-right" size={16} /></a>
                  </Button>
                </div>
              </motion.div>
            </Dialog.Content>
          </Dialog.Portal>
        )}
      </AnimatePresence>
    </Dialog.Root>
  )
}

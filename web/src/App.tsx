import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { StoreProvider, useStore } from './store'
import { ThemeProvider, useTheme } from './theme'
import { ToastProvider, useHashPath, useToast } from './ui'
import { TooltipProvider } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { matchRoute, NAV, type Match } from './routes'
import { setPageMeta } from './meta'
import { BottomBar, Sidebar, SlotCtx, TopBar } from './components/Shell'
import { Palette } from './components/Palette'
import Chat from './pages/Chat'
import Live from './pages/Live'
import Runs from './pages/Runs'
import RunDetail from './pages/RunDetail'
import Settings from './pages/Settings'
import Compare from './pages/Compare'
import Evals from './pages/Evals'
import Agents from './pages/Agents'

// The About page carries its own stack (Tailwind, shadcn/ui, Motion, Geist); load it only when visited.
const About = lazy(() => import('./pages/About'))

// Visually hidden until focused; the first stop for keyboard users.
const SKIP = 'fixed -top-16 left-3 z-[100] rounded-md bg-primary px-3.5 py-2 text-sm font-semibold text-primary-foreground no-underline transition-[top] focus:top-3'

export default function App() {
  return (
    <ThemeProvider>
      <TooltipProvider delayDuration={400}>
        <ToastProvider>
          <StoreProvider url="/events">
            <AppShell />
          </StoreProvider>
        </ToastProvider>
      </TooltipProvider>
    </ThemeProvider>
  )
}

function Page({ match }: { match: Match }) {
  const { params } = match
  switch (match.name) {
    case 'live': return <Live />
    case 'runs': return <Runs />
    case 'run': return <RunDetail params={params} />
    case 'compare': return <Compare params={params} />
    case 'evals': return <Evals params={params} />
    case 'agents': return <Agents params={params} />
    case 'settings': return <Settings />
    case 'about': return <Suspense fallback={null}><About /></Suspense>
    default: return <Chat />
  }
}

function useSidebarCollapsed() {
  const [collapsed, setCollapsed] = useState(() => { try { return localStorage.getItem('tg-sidebar') === '1' } catch { return false } })
  const toggle = useCallback(() => setCollapsed(c => {
    try { localStorage.setItem('tg-sidebar', c ? '0' : '1') } catch { /* keep for this visit */ }
    return !c
  }), [])
  return [collapsed, toggle] as const
}

function AppShell() {
  const { path } = useHashPath()
  const match = matchRoute(path)
  const [collapsed, toggleSidebar] = useSidebarCollapsed()
  const [palette, setPalette] = useState(false)
  const [slot, setSlot] = useState<HTMLElement | null>(null)
  const { cycle } = useTheme()
  const { store } = useStore()
  const toast = useToast()
  const main = useRef<HTMLElement>(null)

  // Global shortcuts: ⌘K/Ctrl+K palette everywhere; single keys only when not typing.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && !e.altKey && e.key.toLowerCase() === 'k') { e.preventDefault(); setPalette(p => !p); return }
      const el = e.target as HTMLElement
      const typing = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable
      if (typing || e.metaKey || e.ctrlKey || e.altKey || palette) return
      if (e.key === 't') cycle()
      else if (e.key === '[') toggleSidebar()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cycle, toggleSidebar, palette])

  // Server errors surface once as toasts (they also live on the run they belong to).
  const lastErr = useRef<string | null>(null)
  useEffect(() => {
    if (!store.lastError || store.lastError === lastErr.current) return
    lastErr.current = store.lastError
    toast.error(store.lastError)
  }, [store.lastError, toast])

  // New page: scroll to top and move focus to the main region for screen readers.
  const pageKey = match.name + ':' + JSON.stringify(match.params)
  useEffect(() => { window.scrollTo(0, 0); main.current?.scrollTo?.(0, 0) }, [pageKey])

  useEffect(() => {
    const t = match.name === 'run' ? `Run #${match.params.qid}` : match.name === 'chat' ? 'Chat' : match.name[0].toUpperCase() + match.name.slice(1)
    const route = NAV.find(n => n.name === match.name)
    const description = match.name === 'run'
      ? `Trace of run #${match.params.qid}: how the question was planned, routed to agents and answered.`
      : route?.description ?? NAV[0].description
    setPageMeta(match.name === 'about' ? 'TraceGraph: every question has a path' : `${t} · TraceGraph`, description)
  }, [pageKey])

  if (match.name === 'about') {
    return (
      <>
        <a className={SKIP} href="#main" onClick={e => { e.preventDefault(); main.current?.focus() }}>Skip to content</a>
        <main id="main" ref={main} tabIndex={-1} className="page-enter outline-none" key={pageKey}><Suspense fallback={null}><About /></Suspense></main>
        <Palette open={palette} onClose={() => setPalette(false)} onToggleSidebar={toggleSidebar} />
      </>
    )
  }

  return (
    <SlotCtx.Provider value={slot}>
      <a className={SKIP} href="#main" onClick={e => { e.preventDefault(); main.current?.focus() }}>Skip to content</a>
      <div className={cn(
        'app grid min-h-dvh transition-[grid-template-columns] duration-200',
        collapsed ? 'md:grid-cols-[var(--sidebar-w-c)_minmax(0,1fr)]' : 'md:grid-cols-[var(--sidebar-w)_minmax(0,1fr)]',
        'route-' + match.name,
      )}>
        <Sidebar route={match.name} collapsed={collapsed} onToggle={toggleSidebar} />
        <div className="flex min-w-0 flex-col">
          <TopBar route={match.name} onPalette={() => setPalette(true)} onSlot={setSlot} />
          <main id="main" ref={main} tabIndex={-1} key={pageKey} className={cn(
            'page page-enter min-w-0 outline-none',
            // Chat owns its scrolling: a fixed-height page between the top bar and (on phones) the tab bar.
            match.name === 'chat'
              ? 'h-[calc(100dvh-var(--topbar-h))] flex-none overflow-hidden max-md:h-[calc(100dvh-var(--topbar-h)-var(--bottombar-h)-env(safe-area-inset-bottom,0px))]'
              : 'flex-1 max-md:pb-[calc(var(--bottombar-h)+env(safe-area-inset-bottom,0px))]',
          )}>
            <Page match={match} />
          </main>
        </div>
        <BottomBar route={match.name} />
      </div>
      <Palette open={palette} onClose={() => setPalette(false)} onToggleSidebar={toggleSidebar} />
    </SlotCtx.Provider>
  )
}

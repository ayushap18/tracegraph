import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { cn } from '@/lib/utils'
import { Sheet, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { Icon, Logo } from '../icons'
import { NAV, MOBILE_TABS, navFor, type NavItem, type RouteName } from '../routes'
import { useStore } from '../store'
import { useTheme, THEMES, THEME_ICON, THEME_LABEL } from '../theme'
import { EnginePicker } from './EnginePicker'
import { IconButton } from '../ui'
import { StatusDot } from './app'

// App chrome: a collapsible left sidebar from md up, a bottom tab bar (+ "More" sheet) below md,
// and a top bar with the page title, a ⌘K search trigger and a slot pages fill with actions.

const SlotCtx = createContext<HTMLElement | null>(null)

/** Render children into the top bar's action slot of the current page. */
export function TopActions({ children }: { children: ReactNode }) {
  const el = useContext(SlotCtx)
  return el ? createPortal(children, el) : null
}

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
export const MOD_KEY = isMac ? '⌘' : 'Ctrl'

// Sidebar groups: the working surfaces, then the system pages.
const GROUPS: Array<{ label: string; items: RouteName[] }> = [
  { label: 'Workspace', items: ['chat', 'live', 'runs', 'compare', 'evals', 'agents'] },
  { label: 'System', items: ['settings', 'about'] },
]

function useConn() {
  const { store } = useStore()
  const flying = store.runs.filter(r => !r.done).length
  const text = store.connected ? (flying ? `Live, ${flying} running` : 'Connected') : store.ready ? 'Offline, retrying…' : 'Connecting…'
  const status = store.connected ? (flying ? 'running' : 'done') : store.ready ? 'error' : 'idle'
  return { text, status }
}

function ConnStatus({ withText }: { withText?: boolean }) {
  const { text, status } = useConn()
  return (
    <span role="status" aria-label={text} title={text} className="inline-flex min-w-0">
      <StatusDot status={status} label={withText ? <span className="truncate">{text}</span> : <span className="sr-only">{text}</span>} />
    </span>
  )
}

function NavLink({ item, active, collapsed, live }: { item: NavItem; active: boolean; collapsed: boolean; live: boolean }) {
  const link = (
    <a href={'#' + item.path} aria-current={active ? 'page' : undefined} aria-label={collapsed ? item.label : undefined}
      className={cn(
        'relative flex h-8 items-center gap-2.5 rounded-md px-2.5 text-[13.5px] font-medium outline-none transition-colors',
        'focus-visible:ring-[3px] focus-visible:ring-ring/35',
        active ? 'bg-subtle text-foreground' : 'text-muted-foreground hover:bg-subtle/70 hover:text-foreground',
        collapsed && 'w-9 justify-center px-0',
      )}>
      <Icon name={item.icon} size={17} strokeWidth={1.85} className={cn('shrink-0', active && 'text-primary')} />
      {!collapsed && <span className="truncate">{item.label}</span>}
      {live && <span className={cn('size-1.5 rounded-full bg-ok', collapsed ? 'absolute top-1.5 right-1.5' : 'ml-auto')} aria-hidden="true" />}
    </a>
  )
  if (!collapsed) return link
  return (
    <Tooltip>
      <TooltipTrigger asChild>{link}</TooltipTrigger>
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  )
}

export function Sidebar({ route, collapsed, onToggle }: { route: RouteName; collapsed: boolean; onToggle: () => void }) {
  const { store } = useStore()
  const { theme, cycle } = useTheme()
  const active = navFor(route).name
  const running = store.runs.some(r => !r.done)
  return (
    <aside aria-label="Main" className={cn(
      'tg-scope sticky top-0 hidden h-dvh flex-col border-r border-border bg-background md:flex',
      collapsed ? 'items-center px-2' : 'px-3',
    )}>
      <div className={cn('flex h-14 w-full shrink-0 items-center gap-2', collapsed ? 'justify-center' : 'justify-between pl-1')}>
        <a href="#/" aria-label="TraceGraph home" className="flex min-w-0 items-center gap-2.5 rounded-md text-foreground outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
          <Logo size={24} />
          {!collapsed && <span className="truncate text-[15px] font-semibold tracking-[-0.02em]">TraceGraph</span>}
        </a>
        {!collapsed && <IconButton icon="sidebar-close" label="Collapse sidebar ([)" size="sm" onClick={onToggle} />}
      </div>

      <nav aria-label="Pages" className={cn('mt-2 flex w-full flex-col gap-5', collapsed && 'items-center')}>
        {GROUPS.map(g => (
          <div key={g.label} className={cn('flex w-full flex-col gap-0.5', collapsed && 'items-center')}>
            {!collapsed && <span className="px-2.5 pb-1 text-xs font-medium text-muted-foreground/80">{g.label}</span>}
            {g.items.map(name => (
              <NavLink key={name} item={navFor(name)} active={active === name} collapsed={collapsed} live={name === 'live' && running} />
            ))}
          </div>
        ))}
      </nav>

      <div className={cn('mt-auto flex w-full flex-col gap-2 border-t border-border py-3', collapsed && 'items-center')}>
        {collapsed && <IconButton icon="sidebar-open" label="Expand sidebar ([)" onClick={onToggle} />}
        <EnginePicker engine={store.engine} engines={store.engines} placement="up" compact={collapsed} />
        <div className={cn('flex min-h-8 items-center justify-between gap-2', collapsed ? 'flex-col' : 'pl-1')}>
          <ConnStatus withText={!collapsed} />
          <IconButton icon={THEME_ICON[theme]} label={`Theme: ${THEME_LABEL[theme]} (t)`} size="sm" onClick={cycle} />
        </div>
      </div>
    </aside>
  )
}

export function TopBar({ route, onPalette, onSlot }: { route: RouteName; onPalette: () => void; onSlot: (el: HTMLElement | null) => void }) {
  const nav = navFor(route)
  const title = route === 'run' ? 'Run detail' : nav.title
  return (
    <header className="tg-scope sticky top-0 z-20 flex h-[var(--topbar-h)] shrink-0 items-center gap-3 border-b border-border bg-background/85 px-4 backdrop-blur-md md:px-6">
      <a href="#/" aria-label="TraceGraph home" className="shrink-0 md:hidden"><Logo size={24} /></a>
      <div className="min-w-0 flex-[0_1_auto]">
        <h1 className="m-0 truncate text-[15px] leading-tight font-semibold tracking-[-0.01em] text-foreground">{title}</h1>
        <p className="m-0 hidden truncate text-xs text-muted-foreground md:block">{route === 'run' ? 'Everything that happened in one run' : nav.subtitle}</p>
      </div>
      <div ref={onSlot} className="ml-auto flex min-w-0 items-center gap-2 empty:hidden" />
      <button type="button" data-slot="button" onClick={onPalette} aria-label={`Open command palette (${MOD_KEY}+K)`}
        className={cn(
          'flex h-8 shrink-0 cursor-pointer items-center gap-2 rounded-md border border-border bg-surface text-[13px] text-muted-foreground shadow-xs',
          'outline-none transition-colors hover:border-edge hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35',
          'w-8 justify-center md:w-auto md:justify-start md:pr-1.5 md:pl-2.5 [:empty+&]:ml-auto',
        )}>
        <Icon name="search" size={15} />
        <span className="hidden pr-6 lg:inline">Search or jump to…</span>
        <span className="hidden items-center gap-0.5 md:flex">
          <kbd className="rounded-sm border border-border bg-subtle px-1 font-mono text-[11px]">{MOD_KEY}</kbd>
          <kbd className="rounded-sm border border-border bg-subtle px-1 font-mono text-[11px]">K</kbd>
        </span>
      </button>
      <span className="shrink-0 md:hidden"><ConnStatus /></span>
    </header>
  )
}

export function BottomBar({ route }: { route: RouteName }) {
  const [more, setMore] = useState(false)
  const active = navFor(route).name
  const moreItems = NAV.filter(n => !MOBILE_TABS.includes(n.name))
  const moreActive = moreItems.some(n => n.name === active)
  useEffect(() => { setMore(false) }, [route])
  const item = 'flex flex-1 flex-col items-center justify-center gap-1 text-[11px] font-medium outline-none transition-colors focus-visible:text-foreground'
  return (
    <>
      <nav aria-label="Main" className="tg-scope fixed inset-x-0 bottom-0 z-40 flex h-[calc(var(--bottombar-h)+env(safe-area-inset-bottom,0px))] border-t border-border bg-background/90 pb-[env(safe-area-inset-bottom,0px)] backdrop-blur-md md:hidden">
        {MOBILE_TABS.map(name => {
          const n = navFor(name)
          const on = active === name
          return (
            <a key={name} href={'#' + n.path} aria-current={on ? 'page' : undefined} className={cn(item, on ? 'text-primary' : 'text-muted-foreground')}>
              <Icon name={n.icon} size={20} strokeWidth={1.85} /><span>{n.label}</span>
            </a>
          )
        })}
        <button type="button" data-slot="button" aria-expanded={more} aria-haspopup="dialog" onClick={() => setMore(true)}
          className={cn(item, 'cursor-pointer', moreActive || more ? 'text-primary' : 'text-muted-foreground')}>
          <Icon name="more" size={20} strokeWidth={1.85} /><span>More</span>
        </button>
      </nav>
      <MoreSheet open={more} onOpenChange={setMore} items={moreItems} active={active} />
    </>
  )
}

function MoreSheet({ open, onOpenChange, items, active }: { open: boolean; onOpenChange: (v: boolean) => void; items: NavItem[]; active: RouteName }) {
  const { store } = useStore()
  const { theme, setTheme } = useTheme()
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="bottom" className="gap-0 rounded-t-xl pb-[calc(16px+env(safe-area-inset-bottom,0px))]">
        <div className="mx-auto mt-2 h-1 w-10 rounded-full bg-edge" aria-hidden="true" />
        <SheetTitle className="px-5 pt-3 pb-1 text-sm">More</SheetTitle>
        <SheetDescription className="sr-only">Other pages, the engine and the theme.</SheetDescription>
        <nav aria-label="More pages" className="flex flex-col px-2">
          {items.map(n => (
            <a key={n.name} href={'#' + n.path} onClick={() => onOpenChange(false)} aria-current={active === n.name ? 'page' : undefined}
              className={cn('flex min-h-12 items-center gap-3 rounded-md px-3 transition-colors hover:bg-subtle', active === n.name && 'bg-subtle')}>
              <Icon name={n.icon} size={18} className={active === n.name ? 'text-primary' : 'text-muted-foreground'} />
              <span className="flex min-w-0 flex-col">
                <span className="text-sm font-medium text-foreground">{n.label}</span>
                <span className="truncate text-xs text-muted-foreground">{n.subtitle}</span>
              </span>
            </a>
          ))}
        </nav>
        <div className="mx-4 mt-3 flex flex-col gap-3 border-t border-border pt-4">
          <EnginePicker engine={store.engine} engines={store.engines} placement="up" />
          <div className="flex items-center justify-between gap-3 px-1">
            <span id="more-theme" className="text-sm text-muted-foreground">Theme</span>
            <div role="radiogroup" aria-labelledby="more-theme" className="flex rounded-md border border-border bg-surface p-0.5">
              {THEMES.map(t => (
                <button key={t} type="button" data-slot="button" role="radio" aria-checked={theme === t} onClick={() => setTheme(t)}
                  className={cn('flex h-8 cursor-pointer items-center gap-1.5 rounded-sm px-2.5 text-[13px] font-medium transition-colors',
                    theme === t ? 'bg-subtle text-foreground' : 'text-muted-foreground hover:text-foreground')}>
                  <Icon name={THEME_ICON[t]} size={14} />{THEME_LABEL[t]}
                </button>
              ))}
            </div>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}

export { SlotCtx }

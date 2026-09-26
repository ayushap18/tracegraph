import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Icon, Logo } from '../icons'
import { NAV, MOBILE_TABS, navFor, type RouteName } from '../routes'
import { useStore } from '../store'
import { useTheme, THEME_ICON, THEME_LABEL } from '../theme'
import { EnginePicker } from './EnginePicker'
import { IconButton } from '../ui'

// App chrome: a collapsible left sidebar on desktop, a bottom tab bar (+ "More" sheet) under 760px,
// and a top bar with the page title, the ⌘K hint and a slot pages fill with contextual actions.

const SlotCtx = createContext<HTMLElement | null>(null)

/** Render children into the top bar's action slot of the current page. */
export function TopActions({ children }: { children: ReactNode }) {
  const el = useContext(SlotCtx)
  return el ? createPortal(children, el) : null
}

const isMac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
export const MOD_KEY = isMac ? '⌘' : 'Ctrl'

function ConnDot({ withText }: { withText?: boolean }) {
  const { store } = useStore()
  const flying = store.runs.filter(r => !r.done).length
  const text = store.connected ? (flying ? `Live · ${flying} running` : 'Connected') : store.ready ? 'Offline, retrying…' : 'Connecting…'
  return (
    <span className="conn-dot" title={text} role="status" aria-label={text}>
      <span className={'dot' + (store.connected ? ' on' : '')} aria-hidden="true" />
      {withText && <span className="conn-text">{text}</span>}
    </span>
  )
}

export function Sidebar({ route, collapsed, onToggle }: { route: RouteName; collapsed: boolean; onToggle: () => void }) {
  const { store } = useStore()
  const { theme, cycle } = useTheme()
  const active = navFor(route).name
  return (
    <aside className={'sidebar' + (collapsed ? ' collapsed' : '')} aria-label="Main">
      <div className="sb-top">
        <a className="sb-brand" href="#/" aria-label="TraceGraph home">
          <Logo size={26} />
          {!collapsed && <span className="sb-name">TraceGraph</span>}
        </a>
        {!collapsed && <IconButton icon="sidebar-close" label="Collapse sidebar ([)" size="sm" onClick={onToggle} />}
      </div>
      <nav className="sb-nav">
        {NAV.map(n => (
          <a key={n.name} href={'#' + n.path} className={'sb-item' + (n.name === active ? ' on' : '')} aria-current={n.name === active ? 'page' : undefined}
            title={collapsed ? n.label : undefined} aria-label={collapsed ? n.label : undefined}>
            <Icon name={n.icon} size={17} strokeWidth={1.85} />
            {!collapsed && <span>{n.label}</span>}
            {!collapsed && n.name === 'live' && store.runs.some(r => !r.done) && <span className="sb-live" aria-hidden="true" />}
          </a>
        ))}
      </nav>
      <div className="sb-bottom">
        {collapsed && <IconButton icon="sidebar-open" label="Expand sidebar ([)" onClick={onToggle} />}
        <EnginePicker engine={store.engine} engines={store.engines} placement="up" compact={collapsed} />
        <div className="sb-row">
          <ConnDot withText={!collapsed} />
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
    <header className="topbar">
      <a className="tb-logo" href="#/" aria-label="TraceGraph home"><Logo size={24} /></a>
      <div className="tb-titles">
        <h1 className="tb-title">{title}</h1>
        <p className="tb-sub">{route === 'run' ? 'Everything that happened in one run' : nav.subtitle}</p>
      </div>
      <div className="tb-actions" ref={onSlot} />
      <button type="button" className="tb-search" onClick={onPalette} aria-label={`Open command palette (${MOD_KEY}+K)`}>
        <Icon name="search" size={15} />
        <span className="tb-search-text">Search or jump to…</span>
        <kbd>{MOD_KEY}</kbd><kbd>K</kbd>
      </button>
      <span className="tb-conn"><ConnDot /></span>
    </header>
  )
}

export function BottomBar({ route }: { route: RouteName }) {
  const [more, setMore] = useState(false)
  const active = navFor(route).name
  const moreItems = NAV.filter(n => !MOBILE_TABS.includes(n.name))
  const moreActive = moreItems.some(n => n.name === active)
  useEffect(() => { setMore(false) }, [route])
  return (
    <>
      <nav className="bottombar" aria-label="Main">
        {MOBILE_TABS.map(name => {
          const n = navFor(name)
          return (
            <a key={name} href={'#' + n.path} className={'bb-item' + (active === name ? ' on' : '')} aria-current={active === name ? 'page' : undefined}>
              <Icon name={n.icon} size={20} strokeWidth={1.85} /><span>{n.label}</span>
            </a>
          )
        })}
        <button type="button" className={'bb-item' + (moreActive || more ? ' on' : '')} aria-expanded={more} aria-haspopup="menu" onClick={() => setMore(m => !m)}>
          <Icon name="more" size={20} strokeWidth={1.85} /><span>More</span>
        </button>
      </nav>
      {more && <MoreSheet items={moreItems} active={active} onClose={() => setMore(false)} />}
    </>
  )
}

function MoreSheet({ items, active, onClose }: { items: typeof NAV; active: RouteName; onClose: () => void }) {
  const { store } = useStore()
  const { theme, cycle } = useTheme()
  const first = useRef<HTMLAnchorElement>(null)
  useEffect(() => {
    first.current?.focus()
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" role="menu" aria-label="More" onClick={e => e.stopPropagation()}>
        <div className="sheet-grip" aria-hidden="true" />
        {items.map((n, i) => (
          <a key={n.name} ref={i === 0 ? first : undefined} role="menuitem" href={'#' + n.path} className={'sheet-item' + (active === n.name ? ' on' : '')} onClick={onClose}>
            <Icon name={n.icon} size={18} /><span>{n.label}</span><span className="sheet-sub">{n.subtitle}</span>
          </a>
        ))}
        <div className="sheet-row">
          <EnginePicker engine={store.engine} engines={store.engines} placement="up" />
          <button type="button" role="menuitem" className="btn btn-secondary btn-sm" onClick={cycle}>
            <Icon name={THEME_ICON[theme]} size={14} /><span className="btn-label">Theme: {THEME_LABEL[theme]}</span>
          </button>
        </div>
      </div>
    </div>
  )
}

export { SlotCtx }

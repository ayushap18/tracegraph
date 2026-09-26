import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import type { UiIconName } from './icons'

export type Theme = 'auto' | 'light' | 'dark'
export type Resolved = 'light' | 'dark'
export const THEMES: Theme[] = ['auto', 'dark', 'light']
export const THEME_ICON: Record<Theme, UiIconName> = { auto: 'auto', dark: 'moon', light: 'sun' }
export const THEME_LABEL: Record<Theme, string> = { auto: 'System', dark: 'Dark', light: 'Light' }

// The stored preference is auto | light | dark. <html data-theme> always holds the resolved
// value, so CSS only needs one light and one dark token block. The boot script in
// index.html applies the same logic before first paint, so there is no flash.
const systemDark = () => typeof matchMedia !== 'undefined' && matchMedia('(prefers-color-scheme: dark)').matches
const resolve = (t: Theme): Resolved => (t === 'auto' ? (systemDark() ? 'dark' : 'light') : t)

interface ThemeCtx { theme: Theme; resolved: Resolved; setTheme: (t: Theme) => void; cycle: () => void }
const Ctx = createContext<ThemeCtx>({ theme: 'auto', resolved: 'light', setTheme: () => {}, cycle: () => {} })

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => {
    try { const t = localStorage.getItem('jr-theme'); return THEMES.includes(t as Theme) ? (t as Theme) : 'auto' } catch { return 'auto' }
  })
  const [resolved, setResolved] = useState<Resolved>(() => resolve(theme))

  useEffect(() => {
    const apply = () => {
      const r = resolve(theme)
      setResolved(r)
      document.documentElement.dataset.theme = r
    }
    apply()
    try { localStorage.setItem('jr-theme', theme) } catch { /* private mode: keep it for this visit only */ }
    if (theme !== 'auto' || typeof matchMedia === 'undefined') return
    // Follow the OS while on "System".
    const mq = matchMedia('(prefers-color-scheme: dark)')
    mq.addEventListener('change', apply)
    return () => mq.removeEventListener('change', apply)
  }, [theme])

  const cycle = useCallback(() => setTheme(t => THEMES[(THEMES.indexOf(t) + 1) % THEMES.length]), [])
  return <Ctx.Provider value={{ theme, resolved, setTheme, cycle }}>{children}</Ctx.Provider>
}

export const useTheme = () => useContext(Ctx)

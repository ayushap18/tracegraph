import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react'
import type { UiIconName } from './icons'

export type Theme = 'auto' | 'light' | 'dark'
export const THEMES: Theme[] = ['auto', 'dark', 'light']
export const THEME_ICON: Record<Theme, UiIconName> = { auto: 'auto', dark: 'moon', light: 'sun' }
export const THEME_LABEL: Record<Theme, string> = { auto: 'System', dark: 'Dark', light: 'Light' }

interface ThemeCtx { theme: Theme; setTheme: (t: Theme) => void; cycle: () => void }
const Ctx = createContext<ThemeCtx>({ theme: 'auto', setTheme: () => {}, cycle: () => {} })

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(() => {
    try { const t = localStorage.getItem('jr-theme'); return THEMES.includes(t as Theme) ? (t as Theme) : 'auto' } catch { return 'auto' }
  })
  useEffect(() => {
    const el = document.documentElement
    if (theme === 'auto') delete el.dataset.theme
    else el.dataset.theme = theme
    try { localStorage.setItem('jr-theme', theme) } catch { /* private mode: keep it for this visit only */ }
  }, [theme])
  const cycle = useCallback(() => setTheme(t => THEMES[(THEMES.indexOf(t) + 1) % THEMES.length]), [])
  return <Ctx.Provider value={{ theme, setTheme, cycle }}>{children}</Ctx.Provider>
}

export const useTheme = () => useContext(Ctx)

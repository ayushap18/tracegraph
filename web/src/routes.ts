import type { UiIconName } from './icons'

export type RouteName = 'chat' | 'live' | 'runs' | 'run' | 'compare' | 'evals' | 'agents' | 'settings' | 'about'

export interface NavItem { name: RouteName; path: string; label: string; icon: UiIconName; title: string; subtitle: string; keywords?: string }

export const NAV: NavItem[] = [
  { name: 'chat', path: '/', label: 'Chat', icon: 'chat', title: 'Chat', subtitle: 'Ask anything; follow-ups keep context', keywords: 'home conversation ask' },
  { name: 'live', path: '/live', label: 'Live', icon: 'live', title: 'Live', subtitle: 'Every run as it streams through the router', keywords: 'dashboard realtime analytics graph' },
  { name: 'runs', path: '/runs', label: 'Runs', icon: 'runs', title: 'Runs', subtitle: 'Search and inspect past runs', keywords: 'history table traces' },
  { name: 'compare', path: '/compare', label: 'Compare', icon: 'compare', title: 'Compare', subtitle: 'One question, several engines, side by side', keywords: 'engines side by side' },
  { name: 'evals', path: '/evals', label: 'Evals', icon: 'evals', title: 'Evals', subtitle: 'Accuracy of the full pipeline on a fixed test set', keywords: 'tests accuracy benchmark' },
  { name: 'agents', path: '/agents', label: 'Agents', icon: 'agents', title: 'Agents', subtitle: 'Built-in, guard and custom agents', keywords: 'custom create' },
  { name: 'settings', path: '/settings', label: 'Settings', icon: 'settings', title: 'Settings', subtitle: 'Engines, theme, shortcuts and data', keywords: 'engine theme preferences keyboard' },
  { name: 'about', path: '/about', label: 'About', icon: 'about', title: 'About', subtitle: 'What TraceGraph is', keywords: 'landing info github' },
]

export const MOBILE_TABS: RouteName[] = ['chat', 'live', 'runs', 'evals']

export interface Match { name: RouteName; params: Record<string, string> }

/** Hash path → route. Unknown routes fall back to Chat. */
export function matchRoute(path: string): Match {
  const seg = path.split('/').filter(Boolean).map(s => { try { return decodeURIComponent(s) } catch { return s } })
  const [a, b] = seg
  if (!a) return { name: 'chat', params: {} }
  if (a === 'runs' && b && seg.length === 2) return { name: 'run', params: { qid: b } }
  if ((a === 'compare' || a === 'evals') && seg.length <= 2) return { name: a, params: b ? { id: b } : {} }
  if (seg.length === 1 && ['live', 'runs', 'agents', 'settings', 'about'].includes(a)) return { name: a as RouteName, params: {} }
  return { name: 'chat', params: {} }
}

export const navFor = (name: RouteName): NavItem => NAV.find(n => n.name === (name === 'run' ? 'runs' : name)) ?? NAV[0]

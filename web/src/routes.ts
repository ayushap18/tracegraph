import type { UiIconName } from './icons'

export type RouteName = 'chat' | 'sandbox' | 'live' | 'runs' | 'run' | 'review' | 'files' | 'compare' | 'evals' | 'agents' | 'settings' | 'about'

export interface NavItem { name: RouteName; path: string; label: string; icon: UiIconName; title: string; subtitle: string; description: string; keywords?: string }

export const NAV: NavItem[] = [
  { name: 'chat', path: '/', label: 'Chat', icon: 'chat', title: 'Chat', subtitle: 'Ask anything; follow-ups keep context', description: 'Ask several things at once and follow up with context. Every TraceGraph answer links to the trace of how it was planned and routed.', keywords: 'home conversation ask' },
  { name: 'sandbox', path: '/sandbox', label: 'Sandbox', icon: 'sandbox', title: 'Sandbox', subtitle: 'Try anything; nothing here is saved', description: 'A private scratch space in TraceGraph: ask anything and watch it get planned and routed. Nothing is saved, counted or shown anywhere else.', keywords: 'scratch private ephemeral temporary test playground' },
  { name: 'live', path: '/live', label: 'Live', icon: 'live', title: 'Live', subtitle: 'Every run as it streams through the router', description: 'Watch every TraceGraph run live: the plan, each routing decision, agent timings and the merged answer, as a graph and a timeline.', keywords: 'dashboard realtime analytics graph' },
  { name: 'runs', path: '/runs', label: 'Runs', icon: 'runs', title: 'Runs', subtitle: 'Search and inspect past runs', description: 'Search and inspect past TraceGraph runs, with the full trace, stage timings and routing confidence for each one.', keywords: 'history table traces' },
  { name: 'review', path: '/review', label: 'Review', icon: 'review', title: 'Review', subtitle: 'Routing decisions that looked shaky', description: 'Check the TraceGraph routing decisions that looked doubtful and mark each one right or wrong, so corrections can become eval cases.', keywords: 'feedback label triage queue wrong route correct' },
  { name: 'files', path: '/files', label: 'Files', icon: 'files', title: 'Files', subtitle: 'PDF, Word, PowerPoint, Excel and Markdown files made in chat', description: 'Every file TraceGraph created from a chat: download it, preview it, convert it to another format and see the rules it was checked against.', keywords: 'created documents download pdf docx pptx xlsx markdown export slides spreadsheet word excel powerpoint' },
  { name: 'compare', path: '/compare', label: 'Compare', icon: 'compare', title: 'Compare', subtitle: 'One question, several engines, side by side', description: 'Send one question to Claude Code, Codex, Antigravity or the Anthropic API and compare the answers side by side.', keywords: 'engines side by side' },
  { name: 'evals', path: '/evals', label: 'Evals', icon: 'evals', title: 'Evals', subtitle: 'Accuracy of the full pipeline on a fixed test set', description: 'Measure TraceGraph routing and answer accuracy on a fixed test set, run through the real pipeline.', keywords: 'tests accuracy benchmark' },
  { name: 'agents', path: '/agents', label: 'Agents', icon: 'agents', title: 'Agents', subtitle: 'Built-in, guard and custom agents', description: 'Browse the built-in and guard agents in TraceGraph, or describe your own agent with a prompt.', keywords: 'custom create' },
  { name: 'settings', path: '/settings', label: 'Settings', icon: 'settings', title: 'Settings', subtitle: 'Engines, theme, shortcuts and data', description: 'Choose the TraceGraph engine, theme and keyboard shortcuts, and manage your local data.', keywords: 'engine theme preferences keyboard' },
  { name: 'about', path: '/about', label: 'About', icon: 'about', title: 'About', subtitle: 'What TraceGraph is', description: 'TraceGraph is an open-source workspace that plans multi-part questions, routes each step to the right agent with Jev, and shows every run as a live trace.', keywords: 'landing info github' },
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
  if (seg.length === 1 && ['sandbox', 'live', 'runs', 'review', 'files', 'agents', 'settings', 'about'].includes(a)) return { name: a as RouteName, params: {} }
  return { name: 'chat', params: {} }
}

export const navFor = (name: RouteName): NavItem => NAV.find(n => n.name === (name === 'run' ? 'runs' : name)) ?? NAV[0]

import {
  Activity, BookOpen, Bot, Calculator, ChartGantt, ChartPie, ChevronLeft, ChevronRight, CircleCheck, CircleHelp, Clock,
  CloudSun, CodeXml, Coins, Globe, Grid3x3, Keyboard, ListTree, Maximize2, Merge, MessageSquare, MessageSquareText,
  MessagesSquare, Minimize2, Monitor, Moon, Pause, Play, Radio, Scan, ScrollText, Search, SendHorizontal, ShieldBan,
  Split, Sun, Target, Timer, Wallet, Waypoints, Workflow, X, Zap, ZoomIn, ZoomOut, type LucideIcon,
} from 'lucide-react'
import type { SVGProps } from 'react'
import { colorOf } from './protocol'

// The one place icons are chosen. Components ask for a meaning ("weather", "zoom-in"), never a glyph,
// so the whole app stays on a single stroke set: 24px grid, 1.75 stroke, round caps.

export const AGENT_ICONS: Record<string, LucideIcon> = {
  math: Calculator, weather: CloudSun, time: Clock, currency: Coins, knowledge: BookOpen,
  code: CodeXml, chat: MessageSquare, research: Globe, clarify: CircleHelp, blocked: ShieldBan,
}

export const UI_ICONS = {
  query: MessageSquareText, planner: Split, jev: Waypoints, merger: Merge, answer: CircleCheck,
  sun: Sun, moon: Moon, auto: Monitor, maximize: Maximize2, minimize: Minimize2,
  'zoom-in': ZoomIn, 'zoom-out': ZoomOut, fit: Scan, prev: ChevronLeft, next: ChevronRight, live: Radio,
  search: Search, send: SendHorizontal, play: Play, pause: Pause, close: X, keyboard: Keyboard, bolt: Zap,
  queries: MessagesSquare, subtasks: ListTree, latency: Timer, confidence: Target, activity: Activity, spend: Wallet,
  heatmap: Grid3x3, traffic: ChartPie, timeline: ChartGantt, graph: Workflow, log: ScrollText, agent: Bot,
} satisfies Record<string, LucideIcon>

export type UiIconName = keyof typeof UI_ICONS

export function agentIcon(agent: string | undefined): LucideIcon {
  return (agent && AGENT_ICONS[agent]) || Bot // new agents still get a sensible icon
}

type IconProps = Omit<SVGProps<SVGSVGElement>, 'ref'> & { size?: number; strokeWidth?: number }

export function Icon({ name, size = 16, strokeWidth = 1.75, ...rest }: IconProps & { name: UiIconName }) {
  const C = UI_ICONS[name]
  return <C size={size} strokeWidth={strokeWidth} aria-hidden="true" focusable="false" {...rest} />
}

export function AgentIcon({ agent, size = 16, strokeWidth = 1.75, tinted = false, ...rest }: IconProps & { agent: string | undefined; tinted?: boolean }) {
  const C = agentIcon(agent)
  return <C size={size} strokeWidth={strokeWidth} aria-hidden="true" focusable="false" color={tinted ? colorOf(agent) : undefined} {...rest} />
}

// Brand mark: a tiny trace graph (one node fanning out to three), drawn on the same stroke grid as the icons.
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" className="logo">
      <rect x="1" y="1" width="22" height="22" rx="6" fill="url(#tg-logo)" />
      <defs>
        <linearGradient id="tg-logo" x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
          <stop stopColor="var(--accent)" /><stop offset="1" stopColor="var(--accent2)" />
        </linearGradient>
      </defs>
      <g fill="none" stroke="var(--on-accent)" strokeWidth="1.6" strokeLinecap="round">
        <path d="M7 12 C10 12 11 7.5 15.5 7.5" /><path d="M7 12 H15.5" /><path d="M7 12 C10 12 11 16.5 15.5 16.5" />
      </g>
      <g fill="var(--on-accent)">
        <circle cx="6.5" cy="12" r="2" /><circle cx="17" cy="7.5" r="1.6" /><circle cx="17" cy="12" r="1.6" /><circle cx="17" cy="16.5" r="1.6" />
      </g>
    </svg>
  )
}

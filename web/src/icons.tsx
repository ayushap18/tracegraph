import {
  Activity, BookOpen, Bot, Calculator, ChartGantt, ChartPie, ChevronLeft, ChevronRight, CircleCheck, CircleHelp, Clock,
  CloudSun, CodeXml, Coins, Globe, Grid3x3, Keyboard, ListTree, Maximize2, Merge, MessageSquare, MessageSquareText,
  MessagesSquare, Minimize2, Monitor, Moon, Pause, Play, Radio, Scan, ScrollText, Search, SendHorizontal, ShieldBan,
  Split, Sun, Target, Timer, Wallet, Waypoints, Workflow, X, Zap, ZoomIn, ZoomOut, type LucideIcon,
  Braces, Check, ChevronDown, Cpu, KeyRound, PlugZap, Rocket, SquareTerminal,
  ArrowDown, ArrowRight, ArrowUp, Ban, Blocks, CircleAlert, CircleX, Command, Copy, CornerDownLeft, Database, Ellipsis, ExternalLink, Eye,
  File, FileJson, FileSpreadsheet, FileText, Filter, FlaskConical, GitCompareArrows, Hash, History, Hourglass, House, Info, Layers,
  Link, LoaderCircle, Lock, Menu, PanelLeftClose, PanelLeftOpen, Paperclip, Plus, RefreshCw, RotateCcw, Server, Settings, Share2,
  Shield, Sparkles, Box, Square, SquarePen, Table, Tag, Trash2, TriangleAlert, Trophy, TrendingUp, Upload, Gauge, Download,
  ListChecks, ThumbsDown, ThumbsUp, Undo2,
  ArrowLeftRight, FileCode, FileType, Files, Presentation, Scale,
} from 'lucide-react'
import { useId, type SVGProps } from 'react'
import { colorOf } from './protocol'

// The one place icons are chosen. Components ask for a meaning ("weather", "zoom-in"), never a glyph,
// so the whole app stays on a single stroke set: 24px grid, 1.75 stroke, round caps.

export const AGENT_ICONS: Record<string, LucideIcon> = {
  math: Calculator, weather: CloudSun, time: Clock, currency: Coins, knowledge: BookOpen,
  code: CodeXml, chat: MessageSquare, research: Globe, clarify: CircleHelp, blocked: ShieldBan,
  document: FileText, data: Table, report: ScrollText, run: SquareTerminal,
}

export const UI_ICONS = {
  query: MessageSquareText, planner: Split, jev: Waypoints, merger: Merge, answer: CircleCheck,
  sun: Sun, moon: Moon, auto: Monitor, maximize: Maximize2, minimize: Minimize2,
  'zoom-in': ZoomIn, 'zoom-out': ZoomOut, fit: Scan, prev: ChevronLeft, next: ChevronRight, live: Radio,
  search: Search, send: SendHorizontal, play: Play, pause: Pause, close: X, keyboard: Keyboard, bolt: Zap,
  queries: MessagesSquare, subtasks: ListTree, latency: Timer, confidence: Target, activity: Activity, spend: Wallet,
  heatmap: Grid3x3, traffic: ChartPie, timeline: ChartGantt, graph: Workflow, log: ScrollText, agent: Bot,
  check: Check, 'chevron-down': ChevronDown,
  // v4 pages and actions (several meanings share a glyph so callers can use the word that reads best)
  chat: MessagesSquare, runs: History, history: History, compare: GitCompareArrows, evals: FlaskConical, eval: FlaskConical,
  agents: Bot, bot: Bot, settings: Settings, about: Info, info: Info, home: House,
  stop: Square, upload: Upload, paperclip: Paperclip, attach: Paperclip, share: Share2, link: Link, copy: Copy,
  trash: Trash2, delete: Trash2, plus: Plus, add: Plus, new: SquarePen, external: ExternalLink, file: File, 'file-text': FileText,
  'file-csv': FileSpreadsheet, 'file-json': FileJson, menu: Menu, 'sidebar-close': PanelLeftClose, 'sidebar-open': PanelLeftOpen,
  refresh: RefreshCw, replay: RotateCcw, retry: RotateCcw, download: Download, filter: Filter, warning: TriangleAlert, alert: CircleAlert,
  error: CircleX, success: CircleCheck, more: Ellipsis, 'arrow-right': ArrowRight, 'arrow-up': ArrowUp, 'arrow-down': ArrowDown, sparkles: Sparkles,
  database: Database, command: Command, enter: CornerDownLeft, shield: Shield, guard: Shield, trophy: Trophy, tag: Tag, lock: Lock,
  spinner: LoaderCircle, table: Table, eye: Eye, code: CodeXml, layers: Layers, hash: Hash, server: Server, blocks: Blocks,
  timeout: Hourglass, cancelled: Ban, trend: TrendingUp, gauge: Gauge, engine: Cpu, web: Globe, clock: Clock, sandbox: Box,
  // learning plan: route feedback and the review queue
  review: ListChecks, 'thumbs-up': ThumbsUp, 'thumbs-down': ThumbsDown, undo: Undo2,
  // created files: the Files page, one glyph per format, converting and the ruleset
  files: Files, 'file-pdf': FileType, 'file-docx': FileText, 'file-pptx': Presentation, 'file-xlsx': FileSpreadsheet, 'file-md': FileCode,
  convert: ArrowLeftRight, rules: Scale,
} satisfies Record<string, LucideIcon>

export type UiIconName = keyof typeof UI_ICONS

// LLM engines: subscription CLIs and the pay-per-token API, plus keyless mode.
export const ENGINE_ICONS: Record<string, LucideIcon> = {
  auto: Zap, 'claude-code': SquareTerminal, codex: Braces, agy: Rocket, anthropic: KeyRound, none: PlugZap,
}

export function EngineIcon({ name, size = 15, strokeWidth = 1.9, className }: { name: string; size?: number; strokeWidth?: number; className?: string }) {
  const C = ENGINE_ICONS[name] ?? Cpu
  return <C size={size} strokeWidth={strokeWidth} aria-hidden="true" focusable="false" className={className} />
}

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
  // Unique gradient id per instance: a shared id breaks when the first copy sits in a display:none subtree.
  const gid = 'tg-logo-' + useId().replace(/[^a-zA-Z0-9_-]/g, '')
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true" className="logo">
      <rect x="1" y="1" width="22" height="22" rx="6" fill={`url(#${gid})`} />
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="24" y2="24" gradientUnits="userSpaceOnUse">
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

// Lucide dropped brand marks, so the GitHub logo is drawn here (single path, currentColor).
export function GithubMark({ size = 16 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="currentColor" aria-hidden="true" focusable="false">
      <path d="M12 .5a11.5 11.5 0 0 0-3.64 22.41c.58.1.79-.25.79-.56v-2c-3.2.7-3.88-1.37-3.88-1.37-.52-1.33-1.28-1.69-1.28-1.69-1.05-.72.08-.7.08-.7 1.16.08 1.77 1.19 1.77 1.19 1.03 1.77 2.7 1.26 3.36.96.1-.75.4-1.26.73-1.55-2.55-.29-5.24-1.28-5.24-5.69 0-1.26.45-2.28 1.19-3.09-.12-.29-.52-1.46.11-3.05 0 0 .97-.31 3.17 1.18a11 11 0 0 1 5.77 0c2.2-1.49 3.17-1.18 3.17-1.18.63 1.59.23 2.76.11 3.05.74.81 1.19 1.83 1.19 3.09 0 4.42-2.7 5.39-5.26 5.68.41.36.78 1.06.78 2.14v3.17c0 .31.21.67.8.56A11.5 11.5 0 0 0 12 .5Z" />
    </svg>
  )
}

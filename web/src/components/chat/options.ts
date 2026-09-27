import type { AnswerChecks, AnswerStyle, ChatMode, EngineInfo, RunRecord, RunTimings } from '../../protocol'
import type { UiIconName } from '../../icons'

// Chat variety (docs/PLAN-speed-evals-chat.md, Track C): modes, answer styles, presets and the per-run extras the
// client Run does not carry (mode, style, forced agent, answer group, timings, checks).

export interface ModeInfo { id: ChatMode; label: string; icon: UiIconName; help: string }
export const MODES: ModeInfo[] = [
  { id: 'quick', label: 'Quick', icon: 'bolt', help: 'Fastest. Built-in agents where possible and short answers. Spends almost no plan quota.' },
  { id: 'balanced', label: 'Balanced', icon: 'gauge', help: 'The usual mix of speed and depth. A good default for most questions.' },
  { id: 'deep', label: 'Deep', icon: 'layers', help: 'The strongest engine thinks harder and double checks facts and numbers. Slower.' },
  { id: 'research', label: 'Research', icon: 'web', help: 'Searches the web and answers with sources you can open.' },
]

export interface StyleInfo { id: AnswerStyle; label: string; help: string }
export const STYLES: StyleInfo[] = [
  { id: 'default', label: 'Default', help: 'Whatever fits the question' },
  { id: 'concise', label: 'Concise', help: 'Short and to the point' },
  { id: 'detailed', label: 'Detailed', help: 'Fuller answers with context' },
  { id: 'bullets', label: 'Bullet points', help: 'A scannable list' },
  { id: 'steps', label: 'Step by step', help: 'Numbered steps in order' },
  { id: 'simple', label: 'Explain simply', help: 'Plain words, no jargon' },
  { id: 'table', label: 'Table', help: 'Rows and columns where it helps' },
]
export const modeInfo = (m: ChatMode | undefined) => MODES.find(x => x.id === m) ?? MODES[1]
export const styleInfo = (s: AnswerStyle | undefined) => STYLES.find(x => x.id === s) ?? STYLES[0]

/** One-click starters. `{}` marks where the caret lands after the template fills the composer. */
export interface Preset { id: string; label: string; icon: UiIconName; template: string; mode: ChatMode; style: AnswerStyle }
export const PRESETS: Preset[] = [
  { id: 'summarize', label: 'Summarize', icon: 'file-text', template: 'Summarize this in a few sentences:\n{}', mode: 'balanced', style: 'concise' },
  { id: 'translate', label: 'Translate', icon: 'query', template: 'Translate into Spanish: {}', mode: 'quick', style: 'default' },
  { id: 'code', label: 'Explain code', icon: 'code', template: 'Explain what this code does:\n{}', mode: 'balanced', style: 'steps' },
  { id: 'compare', label: 'Compare two things', icon: 'compare', template: 'What are the differences between {} and ? Include pros, cons and when to pick each', mode: 'balanced', style: 'table' },
  { id: 'trip', label: 'Plan a trip', icon: 'web', template: 'Plan a 3 day trip to {}, with a day by day plan and rough costs', mode: 'balanced', style: 'steps' },
  { id: 'units', label: 'Unit convert', icon: 'hash', template: 'Convert {} km to miles', mode: 'quick', style: 'concise' },
  { id: 'days', label: 'Days between dates', icon: 'clock', template: 'How many days are there between {} and 31 December 2026?', mode: 'quick', style: 'concise' },
]

/** What the chat knows about a run beyond the client Run: from saved records, the ask response and `done`. */
export interface RunExtras {
  mode?: ChatMode; style?: AnswerStyle; agent?: string | null
  group_id?: string | null; chosen?: boolean
  timings?: RunTimings
  tasks?: Record<string, { checks?: AnswerChecks; cached?: boolean; forced?: boolean }> // saved records only
}

export function extrasOf(r: RunRecord): RunExtras {
  const tasks: NonNullable<RunExtras['tasks']> = {}
  for (const t of r.tasks ?? []) if (t.checks || t.cached || t.forced) tasks[t.tid] = { checks: t.checks, cached: t.cached, forced: t.forced }
  const out: RunExtras = { tasks }
  if (r.mode) out.mode = r.mode
  if (r.style) out.style = r.style
  if (r.agent !== undefined) out.agent = r.agent
  if (r.group_id !== undefined) out.group_id = r.group_id
  if (r.chosen !== undefined) out.chosen = r.chosen
  if (r.timings) out.timings = r.timings
  return out
}

// ---------- engines ----------
/** Engines a question can be sent to by name: available ones plus Keyless. Auto is left out (it picks one of these). */
export function pickableEngines(engines: EngineInfo[]): Array<{ name: string; label: string }> {
  return [...engines.filter(e => e.available && e.name !== 'none' && e.name !== 'auto'), { name: 'none', label: 'Keyless' }]
}
export const engineLabel = (engines: EngineInfo[], name: string | null | undefined, fallback = 'Default engine') =>
  name == null ? fallback : name === 'none' ? 'Keyless' : engines.find(e => e.name === name)?.label ?? name
export const researchReason = (engines: EngineInfo[]) =>
  engines.some(e => e.available && e.web) ? null : 'Needs an engine with web search. Set one up in Settings.'

// ---------- remembered choices (per chat session; storage may be blocked) ----------
const OPTS_KEY = 'tg-chat-opts:'
const COMPARE_KEY = 'tg-chat-compare'
export interface ChatOpts { mode: ChatMode; style: AnswerStyle }
export const DEFAULT_OPTS: ChatOpts = { mode: 'balanced', style: 'default' }

export function readOpts(sid: string | null): ChatOpts {
  try {
    const v = JSON.parse(localStorage.getItem(OPTS_KEY + (sid ?? 'new')) ?? 'null')
    return {
      mode: MODES.some(m => m.id === v?.mode) ? v.mode : DEFAULT_OPTS.mode,
      style: STYLES.some(s => s.id === v?.style) ? v.style : DEFAULT_OPTS.style,
    }
  } catch { return DEFAULT_OPTS }
}
export function writeOpts(sid: string | null, o: ChatOpts) {
  try { localStorage.setItem(OPTS_KEY + (sid ?? 'new'), JSON.stringify(o)) } catch { /* ignore */ }
}
export function readCompare(): string[] {
  try { const v = JSON.parse(localStorage.getItem(COMPARE_KEY) ?? '[]'); return Array.isArray(v) ? v.filter(x => typeof x === 'string').slice(0, 3) : [] } catch { return [] }
}
export function writeCompare(names: string[]) {
  try { localStorage.setItem(COMPARE_KEY, JSON.stringify(names)) } catch { /* ignore */ }
}

/** "@name" anywhere in the text for an offered agent: that agent and the text without it. */
export function takeAgent(text: string, offered: string[]): { agent: string | null; text: string } {
  const re = /(^|\s)@([\w-]+)\b/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    const name = m[2].toLowerCase()
    if (offered.includes(name)) {
      const start = m.index + m[1].length
      return { agent: name, text: (text.slice(0, start) + text.slice(start + m[2].length + 1)).replace(/\s{2,}/g, ' ').trim() }
    }
  }
  return { agent: null, text }
}

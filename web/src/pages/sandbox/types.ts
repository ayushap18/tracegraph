// Shared types for the Sandbox page and its parts (docs/PLAN-sandbox.md).
// Components in this folder receive plain props; Sandbox.tsx owns all state and wires them together.
import type { DraftAgent, EngineInfo, FileInfo, Prices } from '../../protocol'
import type { Run, Store } from '../../useEventStream'

export type { DraftAgent }

/** One thing the user asked. `single`: one run, possibly edited into several versions. `compare`: the same
 *  question sent to several engines at once (side by side). */
export interface TurnGroup {
  id: string
  kind: 'single' | 'compare'
  /** single: every version's qid in order (an edit adds one); compare: one qid per engine, in `engines` order. */
  qids: number[]
  /** single: which version is shown (index into qids). */
  active: number
  /** compare only: the engine names asked, aligned with qids ('none' = keyless). */
  engines?: string[]
}

/** Engine choice for the sandbox: a name from store.engines, or 'none' for keyless. '' means the app's active engine. */
export type EngineChoice = string

// ---------- FE-A: engines and numbers ----------

export interface EngineSelectProps {
  engines: EngineInfo[]            // store.engines (may include 'auto')
  activeLabel: string              // label of the app's active engine, for the "Default" option
  value: EngineChoice
  onChange: (v: EngineChoice) => void
  /** Side-by-side: engines picked for the next question (2 to 4). Empty = off. */
  compareWith: string[]
  onCompareWith: (names: string[]) => void
  disabled?: boolean
}

export interface UsageMeterProps {
  runs: Run[]                      // every finished or running sandbox run shown on the page
  prices: Prices                   // store.prices (per 1M tokens)
  engines: EngineInfo[]            // for billing: subscription engines cost nothing per call
}

export interface SideBySideProps {
  group: TurnGroup                 // kind === 'compare'
  byQid: Map<number, Run>
  engines: EngineInfo[]
  selectedQid: number | null
  onShowTrace: (qid: number) => void
  onStop: (qid: number) => void
}

// export.ts
export interface ExportInput { groups: TurnGroup[]; byQid: Map<number, Run>; engines: EngineInfo[]; draft: DraftAgent | null }

// ---------- FE-B: insight and iteration ----------

export interface RoutingWhyProps {
  run: Run
  agents: Record<string, string>   // store.agents: name -> description
  draftName?: string               // highlight when Jev picked the draft agent
}

export interface TurnGroupViewProps {
  group: TurnGroup                 // kind === 'single'
  byQid: Map<number, Run>
  agents: Record<string, string>
  draftName?: string
  selectedQid: number | null       // turn whose trace panel is open
  busy: boolean                    // a run is in flight: editing is disabled
  onShowTrace: (qid: number) => void
  onEdit: (group: TurnGroup, text: string) => void  // re-run an edited question (adds a version)
  onSelectVersion: (group: TurnGroup, index: number) => void
  fileName?: (id: string) => string  // attachment id -> file name (sandbox files live only in page state)
}

// ---------- FE-C: agents, files, keep ----------

export interface PlaygroundProps {
  value: DraftAgent | null         // null = playground off
  onChange: (v: DraftAgent | null) => void
  engineOk: boolean                // the chosen engine is an LLM (draft agents need one)
  runs: Run[]                      // sandbox runs, to count how often Jev routed to the draft
  builtIn: string[]                // names the draft may not use (built-ins, guards, saved customs)
  open: boolean
  onOpenChange: (v: boolean) => void
}

export interface AttachmentsProps {
  sandboxId: string
  files: FileInfo[]                // attached to the next message (already uploaded to sandbox memory)
  onChange: (files: FileInfo[]) => void
  disabled?: boolean
}

export interface KeepDialogProps {
  sandboxId: string
  qids: number[]                   // the finished runs currently shown (active versions / all compare runs)
  open: boolean
  onOpenChange: (v: boolean) => void
}

export type { Run, Store, FileInfo, EngineInfo, Prices }

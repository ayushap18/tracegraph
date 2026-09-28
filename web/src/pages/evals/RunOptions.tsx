import { useState, type ReactNode } from 'react'
import type { EngineInfo, EvalMode, EvalSplit, JevSource } from '../../protocol'
import { Icon } from '../../icons'
import { buttonClass } from '../../ui'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Select, SelectContent, SelectItem, SelectTrigger } from '@/components/ui/select'
import { cn } from '@/lib/utils'

// Start-run options for the harder suite (docs/PLAN-speed-evals-chat.md B2, B3): which split, which tags, how many
// repeats to measure flakiness, and which engine judges open-ended answers. Accuracy v2 (docs/PLAN-accuracy-v2.md D3,
// D5) adds the mode (the full pipeline, or planning and routing only), where Jev's answers come from (live, or replayed
// from the recorded cassette) and an automatic judge.

// judge 'none' = no judge, 'auto' = the first healthy engine that is not the one under test
export interface RunOpts { split: EvalSplit; tags: string[]; repeat: number; judge: string; mode: EvalMode; jev: JevSource }
export const DEFAULT_OPTS: RunOpts = { split: 'all', tags: [], repeat: 1, judge: 'none', mode: 'full', jev: 'live' }

const MODE_LABEL: Record<EvalMode, string> = { full: 'Full', route: 'Routing only' }
const MODE_HINT: Record<EvalMode, string> = {
  full: 'Runs every agent and scores answers and files',
  route: 'Plans and routes each case, then stops before any agent runs. Fast, and spends no engine quota',
}
const JEV_LABEL: Record<JevSource, string> = { live: 'Live', replay: 'Replay', record: 'Record' }

const SPLIT_LABEL: Record<EvalSplit, string> = { all: 'All cases', dev: 'Dev only', holdout: 'Holdout only' }
const SPLIT_HINT: Record<EvalSplit, string> = {
  all: 'Dev and holdout, scored together',
  dev: 'Cases you may tune routing against',
  holdout: 'Scored only, never used for fixes or labels',
}
const TRIGGER = 'h-9 w-full min-w-0 bg-background text-[13px]'

/** replayReady: a recorded cassette exists (the server has stored a replay or record run), so Replay can be offered. */
export function RunOptions({ opts, onChange, knownTags, engines, engine, disabled, replayReady }: {
  opts: RunOpts; onChange: (o: RunOpts) => void; knownTags: string[]; engines: EngineInfo[]; engine: string; disabled?: boolean
  replayReady?: boolean
}) {
  const set = (patch: Partial<RunOpts>) => onChange({ ...opts, ...patch })
  const judges = engines.filter(e => e.name !== 'none' && e.name !== 'auto')
  const engineLabel = (n: string) => n === 'auto' ? 'Auto' : engines.find(e => e.name === n)?.label ?? n
  const route = opts.mode === 'route'
  const sameJudge = !route && opts.judge !== 'none' && opts.judge !== 'auto' && opts.judge === engine
  const spends = route ? [] : [engine && engine !== 'none' ? `${engineLabel(engine)} answers every case` : null,
    opts.judge === 'auto' ? 'another healthy engine judges open-ended answers' : opts.judge !== 'none' ? `${engineLabel(opts.judge)} judges open-ended answers` : null].filter(Boolean)

  return (
    <div className="flex flex-col gap-3">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <Labeled id="ev-mode" label="Mode">
          <Select value={opts.mode} onValueChange={v => set({ mode: v as EvalMode, ...(v === 'full' ? { jev: 'live' as JevSource } : {}) })} disabled={disabled}>
            <SelectTrigger id="ev-mode" className={TRIGGER} title={MODE_HINT[opts.mode]}>
              <span className="truncate">{MODE_LABEL[opts.mode]}</span>
            </SelectTrigger>
            <SelectContent position="popper">
              {(['full', 'route'] as EvalMode[]).map(m => (
                <SelectItem key={m} value={m}>
                  <span className="flex flex-col"><span>{MODE_LABEL[m]}</span><span className="max-w-72 text-xs text-muted-foreground">{MODE_HINT[m]}</span></span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Labeled>
        <Labeled id="ev-jev" label="Jev">
          <Select value={opts.jev} onValueChange={v => set({ jev: v as JevSource })} disabled={disabled || !route}>
            <SelectTrigger id="ev-jev" className={TRIGGER}
              title={!route ? 'Full runs always ask Jev live' : replayReady ? 'Live asks Jev; Replay reuses its recorded answers' : 'No recorded cassette yet, so only Live is available'}>
              <span className="truncate">{JEV_LABEL[opts.jev]}</span>
            </SelectTrigger>
            <SelectContent position="popper">
              <SelectItem value="live">
                <span className="flex flex-col"><span>Live</span><span className="text-xs text-muted-foreground">Ask Jev for every case (Jev tokens only)</span></span>
              </SelectItem>
              <SelectItem value="replay" disabled={!replayReady}>
                <span className="flex flex-col"><span>Replay</span><span className="max-w-72 text-xs text-muted-foreground">
                  {replayReady ? "Reuse Jev's recorded answers: offline and repeatable" : 'Record a cassette first: python -m jevrouter.evals --mode route --jev record'}
                </span></span>
              </SelectItem>
            </SelectContent>
          </Select>
        </Labeled>
      </div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <Labeled id="ev-split" label="Split">
          <Select value={opts.split} onValueChange={v => set({ split: v as EvalSplit })} disabled={disabled}>
            <SelectTrigger id="ev-split" className={TRIGGER} title={SPLIT_HINT[opts.split]}>
              <span className="truncate">{SPLIT_LABEL[opts.split]}</span>
            </SelectTrigger>
            <SelectContent position="popper">
              {(['all', 'dev', 'holdout'] as EvalSplit[]).map(s => (
                <SelectItem key={s} value={s}>
                  <span className="flex flex-col"><span>{SPLIT_LABEL[s]}</span><span className="text-xs text-muted-foreground">{SPLIT_HINT[s]}</span></span>
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Labeled>
        <Labeled id="ev-tags" label="Tags">
          <TagPicker id="ev-tags" value={opts.tags} known={knownTags} onChange={tags => set({ tags })} disabled={disabled} />
        </Labeled>
        <Labeled id="ev-repeat" label="Repeat">
          <Select value={String(opts.repeat)} onValueChange={v => set({ repeat: Number(v) })} disabled={disabled}>
            <SelectTrigger id="ev-repeat" className={TRIGGER}>
              <span className="truncate">{opts.repeat === 1 ? 'Once' : `${opts.repeat} times`}</span>
            </SelectTrigger>
            <SelectContent position="popper">
              {[1, 2, 3, 4, 5].map(n => <SelectItem key={n} value={String(n)}>{n === 1 ? 'Once' : `${n} times, flags flaky cases`}</SelectItem>)}
            </SelectContent>
          </Select>
        </Labeled>
        <Labeled id="ev-judge" label="Judge">
          <Select value={opts.judge} onValueChange={v => set({ judge: v })} disabled={disabled || route}>
            <SelectTrigger id="ev-judge" className={TRIGGER} title={route ? 'Routing-only runs make no answers to judge' : undefined}>
              <span className="truncate">{route || opts.judge === 'none' ? 'No judge' : engineLabel(opts.judge)}</span>
            </SelectTrigger>
            <SelectContent position="popper" align="end">
              <SelectItem value="none">No judge (open-ended cases are counted as unjudged)</SelectItem>
              <SelectItem value="auto">
                <span className="flex flex-col"><span>Auto</span><span className="text-xs text-muted-foreground">The first healthy engine that is not the one being tested</span></span>
              </SelectItem>
              {judges.map(e => (
                <SelectItem key={e.name} value={e.name} disabled={!e.available}>{e.label}{e.available ? '' : ' (unavailable)'}</SelectItem>
              ))}
            </SelectContent>
          </Select>
        </Labeled>
      </div>
      {(spends.length > 0 || sameJudge) && (
        <p role="note" className="m-0 flex items-start gap-2 rounded-md bg-warn/10 px-3 py-2 text-[13px] leading-snug text-foreground">
          <Icon name="warning" size={15} className="mt-0.5 shrink-0 text-warn" />
          <span>
            {spends.length > 0 && <>{spends.join(' and ')}, which spends your plan quota{opts.repeat > 1 ? `, ${opts.repeat} times over` : ''}. </>}
            {sameJudge && 'The judge is the engine being tested; a different engine gives a fairer score.'}
          </span>
        </p>
      )}
    </div>
  )
}

function Labeled({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <label htmlFor={id} className="text-xs text-muted-foreground">{label}</label>
      {children}
    </div>
  )
}

/** Multi-select of tags: known tags as checkboxes, plus a box to add one the history has not seen yet. */
function TagPicker({ id, value, known, onChange, disabled }: {
  id: string; value: string[]; known: string[]; onChange: (v: string[]) => void; disabled?: boolean
}) {
  const [draft, setDraft] = useState('')
  const all = [...new Set([...known, ...value])].sort()
  const toggle = (t: string) => onChange(value.includes(t) ? value.filter(x => x !== t) : [...value, t])
  const add = () => {
    const t = draft.trim().replace(/^#/, '').toLowerCase()
    if (t && !value.includes(t)) onChange([...value, t])
    setDraft('')
  }
  return (
    <Popover>
      <PopoverTrigger id={id} disabled={disabled} data-slot="button"
        className={cn(buttonClass('secondary', 'md'), 'h-9 w-full min-w-0 justify-between bg-background px-3 text-[13px] font-normal')}>
        <span className="truncate">{value.length ? value.map(t => '#' + t).join(' ') : 'Every tag'}</span>
        <Icon name="chevron-down" size={14} className="shrink-0 opacity-60" />
      </PopoverTrigger>
      <PopoverContent align="start" className="flex w-64 flex-col gap-2 p-3">
        <span className="text-xs text-muted-foreground">Run only cases with any of these tags</span>
        {all.length === 0
          ? <span className="text-[13px] text-muted-foreground">No tags known yet. Add one below, or run once to list them.</span>
          : (
            <ul className="m-0 flex max-h-56 list-none flex-col gap-0.5 overflow-y-auto p-0">
              {all.map(t => (
                <li key={t}>
                  <label className="flex cursor-pointer items-center gap-2 rounded-sm px-1.5 py-1 font-mono text-xs text-foreground hover:bg-subtle">
                    <input type="checkbox" checked={value.includes(t)} onChange={() => toggle(t)} className="size-4 accent-primary" />#{t}
                  </label>
                </li>
              ))}
            </ul>
          )}
        <form className="flex gap-1.5" onSubmit={e => { e.preventDefault(); add() }}>
          <input value={draft} onChange={e => setDraft(e.target.value)} placeholder="Add a tag" aria-label="Add a tag"
            className="h-8 min-w-0 flex-1 rounded-md border border-border bg-background px-2 text-[13px] text-foreground outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35" />
          <button type="submit" className={buttonClass('secondary', 'sm')} disabled={!draft.trim()}>Add</button>
        </form>
        {value.length > 0 && <button type="button" className={cn(buttonClass('ghost', 'sm'), 'self-start')} onClick={() => onChange([])}>Clear, run every tag</button>}
      </PopoverContent>
    </Popover>
  )
}

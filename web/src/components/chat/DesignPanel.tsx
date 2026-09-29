import { useCallback, useContext, useEffect, useId, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import {
  ApiError, createdDesign, createdThumbs, errorText, fontPreviewUrl, listDesignPresets, polishCreated, restyleCreated, searchFonts,
} from '../../api'
import type {
  CreatedFile, DesignCheckSummary, DesignFontUse, DesignLayoutId, DesignPlanSummary, DesignPresetId, DesignPresetInfo, DesignQaResult,
  DesignReport, DesignStop, DesignTemplateId, DesignTemplateInfo, EngineInfo, FontInfo, PolishBody, RestyleBody, Thumb,
} from '../../protocol'
import { Button, EmptyState, Skeleton, Spinner, badgeClass, useToast } from '../../ui'
import { Icon } from '../../icons'
import { useStore } from '../../store'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { cn } from '@/lib/utils'
import { costRemembered, rememberCost, useCostConfirm } from './CostDialog'
import { FileActionsContext } from './CreatedFiles'

// The Design panel of a created file (docs/PLAN-designer.md 5 and 9.9): page thumbnails, the design score and report,
// and restyling from the stored content. Restyle (another preset, template, fonts, dark or light, a print version,
// a layout per slide) makes a new file by code and costs no model tokens. Polish runs one design critic round on an
// engine that can look at images, so it goes through the cost confirmation first.

// ---------- labels ----------

export const LAYOUT_LABEL: Record<DesignLayoutId, string> = {
  'cover-hero': 'Cover with photo', 'cover-type': 'Cover with big title', 'section-divider': 'Section divider', 'title-bullets': 'Title and bullets',
  'image-left-text': 'Picture left, text right', 'image-right-text': 'Text left, picture right', 'full-bleed-image-caption': 'Full picture with caption',
  'big-number': 'Big number', 'stat-cards': 'Stat cards', quote: 'Quote', 'two-column': 'Two columns', comparison: 'Comparison',
  'full-width-diagram': 'Full width diagram', 'chart-focus': 'Chart with takeaway', 'timeline-strip': 'Timeline', closing: 'Closing slide',
  cover: 'Cover page', 'chapter-opener': 'Chapter opener', 'text-side-figure': 'Text with side figure', 'two-column-text': 'Two column text',
  'full-figure': 'Full page figure', 'pull-quote': 'Pull quote', 'key-points': 'Key points box', references: 'References', freeform: 'Free-form',
}
const SLIDE_LAYOUTS: DesignLayoutId[] = ['cover-hero', 'cover-type', 'section-divider', 'title-bullets', 'image-left-text', 'image-right-text',
  'full-bleed-image-caption', 'big-number', 'stat-cards', 'quote', 'two-column', 'comparison', 'full-width-diagram', 'chart-focus', 'timeline-strip', 'closing']
const PAGE_TEMPLATES: DesignLayoutId[] = ['cover', 'chapter-opener', 'text-side-figure', 'two-column-text', 'full-figure', 'pull-quote', 'key-points', 'references']

const STOP_TEXT: Record<DesignStop, string> = {
  pass: 'Every check passed.', rounds: 'Stopped after the most fixing rounds allowed.', budget: 'Stopped at the token budget.',
  deadline: 'Stopped at the time limit.', error: 'The design stage hit an error, so some fixes were skipped.', keyless: 'Designed by code, with no model calls.',
}
const LICENCE_LABEL: Record<string, string> = { 'OFL-1.1': 'SIL Open Font Licence', 'Apache-2.0': 'Apache 2.0', 'UFL-1.0': 'Ubuntu Font Licence', system: 'Installed on this machine', user: 'Your own font', builtin: 'Built in' }
const LICENCE_SHORT: Record<string, string> = { 'OFL-1.1': 'OFL', 'Apache-2.0': 'Apache', 'UFL-1.0': 'UFL' }
const ROLE_LABEL: Record<string, string> = { display: 'Titles', heading: 'Headings', body: 'Body', caption: 'Captions', mono: 'Code' }
type FontSlot = 'display' | 'heading' | 'body'
const FONT_SLOTS: FontSlot[] = ['heading', 'body', 'display']

const unit = (f: Pick<CreatedFile, 'format'>) => (f.format === 'pptx' ? 'Slide' : 'Page')
const PASS = 80
const scoreTone = (s: number) => (s >= PASS ? 'ok' : s >= 60 ? 'warn' : 'bad')

// ---------- shared data ----------

type Presets = { presets: DesignPresetInfo[]; templates: DesignTemplateInfo[] }
let presetsCache: Promise<Presets> | null = null
/** Presets and templates, fetched once for every card on the page (a failure is retried on the next ask). */
function loadPresets(retry = false) {
  if (!presetsCache || retry) {
    presetsCache = listDesignPresets()
    presetsCache.catch(() => { presetsCache = null })
  }
  return presetsCache
}
function usePresets() {
  const [state, setState] = useState<{ data?: Presets; error?: string } | null>(null)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let alive = true
    setState(null)
    loadPresets(nonce > 0).then(d => alive && setState({ data: d }), e => alive && setState({ error: errorText(e) }))
    return () => { alive = false }
  }, [nonce])
  return { ...state, loading: !state, retry: () => setNonce(n => n + 1) }
}

/** Whether an engine can read images: the server says so (`vision`), else Claude Code and most API-key engines can. */
function vision(e: EngineInfo | null | undefined) {
  if (!e) return false
  return e.vision ?? (e.name === 'claude-code' || e.billing === 'api')
}
/** The engine Polish would use and, when it can't, why. Auto resolves to the engine it tries first. */
function usePolishEngine(): { engine: EngineInfo | null; reason: string | null } {
  const { store } = useStore()
  let e = store.engine
  if (e?.name === 'auto') e = store.engines.find(x => x.name === e?.lead) ?? null
  if (!e) return { engine: null, reason: 'Polish needs an engine that can look at images. You are in keyless mode; pick Claude Code or an API key with a vision model in Settings.' }
  if (!e.available) return { engine: e, reason: `${e.label} is not available right now${e.why ? `: ${e.why}` : ''}.` }
  if (!vision(e)) return { engine: e, reason: `${e.label} can't look at images, so it can't polish a design. Switch to Claude Code or an API key with a vision model to use Polish.` }
  return { engine: e, reason: null }
}

// ---------- the panel ----------

export function DesignPanel({ file, onMade, id }: { file: CreatedFile; onMade?: (f: CreatedFile) => void; id?: string }) {
  const [design, setDesign] = useState<{ report: DesignReport | null; plan: DesignPlanSummary | null; error?: string; status?: number } | null>(null)
  const [thumbs, setThumbs] = useState<{ list: Thumb[]; reason?: string; error?: string } | null>(null)
  const [nonce, setNonce] = useState(0)
  const [layouts, setLayouts] = useState<Record<string, DesignLayoutId>>({})
  const [viewer, setViewer] = useState<number | null>(null)

  useEffect(() => {
    let alive = true
    setDesign(null); setThumbs(null)
    createdDesign(file).then(d => alive && setDesign(d), e => alive && setDesign({ report: null, plan: null, error: errorText(e), status: e instanceof ApiError ? e.status : 0 }))
    createdThumbs(file).then(t => alive && setThumbs({ list: t.thumbs ?? [], reason: t.reason }), e => alive && setThumbs({ list: [], error: errorText(e) }))
    return () => { alive = false }
  }, [file.id, file.sandbox, nonce]) // eslint-disable-line react-hooks/exhaustive-deps

  const report = design?.report ?? null
  const pool = file.format === 'pptx' ? SLIDE_LAYOUTS : PAGE_TEMPLATES
  const loading = !design || !thumbs

  return (
    <section id={id} className="flex min-w-0 flex-col gap-4 border-t border-border p-3" aria-label={`Design of ${file.name}`} aria-busy={loading || undefined}>
      {design?.error ? (
        <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground" role="alert">
          <Icon name="alert" size={14} className="text-destructive" />
          {design.status === 404 ? 'This file is gone from the server.' : `Could not load the design: ${design.error}`}
          <Button variant="ghost" size="sm" icon="refresh" onClick={() => setNonce(n => n + 1)}>Retry</Button>
        </div>
      ) : !design ? (
        <div className="flex flex-col gap-2" aria-label="Loading the design"><Skeleton width="45%" height={16} /><Skeleton lines={2} /></div>
      ) : report ? (
        <ReportSummary report={report} file={file} />
      ) : (
        <p className="m-0 text-[13px] leading-relaxed text-muted-foreground">
          This file was made without the design stage, so it has no design report yet. Restyle it below to lay it out with a preset; that costs no model tokens.
        </p>
      )}

      <ThumbStrip file={file} state={thumbs} overrides={layouts} onOpen={setViewer} onRetry={() => setNonce(n => n + 1)} />

      <RestyleForm file={file} report={report} layouts={layouts} clearLayouts={() => setLayouts({})} onMade={onMade} />

      {viewer != null && thumbs?.list.length ? (
        <ThumbViewer file={file} thumbs={thumbs.list} index={viewer} onIndex={setViewer} onClose={() => setViewer(null)}
          pool={pool} overrides={layouts} results={report?.results ?? []}
          setLayout={(page0, l) => setLayouts(o => {
            const next = { ...o }
            const orig = thumbs.list[page0]?.layout
            if (!l || l === orig) delete next[String(page0)]
            else next[String(page0)] = l
            return next
          })} />
      ) : null}
    </section>
  )
}

// ---------- score and report ----------

function ReportSummary({ report: r, file }: { report: DesignReport; file: CreatedFile }) {
  const failing = r.checks.filter(c => !c.ok)
  const tokens = Object.values(r.tokens ?? {}).reduce((n, v) => n + (v || 0), 0)
  return (
    <div className="flex min-w-0 flex-col gap-2.5">
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
        <ScoreMeter score={r.score} />
        <div className="flex min-w-0 flex-1 flex-col gap-0.5">
          <p className="m-0 text-sm font-medium text-foreground">
            {r.score >= PASS ? 'Looks good' : r.score >= 60 ? 'A few things to fix' : 'Needs work'}
            <span className="font-normal text-muted-foreground">{' '}with the {presetName(r.preset)} preset</span>
          </p>
          <p className="m-0 text-xs text-muted-foreground">
            {failing.length ? `${failing.length} of ${r.checks.length} checks still flag something. ` : `All ${r.checks.length} checks passed. `}
            {STOP_TEXT[r.stop] ?? ''}{tokens > 0 ? ` The design used ${tokens.toLocaleString()} tokens.` : ''}
          </p>
        </div>
      </div>
      {r.notes.length > 0 && (
        <ul className="m-0 flex list-disc flex-col gap-1 pl-5 text-[13px] leading-relaxed text-foreground">
          {r.notes.map((n, i) => <li key={i} className="[overflow-wrap:anywhere]">{n}</li>)}
        </ul>
      )}
      <FontsUsed fonts={r.fonts} format={file.format} skip={r.notes} />
      <Collapsible className="flex flex-col">
        <CollapsibleTrigger className="group/d -mx-1 inline-flex cursor-pointer items-center gap-1 self-start rounded-sm px-1 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35">
          <Icon name="rules" size={12} />Design checks <span className="tabular-nums">({r.checks.length - failing.length} of {r.checks.length} passed)</span>
          <Icon name="chevron-down" size={12} className="transition-transform group-data-[state=open]/d:rotate-180" />
        </CollapsibleTrigger>
        <CollapsibleContent>
          <ul className="m-0 mt-1.5 grid list-none grid-cols-1 gap-1 p-0 sm:grid-cols-2" aria-label="Design checks">
            {r.checks.map(c => <CheckRow key={c.id} check={c} results={r.results.filter(x => x.id === c.id)} unitWord={unit(file)} />)}
          </ul>
          {r.critic?.ran && r.critic.edits?.length ? (
            <p className="m-0 mt-2 text-xs text-muted-foreground">The design critic made {r.critic.edits.length} change{r.critic.edits.length === 1 ? '' : 's'}{r.critic.rolled_back?.length ? ` and ${r.critic.rolled_back.length} was undone because it made things worse` : ''}.</p>
          ) : null}
          {r.fallbacks.length > 0 && (
            <ul className="m-0 mt-2 flex list-disc flex-col gap-0.5 pl-5 text-xs text-muted-foreground">
              {r.fallbacks.map((f, i) => <li key={i}>{unit(file)} {f.page + 1} uses {LAYOUT_LABEL[f.to as DesignLayoutId] ?? f.to} instead of {LAYOUT_LABEL[f.from as DesignLayoutId] ?? f.from}: {f.why}</li>)}
            </ul>
          )}
        </CollapsibleContent>
      </Collapsible>
    </div>
  )
}

function ScoreMeter({ score }: { score: number }) {
  const s = Math.max(0, Math.min(100, Math.round(score)))
  const tone = scoreTone(s)
  const color = tone === 'ok' ? 'var(--color-ok, #16a34a)' : tone === 'warn' ? 'var(--color-warn, #d97706)' : 'var(--color-destructive, #dc2626)'
  return (
    <span className="relative grid size-12 shrink-0 place-items-center" role="img" aria-label={`Design score ${s} out of 100${s >= PASS ? ', a pass' : ''}`}>
      <svg viewBox="0 0 36 36" className="absolute inset-0 size-12 -rotate-90" aria-hidden="true">
        <circle cx="18" cy="18" r="15.5" fill="none" stroke="currentColor" strokeWidth="3" className="text-border" />
        <circle cx="18" cy="18" r="15.5" fill="none" stroke={color} strokeWidth="3" strokeLinecap="round" strokeDasharray={`${(s / 100) * 97.4} 97.4`} />
      </svg>
      <span className="text-sm font-semibold tabular-nums text-foreground">{s}</span>
    </span>
  )
}

function CheckRow({ check: c, results, unitWord }: { check: DesignCheckSummary; results: DesignQaResult[]; unitWord: string }) {
  return (
    <li className="flex min-w-0 items-start gap-1.5 text-xs">
      <Icon name={c.ok ? 'check' : 'warning'} size={13} strokeWidth={2.1} className={cn('mt-px shrink-0', c.ok ? 'text-ok' : 'text-warn')} />
      <span className="sr-only">{c.ok ? 'passed' : 'flagged'}: </span>
      <span className="min-w-0 [overflow-wrap:anywhere]">
        <span className="mr-1.5 font-mono text-[11px] text-muted-foreground">{c.id}</span>
        <span className="font-medium text-foreground">{c.name}</span>
        {!c.ok && (
          <span className="block text-muted-foreground">
            {results.length ? results.slice(0, 3).map(x => (x.page != null && !x.note.includes(unitWord) ? `${unitWord} ${x.page + 1}: ${x.note}` : x.note)).join(' ') : c.note}
          </span>
        )}
      </span>
    </li>
  )
}

function FontsUsed({ fonts, format, skip = [] }: { fonts: DesignFontUse[]; format: CreatedFile['format']; skip?: string[] }) {
  if (!fonts.length) return null
  const notes = [...new Set(fonts.map(f => f.note).filter((n): n is string => !!n && !skip.includes(n)))]
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0" aria-label="Fonts used">
        {fonts.map(f => (
          <li key={f.role + f.family} className="inline-flex h-6 min-w-0 max-w-full items-center gap-1 rounded-md border border-border bg-subtle/60 px-1.5 text-xs text-muted-foreground"
            title={`${LICENCE_LABEL[f.licence] ?? f.licence}${f.embedded ? ', embedded in the file' : ''}${f.fallback ? `. Without it, ${format === 'pptx' ? 'PowerPoint' : 'Word'} shows ${f.fallback}.` : ''}`}>
            <Icon name="font" size={12} className="shrink-0" />
            <span className="text-muted-foreground">{ROLE_LABEL[f.role] ?? f.role}</span>
            <span className="truncate font-medium text-foreground">{f.family}</span>
            {LICENCE_SHORT[f.licence] && <span className="font-mono text-[10px]">{LICENCE_SHORT[f.licence]}</span>}
            {f.embedded && <span className="sr-only">, embedded</span>}
          </li>
        ))}
      </ul>
      {notes.map((n, i) => <p key={i} className="m-0 text-xs text-muted-foreground [overflow-wrap:anywhere]">{n}</p>)}
    </div>
  )
}

// ---------- thumbnails ----------

function ThumbStrip({ file, state, overrides, onOpen, onRetry }: {
  file: CreatedFile; state: { list: Thumb[]; reason?: string; error?: string } | null
  overrides: Record<string, DesignLayoutId>; onOpen: (i: number) => void; onRetry: () => void
}) {
  const refs = useRef<Array<HTMLButtonElement | null>>([])
  const [focus, setFocus] = useState(0)
  const word = unit(file)
  if (!state) {
    return (
      <div className="flex gap-2 overflow-hidden" aria-label="Loading thumbnails">
        {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} width={file.format === 'pptx' ? 144 : 96} height={file.format === 'pptx' ? 81 : 136} radius={6} className="shrink-0" />)}
      </div>
    )
  }
  if (state.error) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground" role="alert">
        <Icon name="alert" size={14} className="text-destructive" />Could not load the thumbnails: {state.error}
        <Button variant="ghost" size="sm" icon="refresh" onClick={onRetry}>Retry</Button>
      </div>
    )
  }
  if (!state.list.length) {
    return <p className="m-0 flex items-start gap-1.5 text-[13px] text-muted-foreground"><Icon name="images" size={14} className="mt-0.5 shrink-0" />{state.reason || 'No thumbnails for this file.'}</p>
  }
  const move = (e: KeyboardEvent, i: number) => {
    const last = state.list.length - 1
    const to = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? Math.min(last, i + 1) : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? Math.max(0, i - 1)
      : e.key === 'Home' ? 0 : e.key === 'End' ? last : null
    if (to == null) return
    e.preventDefault()
    setFocus(to)
    refs.current[to]?.focus()
    refs.current[to]?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }
  const changed = Object.keys(overrides).length
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <p className="m-0 flex flex-wrap items-center gap-x-2 text-xs text-muted-foreground" id={`thumbs-${file.id}`}>
        <span>{state.list.length} {word.toLowerCase()}s. Open one for a larger view{file.format === 'pptx' || file.format === 'pdf' ? ' or to change its layout' : ''}.</span>
        {changed > 0 && <span className={badgeClass('info')}>{changed} layout change{changed === 1 ? '' : 's'}</span>}
      </p>
      <ol className="m-0 flex list-none snap-x gap-2 overflow-x-auto p-0.5 pb-2 [scrollbar-width:thin]" aria-labelledby={`thumbs-${file.id}`}>
        {state.list.map((t, i) => {
          const override = overrides[String(t.page - 1)]
          const ratio = t.w > 0 && t.h > 0 ? t.w / t.h : file.format === 'pptx' ? 16 / 9 : 3 / 4
          return (
            <li key={t.page} className="shrink-0 snap-start">
              <button ref={el => { refs.current[i] = el }} type="button" data-thumb={`${file.id}:${i}`} tabIndex={i === focus ? 0 : -1}
                onClick={() => { setFocus(i); onOpen(i) }} onKeyDown={e => move(e, i)} onFocus={() => setFocus(i)}
                aria-label={`${word} ${t.page}: ${LAYOUT_LABEL[override ?? t.layout] ?? t.layout}${override ? ' (changed, restyle to apply)' : ''}. Open a larger view`}
                className="group/t relative block cursor-zoom-in overflow-hidden rounded-md border border-border bg-subtle outline-none transition-shadow hover:border-edge focus-visible:ring-[3px] focus-visible:ring-ring/50"
                style={{ height: ratio >= 1 ? 81 : 136, width: Math.round((ratio >= 1 ? 81 : 136) * ratio) }}>
                <ThumbImage src={t.url} alt="" />
                <span className="absolute bottom-1 left-1 rounded bg-black/60 px-1 text-[10px] font-medium tabular-nums text-white">{t.page}</span>
                {override && <span className="absolute top-1 right-1 grid size-4 place-items-center rounded-full bg-primary text-primary-foreground" aria-hidden="true"><Icon name="layers" size={10} strokeWidth={2.4} /></span>}
              </button>
            </li>
          )
        })}
      </ol>
    </div>
  )
}

/** A lazy image that shows a placeholder while loading and says so when it fails. */
function ThumbImage({ src, alt, className, eager }: { src: string; alt: string; className?: string; eager?: boolean }) {
  const [state, setState] = useState<'loading' | 'ok' | 'error'>('loading')
  useEffect(() => setState('loading'), [src])
  return (
    <>
      {state !== 'ok' && (
        <span className="absolute inset-0 grid place-items-center text-muted-foreground" aria-hidden={state === 'loading'}>
          {state === 'loading' ? <Spinner size={14} /> : <span className="flex flex-col items-center gap-0.5 text-[10px]"><Icon name="images" size={14} />Not available</span>}
        </span>
      )}
      <img src={src} alt={alt} loading={eager ? 'eager' : 'lazy'} decoding="async" draggable={false}
        onLoad={() => setState('ok')} onError={() => setState('error')}
        className={cn('block size-full object-contain transition-opacity', state === 'ok' ? 'opacity-100' : 'opacity-0', className)} />
    </>
  )
}

function ThumbViewer({ file, thumbs, index, onIndex, onClose, pool, overrides, setLayout, results }: {
  file: CreatedFile; thumbs: Thumb[]; index: number; onIndex: (i: number) => void; onClose: () => void
  pool: DesignLayoutId[]; overrides: Record<string, DesignLayoutId>; setLayout: (page0: number, l: DesignLayoutId | null) => void
  results: DesignQaResult[]
}) {
  const t = thumbs[Math.min(index, thumbs.length - 1)]
  const word = unit(file)
  const selectId = useId()
  const current = overrides[String(t.page - 1)] ?? t.layout
  const canPick = file.format === 'pptx' || file.format === 'pdf'
  const issues = results.filter(r => r.page === t.page - 1 && !r.ok)
  const go = (d: number) => onIndex(Math.max(0, Math.min(thumbs.length - 1, index + d)))
  const ratio = t.w > 0 && t.h > 0 ? t.w / t.h : file.format === 'pptx' ? 16 / 9 : 3 / 4
  const boxId = useId()
  // A button that becomes disabled (Next on the last slide) drops focus; keep it in the dialog so the arrows still work.
  useEffect(() => { if (document.activeElement === document.body) document.getElementById(boxId)?.focus() }, [index, boxId])
  return (
    <Dialog open onOpenChange={o => { if (!o) onClose() }}>
      <DialogContent className="max-h-[calc(100dvh-2rem)] gap-3 overflow-y-auto p-4 sm:max-w-3xl"
        id={boxId} onOpenAutoFocus={e => { e.preventDefault(); document.getElementById(boxId)?.focus() }}
        onCloseAutoFocus={e => { e.preventDefault(); document.querySelector<HTMLButtonElement>(`[data-thumb="${file.id}:${index}"]`)?.focus() }}
        onKeyDown={e => {
          if ((e.target as HTMLElement).tagName === 'SELECT') return
          if (e.key === 'ArrowRight') { e.preventDefault(); go(1) }
          if (e.key === 'ArrowLeft') { e.preventDefault(); go(-1) }
        }}>
        <DialogHeader className="gap-1 pr-8 text-left">
          <DialogTitle className="text-[15px]">{word} {t.page} of {thumbs.length}</DialogTitle>
          <DialogDescription className="text-[13px]">
            {LAYOUT_LABEL[t.layout] ?? t.layout}. Use the left and right arrow keys to move between {word.toLowerCase()}s.
          </DialogDescription>
        </DialogHeader>
        <div className="relative mx-auto w-full overflow-hidden rounded-md border border-border bg-subtle" style={{ aspectRatio: String(ratio), maxWidth: ratio >= 1 ? '100%' : `min(100%, calc(60dvh * ${ratio}))` }}>
          <ThumbImage key={t.url} src={t.url} alt={`${word} ${t.page}, ${LAYOUT_LABEL[t.layout] ?? t.layout}`} eager />
        </div>
        {issues.length > 0 && (
          <ul className="m-0 flex list-none flex-col gap-1 p-0">
            {issues.map((r, i) => (
              <li key={i} className="flex items-start gap-1.5 text-xs text-warn"><Icon name="warning" size={13} className="mt-px shrink-0" /><span className="[overflow-wrap:anywhere]"><span className="mr-1 font-mono text-[11px]">{r.id}</span>{r.note}</span></li>
            ))}
          </ul>
        )}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
          {canPick ? (
            <div className="flex min-w-0 flex-col gap-1">
              <label htmlFor={selectId} className="text-xs font-medium text-foreground">Layout for this {word.toLowerCase()}</label>
              <select id={selectId} value={current} onChange={e => setLayout(t.page - 1, e.target.value as DesignLayoutId)}
                className="h-9 max-w-full rounded-md border border-border bg-surface px-2.5 text-sm text-foreground shadow-xs outline-none focus-visible:border-edge focus-visible:ring-[3px] focus-visible:ring-ring/35">
                {!pool.includes(t.layout) && <option value={t.layout}>{LAYOUT_LABEL[t.layout] ?? t.layout}</option>}
                {pool.map(l => <option key={l} value={l}>{LAYOUT_LABEL[l]}{l === t.layout ? ' (now)' : ''}</option>)}
              </select>
              <span className="text-xs text-muted-foreground">{current !== t.layout ? 'Changed. Press Restyle to apply it; that costs no model tokens.' : 'Pick another layout, then press Restyle.'}</span>
            </div>
          ) : <span />}
          <div className="flex shrink-0 gap-2">
            <Button variant="secondary" size="sm" icon="prev" disabled={index === 0} onClick={() => go(-1)}>Previous</Button>
            <Button variant="secondary" size="sm" iconRight="next" disabled={index >= thumbs.length - 1} onClick={() => go(1)}>Next</Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}

// ---------- restyle and polish ----------

type Look = 'keep' | 'light' | 'dark'
type StyleChoice = { kind: 'preset'; id: DesignPresetId } | { kind: 'template'; id: DesignTemplateId } | null

const presetName = (id: string | null | undefined, list?: DesignPresetInfo[]) =>
  list?.find(p => p.id === id)?.name ?? (id === 'custom' ? 'custom' : id ? id.replace(/-/g, ' ').replace(/^./, c => c.toUpperCase()) : 'default')

function RestyleForm({ file, report, layouts, clearLayouts, onMade }: {
  file: CreatedFile; report: DesignReport | null; layouts: Record<string, DesignLayoutId>; clearLayouts: () => void; onMade?: (f: CreatedFile) => void
}) {
  const toast = useToast()
  const ctx = useContext(FileActionsContext)
  const presets = usePresets()
  const [style, setStyle] = useState<StyleChoice>(null)
  const [fonts, setFonts] = useState<Partial<Record<FontSlot, string>>>({})
  const [look, setLook] = useState<Look>('keep')
  const [print, setPrint] = useState(false)
  const [busy, setBusy] = useState<'restyle' | 'polish' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [status, setStatus] = useState('')
  const engineCheck = usePolishEngine()
  // Polish works on a stored design plan; a file made without Studio has none until it is restyled.
  const polishEngine = !report && !engineCheck.reason ? { ...engineCheck, reason: 'Polish needs a design plan, and this file has none yet. Restyle it first, then polish the new file.' } : engineCheck
  const cost = useCostConfirm()
  const lookName = useId()

  const current = report?.preset ?? file.design?.preset ?? null
  const body = useMemo<RestyleBody>(() => {
    const b: RestyleBody = {}
    if (style?.kind === 'preset') b.preset = style.id
    if (style?.kind === 'template') b.template = style.id
    const f = Object.fromEntries(Object.entries(fonts).filter(([, v]) => !!v))
    if (Object.keys(f).length) b.fonts = f
    if (look !== 'keep') b.dark = look === 'dark'
    if (print) b.print = true
    if (Object.keys(layouts).length) b.layouts = layouts
    return b
  }, [style, fonts, look, print, layouts])
  const changes = describe(body, presets.data)
  const empty = changes.length === 0

  const reset = () => { setStyle(null); setFonts({}); setLook('keep'); setPrint(false); clearLayouts(); setError(null) }

  const made = (n: CreatedFile, what: string) => {
    onMade?.(n)
    const msg = `${what} as ${n.name}`
    setStatus(msg)
    toast.success(msg)
  }

  const restyle = async () => {
    if (empty || busy) return
    setBusy('restyle'); setError(null); setStatus('Restyling')
    try {
      const n = await restyleCreated(file, body)
      made(n, 'Restyled with no model tokens')
      reset()
    } catch (e) {
      const s = e instanceof ApiError ? e.status : 0
      const msg = s === 409 ? `This file can't be restyled: ${errorText(e)}`
        : s === 422 ? `The restyled file failed a check, so it was not saved: ${errorText(e)}`
          : s === 404 ? 'This file is gone from the server.'
            : `Could not restyle: ${errorText(e)}`
      setError(msg); setStatus('')
    } finally { setBusy(null) }
  }

  const sendPolish = async (b: PolishBody): Promise<void> => {
    setBusy('polish'); setError(null); setStatus('Polishing')
    try {
      const r = await polishCreated(file, b)
      if (r.kind === 'confirm') {
        setStatus('')
        cost.ask({ estimate: r.estimate, body: b, go: sendPolish, ...(ctx.rememberKey ? { onRemember: () => rememberCost(ctx.rememberKey) } : {}) })
        return
      }
      made(r.res, 'Polished')
    } catch (e) {
      const s = e instanceof ApiError ? e.status : 0
      setError(s === 409 ? `This file can't be polished: ${errorText(e)}` : s === 400 ? errorText(e) : s === 503 ? `The engine is not available: ${errorText(e)}` : `Could not polish: ${errorText(e)}`)
      setStatus('')
    } finally { setBusy(null) }
  }
  const polish = () => {
    if (polishEngine.reason || busy) return
    void sendPolish({ ...(polishEngine.engine ? { engine: polishEngine.engine.name } : {}), ...(costRemembered(ctx.rememberKey) ? { confirm_cost: true } : {}) })
  }

  const polishHint = `polish-hint-${file.id}`
  const restyleHint = `restyle-hint-${file.id}`
  return (
    <div className="flex min-w-0 flex-col gap-3 rounded-md border border-border bg-subtle/30 p-3">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <StylePicker presets={presets} value={style} current={current} onChange={setStyle} format={file.format} />
        <FontPicker value={fonts} onChange={setFonts} />
        <fieldset className="m-0 flex min-w-0 items-center border-0 p-0">
          <legend className="sr-only">Background</legend>
          <div className="inline-flex h-8 items-center rounded-md border border-border bg-surface p-0.5 shadow-xs">
            {(['keep', 'light', 'dark'] as Look[]).map(v => (
              <label key={v} className={cn('relative inline-flex h-full cursor-pointer items-center gap-1 rounded-[5px] px-2 text-[13px] font-medium transition-colors has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/35',
                look === v ? 'bg-subtle text-foreground' : 'text-muted-foreground hover:text-foreground')}>
                <input type="radio" name={lookName} value={v} checked={look === v} onChange={() => setLook(v)} className="sr-only" />
                {v !== 'keep' && <Icon name={v === 'dark' ? 'moon' : 'sun'} size={13} />}
                {v === 'keep' ? 'As is' : v === 'dark' ? 'Dark' : 'Light'}
              </label>
            ))}
          </div>
        </fieldset>
        <label className="inline-flex h-8 cursor-pointer items-center gap-2 rounded-md px-1 text-[13px] text-foreground has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/35"
          title="Black and white, print friendly sizes and no dark backgrounds">
          <input type="checkbox" checked={print} onChange={e => setPrint(e.target.checked)} className="size-4 cursor-pointer accent-primary" />
          Print version
        </label>
      </div>

      {changes.length > 0 && (
        <p className="m-0 text-xs text-muted-foreground [overflow-wrap:anywhere]"><span className="font-medium text-foreground">Changes: </span>{changes.join(', ')}.</p>
      )}

      <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:flex-wrap sm:items-center">
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="primary" size="sm" icon="palette" loading={busy === 'restyle'} disabled={empty || busy === 'polish'} onClick={() => void restyle()}
            aria-describedby={restyleHint}>
            {busy === 'restyle' ? 'Restyling…' : 'Restyle'}
          </Button>
          <span className={badgeClass('ok')} title="Restyling reuses the stored content and lays it out again by code">0 tokens</span>
          {changes.length > 0 && <Button variant="ghost" size="sm" onClick={reset} disabled={!!busy}>Reset</Button>}
        </div>
        <div className="flex flex-wrap items-center gap-2 sm:ml-auto">
          <Button variant="secondary" size="sm" icon="sparkles" loading={busy === 'polish'} disabled={!!polishEngine.reason || busy === 'restyle'}
            onClick={polish} aria-describedby={polishHint}>
            {busy === 'polish' ? 'Polishing…' : 'Polish'}
          </Button>
        </div>
      </div>
      <div className="flex flex-col gap-1 text-xs leading-relaxed text-muted-foreground">
        <p id={restyleHint} className="m-0">{empty ? 'Pick a preset, template, font, background, print version or slide layout, then Restyle. ' : ''}Restyle makes a new file from the stored content and costs no model tokens.</p>
        <p id={polishHint} className={cn('m-0', polishEngine.reason && 'text-foreground')}>
          {polishEngine.reason
            ? <><Icon name="info" size={12} className="mr-1 inline align-[-2px]" />{polishEngine.reason}</>
            : `Polish asks ${polishEngine.engine?.label ?? 'the engine'} to look at the ${file.format === 'pptx' ? 'slides' : 'pages'} and suggest fixes, which code applies. It uses model tokens, so you see the cost first.`}
        </p>
      </div>
      {error && <p className="m-0 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-[13px] text-destructive [overflow-wrap:anywhere]" role="alert">{error}</p>}
      <p className="sr-only" role="status" aria-live="polite">{status}</p>
      {cost.dialog}
    </div>
  )
}

function describe(b: RestyleBody, presets?: Presets): string[] {
  const out: string[] = []
  if (b.template) out.push(`${presets?.templates.find(t => t.id === b.template)?.name ?? b.template} template`)
  if (b.preset) out.push(`${presetName(b.preset, presets?.presets)} preset`)
  for (const [role, fam] of Object.entries(b.fonts ?? {})) out.push(`${fam} for ${(ROLE_LABEL[role] ?? role).toLowerCase()}`)
  if (b.dark != null) out.push(b.dark ? 'dark background' : 'light background')
  if (b.print) out.push('print version')
  const n = Object.keys(b.layouts ?? {}).length
  if (n) out.push(`${n} layout change${n === 1 ? '' : 's'}`)
  return out
}

// ---------- the Presets menu: presets with previews, then student templates ----------

function StylePicker({ presets, value, current, onChange, format }: {
  presets: ReturnType<typeof usePresets>; value: StyleChoice; current: string | null; onChange: (v: StyleChoice) => void; format: CreatedFile['format']
}) {
  const [open, setOpen] = useState(false)
  const name = useId()
  const label = value?.kind === 'template'
    ? presets.data?.templates.find(t => t.id === value.id)?.name ?? value.id
    : value?.kind === 'preset' ? presetName(value.id, presets.data?.presets) : current ? `Preset: ${presetName(current, presets.data?.presets)}` : 'Presets'
  const pick = (v: StyleChoice) => onChange(v)
  const isSel = (kind: 'preset' | 'template', id: string) => value?.kind === kind && value.id === id
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="secondary" size="sm" icon="palette" iconRight="chevron-down" aria-label={`Presets and templates. ${value ? `Chosen: ${label}` : label}`}>
          <span className="max-w-[12rem] truncate">{label}</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="max-h-[min(70dvh,560px)] w-[min(460px,calc(100vw-32px))] overflow-y-auto p-0">
        {presets.error ? (
          <EmptyState compact icon="alert" title="Could not load the presets" text={presets.error}
            action={<Button variant="secondary" size="sm" icon="refresh" onClick={presets.retry}>Retry</Button>} />
        ) : !presets.data ? (
          <div className="grid grid-cols-2 gap-2 p-3" aria-busy="true" aria-label="Loading the presets">
            {Array.from({ length: 4 }, (_, i) => <Skeleton key={i} height={96} radius={6} />)}
          </div>
        ) : (
          <fieldset className="m-0 flex flex-col gap-3 border-0 p-3">
            <legend className="sr-only">Pick a preset or a student template</legend>
            <label className={cn('flex cursor-pointer items-center gap-2 rounded-md px-1 py-1 text-[13px] has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/35', !value ? 'font-medium text-foreground' : 'text-muted-foreground')}>
              <input type="radio" name={name} checked={!value} onChange={() => pick(null)} className="size-3.5 accent-primary" />
              {current ? `Keep ${presetName(current, presets.data.presets)}` : 'Keep the file as it is'}
            </label>
            <div className="flex flex-col gap-1.5">
              <h4 className="m-0 text-xs font-medium tracking-wide text-muted-foreground uppercase">Presets</h4>
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                {presets.data.presets.map(p => (
                  <label key={p.id} title={p.description}
                    className={cn('group/p flex cursor-pointer flex-col gap-1 rounded-md border p-1.5 text-left transition-colors has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/35',
                      isSel('preset', p.id) ? 'border-primary bg-primary/5' : 'border-border hover:bg-subtle')}>
                    <input type="radio" name={name} value={p.id} checked={isSel('preset', p.id)} onChange={() => pick({ kind: 'preset', id: p.id })} className="sr-only" />
                    <span className="relative block aspect-[16/9] overflow-hidden rounded-sm border border-border" style={{ background: '#' + p.colors.bg }}>
                      <ThumbImage src={p.thumb} alt="" />
                    </span>
                    <span className="flex items-center gap-1 text-[13px] font-medium text-foreground">
                      <span className="min-w-0 truncate">{p.name}</span>
                      {p.id === current && <span className="text-[11px] font-normal text-muted-foreground">(now)</span>}
                      {isSel('preset', p.id) && <Icon name="check" size={13} className="ml-auto shrink-0 text-primary" />}
                    </span>
                    <span className="line-clamp-2 text-[11px] leading-snug text-muted-foreground">{p.description}</span>
                  </label>
                ))}
              </div>
            </div>
            <div className="flex flex-col gap-1.5">
              <h4 className="m-0 text-xs font-medium tracking-wide text-muted-foreground uppercase">Student templates</h4>
              <ul className="m-0 flex list-none flex-col gap-1 p-0">
                {presets.data.templates.map(t => (
                  <li key={t.id}>
                    <label className={cn('flex cursor-pointer items-start gap-2 rounded-md border px-2 py-1.5 transition-colors has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/35',
                      isSel('template', t.id) ? 'border-primary bg-primary/5' : 'border-transparent hover:bg-subtle')}>
                      <input type="radio" name={name} value={t.id} checked={isSel('template', t.id)} onChange={() => pick({ kind: 'template', id: t.id })} className="mt-1 size-3.5 shrink-0 accent-primary" />
                      <span className="flex min-w-0 flex-col">
                        <span className="text-[13px] font-medium text-foreground">
                          {t.name}
                          <span className="ml-1.5 font-normal text-muted-foreground">{presetName(t.preset, presets.data?.presets)}{t.paper ? `, ${t.paper.toUpperCase()}` : ''}{t.format !== format ? `, made for ${t.format.toUpperCase()}` : ''}</span>
                        </span>
                        <span className="text-[11px] leading-snug text-muted-foreground">{t.description}</span>
                      </span>
                    </label>
                  </li>
                ))}
              </ul>
            </div>
            <div className="flex justify-end border-t border-border pt-2">
              <Button variant="primary" size="sm" onClick={() => setOpen(false)}>Done</Button>
            </div>
          </fieldset>
        )}
      </PopoverContent>
    </Popover>
  )
}

// ---------- the font picker: open fonts only, with live previews and licences ----------

function FontPicker({ value, onChange }: { value: Partial<Record<FontSlot, string>>; onChange: (v: Partial<Record<FontSlot, string>>) => void }) {
  const [open, setOpen] = useState(false)
  const [role, setRole] = useState<FontSlot>('heading')
  const [q, setQ] = useState('')
  const [res, setRes] = useState<{ fonts: FontInfo[]; offline: boolean; q: string } | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [nonce, setNonce] = useState(0)
  const name = useId()
  const roleId = useId()
  const searchId = useId()

  useEffect(() => {
    if (!open) return
    let alive = true
    const query = q.trim().slice(0, 64)
    setLoading(true); setError(null)
    const t = setTimeout(() => {
      searchFonts(query, 20).then(r => { if (alive) setRes({ ...r, q: query }) }, e => { if (alive) setError(errorText(e)) })
        .finally(() => { if (alive) setLoading(false) })
    }, query ? 250 : 0)
    return () => { alive = false; clearTimeout(t) }
  }, [q, open, nonce])

  const chosen = FONT_SLOTS.filter(r => value[r])
  const label = chosen.length ? chosen.map(r => value[r]).join(', ') : 'Fonts'
  const set = (fam: string | null) => {
    const next = { ...value }
    if (fam) next[role] = fam; else delete next[role]
    onChange(next)
  }
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button variant="secondary" size="sm" icon="font" iconRight="chevron-down" aria-label={chosen.length ? `Fonts. Chosen: ${chosen.map(r => `${value[r]} for ${ROLE_LABEL[r].toLowerCase()}`).join(', ')}` : 'Fonts'}>
          <span className="max-w-[10rem] truncate">{label}</span>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="flex max-h-[min(70dvh,560px)] w-[min(420px,calc(100vw-32px))] flex-col gap-2.5 overflow-hidden p-3">
        <div className="flex flex-wrap items-end gap-2">
          <div className="flex flex-col gap-1">
            <label htmlFor={roleId} className="text-xs font-medium text-foreground">Use for</label>
            <select id={roleId} value={role} onChange={e => setRole(e.target.value as FontSlot)}
              className="h-8 rounded-md border border-border bg-surface px-2 text-[13px] text-foreground shadow-xs outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35">
              {FONT_SLOTS.map(r => <option key={r} value={r}>{ROLE_LABEL[r]}{value[r] ? `: ${value[r]}` : ''}</option>)}
            </select>
          </div>
          <div className="flex min-w-0 flex-1 flex-col gap-1">
            <label htmlFor={searchId} className="text-xs font-medium text-foreground">Search open fonts</label>
            <input id={searchId} type="search" value={q} maxLength={64} onChange={e => setQ(e.target.value)} placeholder="Poppins, serif, mono"
              className="h-8 w-full min-w-0 rounded-md border border-border bg-surface px-2.5 text-[13px] text-foreground shadow-xs outline-none placeholder:text-muted-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35" />
          </div>
        </div>
        <p className="m-0 text-[11px] leading-snug text-muted-foreground">Only openly licensed fonts are listed (SIL Open Font Licence, Apache 2.0 or Ubuntu Font Licence), so the file is free to share.</p>
        {res?.offline && <p className="m-0 flex items-center gap-1.5 text-[11px] text-warn"><Icon name="warning" size={12} />Offline: only fonts already on this machine are listed.</p>}
        <div className="min-h-0 flex-1 overflow-y-auto" aria-busy={loading || undefined}>
          {error ? (
            <EmptyState compact icon="alert" title="Could not search fonts" text={error}
              action={<Button variant="secondary" size="sm" icon="refresh" onClick={() => setNonce(n => n + 1)}>Retry</Button>} />
          ) : !res ? (
            <div className="flex flex-col gap-2" aria-label="Loading fonts">{Array.from({ length: 4 }, (_, i) => <Skeleton key={i} height={40} radius={6} />)}</div>
          ) : !res.fonts.length ? (
            <EmptyState compact icon="font" title="No open font matches"
              text={res.q ? `Nothing openly licensed is called "${res.q}". Proprietary fonts can't be used; try a similar open one, like Inter or Source Serif 4.` : 'No fonts are available.'} />
          ) : (
            <fieldset className={cn('m-0 flex flex-col gap-1 border-0 p-0 transition-opacity', loading && 'opacity-60')}>
              <legend className="sr-only">Font for {ROLE_LABEL[role].toLowerCase()}</legend>
              <label className={cn('flex cursor-pointer items-center gap-2 rounded-md border px-2 py-1.5 text-[13px] has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/35',
                !value[role] ? 'border-primary bg-primary/5 text-foreground' : 'border-transparent text-muted-foreground hover:bg-subtle')}>
                <input type="radio" name={name} checked={!value[role]} onChange={() => set(null)} className="size-3.5 accent-primary" />
                Keep the current {ROLE_LABEL[role].toLowerCase()} font
              </label>
              {res.fonts.map(f => (
                <label key={f.family} className={cn('flex cursor-pointer items-center gap-2 rounded-md border px-2 py-1.5 has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/35',
                  value[role] === f.family ? 'border-primary bg-primary/5' : 'border-transparent hover:bg-subtle')}>
                  <input type="radio" name={name} value={f.family} checked={value[role] === f.family} onChange={() => set(f.family)} className="size-3.5 shrink-0 accent-primary" />
                  <span className="flex min-w-0 flex-1 flex-col gap-0.5">
                    <span className="flex min-w-0 items-center gap-1.5 text-[13px] font-medium text-foreground">
                      <span className="truncate">{f.family}</span>
                      <span className={badgeClass('neutral')} title={LICENCE_LABEL[f.licence] ?? f.licence}>{LICENCE_SHORT[f.licence] ?? f.licence}</span>
                      <span className="text-[11px] font-normal text-muted-foreground">{f.category}{f.installed ? '' : ', downloads once'}</span>
                    </span>
                    <FontPreview family={f.family} />
                  </span>
                </label>
              ))}
            </fieldset>
          )}
        </div>
        <div className="flex justify-end border-t border-border pt-2">
          <Button variant="primary" size="sm" onClick={() => setOpen(false)}>Done</Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}

/** The server's rendering of the family; the family's name in plain text when the font isn't on the server yet. */
function FontPreview({ family }: { family: string }) {
  const [failed, setFailed] = useState(false)
  const src = fontPreviewUrl(family, 'The quick brown fox')
  const onError = useCallback(() => setFailed(true), [])
  if (failed) return <span className="text-[11px] text-muted-foreground">Preview after it downloads</span>
  return (
    <span className="block h-6 overflow-hidden rounded-sm bg-white/90 dark:bg-white/85">
      <img src={src} alt={`${family} sample: The quick brown fox`} loading="lazy" decoding="async" onError={onError} className="block h-6 w-auto max-w-none" />
    </span>
  )
}


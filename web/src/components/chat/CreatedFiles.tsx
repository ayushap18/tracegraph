import { useEffect, useState } from 'react'
import { convertCreated, createdUrl, errorText, previewCreated } from '../../api'
import type { CreatedFile, FileFormat, FilePhase, FilePreview, ImageCredit, RuleResult, ThemeName } from '../../protocol'
import { Button, IconButton, Skeleton, buttonClass, timeAgo, useToast } from '../../ui'
import { safeHref } from '../../lib'
import { Icon, type UiIconName } from '../../icons'
import Markdown from '../Markdown'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'

// Files the `create` agent made (docs/PLAN-files.md): one card per file with its format, size and shape, the tokens
// its content cost, a download link, "Convert to…" (re-rendered from the stored spec, 0 tokens), an inline preview and
// the ruleset results (docs/RULES-files.md): warnings shown, automatic fixes folded away. Chat, Run detail and the
// Files page all use these cards.

export const FORMATS: FileFormat[] = ['pdf', 'docx', 'pptx', 'xlsx', 'md']

// One colour per format, tuned for both themes; the icon and the label carry the meaning too (A4: never colour alone).
export const FORMAT_INFO: Record<FileFormat, { label: string; short: string; icon: UiIconName; tone: string }> = {
  pdf: { label: 'PDF', short: 'PDF', icon: 'file-pdf', tone: 'bg-red-500/10 text-red-600 dark:text-red-400' },
  docx: { label: 'Word', short: 'DOCX', icon: 'file-docx', tone: 'bg-blue-500/10 text-blue-600 dark:text-blue-400' },
  pptx: { label: 'PowerPoint', short: 'PPTX', icon: 'file-pptx', tone: 'bg-orange-500/10 text-orange-600 dark:text-orange-400' },
  xlsx: { label: 'Excel', short: 'XLSX', icon: 'file-xlsx', tone: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-400' },
  md: { label: 'Markdown', short: 'MD', icon: 'file-md', tone: 'bg-zinc-500/10 text-zinc-700 dark:text-zinc-300' },
}
const infoOf = (f: FileFormat) => FORMAT_INFO[f] ?? { label: String(f).toUpperCase(), short: String(f).toUpperCase(), icon: 'file' as const, tone: 'bg-subtle text-muted-foreground' }

const SOURCE: Record<CreatedFile['source'], string> = {
  llm: 'written by the model', answer: 'from the answer', table: 'from an attached table', convert: 'converted',
}

export function fileSize(bytes: number) {
  if (!Number.isFinite(bytes) || bytes < 0) return '-'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10 * 1024 ? 1 : 0)} KB`
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

export const tokensText = (n: number) => (n > 0 ? `${n.toLocaleString()} token${n === 1 ? '' : 's'}` : 'no model tokens')
const plural = (n: number, w: string) => `${n.toLocaleString()} ${w}${n === 1 ? '' : 's'}`

const range = (r: [number, number]) => (r[0] === r[1] ? `${r[0]}` : `${r[0]}-${r[1]}`)

/** What the request asked for, beside what was made: "asked 12-13" (pages or slides). */
export function askedText(f: CreatedFile) {
  if (f.slides != null && f.brief?.slides) return `asked ${range(f.brief.slides)}`
  if (f.pages != null && f.brief?.pages) return `asked ${range(f.brief.pages)}`
  return null
}

// How a file met the request's brief (docs/PLAN-accuracy-v2.md C4-C7): one line per conformance rule.
const BRIEF_RULES: Record<string, string> = { V5: 'Length', V6: 'Images', V7: 'Font', V8: 'Look', V9: 'Diagrams' }
const THEME_LABEL: Record<ThemeName, string> = { clean: 'Clean', dark: 'Dark', warm: 'Warm', mono: 'Black and white' }
const PHASE_LABEL: Record<FilePhase['phase'], string> = { outline: 'Outline', sections: 'Sections', topup: 'Top-up', assets: 'Images', render: 'Render' }

/** "4 pages", "10 slides" or "2 sheets: Sales, Costs". */
export function shapeText(f: CreatedFile) {
  if (f.pages != null) return plural(f.pages, 'page')
  if (f.slides != null) return plural(f.slides, 'slide')
  if (f.sheets?.length) return `${plural(f.sheets.length, 'sheet')}: ${f.sheets.join(', ')}`
  return null
}

/** Every created file on a run's tasks, in step order, without duplicates. */
export function filesOfTasks(tasks: Array<{ answered?: { created_files?: CreatedFile[] } | undefined; created_files?: CreatedFile[] }>): CreatedFile[] {
  const out: CreatedFile[] = []
  for (const t of tasks) for (const f of t.answered?.created_files ?? t.created_files ?? []) if (!out.some(x => x.id === f.id)) out.push(f)
  return out
}

/** The primary file of a run: the merger's pick, else the last file marked primary. Null when neither is known (older
 *  runs), so callers fall back to one flat list. */
export function primaryOf(files: CreatedFile[], primary?: string | null): CreatedFile | null {
  if (primary) { const hit = files.find(f => f.id === primary); if (hit) return hit }
  if (!files.some(f => f.role)) return null
  return [...files].reverse().find(f => f.role === 'primary') ?? null
}

/** A run's files: the primary file first, then the files made along the way folded under "Working files". */
export function RunFiles({ files, primary, className }: { files: CreatedFile[]; primary?: string | null; className?: string }) {
  const main = primaryOf(files, primary)
  if (!main || files.length < 2) return <CreatedFiles files={files} className={className} />
  const rest = files.filter(f => f.id !== main.id)
  return (
    <div className={cn('flex min-w-0 flex-col gap-2', className)}>
      <CreatedFiles files={[main]} label="Created file" />
      <Collapsible>
        <CollapsibleTrigger className="group/work -mx-1 inline-flex cursor-pointer items-center gap-1.5 rounded-sm px-1 py-0.5 text-[13px] text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35">
          <Icon name="files" size={14} />Working files <span className="tabular-nums">({rest.length})</span>
          <Icon name="chevron-down" size={14} className="transition-transform group-data-[state=open]/work:rotate-180" />
        </CollapsibleTrigger>
        <CollapsibleContent>
          <CreatedFiles files={rest} label="Working files" className="mt-2" />
        </CollapsibleContent>
      </Collapsible>
    </div>
  )
}

/** A run's files as a list of cards. Converted files join the list under the file they came from. */
export function CreatedFiles({ files, className, label = 'Created files' }: { files: CreatedFile[]; className?: string; label?: string }) {
  const [extra, setExtra] = useState<CreatedFile[]>([])
  const shown = [...files, ...extra.filter(x => !files.some(f => f.id === x.id))]
  if (!shown.length) return null
  // Place each converted file right after its source, so a chain of conversions reads top to bottom.
  const ordered: CreatedFile[] = []
  const place = (f: CreatedFile) => {
    if (ordered.includes(f)) return
    ordered.push(f)
    for (const c of shown) if (c.from_id === f.id && extra.includes(c)) place(c)
  }
  for (const f of shown) if (!extra.includes(f) || !shown.some(s => s.id === f.from_id)) place(f)
  return (
    <ul className={cn('m-0 flex list-none flex-col gap-2 p-0', className)} aria-label={label}>
      {ordered.map(f => (
        <li key={f.id}><CreatedFileCard file={f} onConverted={n => setExtra(xs => [...xs, n])} /></li>
      ))}
    </ul>
  )
}

export function CreatedFileCard({ file: f, onConverted, onDelete, origin, now, className }: {
  file: CreatedFile
  onConverted?: (f: CreatedFile) => void
  onDelete?: () => void
  origin?: boolean // show when it was made and the run it came from (Files page)
  now?: number
  className?: string
}) {
  const toast = useToast()
  const info = infoOf(f.format)
  const [open, setOpen] = useState(false)
  const [converting, setConverting] = useState<FileFormat | null>(null)
  const shape = shapeText(f)
  const asked = askedText(f)
  // Brief conformance gets its own row, so its failures are not listed twice among the warnings.
  const brief = f.brief ? f.rules?.filter(r => r.id in BRIEF_RULES) ?? [] : []
  const warns = f.rules?.filter(r => r.severity === 'warn' && !r.ok && !brief.includes(r)) ?? []
  const fixes = f.rules?.filter(r => r.severity === 'fix' && !r.ok) ?? []
  const previewId = `cf-preview-${f.id}`

  const convert = async (to: FileFormat) => {
    setConverting(to)
    try {
      const n = await convertCreated(f.id, to)
      onConverted?.(n)
      toast.success(`Converted to ${n.name}`)
    } catch (e) {
      toast.error(`Could not convert to ${infoOf(to).label}: ${errorText(e)}`)
    } finally { setConverting(null) }
  }

  return (
    <article className={cn('min-w-0 rounded-lg border border-border bg-surface', className)} aria-label={f.name}>
      <div className="flex min-w-0 flex-wrap items-start gap-x-3 gap-y-2.5 p-3 sm:flex-nowrap sm:items-center">
        <span className={cn('grid size-10 shrink-0 place-items-center rounded-md', info.tone)} aria-hidden="true">
          <Icon name={info.icon} size={20} strokeWidth={1.8} />
        </span>
        <div className="flex min-w-0 flex-1 basis-[calc(100%-52px)] flex-col gap-0.5 sm:basis-auto">
          <a href={createdUrl(f, 'download')} download={f.name} title={`Download ${f.name}`}
            className="min-w-0 truncate rounded-sm text-sm font-medium text-foreground outline-none hover:underline hover:underline-offset-2 focus-visible:ring-[3px] focus-visible:ring-ring/35">
            {f.name}
          </a>
          <p className="m-0 flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground">
            <span className="font-medium">{info.short}</span>
            <Dot /><span className="tabular-nums">{fileSize(f.size)}</span>
            {shape && <><Dot /><span className="min-w-0 max-w-full truncate tabular-nums" title={shape}>{shape}</span></>}
            {asked && <><Dot /><span className="tabular-nums" title="What the request asked for">{asked}</span></>}
            <Dot /><span className="tabular-nums" title="LLM tokens the content cost. Conversions and files made from an answer or a table cost none.">{tokensText(f.tokens)}</span>
            <Dot /><span>{SOURCE[f.source] ?? f.source}</span>
            {f.sandbox && <><Dot /><span className="inline-flex items-center gap-1"><Icon name="sandbox" size={11} />sandbox only</span></>}
          </p>
          {origin && (
            <p className="m-0 flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
              <span className="tabular-nums" title={new Date(f.created * 1000).toLocaleString()}>{timeAgo(f.created, now)}</span>
              {f.qid != null && <><Dot /><a href={`#/runs/${f.qid}`} className="rounded-sm font-medium text-primary outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/35">Run #{f.qid}</a></>}
            </p>
          )}
        </div>
        <div className="flex min-w-0 basis-full flex-wrap items-center gap-1.5 pl-[52px] sm:basis-auto sm:shrink-0 sm:pl-0">
          <a href={createdUrl(f, 'download')} download={f.name} className={buttonClass('secondary', 'sm')} aria-label={`Download ${f.name}`}>
            <Icon name="download" size={14} strokeWidth={2} /><span>Download</span>
          </a>
          <Button variant="ghost" size="sm" icon="eye" aria-expanded={open} aria-controls={previewId} onClick={() => setOpen(o => !o)}>
            {open ? 'Hide preview' : 'Preview'}
          </Button>
          {!f.sandbox && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" icon="convert" loading={converting != null} iconRight="chevron-down" aria-label={`Convert ${f.name} to another format`}>
                  {converting ? 'Converting…' : 'Convert'}
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" className="w-56">
                <DropdownMenuLabel className="text-xs font-medium text-muted-foreground">Convert to… (no model tokens)</DropdownMenuLabel>
                {FORMATS.filter(x => x !== f.format).map(x => (
                  <DropdownMenuItem key={x} onSelect={() => void convert(x)}>
                    <Icon name={FORMAT_INFO[x].icon} size={14} />
                    <span>{FORMAT_INFO[x].label}</span>
                    <span className="ml-auto font-mono text-[11px] text-muted-foreground">.{x}</span>
                  </DropdownMenuItem>
                ))}
              </DropdownMenuContent>
            </DropdownMenu>
          )}
          {onDelete && <IconButton icon="trash" label={`Delete ${f.name}`} size="sm" onClick={onDelete} />}
        </div>
      </div>

      <FileChips file={f} />
      {brief.length > 0 && <BriefRow rules={brief} />}
      {(warns.length > 0 || fixes.length > 0) && <RuleNotes warns={warns} fixes={fixes} />}
      {(!!f.credits?.length || !!f.phases?.length) && (
        <div className="flex flex-col gap-1.5 border-t border-border px-3 py-2">
          {!!f.credits?.length && <Credits credits={f.credits} />}
          {!!f.phases?.length && <Phases phases={f.phases} />}
        </div>
      )}

      {open && (
        <div id={previewId} className="border-t border-border p-3">
          <Preview file={f} />
        </div>
      )}
    </article>
  )
}

const Dot = () => <span aria-hidden="true" className="text-muted-foreground/60">·</span>

const CHIP = 'inline-flex h-6 min-w-0 max-w-full items-center gap-1 rounded-md border border-border bg-subtle/60 px-1.5 text-xs text-muted-foreground'

/** Theme, font used, diagrams and images, when the server reports them. */
function FileChips({ file: f }: { file: CreatedFile }) {
  const theme = f.theme ?? f.brief?.theme ?? null
  const asked = f.brief?.font
  const fontTitle = f.font_used
    ? asked && asked.toLowerCase() !== f.font_used.toLowerCase() ? `You asked for ${asked}; the file uses ${f.font_used}` : `The file uses ${f.font_used}`
    : undefined
  if (!theme && !f.font_used && f.diagrams == null && f.images == null) return null
  return (
    <ul className="m-0 flex list-none flex-wrap gap-1.5 border-t border-border px-3 py-2" aria-label="How the file looks">
      {theme && <li className={CHIP} title="Colour theme"><Icon name="palette" size={12} className="shrink-0" />{THEME_LABEL[theme] ?? theme}</li>}
      {f.font_used && <li className={CHIP} title={fontTitle}><Icon name="font" size={12} className="shrink-0" /><span className="truncate">{f.font_used}</span></li>}
      {f.diagrams != null && <li className={CHIP}><Icon name="graph" size={12} className="shrink-0" /><span className="tabular-nums">{plural(f.diagrams, 'diagram')}</span></li>}
      {f.images != null && <li className={CHIP}><Icon name="images" size={12} className="shrink-0" /><span className="tabular-nums">{plural(f.images, 'image')}</span></li>}
    </ul>
  )
}

/** "Brief": did the file meet what was asked (length, images, font, look, diagrams)? Failures say why. */
function BriefRow({ rules }: { rules: RuleResult[] }) {
  return (
    <div className="flex min-w-0 flex-wrap items-start gap-x-3 gap-y-1 border-t border-border px-3 py-2 text-xs">
      <span className="font-medium text-muted-foreground">Brief</span>
      <ul className="m-0 flex min-w-0 flex-1 list-none flex-col gap-1 p-0" aria-label="How the file met the request">
        {rules.map(r => (
          <li key={r.id} className={cn('flex min-w-0 items-start gap-1.5', r.ok ? 'text-foreground' : 'text-warn')}>
            <Icon name={r.ok ? 'check' : 'close'} size={13} strokeWidth={2.2} className={cn('mt-px shrink-0', r.ok ? 'text-ok' : 'text-warn')} />
            <span className="sr-only">{r.ok ? 'met' : 'not met'}: </span>
            <span className="min-w-0 [overflow-wrap:anywhere]"><RuleId id={r.id} />{BRIEF_RULES[r.id]}{!r.ok && r.note ? `: ${r.note}` : ''}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

const DISCLOSURE = 'group/d -mx-1 inline-flex cursor-pointer items-center gap-1 self-start rounded-sm px-1 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35'
const LINKED = 'rounded-sm text-foreground underline decoration-border underline-offset-2 outline-none hover:decoration-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35'

function Credits({ credits }: { credits: ImageCredit[] }) {
  return (
    <Collapsible className="flex flex-col">
      <CollapsibleTrigger className={DISCLOSURE}>
        <Icon name="images" size={12} />Image credits <span className="tabular-nums">({credits.length})</span>
        <Icon name="chevron-down" size={12} className="transition-transform group-data-[state=open]/d:rotate-180" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ol className="m-0 mt-1.5 flex list-decimal flex-col gap-1 pl-5 text-xs text-muted-foreground marker:tabular-nums">
          {credits.map(c => (
            <li key={c.asset} className="[overflow-wrap:anywhere]">
              <a href={safeHref(c.source_url)} target="_blank" rel="noreferrer noopener" className={LINKED}>{c.title || 'Image'}</a>
              {c.author && <> by {c.author}</>}{', '}
              {c.license_url ? <a href={safeHref(c.license_url)} target="_blank" rel="noreferrer noopener" className={LINKED}>{c.license}</a> : c.license}
              {c.caption && <span className="block">{c.caption}</span>}
            </li>
          ))}
        </ol>
      </CollapsibleContent>
    </Collapsible>
  )
}

/** Where the file's tokens went: outline, sections, top-up, images and render. */
function Phases({ phases }: { phases: FilePhase[] }) {
  const total = phases.reduce((n, p) => n + p.llm_in + p.llm_out, 0)
  return (
    <Collapsible className="flex flex-col">
      <CollapsibleTrigger className={DISCLOSURE}>
        <Icon name="spend" size={12} />Token use by phase <span className="tabular-nums">({tokensText(total)})</span>
        <Icon name="chevron-down" size={12} className="transition-transform group-data-[state=open]/d:rotate-180" />
      </CollapsibleTrigger>
      <CollapsibleContent>
        <div className="mt-1.5 overflow-x-auto">
          <table className="w-full max-w-md border-collapse text-xs tabular-nums">
            <thead>
              <tr className="text-muted-foreground">
                <th scope="col" className="py-1 pr-3 text-left font-medium">Phase</th>
                <th scope="col" className="py-1 pr-3 text-right font-medium">Calls</th>
                <th scope="col" className="py-1 pr-3 text-right font-medium">In</th>
                <th scope="col" className="py-1 pr-3 text-right font-medium">Out</th>
                <th scope="col" className="py-1 text-right font-medium">Time</th>
              </tr>
            </thead>
            <tbody>
              {phases.map((p, i) => (
                <tr key={p.phase + i} className="border-t border-border text-foreground">
                  <td className="py-1 pr-3">{PHASE_LABEL[p.phase] ?? p.phase}</td>
                  <td className="py-1 pr-3 text-right">{p.calls}</td>
                  <td className="py-1 pr-3 text-right">{p.llm_in.toLocaleString()}</td>
                  <td className="py-1 pr-3 text-right">{p.llm_out.toLocaleString()}</td>
                  <td className="py-1 text-right">{p.ms >= 1000 ? `${(p.ms / 1000).toFixed(1)} s` : `${Math.round(p.ms)} ms`}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </CollapsibleContent>
    </Collapsible>
  )
}

function RuleNotes({ warns, fixes }: { warns: RuleResult[]; fixes: RuleResult[] }) {
  return (
    <div className="flex flex-col gap-1.5 border-t border-border px-3 py-2">
      {warns.length > 0 && (
        <ul className="m-0 flex list-none flex-col gap-1 p-0" aria-label="Rule warnings">
          {warns.map((r, i) => (
            <li key={r.id + i} className="flex min-w-0 items-start gap-1.5 text-xs text-warn">
              <Icon name="warning" size={13} className="mt-px shrink-0" />
              <span className="min-w-0 [overflow-wrap:anywhere]"><RuleId id={r.id} />{r.note || 'Check failed'}</span>
            </li>
          ))}
        </ul>
      )}
      {fixes.length > 0 && (
        <Collapsible>
          <CollapsibleTrigger className="group/fixes -mx-1 inline-flex cursor-pointer items-center gap-1 rounded-sm px-1 text-xs text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/35">
            <Icon name="check" size={12} />{fixes.length === 1 ? '1 automatic fix' : `${fixes.length} automatic fixes`}
            <Icon name="chevron-down" size={12} className="transition-transform group-data-[state=open]/fixes:rotate-180" />
          </CollapsibleTrigger>
          <CollapsibleContent>
            <ul className="m-0 mt-1.5 flex list-none flex-col gap-1 p-0">
              {fixes.map((r, i) => (
                <li key={r.id + i} className="text-xs text-muted-foreground [overflow-wrap:anywhere]"><RuleId id={r.id} />{r.note || 'Corrected'}</li>
              ))}
            </ul>
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  )
}

const RuleId = ({ id }: { id: string }) => (
  <a href="#/files?rules=1" title={`Rule ${id}: see the ruleset`} className="mr-1.5 rounded-sm font-mono text-[11px] font-medium text-muted-foreground outline-none hover:text-foreground hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/35">{id}</a>
)

// Fetched on first open and kept while the card lives.
function Preview({ file }: { file: CreatedFile }) {
  const [state, setState] = useState<{ data?: FilePreview; error?: string } | null>(null)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    let alive = true
    setState(null)
    previewCreated(file).then(d => alive && setState({ data: d }), e => alive && setState({ error: errorText(e) }))
    return () => { alive = false }
  }, [file.id, file.sandbox, nonce]) // eslint-disable-line react-hooks/exhaustive-deps

  if (!state) return <div aria-busy="true" aria-label="Loading the preview"><Skeleton lines={4} /></div>
  if (state.error) {
    return (
      <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted-foreground" role="alert">
        <Icon name="alert" size={14} className="text-destructive" />Could not load the preview: {state.error}
        <Button variant="ghost" size="sm" icon="refresh" onClick={() => setNonce(n => n + 1)}>Retry</Button>
      </div>
    )
  }
  const p = state.data!
  if (p.kind === 'markdown') {
    return p.text.trim()
      ? <div className="max-h-80 overflow-auto text-[14px] leading-relaxed text-foreground [overflow-wrap:anywhere]"><Markdown text={p.text} /></div>
      : <p className="m-0 text-[13px] text-muted-foreground">The file is empty.</p>
  }
  if (p.kind === 'outline') {
    const count = p.slides != null ? plural(p.slides, 'slide') : p.pages != null ? plural(p.pages, 'page') : null
    return (
      <div className="flex flex-col gap-2">
        {count && <p className="m-0 text-xs font-medium text-muted-foreground">{count}</p>}
        {p.items.length ? (
          <ol className="m-0 flex max-h-80 list-none flex-col gap-1 overflow-auto p-0">
            {p.items.map((it, i) => (
              <li key={i} className={cn('min-w-0 text-[13px] [overflow-wrap:anywhere]', it.level <= 1 ? 'font-medium text-foreground' : 'text-muted-foreground')}
                style={{ paddingLeft: `${Math.max(0, Math.min(it.level, 4) - 1) * 16}px` }}>
                {it.text}
              </li>
            ))}
          </ol>
        ) : <p className="m-0 text-[13px] text-muted-foreground">No headings found.</p>}
      </div>
    )
  }
  if (!p.sheets.length) return <p className="m-0 text-[13px] text-muted-foreground">The workbook has no sheets.</p>
  return (
    <div className="flex max-h-[28rem] flex-col gap-4 overflow-auto">
      {p.sheets.map(s => {
        const more = Math.max(0, s.total_rows - s.rows.length)
        return (
          <section key={s.name} className="flex min-w-0 flex-col gap-1.5" aria-label={`Sheet ${s.name}`}>
            <h4 className="m-0 flex items-center gap-1.5 text-xs font-medium text-foreground"><Icon name="table" size={13} className="text-muted-foreground" />{s.name}</h4>
            <div className="min-w-0 overflow-x-auto rounded-md border border-border">
              <table className="w-full border-collapse text-xs">
                <thead className="bg-subtle">
                  <tr>{s.columns.map((c, i) => <th key={i} scope="col" className={cn('whitespace-nowrap border-b border-border px-2 py-1.5 text-left font-medium text-foreground', typeof s.rows[0]?.[i] === 'number' && 'text-right')}>{c}</th>)}</tr>
                </thead>
                <tbody>
                  {s.rows.map((r, i) => (
                    <tr key={i} className="border-b border-border last:border-0">
                      {s.columns.map((_, j) => {
                        const v = r[j]
                        return <td key={j} className={cn('max-w-[16rem] truncate px-2 py-1 text-foreground', typeof v === 'number' && 'text-right tabular-nums')} title={v == null ? '' : String(v)}>{v == null ? '' : typeof v === 'number' ? v.toLocaleString() : v}</td>
                      })}
                    </tr>
                  ))}
                  {!s.rows.length && <tr><td colSpan={Math.max(1, s.columns.length)} className="px-2 py-1.5 text-muted-foreground">No rows</td></tr>}
                </tbody>
              </table>
            </div>
            {more > 0 && <p className="m-0 text-xs tabular-nums text-muted-foreground">{plural(more, 'more row')}</p>}
          </section>
        )
      })}
    </div>
  )
}

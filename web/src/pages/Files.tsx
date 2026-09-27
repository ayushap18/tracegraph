import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { deleteCreated, errorText, listCreated, listRules } from '../api'
import type { CreatedFile, FileFormat, RuleInfo, RuleSeverity } from '../protocol'
import { Badge, Button, EmptyState, IconButton, Skeleton, navigate, useHashPath, useNow, useToast } from '../ui'
import { Icon } from '../icons'
import { PageBody } from '../components/app'
import { TopActions } from '../components/Shell'
import { CreatedFileCard, FORMATS, FORMAT_INFO } from '../components/chat/CreatedFiles'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { cn } from '@/lib/utils'

// Every created file (docs/PLAN-files.md), newest first: filter by format, search by name, download, preview, convert
// and delete. Filters apply to the files loaded so far; "Load more" fetches older ones. The "Rules" drawer lists the
// ruleset every file is checked against (docs/RULES-files.md); #/files?rules=1 opens it.

const PAGE = 30
const SEVERITY_TONE: Record<RuleSeverity, string> = { block: 'bad', fix: 'info', warn: 'warn' }

export default function Files() {
  const toast = useToast()
  const now = useNow(30000)
  const { query } = useHashPath()
  const [files, setFiles] = useState<CreatedFile[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [more, setMore] = useState(false) // the last page was full, so older files may exist
  const [loadingMore, setLoadingMore] = useState(false)
  const [nonce, setNonce] = useState(0)
  const [format, setFormat] = useState<FileFormat | ''>('')
  const [q, setQ] = useState('')
  const [confirm, setConfirm] = useState<CreatedFile | null>(null)
  const [rulesOpen, setRulesOpen] = useState(query.get('rules') === '1')
  const rulesParam = query.get('rules') === '1'
  useEffect(() => { if (rulesParam) setRulesOpen(true) }, [rulesParam])

  useEffect(() => {
    let alive = true
    setLoading(true); setError(null)
    listCreated(PAGE).then(
      r => { if (alive) { setFiles(r.files); setMore(r.files.length >= PAGE) } },
      e => { if (alive) { setFiles([]); setMore(false); setError(errorText(e)) } },
    ).finally(() => { if (alive) setLoading(false) })
    return () => { alive = false }
  }, [nonce])

  const loadMore = useCallback(async () => {
    const last = files[files.length - 1]
    if (!last || loadingMore) return
    setLoadingMore(true)
    try {
      const r = await listCreated(PAGE, last.created)
      setFiles(xs => [...xs, ...r.files.filter(f => !xs.some(x => x.id === f.id))])
      setMore(r.files.length >= PAGE)
    } catch (e) {
      toast.error(`Could not load more files: ${errorText(e)}`)
    } finally { setLoadingMore(false) }
  }, [files, loadingMore, toast])

  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return files.filter(f => (!format || f.format === format) && (!needle || f.name.toLowerCase().includes(needle) || f.title?.toLowerCase().includes(needle)))
  }, [files, format, q])
  const counts = useMemo(() => {
    const c: Partial<Record<FileFormat, number>> = {}
    for (const f of files) c[f.format] = (c[f.format] ?? 0) + 1
    return c
  }, [files])

  // A converted file joins the top of the list (it is the newest).
  const onConverted = (n: CreatedFile) => setFiles(xs => [n, ...xs.filter(x => x.id !== n.id)])
  const filtered = !!format || !!q.trim()

  return (
    <PageBody width="narrow">
      <TopActions>
        <Button variant="secondary" size="sm" icon="rules" onClick={() => setRulesOpen(true)}>Rules</Button>
      </TopActions>

      <div className="flex flex-col gap-3">
        <label className="relative block">
          <span className="sr-only">Search files by name</span>
          <Icon name="search" size={15} className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-muted-foreground" />
          <input type="search" value={q} onChange={e => setQ(e.target.value)} placeholder="Search by name"
            className="h-9 w-full rounded-md border border-border bg-surface pr-3 pl-9 text-sm text-foreground shadow-xs outline-none placeholder:text-muted-foreground focus-visible:border-edge focus-visible:ring-[3px] focus-visible:ring-ring/35" />
        </label>
        <div className="flex min-w-0 flex-wrap items-center gap-1.5" role="group" aria-label="Filter by format">
          <Chip active={!format} onClick={() => setFormat('')}>All</Chip>
          {FORMATS.map(f => (
            <Chip key={f} active={format === f} onClick={() => setFormat(format === f ? '' : f)}>
              <Icon name={FORMAT_INFO[f].icon} size={13} />{FORMAT_INFO[f].label}
              {counts[f] ? <span className="tabular-nums opacity-70">{counts[f]}{more ? '+' : ''}</span> : null}
            </Chip>
          ))}
        </div>
      </div>

      <section className="flex min-w-0 flex-col gap-2" aria-label="Created files" aria-busy={loading || undefined}>
        {loading ? <FilesSkeleton />
          : error ? (
            <div className="rounded-lg border border-border bg-surface">
              <EmptyState icon="alert" title="Could not load your files" text={error}
                action={<Button variant="secondary" icon="refresh" onClick={() => setNonce(n => n + 1)}>Retry</Button>} />
            </div>
          ) : !files.length ? (
            <div className="rounded-lg border border-border bg-surface">
              <EmptyState icon="files" title="No files yet"
                text='Ask in Chat for a file, like "make a PDF report of this" or "turn this table into an Excel sheet". Every file you create shows up here.'
                action={<Button variant="secondary" icon="chat" onClick={() => navigate('/')}>Go to Chat</Button>} />
            </div>
          ) : !shown.length ? (
            <div className="rounded-lg border border-border bg-surface">
              <EmptyState icon="filter" title="No files match"
                text={more ? 'Nothing matches in the files loaded so far. Older files may match.' : 'Try another name or format.'}
                action={<>
                  <Button variant="secondary" onClick={() => { setFormat(''); setQ('') }}>Clear filters</Button>
                  {more && <Button variant="ghost" loading={loadingMore} onClick={() => void loadMore()}>Load older files</Button>}
                </>} />
            </div>
          ) : (
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {shown.map(f => (
                <li key={f.id}>
                  <CreatedFileCard file={f} origin now={now} onConverted={onConverted} onDelete={() => setConfirm(f)} />
                </li>
              ))}
            </ul>
          )}
        {!loading && !error && more && shown.length > 0 && (
          <div className="flex justify-center pt-2">
            <Button variant="secondary" loading={loadingMore} onClick={() => void loadMore()}>Load more</Button>
          </div>
        )}
        {!loading && !error && files.length > 0 && (
          <p className="m-0 pt-1 text-center text-xs tabular-nums text-muted-foreground">
            {filtered ? `${shown.length} of ${files.length}${more ? '+' : ''} files` : `${files.length}${more ? '+' : ''} files`}
          </p>
        )}
      </section>

      <DeleteDialog file={confirm} onOpenChange={v => { if (!v) setConfirm(null) }}
        onDeleted={id => { setFiles(xs => xs.filter(x => x.id !== id)); setConfirm(null) }} />
      <RulesDrawer open={rulesOpen} onOpenChange={v => {
        setRulesOpen(v)
        if (!v && query.get('rules')) location.replace('#/files')
      }} />
    </PageBody>
  )
}

function Chip({ active, onClick, children }: { active: boolean; onClick: () => void; children: ReactNode }) {
  return (
    <button type="button" data-slot="button" aria-pressed={active} onClick={onClick}
      className={cn('inline-flex h-7 items-center gap-1.5 rounded-md border px-2.5 text-[13px] font-medium outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/35',
        active ? 'border-primary/40 bg-primary/10 text-primary' : 'border-border bg-surface text-muted-foreground hover:bg-subtle hover:text-foreground')}>
      {children}
    </button>
  )
}

function DeleteDialog({ file, onOpenChange, onDeleted }: { file: CreatedFile | null; onOpenChange: (v: boolean) => void; onDeleted: (id: string) => void }) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Keep the last file while the dialog animates closed.
  const [shown, setShown] = useState<CreatedFile | null>(file)
  useEffect(() => { if (file) { setShown(file); setError(null) } }, [file])

  const remove = async () => {
    if (!file || busy) return
    setBusy(true); setError(null)
    try {
      await deleteCreated(file.id)
      toast.success(`Deleted ${file.name}`)
      onDeleted(file.id)
    } catch (e) { setError(errorText(e)) } finally { setBusy(false) }
  }

  return (
    <Dialog open={!!file} onOpenChange={v => { if (!busy) onOpenChange(v) }}>
      <DialogContent showCloseButton={false} className="gap-5 rounded-xl border-border bg-surface p-5 sm:max-w-md">
        <DialogHeader className="gap-1.5 text-left">
          <DialogTitle className="text-[15px] font-semibold leading-snug">Delete this file?</DialogTitle>
          <DialogDescription className="text-[13px] leading-relaxed text-pretty">
            <span className="font-medium break-all text-foreground">{shown?.name}</span> will be removed for good. The chat and the run it came from stay as they are, and files converted from it are kept.
          </DialogDescription>
        </DialogHeader>
        {error && (
          <p className="m-0 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-[13px] text-destructive" role="alert">
            Could not delete: {error}
          </p>
        )}
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>Cancel</Button>
          <Button variant="danger" icon="trash" loading={busy} onClick={() => void remove()}>{busy ? 'Deleting…' : 'Delete'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

// Loaded on first open and kept for the visit.
function RulesDrawer({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const [rules, setRules] = useState<RuleInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  useEffect(() => {
    if (!open || rules) return
    let alive = true
    setError(null)
    listRules().then(r => alive && setRules(r.rules), e => alive && setError(errorText(e)))
    return () => { alive = false }
  }, [open, rules, nonce])

  const groups = useMemo(() => {
    const out: Array<{ group: string; rules: RuleInfo[] }> = []
    for (const r of rules ?? []) {
      const g = out.find(x => x.group === r.group)
      if (g) g.rules.push(r); else out.push({ group: r.group, rules: [r] })
    }
    return out
  }, [rules])

  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      <SheetContent side="right" showCloseButton={false} className="w-full gap-0 border-border sm:max-w-lg">
        <SheetHeader className="flex-row items-start justify-between gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0">
            <SheetTitle className="text-[15px]">Rules for created files</SheetTitle>
            <SheetDescription className="mt-1 text-[13px]">Every file is checked against these before you can download it. A block stops the file, a fix is corrected for you, a warning shows on the file.</SheetDescription>
          </div>
          <IconButton icon="close" label="Close" size="sm" onClick={() => onOpenChange(false)} />
        </SheetHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {error ? (
            <EmptyState compact icon="alert" title="Could not load the rules" text={error}
              action={<Button variant="secondary" size="sm" icon="refresh" onClick={() => { setError(null); setNonce(n => n + 1) }}>Retry</Button>} />
          ) : !rules ? (
            <div className="flex flex-col gap-4" aria-busy="true" aria-label="Loading the rules">
              {Array.from({ length: 3 }, (_, i) => <div key={i} className="flex flex-col gap-2"><Skeleton width="40%" height={14} /><Skeleton lines={3} /></div>)}
            </div>
          ) : !rules.length ? (
            <EmptyState compact icon="rules" title="No rules reported" text="The server did not list any rules." />
          ) : (
            <div className="flex flex-col gap-6">
              {groups.map(g => (
                <section key={g.group} className="flex flex-col gap-2" aria-label={g.group}>
                  <h3 className="m-0 text-xs font-medium tracking-wide text-muted-foreground uppercase">{g.group}</h3>
                  <ul className="m-0 flex list-none flex-col p-0">
                    {g.rules.map(r => (
                      <li key={r.id} className="flex min-w-0 gap-3 border-b border-border py-2.5 last:border-0">
                        <span className="w-7 shrink-0 pt-px font-mono text-xs font-medium text-foreground">{r.id}</span>
                        <div className="flex min-w-0 flex-1 flex-col gap-1.5">
                          <p className="m-0 text-[13px] leading-relaxed text-foreground [overflow-wrap:anywhere]">{r.text}</p>
                          <div className="flex flex-wrap items-center gap-1.5">
                            {r.severity && <Badge tone={SEVERITY_TONE[r.severity] ?? 'neutral'}>{r.severity}</Badge>}
                            {!r.enforced && <Badge tone="neutral">not enforced yet</Badge>}
                          </div>
                        </div>
                      </li>
                    ))}
                  </ul>
                </section>
              ))}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  )
}

function FilesSkeleton() {
  return (
    <div className="flex flex-col gap-2" aria-label="Loading files">
      {Array.from({ length: 5 }, (_, i) => (
        <div key={i} className="flex items-center gap-3 rounded-lg border border-border bg-surface p-3" aria-hidden="true">
          <Skeleton width={40} height={40} radius={6} />
          <div className="flex min-w-0 flex-1 flex-col gap-2">
            <Skeleton width={`${40 + ((i * 13) % 35)}%`} height={14} />
            <Skeleton width="55%" height={12} />
          </div>
        </div>
      ))}
    </div>
  )
}

import { useCallback, useLayoutEffect, useRef, useState, useSyncExternalStore, type DragEvent, type ReactNode } from 'react'
import { deleteSandboxFile, errorText, uploadSandboxFile } from '../../api'
import { IconButton, Spinner, useToast } from '../../ui'
import { Icon } from '../../icons'
import { cn } from '@/lib/utils'
import type { AttachmentsProps, FileInfo } from './types'

// Temporary sandbox attachments (docs/PLAN-sandbox.md, feature 10). Files are uploaded into the sandbox's
// server memory only. `files` (props) are the ready ones; uploads in flight and failed uploads are kept in a
// small module store keyed by sandbox id, so AttachButton, useSandboxDrop and the Attachments chips row
// share them without the page holding extra state.

const ACCEPT = '.txt,.md,.csv,.json,.pdf'
const MAX_FILES = 5
const MAX_FILE = 10 * 1024 * 1024
export const TEMP_NOTE = 'Kept in memory for this sandbox only, never saved.'

const fmtSize = (n: number) => (n < 1024 ? `${n} B` : n < 1024 * 1024 ? `${(n / 1024).toFixed(0)} KB` : `${(n / 1024 / 1024).toFixed(1)} MB`)
const fileIcon = (name: string) => (name.endsWith('.csv') ? 'file-csv' : name.endsWith('.json') ? 'file-json' : 'file-text') as 'file-csv' | 'file-json' | 'file-text'

// ---------- shared pending store ----------

interface Pending { key: string; name: string; size: number; status: 'uploading' | 'error'; error?: string }

const pendingBy = new Map<string, Pending[]>()
/** Latest known ready list per sandbox, so uploads finishing together each append to the newest list. */
const latestBy = new Map<string, FileInfo[]>()
const listeners = new Set<() => void>()
const NONE: Pending[] = []

const emit = () => listeners.forEach(l => l())
const subscribe = (l: () => void) => { listeners.add(l); return () => { listeners.delete(l) } }
const getPending = (sid: string) => pendingBy.get(sid) ?? NONE
const setPending = (sid: string, fn: (p: Pending[]) => Pending[]) => {
  const next = fn(getPending(sid))
  if (next.length) pendingBy.set(sid, next); else pendingBy.delete(sid)
  emit()
}

/** Drop uploads in flight and failed chips for a sandbox (call when the sandbox is reset). */
export function clearPendingUploads(sandboxId: string) {
  pendingBy.delete(sandboxId); latestBy.delete(sandboxId); emit()
}

function usePending(sandboxId: string) {
  return useSyncExternalStore(subscribe, () => getPending(sandboxId))
}

/** Upload logic shared by the button and the drop target. */
function useUploader(sandboxId: string, files: FileInfo[], onChange: (files: FileInfo[]) => void) {
  const toast = useToast()
  const onChangeRef = useRef(onChange)
  useLayoutEffect(() => {
    onChangeRef.current = onChange
    latestBy.set(sandboxId, files)
  })

  return useCallback((list: FileList | File[]) => {
    const all = Array.from(list)
    if (!all.length) return
    const inFlight = getPending(sandboxId).filter(p => p.status === 'uploading').length
    let room = MAX_FILES - (latestBy.get(sandboxId) ?? files).length - inFlight
    let skipped = 0
    for (const f of all) {
      const ext = f.name.slice(f.name.lastIndexOf('.')).toLowerCase()
      if (!ACCEPT.split(',').includes(ext)) { toast.error(`${f.name}: only .txt, .md, .csv, .json and .pdf files are supported`); continue }
      if (f.size > MAX_FILE) { toast.error(`${f.name} is larger than 10 MB`); continue }
      if (room <= 0) { skipped++; continue }
      room--
      const key = `${f.name}:${f.size}:${Math.random().toString(36).slice(2, 7)}`
      setPending(sandboxId, p => [...p, { key, name: f.name, size: f.size, status: 'uploading' }])
      uploadSandboxFile(sandboxId, f).then(
        info => {
          setPending(sandboxId, p => p.filter(x => x.key !== key))
          const next = [...(latestBy.get(sandboxId) ?? []), info]
          latestBy.set(sandboxId, next)
          onChangeRef.current(next)
        },
        e => {
          setPending(sandboxId, p => p.map(x => (x.key === key ? { ...x, status: 'error', error: errorText(e) } : x)))
          toast.error(`Upload failed for ${f.name}: ${errorText(e)}`)
        },
      )
    }
    if (skipped) toast.error(`A sandbox holds up to ${MAX_FILES} files; ${skipped} ${skipped === 1 ? 'file was' : 'files were'} not attached.`)
  }, [sandboxId, files, toast])
}

// ---------- chips row ----------

export function Attachments({ sandboxId, files, onChange, disabled }: AttachmentsProps) {
  const toast = useToast()
  const pending = usePending(sandboxId)
  const [removing, setRemoving] = useState<Set<string>>(() => new Set())

  if (!files.length && !pending.length) return null

  const remove = async (f: FileInfo) => {
    setRemoving(s => new Set(s).add(f.id))
    try {
      await deleteSandboxFile(sandboxId, f.id)
    } catch (e) {
      // The sandbox may already have forgotten it (idle timeout); either way it is no longer attached.
      toast.error(`Could not remove ${f.name} from sandbox memory: ${errorText(e)}`)
    } finally {
      setRemoving(s => { const n = new Set(s); n.delete(f.id); return n })
      const next = (latestBy.get(sandboxId) ?? files).filter(x => x.id !== f.id)
      latestBy.set(sandboxId, next)
      onChange(next)
    }
  }

  return (
    <div className="flex flex-col gap-1.5 px-0.5 pt-0.5">
      <ul className="m-0 flex list-none flex-wrap gap-1.5 p-0" aria-label="Attached files (temporary)">
        {files.map(f => (
          <Chip key={f.id} name={f.name} title={`${f.name}: ${TEMP_NOTE}`}
            icon={<Icon name={fileIcon(f.name)} size={14} className="shrink-0 text-primary" />}
            meta={`${f.kind} · ${f.rows != null ? `${f.rows} rows` : fmtSize(f.size)}`}
            busy={removing.has(f.id)} disabled={disabled} onRemove={() => void remove(f)} />
        ))}
        {pending.map(p => (
          <Chip key={p.key} name={p.name} title={p.error ?? p.name} error={p.status === 'error'}
            icon={p.status === 'uploading' ? <Spinner size={13} className="shrink-0 text-muted-foreground" />
              : <Icon name="alert" size={14} className="shrink-0 text-destructive" />}
            meta={p.status === 'uploading' ? 'uploading' : 'failed'}
            disabled={p.status === 'uploading'} onRemove={() => setPending(sandboxId, list => list.filter(x => x.key !== p.key))} />
        ))}
      </ul>
      <p className="m-0 flex items-center gap-1 text-xs text-muted-foreground">
        <Icon name="clock" size={12} className="shrink-0" /> {TEMP_NOTE}
      </p>
    </div>
  )
}

function Chip({ name, title, icon, meta, error, busy, disabled, onRemove }: {
  name: string; title: string; icon: ReactNode; meta: string; error?: boolean; busy?: boolean; disabled?: boolean; onRemove: () => void
}) {
  return (
    <li title={title}
      className={cn('inline-flex h-7 min-w-0 max-w-full items-center gap-1.5 rounded-md border bg-subtle/60 pl-2 pr-0.5 text-[13px]',
        error ? 'border-destructive/30' : 'border-border')}>
      {icon}
      <span className="min-w-0 max-w-[200px] truncate font-medium">{name}</span>
      <span className={cn('whitespace-nowrap text-xs tabular-nums', error ? 'text-destructive' : 'text-muted-foreground')}>{meta}</span>
      <button type="button" data-slot="button" aria-label={`Remove ${name}`} onClick={onRemove} disabled={disabled || busy}
        className="grid size-6 shrink-0 place-items-center rounded-sm border-0 bg-transparent text-muted-foreground transition-colors hover:bg-subtle hover:text-foreground focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring/35 disabled:pointer-events-none disabled:opacity-50">
        {busy ? <Spinner size={12} /> : <Icon name="close" size={12} />}
      </button>
    </li>
  )
}

// ---------- attach button ----------

export function AttachButton({ sandboxId, files, onChange, disabled, className }: AttachmentsProps & { className?: string }) {
  const input = useRef<HTMLInputElement>(null)
  const upload = useUploader(sandboxId, files, onChange)
  const pending = usePending(sandboxId)
  const full = files.length + pending.filter(p => p.status === 'uploading').length >= MAX_FILES
  return (
    <>
      <input ref={input} type="file" accept={ACCEPT} multiple hidden tabIndex={-1} aria-hidden="true"
        onChange={e => { if (e.target.files) upload(e.target.files); e.target.value = '' }} />
      <IconButton icon="paperclip" label={full ? `Up to ${MAX_FILES} files per sandbox` : 'Attach a file (not saved)'}
        title={full ? `Up to ${MAX_FILES} files per sandbox` : 'Attach a file (not saved): .txt .md .csv .json .pdf, up to 10 MB, kept in memory for this sandbox only'}
        disabled={disabled || full} onClick={() => input.current?.click()} className={className} />
    </>
  )
}

// ---------- drop target ----------

/** Make any element a drop target for sandbox files: spread `bind` on it and show an overlay while `dragging`. */
export function useSandboxDrop(sandboxId: string, files: FileInfo[], onChange: (files: FileInfo[]) => void, disabled?: boolean) {
  const [dragging, setDragging] = useState(false)
  const upload = useUploader(sandboxId, files, onChange)
  const bind = {
    onDragOver: (e: DragEvent<HTMLElement>) => {
      if (disabled || !e.dataTransfer.types.includes('Files')) return
      e.preventDefault()
      e.dataTransfer.dropEffect = 'copy'
      if (!dragging) setDragging(true)
    },
    onDragLeave: (e: DragEvent<HTMLElement>) => {
      // Only when leaving the target itself, not when moving between its children.
      if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false)
    },
    onDrop: (e: DragEvent<HTMLElement>) => {
      if (disabled || !e.dataTransfer.files.length) { setDragging(false); return }
      e.preventDefault(); setDragging(false)
      upload(e.dataTransfer.files)
    },
  }
  return { dragging, bind }
}

export default Attachments

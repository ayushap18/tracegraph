import { useEffect, useState } from 'react'
import { errorText, keepSandbox } from '../../api'
import { Button, navigate, useToast } from '../../ui'
import { Icon } from '../../icons'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import type { KeepDialogProps } from './types'

// "Keep this" (docs/PLAN-sandbox.md, feature 8): copy the shown sandbox turns into a normal saved chat.

export function KeepDialog({ sandboxId, qids, open, onOpenChange }: KeepDialogProps) {
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => { if (open) setError(null) }, [open])

  const n = qids.length
  const save = async () => {
    if (!n || busy) return
    setBusy(true); setError(null)
    try {
      const res = await keepSandbox(sandboxId, qids)
      toast.success('Saved as a chat')
      onOpenChange(false)
      navigate(`/?s=${encodeURIComponent(res.session_id)}`)
    } catch (e) {
      setError(errorText(e))
    } finally { setBusy(false) }
  }

  return (
    <Dialog open={open} onOpenChange={v => { if (!busy) onOpenChange(v) }}>
      <DialogContent showCloseButton={false} className="gap-5 rounded-xl border-border bg-surface p-5 sm:max-w-md">
        <DialogHeader className="gap-1.5 text-left">
          <DialogTitle className="text-[15px] font-semibold leading-snug">Keep this conversation?</DialogTitle>
          <DialogDescription className="text-[13px] leading-relaxed text-pretty">
            {n
              ? <>This saves the <span className="font-medium tabular-nums text-foreground">{n}</span> {n === 1 ? 'turn' : 'turns'} as a normal chat you can find in Chat and Runs. For an edited question, the latest version is saved. Side-by-side answers are not included.</>
              : 'There are no finished turns to save yet. Ask something first.'}
          </DialogDescription>
        </DialogHeader>
        <ul className="m-0 flex list-none flex-col gap-2 p-0 text-[13px] text-muted-foreground">
          <li className="flex items-start gap-2"><Icon name="sandbox" size={14} className="mt-0.5 shrink-0" />The sandbox stays as it is; you can keep experimenting.</li>
          <li className="flex items-start gap-2"><Icon name="paperclip" size={14} className="mt-0.5 shrink-0" />Attached files are not saved; the chat keeps only the questions and answers.</li>
        </ul>
        {error && (
          <p className="m-0 rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-[13px] text-destructive" role="alert">
            Could not save: {error}
          </p>
        )}
        <DialogFooter className="gap-2">
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>Cancel</Button>
          <Button icon="download" loading={busy} disabled={!n} onClick={() => void save()}>{busy ? 'Saving…' : 'Save as chat'}</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default KeepDialog

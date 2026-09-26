import { useCallback, useEffect, useRef, useState } from 'react'
import { createLabel, errorText, listReview } from '../api'
import type { ReviewItem, ShakyReason } from '../protocol'
import { Badge, Button, EmptyState, Kbd, Skeleton, navigate, timeAgo, useNow, useToast } from '../ui'
import { Icon } from '../icons'
import { AgentBadge, PageBody } from '../components/app'
import { AgentPicker } from '../components/RouteFeedback'
import { TopActions } from '../components/Shell'
import { pct } from '../lib'
import { cn } from '@/lib/utils'

// The review queue: saved subtasks whose routing looked shaky, newest first. Mark each right or wrong inline;
// labelled items leave the list at once. Keyboard: j/k move, y right, n wrong (opens the picker), Enter opens the run.
// The server returns at most LIMIT items; when a full page runs low, the next batch is fetched and appended.

const LIMIT = 100
const LOW_WATER = 20
const REASONS: ShakyReason[] = ['low confidence', 'low margin', 'clarify', 'agent failed', 're-asked']
const REASON_TONE: Record<ShakyReason, string> = {
  'low confidence': 'warn', 'low margin': 'warn', clarify: 'neutral', 'agent failed': 'bad', 're-asked': 'info',
}
const keyOf = (it: ReviewItem) => `${it.qid}:${it.tid}`

export default function Review() {
  const toast = useToast()
  const now = useNow(30000)
  const [items, setItems] = useState<ReviewItem[]>([])
  const [scanned, setScanned] = useState(0)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [reason, setReason] = useState<ShakyReason | ''>('')
  const [sel, setSel] = useState(0)
  const [picking, setPicking] = useState<string | null>(null)
  const [nonce, setNonce] = useState(0)
  const [full, setFull] = useState(false) // the last page was LIMIT long, so the server may hold more
  const [refilling, setRefilling] = useState(false)
  const list = useRef<HTMLUListElement>(null)
  const current = useRef(items)
  current.current = items
  const gen = useRef(0) // bumped on every full load, so a refill that started before it is dropped
  const hidden = useRef(new Set<string>()) // labelled or being labelled: never shown again, even if a fetch races the save

  useEffect(() => {
    let alive = true
    const g = ++gen.current
    setLoading(true); setError(null)
    listReview(LIMIT, reason || undefined).then(
      r => {
        if (!alive) return
        setItems(r.items.filter(x => !hidden.current.has(keyOf(x)))); setScanned(r.scanned); setSel(0)
        setFull(r.items.length >= LIMIT)
      },
      e => { if (alive) { setItems([]); setFull(false); setError(errorText(e)) } },
    ).finally(() => { if (alive && g === gen.current) setLoading(false) })
    return () => { alive = false }
  }, [reason, nonce])

  // Refill: a full page that runs low fetches again (the server leaves labelled items out) and appends what's new.
  useEffect(() => {
    if (loading || error || !full || refilling || items.length > LOW_WATER) return
    const g = gen.current
    setRefilling(true)
    listReview(LIMIT, reason || undefined).then(
      r => {
        if (g !== gen.current) return
        const have = new Set(current.current.map(keyOf))
        const extra = r.items.filter(x => !hidden.current.has(keyOf(x)) && !have.has(keyOf(x)))
        if (extra.length) setItems(xs => [...xs, ...extra.filter(x => !xs.some(y => keyOf(y) === keyOf(x)))])
        setScanned(r.scanned)
        setFull(r.items.length >= LIMIT && extra.length > 0)
      },
      e => { if (g === gen.current) { setFull(false); toast.error(`Could not load more to review: ${errorText(e)}`) } },
    ).finally(() => setRefilling(false))
  }, [loading, error, full, refilling, items.length, reason, toast])

  // Optimistic: the item leaves at once and comes back where it was if the save fails.
  const mark = useCallback(async (it: ReviewItem, correct?: string) => {
    const k = keyOf(it)
    const at = Math.max(0, current.current.findIndex(x => keyOf(x) === k))
    hidden.current.add(k)
    setItems(xs => xs.filter(x => keyOf(x) !== k))
    setPicking(null)
    try {
      await createLabel(correct ? { qid: it.qid, tid: it.tid, verdict: 'wrong', correct } : { qid: it.qid, tid: it.tid, verdict: 'right' })
    } catch (e) {
      hidden.current.delete(k)
      setItems(xs => xs.some(x => keyOf(x) === k) ? xs : [...xs.slice(0, at), it, ...xs.slice(at)])
      toast.error(`Could not save the label: ${errorText(e)}`)
    }
  }, [toast])

  // Keep the selection on a real row as items leave.
  useEffect(() => { setSel(s => Math.max(0, Math.min(s, items.length - 1))) }, [items.length])
  useEffect(() => {
    list.current?.querySelector<HTMLElement>(`[data-index="${sel}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [sel])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement
      const typing = el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable
      if (typing || e.metaKey || e.ctrlKey || e.altKey || picking || !items.length) return
      // A held key auto-repeats: only j/k may repeat, or holding y would mark row after row right.
      if (e.repeat && e.key !== 'j' && e.key !== 'k') return
      // Keys typed inside another overlay (the mobile nav sheet, a menu, a toast) aren't meant for the queue.
      if (el.closest('[role="dialog"], [role="alertdialog"], [role="menu"], [role="listbox"]')) return
      const it = items[sel]
      if (e.key === 'j') { e.preventDefault(); setSel(s => Math.min(items.length - 1, s + 1)) }
      else if (e.key === 'k') { e.preventDefault(); setSel(s => Math.max(0, s - 1)) }
      else if (e.key === 'y' && it) { e.preventDefault(); void mark(it) }
      else if (e.key === 'n' && it) { e.preventDefault(); setPicking(keyOf(it)) }
      else if (e.key === 'Enter' && it && !el.closest('button, a')) { e.preventDefault(); navigate(`/runs/${it.qid}?tab=subtasks`) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [items, sel, picking, mark])

  return (
    <PageBody width="narrow">
      <TopActions>
        {!loading && !error && <span className="text-xs tabular-nums text-muted-foreground">{items.length}{full ? '+' : ''} to review</span>}
      </TopActions>

      <div className="flex flex-col gap-3">
        <div className="flex min-w-0 flex-wrap items-center gap-1.5" role="group" aria-label="Filter by reason">
          <ReasonChip active={!reason} onClick={() => setReason('')}>All</ReasonChip>
          {REASONS.map(r => <ReasonChip key={r} active={reason === r} onClick={() => setReason(reason === r ? '' : r)}>{r}</ReasonChip>)}
        </div>
        <p className="m-0 hidden flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground md:flex">
          <span className="inline-flex items-center gap-1"><Kbd>j</Kbd><Kbd>k</Kbd> move</span>
          <span className="inline-flex items-center gap-1"><Kbd>y</Kbd> right</span>
          <span className="inline-flex items-center gap-1"><Kbd>n</Kbd> wrong</span>
          <span className="inline-flex items-center gap-1"><Kbd>Enter</Kbd> open run</span>
          {scanned > 0 && <span className="ml-auto tabular-nums">{scanned.toLocaleString()} runs scanned</span>}
        </p>
      </div>

      <section className="min-w-0 overflow-hidden rounded-lg border border-border bg-surface" aria-label="Review queue" aria-busy={loading || undefined}>
        {loading || (!items.length && full) ? <ReviewSkeleton />
          : error ? <EmptyState icon="alert" title="Could not load the review queue" text={error}
            action={<Button variant="secondary" icon="refresh" onClick={() => setNonce(n => n + 1)}>Retry</Button>} />
          : !items.length ? (
            reason
              ? <EmptyState icon="filter" title="Nothing to review for this reason" text="Try another reason or show them all." action={<Button variant="secondary" onClick={() => setReason('')}>Show all</Button>} />
              : <EmptyState icon="review" title="Nothing to review" text="Every saved routing decision looks confident, or you have labelled it already. New shaky ones show up here as you ask." action={<Button variant="secondary" icon="runs" onClick={() => navigate('/runs')}>Browse runs</Button>} />
          ) : (
            <ul ref={list} className="m-0 flex list-none flex-col p-0">
              {items.map((it, i) => (
                <Row key={keyOf(it)} item={it} index={i} selected={i === sel} now={now} onSelect={() => setSel(i)}
                  picking={picking === keyOf(it)} onPicking={open => setPicking(open ? keyOf(it) : null)} onMark={c => void mark(it, c)} />
              ))}
            </ul>
          )}
      </section>
    </PageBody>
  )
}

function Row({ item: it, index, selected, now, onSelect, picking, onPicking, onMark }: {
  item: ReviewItem; index: number; selected: boolean; now: number; onSelect: () => void
  picking: boolean; onPicking: (open: boolean) => void; onMark: (correct?: string) => void
}) {
  return (
    <li data-index={index} onClick={onSelect}
      className={cn('grid min-w-0 gap-3 border-b border-border px-4 py-3.5 last:border-0 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center sm:px-5',
        selected ? 'bg-subtle/70 shadow-[inset_2px_0_0_var(--accent)]' : 'hover:bg-subtle/40')}
      aria-current={selected || undefined}>
      <div className="flex min-w-0 flex-col gap-2">
        <p className="m-0 min-w-0 text-sm font-medium break-words text-foreground">{it.text}</p>
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-muted-foreground">
          <AgentBadge agent={it.picked} />
          <Confidence value={it.confidence} />
          <span className="tabular-nums" title="Top probability minus the second">margin {pct(it.margin)}</span>
          {it.runner_up && <span className="inline-flex min-w-0 items-center gap-1">then <AgentBadge agent={it.runner_up} dot pct={it.probabilities[it.runner_up]} /></span>}
        </div>
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          {it.reasons.map(r => <Badge key={r} tone={REASON_TONE[r] ?? 'neutral'}>{r}</Badge>)}
          <a href={`#/runs/${it.qid}?tab=subtasks`} onClick={e => e.stopPropagation()}
            className="ml-auto inline-flex items-center gap-1 rounded-sm text-xs font-medium text-primary outline-none hover:underline focus-visible:ring-[3px] focus-visible:ring-ring/35">
            Run #{it.qid}<Icon name="arrow-right" size={12} />
          </a>
          <span className="text-xs tabular-nums text-muted-foreground" title={new Date(it.at * 1000).toLocaleString()}>{timeAgo(it.at, now)}</span>
        </div>
      </div>
      <div className="flex items-center gap-1.5 sm:flex-col sm:items-stretch">
        <Button variant="secondary" size="sm" icon="thumbs-up" className="flex-1" onClick={e => { e.stopPropagation(); onMark() }}>Right</Button>
        <AgentPicker picked={it.picked} probabilities={it.probabilities} open={picking} onOpenChange={onPicking} onPick={a => onMark(a)}>
          <Button variant="secondary" size="sm" icon="thumbs-down" className="flex-1" onClick={e => e.stopPropagation()} aria-label="Wrong, pick the right agent">Wrong</Button>
        </AgentPicker>
      </div>
    </li>
  )
}

// Confidence as a small bar, toned by how shaky it is.
function Confidence({ value }: { value: number }) {
  const tone = value < 0.45 ? 'bg-destructive' : value < 0.6 ? 'bg-warn' : 'bg-ok'
  return (
    <span className="inline-flex items-center gap-1.5" title="Jev's confidence in its pick">
      <span className="h-1.5 w-14 overflow-hidden rounded-full bg-subtle" role="meter" aria-label="Confidence" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(value * 100)}>
        <span className={cn('block h-full rounded-full', tone)} style={{ width: `${Math.max(0, Math.min(1, value)) * 100}%` }} />
      </span>
      <span className="tabular-nums">{pct(value)}</span>
    </span>
  )
}

function ReasonChip({ active, onClick, children }: { active: boolean; onClick: () => void; children: string }) {
  return (
    <button type="button" data-slot="button" aria-pressed={active} onClick={onClick}
      className={cn('inline-flex h-7 items-center rounded-md border px-2.5 text-[13px] font-medium outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/35',
        active ? 'border-primary/40 bg-primary/10 text-primary' : 'border-border bg-surface text-muted-foreground hover:bg-subtle hover:text-foreground')}>
      {children}
    </button>
  )
}

function ReviewSkeleton() {
  return (
    <div aria-label="Loading the review queue">
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="flex flex-col gap-2.5 border-b border-border px-4 py-4 last:border-0 sm:px-5">
          <Skeleton width={`${50 + ((i * 17) % 40)}%`} height={14} />
          <Skeleton width="45%" height={20} />
          <Skeleton width="30%" height={16} />
        </div>
      ))}
    </div>
  )
}

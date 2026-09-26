import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { useEventStream, fromRecord, type Listener, type Run, type Store } from './useEventStream'
import { getRun, ApiError } from './api'

// One EventSource for the whole app. Pages read the live store and subscribe to raw events (eval progress etc.).

interface StoreCtx { store: Store; subscribe: (fn: Listener) => () => void }

const Ctx = createContext<StoreCtx | null>(null)

export function StoreProvider({ children, url = '/events' }: { children: ReactNode; url?: string }) {
  const value = useEventStream(url)
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useStore(): StoreCtx {
  const v = useContext(Ctx)
  if (!v) throw new Error('useStore must be used inside <StoreProvider>')
  return v
}

export interface RunState { run: Run | undefined; loading: boolean; error: string | null; notFound: boolean }

/** The live Run from the store when present, otherwise fetched once via GET /api/runs/:qid (with loading and error state). */
export function useRunState(qid: number | null | undefined): RunState {
  const { store } = useStore()
  const live = qid == null ? undefined : store.runs.find(r => r.qid === qid)
  const [fetched, setFetched] = useState<{ qid: number; run?: Run; error?: string; notFound?: boolean } | null>(null)
  const have = !!live && (!!live.text || live.order.length > 0)

  useEffect(() => {
    if (qid == null || !Number.isFinite(qid) || have) return
    if (fetched?.qid === qid) return
    let alive = true
    setFetched({ qid })
    getRun(qid)
      .then(r => { if (alive) setFetched({ qid, run: fromRecord(r) }) })
      .catch(e => { if (alive) setFetched({ qid, error: e instanceof Error ? e.message : String(e), notFound: e instanceof ApiError && e.status === 404 }) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [qid, have])

  if (qid == null) return { run: undefined, loading: false, error: null, notFound: false }
  const f = fetched?.qid === qid ? fetched : null
  // A live stub (joined mid-run) lacks the query text; fill it from the fetched record.
  let run = have ? live : f?.run
  if (have && live && f?.run && !live.text) run = { ...live, text: f.run.text, source: f.run.source, at: f.run.at }
  return {
    run,
    loading: !run && (!f || (!f.run && !f.error)),
    error: run ? null : f?.error ?? null,
    notFound: !run && !!f?.notFound,
  }
}

/** The live Run if it is in the store, otherwise the one fetched from getRun (undefined while loading or if missing). */
export function useRun(qid: number | null | undefined): Run | undefined {
  return useRunState(qid).run
}

export type { Run, Store, Listener }

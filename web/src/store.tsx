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

/** The live Run from the store when present, otherwise fetched via GET /api/runs/:qid (with loading and error state). */
export function useRunState(qid: number | null | undefined): RunState {
  const { store } = useStore()
  const live = qid == null ? undefined : store.runs.find(r => r.qid === qid)
  const [fetched, setFetched] = useState<{ qid: number; run?: Run; error?: string; notFound?: boolean } | null>(null)
  const have = !!live?.text
  const needsRefresh = !have || (!store.connected && !live?.done)

  useEffect(() => {
    if (qid == null || !Number.isFinite(qid) || !needsRefresh) return
    let alive = true
    let timer: ReturnType<typeof setTimeout> | undefined
    setFetched(prev => prev?.qid === qid ? prev : { qid })
    const load = async () => {
      try {
        const run = fromRecord(await getRun(qid))
        if (!alive) return
        setFetched({ qid, run })
        if (!run.done) timer = setTimeout(load, 3000)
      } catch (e) {
        if (!alive) return
        setFetched(prev => ({ qid, run: prev?.qid === qid ? prev.run : undefined,
          error: e instanceof Error ? e.message : String(e), notFound: e instanceof ApiError && e.status === 404 }))
      }
    }
    void load()
    return () => { alive = false; clearTimeout(timer) }
  }, [qid, needsRefresh])

  if (qid == null) return { run: undefined, loading: false, error: null, notFound: false }
  const f = fetched?.qid === qid ? fetched : null
  // A live stub (joined mid-run) lacks the query text; fill it from the fetched record.
  let run = have && (store.connected || live?.done) ? live : f?.run ?? live
  if (live && !live.text && f?.run && store.connected) {
    run = f.run.done && !live.done ? f.run : { ...f.run, ...live, text: f.run.text, source: f.run.source, at: f.run.at,
      plan: live.plan ?? f.run.plan, order: [...new Set([...f.run.order, ...live.order])],
      tasks: { ...f.run.tasks, ...live.tasks } }
  }
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

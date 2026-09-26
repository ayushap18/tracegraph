import { useMemo } from 'react'
import { rows } from '../lib'
import type { Store } from '../useEventStream'
import type { AgentStat } from './Graph'

// Graph inputs derived from the live store: every agent to draw, per-agent stats and Jev's averages.
// Recomputed only on structural events (store.rev), never per streamed token.
export function useTrace(store: Store) {
  const allAgents = useMemo(() => {
    const list = [...Object.keys(store.agents), ...store.guards]
    for (const a of Object.keys(store.stats.by_agent)) if (!list.includes(a)) list.push(a)
    return list
  }, [store.agents, store.guards, store.stats.by_agent])

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const allRows = useMemo(() => rows(store.runs), [store.rev])

  const agentStats = useMemo(() => {
    const out: Record<string, AgentStat> = {}
    for (const a of allAgents) {
      const mine = allRows.filter(r => r.task.routed?.agent === a)
      const timed = mine.filter(r => r.task.answered)
      out[a] = {
        count: store.stats.by_agent[a] ?? 0,
        avgMs: timed.length ? timed.reduce((s, r) => s + (r.task.answered?.agent_ms ?? 0), 0) / timed.length : null,
        avgConf: mine.length ? mine.reduce((s, r) => s + (r.task.routed?.confidence ?? 0), 0) / mine.length : null,
      }
    }
    return out
  }, [allRows, allAgents, store.stats.by_agent])

  const jev = useMemo(() => {
    const routed = allRows.map(r => r.task.routed).filter((r): r is NonNullable<typeof r> => !!r)
    const avg = (f: (r: (typeof routed)[number]) => number) => (routed.length ? routed.reduce((s, r) => s + f(r), 0) / routed.length : null)
    return { model: routed[routed.length - 1]?.model ?? '', avgMs: avg(r => r.jev_ms), avgConf: avg(r => r.confidence) }
  }, [allRows])

  return { allAgents, allRows, agentStats, jev }
}

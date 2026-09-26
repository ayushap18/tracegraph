import { useEffect, useRef, useState } from 'react'
import type { Prices, Stats } from './protocol'
import type { Run, Task } from './useEventStream'

export interface Row { run: Run; task: Task }

export const rows = (runs: Run[]): Row[] => runs.flatMap(run => run.order.map(tid => ({ run, task: run.tasks[tid] })).filter(r => r.task))

// Every analytics chart body is this tall, so panels side by side line up exactly.
export const CHART_H = 200

export const inFlight = (runs: Run[]) => runs.filter(r => !r.done)

// Prices come as $ per million tokens (0.042 / 5 / 25). Tolerate a per-token feed too.
const perToken = (p: number) => (p > 1e-3 ? p / 1e6 : p)
export function costs(stats: Stats, prices: Prices) {
  const jev = stats.jev_input_tokens * perToken(prices.jev_in)
  const claude = stats.claude_input_tokens * perToken(prices.claude_in) + stats.claude_output_tokens * perToken(prices.claude_out)
  return { jev, claude }
}

export const money = (v: number) => '$' + (v === 0 ? '0' : v < 0.01 ? v.toFixed(5) : v.toFixed(3))
export const pct = (v: number | undefined) => (v == null ? '-' : Math.round(v * 100) + '%')
export const ms = (v: number | null | undefined) => (v == null ? '-' : Math.round(v).toLocaleString() + ' ms')
export const clock = (at: number) => new Date(at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })

// Source links from agents: only ever render http(s) hrefs.
export const safeHref = (s: string) => (/^https?:\/\//i.test(s) ? s : 'https://' + s.replace(/^[a-z]+:/i, ''))

// Slowest subtask wins, since subtasks route and run in parallel.
export function latency(run: Run) {
  const ts = run.order.map(t => run.tasks[t]).filter(Boolean)
  const jev = Math.max(0, ...ts.map(t => t.routed?.jev_ms ?? 0))
  const agent = Math.max(0, ...ts.map(t => t.answered?.agent_ms ?? 0))
  const merge = run.merged?.ms ?? 0
  return { jev, agent, merge }
}

export function useSize<T extends Element>() {
  const ref = useRef<T>(null)
  const [size, setSize] = useState({ width: 0, height: 0 })
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect
      setSize(s => (Math.abs(s.width - width) < 1 && Math.abs(s.height - height) < 1 ? s : { width, height }))
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  return [ref, size] as const
}

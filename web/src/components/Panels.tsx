import { useEffect, useRef, useState, type CSSProperties, type FormEvent, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { colorOf, type ControlState } from '../protocol'
import type { Run, Store, Task } from '../useEventStream'
import { post } from '../useEventStream'
import { clock, costs, latency, money, ms, pct, rows, safeHref } from '../lib'
import Markdown from './Markdown'
import { AgentIcon, Icon, type UiIconName } from '../icons'
import { Sparkline, useCountUp } from './Viz'

const cvar = (c: string) => ({ '--c': c }) as CSSProperties

export function Chip({ agent }: { agent: string | undefined }) {
  return <span className="chip" style={cvar(colorOf(agent))}>{agent && <AgentIcon agent={agent} size={12} strokeWidth={2.25} />}{agent ?? '…'}</span>
}

export function Bars({ probs, max = 5 }: { probs: Record<string, number>; max?: number }) {
  const list = Object.entries(probs).sort((a, b) => b[1] - a[1]).slice(0, max)
  return (
    <div className="bars">
      {list.map(([a, p]) => (
        <div className="bar" key={a}>
          <span>{a}</span>
          <div className="track"><div className="fill" style={{ ...cvar(colorOf(a)), width: `${p * 100}%` }} /></div>
          <span className="n">{pct(p)}</span>
        </div>
      ))}
    </div>
  )
}

export function AskBox({ samples, state, inputRef }: { samples: string[]; state: ControlState; inputRef: RefObject<HTMLInputElement> }) {
  const [q, setQ] = useState('')
  const past = useRef<string[]>([])
  const cursor = useRef(-1)
  const [interval, setIntervalV] = useState(state.interval)
  const [busy, setBusy] = useState(false)
  useEffect(() => setIntervalV(state.interval), [state.interval])
  const ask = async (text: string) => {
    const v = text.trim().slice(0, 500)
    if (!v) return
    past.current = [v, ...past.current.filter(p => p !== v)].slice(0, 30)
    cursor.current = -1
    setBusy(true)
    await post('/ask', { query: v })
    setBusy(false)
  }
  const submit = (e: FormEvent) => { e.preventDefault(); void ask(q); setQ('') }
  // Up/Down walk back through what you asked this session, like a shell.
  const recall = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return
    const list = past.current
    if (!list.length) return
    e.preventDefault()
    cursor.current = Math.max(-1, Math.min(list.length - 1, cursor.current + (e.key === 'ArrowUp' ? 1 : -1)))
    setQ(cursor.current < 0 ? '' : list[cursor.current])
  }
  return (
    <section className="panel">
      <form className="ask" onSubmit={submit}>
        <div className="ask-field">
          <Icon name="search" className="ask-icon" size={18} />
          <input ref={inputRef} value={q} onChange={e => setQ(e.target.value)} onKeyDown={recall} maxLength={500} autoComplete="off" aria-label="Query"
            placeholder="Ask anything, or several things: weather in Paris and convert 100 EUR to INR" />
          <kbd className="hint">/</kbd>
        </div>
        <button type="submit" className="with-icon" disabled={busy || !q.trim()}><Icon name="send" size={16} strokeWidth={2} />Route</button>
        <div className="auto">
          <button type="button" className={'ghost with-icon' + (state.autopilot ? ' on' : '')} aria-pressed={state.autopilot}
            onClick={() => void post('/control', { autopilot: !state.autopilot })}>
            <Icon name={state.autopilot ? 'pause' : 'play'} size={14} strokeWidth={2} />Autopilot {state.autopilot ? 'on' : 'off'}
          </button>
          <label>every <input type="range" min={1} max={15} step={0.5} value={interval}
            onChange={e => setIntervalV(+e.target.value)}
            onPointerUp={() => void post('/control', { interval })}
            onKeyUp={() => void post('/control', { interval })} /> <span className="num">{interval}s</span></label>
        </div>
      </form>
      {samples.length > 0 && (
        <div className="samples">
          {samples.slice(0, 14).map(s => <button type="button" key={s} onClick={() => void ask(s)}>{s}</button>)}
        </div>
      )}
    </section>
  )
}

function Kpi({ icon, label, value, fmt, spark, color, min, note }: {
  icon: UiIconName; label: string; value: number | null; fmt: (v: number) => string; spark?: number[]; color?: string; min?: number; note?: string
}) {
  const v = useCountUp(value ?? 0)
  return (
    <div className="kpi-card">
      <div className="kpi-l"><Icon name={icon} size={13} strokeWidth={2} />{label}</div>
      <div className="kpi-v">{value == null ? '–' : fmt(v)}</div>
      {spark && spark.length > 1 ? <Sparkline values={spark} color={color} min={min} /> : <div className="kpi-note">{note ?? '\u00a0'}</div>}
    </div>
  )
}

export function Kpis({ store }: { store: Store }) {
  const routed = rows(store.runs).map(r => r.task.routed).filter((r): r is NonNullable<Task['routed']> => !!r)
  const avg = (f: (x: NonNullable<Task['routed']>) => number) => (routed.length ? routed.reduce((s, r) => s + f(r), 0) / routed.length : null)
  const c = costs(store.stats, store.prices)
  const recent = routed.slice(-30)
  const done = store.runs.filter(r => r.done && r.total_ms != null).slice(-30)
  const multi = store.runs.filter(r => r.order.length > 1).length
  const int = (v: number) => Math.round(v).toLocaleString()
  return (
    <section className="kpi-strip" aria-label="Key numbers">
      <Kpi icon="queries" label="queries" value={store.stats.queries} fmt={int} spark={store.runs.slice(-30).map(r => r.order.length)} min={0}
        note={`${multi} multi-agent`} />
      <Kpi icon="subtasks" label="subtasks routed" value={store.stats.subtasks} fmt={int} note={`${store.stats.errors} errors`} />
      <Kpi icon="latency" label="avg Jev latency" value={avg(r => r.jev_ms)} fmt={v => int(v) + ' ms'} spark={recent.map(r => r.jev_ms)} />
      <Kpi icon="confidence" label="avg confidence" value={avg(r => r.confidence)} fmt={v => Math.round(v * 100) + '%'} spark={recent.map(r => r.confidence)} color="var(--ok)" min={0} />
      <Kpi icon="activity" label="end-to-end" value={done.length ? done.reduce((s, r) => s + (r.total_ms ?? 0), 0) / done.length : null} fmt={v => int(v) + ' ms'}
        spark={done.map(r => r.total_ms ?? 0)} color="var(--warn)" min={0} />
      <Kpi icon="spend" label="spend" value={c.jev + c.claude} fmt={money}
        note={!store.engine ? `Jev only · ${store.stats.jev_input_tokens.toLocaleString()} tokens`
          : store.engine.billing === 'subscription' ? `Jev ${money(c.jev)} · ${store.engine.label} on subscription`
          : `Jev ${money(c.jev)} · ${store.engine.label} ${money(c.claude)}`} />
    </section>
  )
}

export function TaskCard({ task }: { task: Task }) {
  const r = task.routed, a = task.answered
  const text = a ? a.answer : task.stream
  return (
    <div className="task">
      <div className="task-head">
        <span className="tid">{task.tid}</span>
        <span className="task-text">{task.text || '…'}</span>
        {r ? <Chip agent={r.agent} /> : <span className="muted small">{task.error ? 'not routed' : 'routing…'}</span>}
      </div>
      {r && (
        <div className="meta">
          <span>{pct(r.confidence)} conf</span><span>{r.jev_ms} ms</span>
          {r.reason && <span>{r.reason}</span>}
          <span>urgency {r.urgency.toFixed(1)}</span><span>clear {pct(r.clear)}</span><span>unsafe {pct(r.unsafe)}</span>
        </div>
      )}
      {r && <Bars probs={r.probabilities} max={4} />}
      {task.error && !r && <div className="answer fail">{task.error}</div>}
      {(text || r) && (
        <div className={'answer' + (a && !a.ok ? ' fail' : '') + (!a && r ? ' streaming' : '')}>{text ? <Markdown text={text} /> : 'Agent working…'}</div>
      )}
      {a && (
        <div className="meta">
          <span>{a.engine}</span><span>{a.agent_ms} ms</span>
          {a.source && <a href={safeHref(a.source)} target="_blank" rel="noopener noreferrer">{a.source}</a>}
        </div>
      )}
    </div>
  )
}

export function LatestRun({ run, engine }: { run: Run | undefined; engine: Store['engine'] }) {
  if (!run) return (
    <section className="panel latest">
      <h2><span className="h-title"><Icon name="activity" size={14} strokeWidth={2} />Latest run</span></h2>
      <p className="muted">Ask something above, or switch on Autopilot to stream sample queries. {engine ? `LLM engine: ${engine.label}.` : 'Running keyless (no LLM engine).'}</p>
    </section>
  )
  const multi = run.order.length > 1
  const mergeText = run.merged?.answer ?? run.mergeStream
  return (
    <section className="panel latest">
      <h2><span className="h-title"><Icon name="activity" size={14} strokeWidth={2} />Latest run</span> <span>#{run.qid} · {run.done ? ms(run.total_ms) : 'running'}</span></h2>
      <p className="q">{run.text || '…'} {run.source === 'autopilot' && <span className="src">auto</span>}</p>
      <div className="meta">
        {run.plan ? <>
          <span className="tag">{run.plan.planner} planner</span>
          <span>{run.order.length} subtask{run.order.length === 1 ? '' : 's'}</span>
          {run.plan.ms != null && <span>{run.plan.ms} ms</span>}
          {run.plan.multi != null && <span>multi {pct(run.plan.multi)}</span>}
        </> : <span>planning…</span>}
      </div>
      <div className="tasks">{run.order.map(tid => run.tasks[tid] && <TaskCard key={tid} task={run.tasks[tid]} />)}</div>
      {(multi || (run.merged && run.merged.engine !== 'single')) && (
        <div className="merged">
          <h3>Merged answer {run.merged && <span className="tag">{run.merged.engine}</span>} {run.merged?.ms != null && <span className="muted small">{run.merged.ms} ms</span>}</h3>
          <div className={'answer big' + (!run.merged ? ' streaming' : '')}>{mergeText ? <Markdown text={mergeText} /> : 'Waiting for agents…'}</div>
        </div>
      )}
      {run.error && <p className="err">{run.error}</p>}
    </section>
  )
}

export function RoutingLog({ runs }: { runs: Run[] }) {
  const list = rows(runs).reverse().slice(0, 30)
  return (
    <section className="panel">
      <h2><span className="h-title"><Icon name="log" size={14} strokeWidth={2} />Routing log</span> <span>{list.length ? `last ${list.length} subtasks` : 'empty'}</span></h2>
      <div className="table-wrap">
        <table>
          <thead><tr><th className="hide-sm">time</th><th>query</th><th>agent</th><th>conf</th><th className="hide-sm">Jev ms</th><th className="hide-sm">answer</th></tr></thead>
          <tbody>
            {list.map(({ run, task }) => (
              <tr key={task.tid}>
                <td className="num hide-sm">{clock(run.at)}</td>
                <td>
                  {task.text || run.text}
                  {run.order.length > 1 && <span className="src"> {task.tid}</span>}
                  {run.source === 'autopilot' && <span className="src"> auto</span>}
                </td>
                <td><Chip agent={task.routed?.agent} /></td>
                <td className="num">{pct(task.routed?.confidence)}</td>
                <td className="num hide-sm">{task.routed?.jev_ms ?? '–'}</td>
                <td className="ans hide-sm" title={task.answered?.answer ?? ''}>{task.answered?.answer ?? task.stream}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

const HUB_ICON: Record<string, UiIconName> = { query: 'query', planner: 'planner', jev: 'jev', merger: 'merger', answer: 'answer' }

const HUB_INFO: Record<string, string> = {
  query: 'Incoming queries, from you or autopilot.',
  planner: 'Splits a query into up to 4 self-contained subtasks (the LLM engine when one is active, else a heuristic plus Jev\'s "multi" check).',
  jev: 'Jev routes every subtask with one system_one call: route choice, urgency, unsafe and clear.',
  merger: 'Combines subtask answers: single passes through, concat joins, the LLM engine writes one answer.',
  answer: 'The final answer streamed back to the page.',
}

export function Inspector({ id, store, onClose, run }: { id: string; store: Store; onClose: () => void; run?: Run }) {
  // A run fetched from the API (not in the live store) is searched first, so its subtasks still inspect.
  const all = run && !store.runs.some(r => r.qid === run.qid) ? [...rows([run]), ...rows(store.runs)] : rows(store.runs)
  let body: JSX.Element
  if (id.startsWith('a:')) {
    const agent = id.slice(2)
    const mine = all.filter(r => r.task.routed?.agent === agent)
    const conf = mine.length ? mine.reduce((s, r) => s + (r.task.routed?.confidence ?? 0), 0) / mine.length : undefined
    body = <>
      <h3><Chip agent={agent} /></h3>
      <p className="muted small">{store.agents[agent] ?? (store.guards.includes(agent) ? 'Guard outcome decided from Jev\'s other answers.' : '')}</p>
      <div className="meta"><span>{store.stats.by_agent[agent] ?? 0} subtasks</span><span>avg conf {pct(conf)}</span></div>
      <ul className="recent">{mine.slice(-6).reverse().map(r => <li key={r.task.tid}>{r.task.text || r.run.text} <span className="muted">{pct(r.task.routed?.confidence)}</span></li>)}</ul>
      {!mine.length && <p className="muted small">No recent subtasks.</p>}
    </>
  } else if (id.startsWith('t:')) {
    const tid = id.slice(2)
    const hit = all.find(r => r.task.tid === tid)
    body = hit ? <>
      <h3><Icon name="subtasks" size={15} />Subtask {tid}</h3>
      <p>{hit.task.text}</p>
      {hit.task.routed ? <><Chip agent={hit.task.routed.agent} /><Bars probs={hit.task.routed.probabilities} max={8} /></> : <p className="muted">{hit.task.error ?? 'routing…'}</p>}
      <div className="answer"><Markdown text={hit.task.answered?.answer ?? (hit.task.stream || '…')} /></div>
    </> : <p className="muted">Subtask {tid} is no longer in memory.</p>
  } else {
    const done = store.runs.filter(r => r.done)
    const lat = done.map(latency)
    const avg = (f: (l: ReturnType<typeof latency>) => number) => (lat.length ? lat.reduce((s, l) => s + f(l), 0) / lat.length : null)
    const last = store.runs[store.runs.length - 1]
    body = <>
      <h3>{(id in HUB_ICON) && <Icon name={HUB_ICON[id]} size={15} />}{id === 'jev' ? 'Jev router' : id}</h3>
      <p className="muted small">{HUB_INFO[id] ?? ''}</p>
      <div className="meta">
        {id === 'jev' && <><span>avg {ms(avg(l => l.jev))}</span><span>{store.stats.jev_input_tokens.toLocaleString()} input tokens</span>
          {last && last.order[0] && last.tasks[last.order[0]]?.routed && <span>{last.tasks[last.order[0]].routed!.model}</span>}</>}
        {id === 'planner' && <><span>{store.engine ? store.engine.label : 'heuristic'}</span>{last?.plan && <span>last: {last.plan.planner}, {last.order.length} subtasks</span>}</>}
        {id === 'merger' && <><span>avg {ms(avg(l => l.merge))}</span>{last?.merged && <span>last: {last.merged.engine}</span>}</>}
        {(id === 'query' || id === 'answer') && <><span>{store.stats.queries} queries</span><span>{store.stats.errors} errors</span></>}
      </div>
    </>
  }
  return (
    <div className="inspector" role="dialog" aria-label="Node details">
      <button type="button" className="close" onClick={onClose} aria-label="Close"><Icon name="close" size={16} /></button>
      {body}
    </div>
  )
}

// One template for every analytics panel: title + headline number, a fixed-height chart body, and a legend footer.
// Paired panels therefore always line up, whatever their data.
export function ChartPanel({ icon, title, stat, note, legend, children }: {
  icon: UiIconName; title: string; stat: ReactNode; note?: ReactNode; legend?: ReactNode; children: ReactNode
}) {
  return (
    <section className="panel chart-panel">
      <div className="cp-head">
        <h2><span className="h-title"><Icon name={icon} size={14} strokeWidth={2} />{title}</span></h2>
        <div className="cp-stat"><span className="cp-v num">{stat}</span>{note && <span className="cp-note">{note}</span>}</div>
      </div>
      <div className="cp-body">{children}</div>
      <div className="legend cp-legend">{legend}</div>
    </section>
  )
}

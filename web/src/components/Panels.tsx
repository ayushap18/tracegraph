import { useEffect, useState, type CSSProperties, type FormEvent } from 'react'
import { colorOf, type ControlState } from '../protocol'
import type { Run, Store, Task } from '../useEventStream'
import { post } from '../useEventStream'
import { clock, costs, latency, money, ms, pct, rows, safeHref } from '../lib'

const cvar = (c: string) => ({ '--c': c }) as CSSProperties

export function Chip({ agent }: { agent: string | undefined }) {
  return <span className="chip" style={cvar(colorOf(agent))}>{agent ?? '…'}</span>
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

export function AskBox({ samples, state }: { samples: string[]; state: ControlState }) {
  const [q, setQ] = useState('')
  const [interval, setIntervalV] = useState(state.interval)
  const [busy, setBusy] = useState(false)
  useEffect(() => setIntervalV(state.interval), [state.interval])
  const ask = async (text: string) => {
    const v = text.trim().slice(0, 500)
    if (!v) return
    setBusy(true)
    await post('/ask', { query: v })
    setBusy(false)
  }
  const submit = (e: FormEvent) => { e.preventDefault(); void ask(q); setQ('') }
  return (
    <section className="panel">
      <form className="ask" onSubmit={submit}>
        <input value={q} onChange={e => setQ(e.target.value)} maxLength={500} autoComplete="off" aria-label="Query"
          placeholder="Ask anything, or several things: weather in Paris and convert 100 EUR to INR" />
        <button type="submit" disabled={busy || !q.trim()}>Route</button>
        <div className="auto">
          <button type="button" className={'ghost' + (state.autopilot ? ' on' : '')} aria-pressed={state.autopilot}
            onClick={() => void post('/control', { autopilot: !state.autopilot })}>
            Autopilot {state.autopilot ? 'on' : 'off'}
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

export function Kpis({ store }: { store: Store }) {
  const routed = rows(store.runs).map(r => r.task.routed).filter(Boolean)
  const avg = (f: (x: NonNullable<Task['routed']>) => number) => (routed.length ? routed.reduce((s, r) => s + f(r!), 0) / routed.length : undefined)
  const c = costs(store.stats, store.prices)
  const jm = avg(r => r.jev_ms)
  const items: Array<[string, string]> = [
    [store.stats.queries.toLocaleString(), 'queries'],
    [store.stats.subtasks.toLocaleString(), 'subtasks'],
    [jm == null ? '–' : ms(jm), 'avg Jev latency'],
    [pct(avg(r => r.confidence)), 'avg confidence'],
    [money(c.jev), 'Jev cost'],
    [store.claude ? money(c.claude) : 'off', 'Claude cost'],
  ]
  return (
    <div className="kpis">
      {items.map(([v, l]) => <div className="kpi" key={l}><div className="v">{v}</div><div className="l">{l}</div></div>)}
    </div>
  )
}

function TaskCard({ task }: { task: Task }) {
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
      {task.error && !r && <pre className="answer fail">{task.error}</pre>}
      {(text || r) && (
        <pre className={'answer' + (a && !a.ok ? ' fail' : '') + (!a && r ? ' streaming' : '')}>{text || 'Agent working…'}</pre>
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

export function LatestRun({ run, claude }: { run: Run | undefined; claude: boolean }) {
  if (!run) return (
    <section className="panel latest">
      <h2>Latest run</h2>
      <p className="muted">Ask something above, or switch on Autopilot to stream sample queries. {claude ? 'Claude is on.' : 'Running keyless (no Claude key).'}</p>
    </section>
  )
  const multi = run.order.length > 1
  const mergeText = run.merged?.answer ?? run.mergeStream
  return (
    <section className="panel latest">
      <h2>Latest run <span>#{run.qid} · {run.done ? ms(run.total_ms) : 'running'}</span></h2>
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
          <pre className={'answer' + (!run.merged ? ' streaming' : '')}>{mergeText || 'Waiting for agents…'}</pre>
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
      <h2>Routing log <span>{list.length ? `last ${list.length} subtasks` : 'empty'}</span></h2>
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

const HUB_INFO: Record<string, string> = {
  query: 'Incoming queries, from you or autopilot.',
  planner: 'Splits a query into up to 4 self-contained subtasks (Claude when available, else a heuristic plus Jev\'s "multi" check).',
  jev: 'Jev routes every subtask with one system_one call: route choice, urgency, unsafe and clear.',
  merger: 'Combines subtask answers: single passes through, concat joins, Claude writes one answer.',
  answer: 'The final answer streamed back to the page.',
}

export function Inspector({ id, store, onClose }: { id: string; store: Store; onClose: () => void }) {
  const all = rows(store.runs)
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
      <h3>Subtask {tid}</h3>
      <p>{hit.task.text}</p>
      {hit.task.routed ? <><Chip agent={hit.task.routed.agent} /><Bars probs={hit.task.routed.probabilities} max={8} /></> : <p className="muted">{hit.task.error ?? 'routing…'}</p>}
      <pre className="answer">{hit.task.answered?.answer ?? (hit.task.stream || '…')}</pre>
    </> : <p className="muted">Subtask {tid} is no longer in memory.</p>
  } else {
    const done = store.runs.filter(r => r.done)
    const lat = done.map(latency)
    const avg = (f: (l: ReturnType<typeof latency>) => number) => (lat.length ? lat.reduce((s, l) => s + f(l), 0) / lat.length : null)
    const last = store.runs[store.runs.length - 1]
    body = <>
      <h3>{id === 'jev' ? 'Jev' : id}</h3>
      <p className="muted small">{HUB_INFO[id] ?? ''}</p>
      <div className="meta">
        {id === 'jev' && <><span>avg {ms(avg(l => l.jev))}</span><span>{store.stats.jev_input_tokens.toLocaleString()} input tokens</span>
          {last && last.order[0] && last.tasks[last.order[0]]?.routed && <span>{last.tasks[last.order[0]].routed!.model}</span>}</>}
        {id === 'planner' && <><span>{store.claude ? 'Claude' : 'heuristic'}</span>{last?.plan && <span>last: {last.plan.planner}, {last.order.length} subtasks</span>}</>}
        {id === 'merger' && <><span>avg {ms(avg(l => l.merge))}</span>{last?.merged && <span>last: {last.merged.engine}</span>}</>}
        {(id === 'query' || id === 'answer') && <><span>{store.stats.queries} queries</span><span>{store.stats.errors} errors</span></>}
      </div>
    </>
  }
  return (
    <div className="inspector" role="dialog" aria-label="Node details">
      <button type="button" className="close" onClick={onClose} aria-label="Close">×</button>
      {body}
    </div>
  )
}

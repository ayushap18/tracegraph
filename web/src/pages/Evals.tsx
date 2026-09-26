import { useCallback, useEffect, useMemo, useState } from 'react'
import { cancelEval, errorText as errText, getEval, listEvals, runEval } from '../api'
import { Badge, Button, EmptyState, ProgressBar, Skeleton, navigate, useToast } from '../ui'
import { useStore } from '../store'
import { EngineIcon, Icon } from '../icons'
import type { EvalCase, EvalDetail, EvalSummary } from '../protocol'
import { Chip } from '../components/Panels'
import { Sparkline } from '../components/Viz'
import { ms } from '../lib'

// Eval suite: run the cases in evals/cases.jsonl through the real pipeline, watch progress live over SSE,
// and compare accuracy across runs. `#/evals/:id` drills into one run's cases.

const pctOf = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? '–' : `${Math.round(v * 100)}%`)
const when = (at: number) => new Date(at * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
const STATUS_TONE: Record<string, string> = { running: 'info', done: 'ok', cancelled: 'warn', error: 'bad' }

export default function Evals({ params }: { params: Record<string, string> }) {
  return params.id ? <EvalDetailPage key={params.id} id={params.id} /> : <EvalList />
}

interface Progress { eval_id: string; done: number; total: number; passed: number }

function EvalList() {
  const { store, subscribe } = useStore()
  const toast = useToast()
  const [evals, setEvals] = useState<EvalSummary[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [engine, setEngine] = useState<string>('')
  const [starting, setStarting] = useState(false)
  const [progress, setProgress] = useState<Progress | null>(null)

  const load = useCallback(async () => {
    try { const res = await listEvals(); setEvals(res.evals); setError(null) } catch (err) { setError(errText(err)) }
  }, [])
  useEffect(() => { void load() }, [load])

  // Default the engine select to the active engine once it is known.
  useEffect(() => { if (!engine && store.ready) setEngine(store.engine?.name ?? 'none') }, [store.ready, store.engine, engine])

  // The server's list is the source of truth: seed progress for an eval already running when the page opens, and
  // clear it once that eval is no longer running (a late progress event after Cancel would otherwise stick).
  useEffect(() => {
    const running = evals?.find(e => e.status === 'running')
    if (running) setProgress({ eval_id: running.eval_id, done: running.done ?? 0, total: running.total, passed: running.passed })
    else if (progress && evals?.some(e => e.eval_id === progress.eval_id && e.status !== 'running')) setProgress(null)
  }, [evals]) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => subscribe(e => {
    if (e.type === 'eval_progress') {
      setProgress({ eval_id: e.eval_id, done: e.done, total: e.total, passed: e.passed })
    } else if (e.type === 'eval_done') {
      setProgress(p => (p && p.eval_id !== e.eval_id ? p : null))
      if (e.status === 'cancelled') toast.info(`Eval cancelled after ${e.passed}/${e.total} passed`)
      else toast.success(`Eval finished: ${e.passed}/${e.total} passed (${pctOf(e.accuracy)})`)
      void load()
    }
  }), [subscribe, load, toast])

  const start = async () => {
    setStarting(true)
    try {
      const res = await runEval(engine || undefined)
      setProgress(p => p?.eval_id === res.eval_id ? p : { eval_id: res.eval_id, done: 0, total: 0, passed: 0 })
      void load()
    } catch (err) {
      toast.error(`Could not start the eval: ${errText(err)}`)
    } finally { setStarting(false) }
  }
  const stop = async () => {
    if (!progress) return
    try { await cancelEval(progress.eval_id); setProgress(null); void load() }
    catch (err) { toast.error(`Could not cancel: ${errText(err)}`) }
  }

  useEffect(() => {
    if (!progress) return
    const timer = window.setInterval(() => void load(), 3000)
    return () => clearInterval(timer)
  }, [!!progress, load])

  const sorted = useMemo(() => (evals ?? []).slice().sort((a, b) => b.at - a.at), [evals])
  const trend = useMemo(() => sorted.filter(e => e.status === 'done' && e.total > 0).map(e => e.accuracy).reverse(), [sorted])
  const latest = sorted.find(e => e.status === 'done')
  const prev = sorted.filter(e => e.status === 'done')[1]
  const delta = latest && prev ? latest.accuracy - prev.accuracy : null
  const best = trend.length ? Math.max(...trend) : null

  return (
    <div className="ev-page">
      <section className="panel ev-run" aria-label="Run an eval">
        <div className="ev-run-row">
          <div className="ev-run-text">
            <h2 className="ev-title"><Icon name="evals" size={16} /> Evals</h2>
            <p className="muted small">Runs every case in <code>evals/cases.jsonl</code> through the real pipeline and scores agents, outcomes and answers.</p>
          </div>
          <div className="ev-run-ctl">
            <label className="sr-only" htmlFor="ev-engine">Engine</label>
            <select id="ev-engine" className="ev-select" value={engine} onChange={e => setEngine(e.target.value)} disabled={!!progress}>
              <option value="none">Keyless (no engine)</option>
              {store.engines.filter(e => e.name !== 'none').map(e => (
                <option key={e.name} value={e.name} disabled={!e.available}>{e.label}{e.available ? '' : ' (unavailable)'}</option>
              ))}
            </select>
            {progress
              ? <Button variant="danger" icon="stop" onClick={() => void stop()}>Cancel</Button>
              : <Button icon="play" onClick={() => void start()} disabled={starting}>{starting ? 'Starting…' : 'Run eval'}</Button>}
          </div>
        </div>
        {progress && (
          <div className="ev-progress" aria-live="polite">
            <div className="ev-progress-meta small">
              <span>Running <a href={`#/evals/${progress.eval_id}`}>{progress.eval_id}</a></span>
              <span className="num">{progress.total ? `${progress.done}/${progress.total} cases · ${progress.passed} passed` : 'starting…'}</span>
            </div>
            <ProgressBar value={progress.total ? progress.done / progress.total : 0} label="Eval progress" />
          </div>
        )}
      </section>

      <section className="ev-kpis" aria-label="Accuracy summary">
        <div className="kpi-card">
          <div className="kpi-l">Latest accuracy</div>
          <div className="kpi-v num">{latest ? pctOf(latest.accuracy) : '–'}</div>
          <div className="kpi-note">{delta == null ? 'no previous run' : `${delta >= 0 ? '+' : ''}${Math.round(delta * 100)} pts vs previous`}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-l">Trend</div>
          <div className="kpi-v num">{best == null ? '–' : pctOf(best)}</div>
          {trend.length > 1 ? <Sparkline values={trend} min={0} /> : <div className="kpi-note">best accuracy</div>}
        </div>
        <div className="kpi-card">
          <div className="kpi-l">Silent wrong</div>
          <div className={'kpi-v num' + (latest?.silent_wrong ? ' ev-bad' : '')}>{latest ? latest.silent_wrong : '–'}</div>
          <div className="kpi-note">failed cases that looked ok</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-l">Runs</div>
          <div className="kpi-v num">{evals ? evals.length : '–'}</div>
          <div className="kpi-note">{latest ? `last ${when(latest.at)}` : 'none yet'}</div>
        </div>
      </section>

      <section className="panel" aria-label="Eval history">
        <h2><span className="h-title"><Icon name="history" size={14} /> History</span></h2>
        {evals == null && !error ? <div className="ev-skel"><Skeleton height={28} /><Skeleton height={28} /><Skeleton height={28} /></div>
          : error && !evals ? <EmptyState icon="evals" title="Could not load evals" text={error}
              action={<Button variant="ghost" onClick={() => void load()}>Retry</Button>} />
          : sorted.length === 0 ? <EmptyState icon="evals" title="No evals yet" text="Run the suite to get a baseline accuracy." />
          : (
            <div className="table-wrap ev-table-wrap">
              <table className="ev-table">
                <thead>
                  <tr><th>When</th><th>Engine</th><th>Status</th><th className="r">Passed</th><th className="r">Accuracy</th><th className="r">Silent wrong</th><th><span className="sr-only">Open</span></th></tr>
                </thead>
                <tbody>
                  {sorted.map(e => (
                    <tr key={e.eval_id} className="ev-row" onClick={() => navigate(`/evals/${e.eval_id}`)}>
                      <td data-l="When" className="num">{when(e.at)}</td>
                      <td data-l="Engine"><span className="ev-engine"><EngineIcon name={e.engine ?? 'none'} size={14} /> {e.engine ?? 'keyless'}</span></td>
                      <td data-l="Status"><Badge tone={STATUS_TONE[e.status] ?? 'muted'}>{e.status}</Badge></td>
                      <td data-l="Passed" className="num r">{e.passed}/{e.total}</td>
                      <td data-l="Accuracy" className="num r"><AccBar v={e.accuracy} /></td>
                      <td data-l="Silent wrong" className={'num r' + (e.silent_wrong ? ' ev-bad' : '')}>{e.silent_wrong}</td>
                      <td className="r"><a href={`#/evals/${e.eval_id}`} onClick={ev => ev.stopPropagation()} aria-label={`Open eval ${e.eval_id}`}>
                        <Icon name="next" size={15} /></a></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
      </section>
    </div>
  )
}

function AccBar({ v }: { v: number }) {
  const tone = v >= 0.9 ? 'ok' : v >= 0.7 ? 'warn' : 'bad'
  return (
    <span className="ev-acc">
      <span className="ev-acc-track" aria-hidden="true"><span className={'ev-acc-fill ' + tone} style={{ width: `${Math.max(0, Math.min(1, v)) * 100}%` }} /></span>
      {pctOf(v)}
    </span>
  )
}

type PassFilter = 'all' | 'pass' | 'fail'

function EvalDetailPage({ id }: { id: string }) {
  const { subscribe } = useStore()
  const [data, setData] = useState<EvalDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [filter, setFilter] = useState<PassFilter>('all')
  const [tag, setTag] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)

  const load = useCallback(async () => {
    try { setData(await getEval(id)); setError(null) } catch (err) { setError(errText(err)) }
  }, [id])
  useEffect(() => { void load() }, [load])

  // While this eval runs, refresh after each finished case.
  useEffect(() => subscribe(e => {
    if ((e.type === 'eval_progress' || e.type === 'eval_done') && e.eval_id === id) void load()
  }), [subscribe, id, load])

  useEffect(() => {
    if (data?.status !== 'running') return
    const timer = window.setInterval(() => void load(), 3000)
    return () => clearInterval(timer)
  }, [data?.status, load])

  const cases = data?.cases ?? []
  const tags = useMemo(() => [...new Set(cases.flatMap(c => c.tags ?? []))].sort(), [cases])
  const shown = cases.filter(c => (filter === 'all' || (filter === 'pass') === c.pass) && (!tag || c.tags?.includes(tag)))
  const failCount = cases.filter(c => !c.pass).length

  if (!data) {
    return (
      <div className="ev-page">
        <a className="ev-back small" href="#/evals"><Icon name="prev" size={14} /> All evals</a>
        {error
          ? <div className="panel"><EmptyState icon="evals" title="Eval not found" text={error}
              action={<Button variant="ghost" onClick={() => navigate('/evals')}>Back to evals</Button>} /></div>
          : <div className="panel ev-skel" aria-busy="true"><Skeleton height={60} /><Skeleton height={28} /><Skeleton height={28} /><Skeleton height={28} /></div>}
      </div>
    )
  }

  return (
    <div className="ev-page">
      <a className="ev-back small" href="#/evals"><Icon name="prev" size={14} /> All evals</a>
      <section className="ev-kpis" aria-label="Eval summary">
        <div className="kpi-card">
          <div className="kpi-l">Accuracy</div>
          <div className="kpi-v num">{pctOf(data.accuracy)}</div>
          <div className="kpi-note">{data.passed}/{data.total} passed</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-l">Engine</div>
          <div className="kpi-v ev-engine-v"><EngineIcon name={data.engine ?? 'none'} size={18} /> {data.engine ?? 'keyless'}</div>
          <div className="kpi-note">{when(data.at)}</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-l">Status</div>
          <div className="kpi-v"><Badge tone={STATUS_TONE[data.status] ?? 'muted'}>{data.status}</Badge></div>
          <div className="kpi-note">{cases.length} of {data.total} cases recorded</div>
        </div>
        <div className="kpi-card">
          <div className="kpi-l">Silent wrong</div>
          <div className={'kpi-v num' + (data.silent_wrong ? ' ev-bad' : '')}>{data.silent_wrong}</div>
          <div className="kpi-note">failed, yet every answer said ok</div>
        </div>
      </section>
      {data.status === 'running' && <ProgressBar value={data.total ? cases.length / data.total : 0} label="Eval progress" />}

      <section className="panel" aria-label="Cases">
        <div className="ev-filters">
          <div className="ev-chips" role="group" aria-label="Filter by result">
            {(['all', 'pass', 'fail'] as PassFilter[]).map(f => (
              <button key={f} type="button" className={'ev-chip' + (filter === f ? ' on' : '')} aria-pressed={filter === f} onClick={() => setFilter(f)}>
                {f === 'all' ? `All ${cases.length}` : f === 'pass' ? `Pass ${cases.length - failCount}` : `Fail ${failCount}`}
              </button>
            ))}
          </div>
          {tags.length > 0 && (
            <div className="ev-chips" role="group" aria-label="Filter by tag">
              <button type="button" className={'ev-chip tag-chip' + (!tag ? ' on' : '')} aria-pressed={!tag} onClick={() => setTag(null)}>any tag</button>
              {tags.map(t => (
                <button key={t} type="button" className={'ev-chip tag-chip' + (tag === t ? ' on' : '')} aria-pressed={tag === t}
                  onClick={() => setTag(tag === t ? null : t)}>#{t}</button>
              ))}
            </div>
          )}
        </div>
        {shown.length === 0
          ? <EmptyState icon="search" title="No matching cases" text={cases.length ? 'Try another filter.' : 'No cases have finished yet.'} />
          : (
            <div className="table-wrap ev-table-wrap tall">
              <table className="ev-table ev-cases">
                <thead>
                  <tr><th>Result</th><th>Query</th><th>Agents (expected / got)</th><th>Reasons</th><th>Answer</th><th className="r">Time</th><th><span className="sr-only">Run</span></th></tr>
                </thead>
                <tbody>
                  {shown.map(c => <CaseRow key={c.id} c={c} open={open === c.id} onToggle={() => setOpen(open === c.id ? null : c.id)} />)}
                </tbody>
              </table>
            </div>
          )}
      </section>
    </div>
  )
}

function CaseRow({ c, open, onToggle }: { c: EvalCase; open: boolean; onToggle: () => void }) {
  // The API may echo the case's expectations; show them when present.
  const expected = (c as EvalCase & { expect_agents?: string[] }).expect_agents ?? []
  const answer = c.answer ?? ''
  return (
    <tr className={c.pass ? 'pass' : 'fail'}>
      <td data-l="Result"><Badge tone={c.pass ? 'ok' : 'bad'}>{c.pass ? 'pass' : 'fail'}</Badge></td>
      <td data-l="Query" className="ev-query">
        <span>{c.query}</span>
        {c.tags?.length > 0 && <span className="ev-tags">{c.tags.map(t => <span key={t} className="tag">#{t}</span>)}</span>}
      </td>
      <td data-l="Agents">
        <div className="ev-agents">
          {expected.length > 0 && <span className="ev-exp"><span className="muted small">expected</span>{expected.map(a => <Chip key={a} agent={a} />)}</span>}
          <span className="ev-got">{expected.length > 0 && <span className="muted small">got</span>}
            {c.agents?.length ? c.agents.map((a, i) => <Chip key={i} agent={a} />) : <span className="muted">–</span>}</span>
        </div>
      </td>
      <td data-l="Reasons" className="ev-reasons">
        {c.reasons?.length ? <ul>{c.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul> : <span className="muted">–</span>}
      </td>
      <td data-l="Answer" className="ev-answer">
        {answer.length > 160
          ? <button type="button" className="ev-more" aria-expanded={open} onClick={onToggle}>
              {open ? answer : answer.slice(0, 160) + '…'}
            </button>
          : <span>{answer || <span className="muted">–</span>}</span>}
      </td>
      <td data-l="Time" className="num r">{ms(c.ms)}</td>
      <td className="r">{c.qid != null
        ? <a href={`#/runs/${c.qid}`} aria-label={`Open run ${c.qid}`} title={`Run #${c.qid}`}><Icon name="external" size={14} /></a>
        : null}</td>
    </tr>
  )
}

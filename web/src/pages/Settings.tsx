import { useCallback, useEffect, useState } from 'react'
import { deleteFile, errorText, listAgents, listEvals, listFiles, listSessions, setEngine, testEngine } from '../api'
import type { EngineInfo, EngineTestResult, FileInfo } from '../protocol'
import { useStore } from '../store'
import { useTheme, THEMES, THEME_ICON, THEME_LABEL } from '../theme'
import { Badge, Button, Card, EmptyState, IconButton, Kbd, Skeleton, Spinner, useToast } from '../ui'
import { EngineIcon, Icon } from '../icons'
import { MOD_KEY } from '../components/Shell'

// Engines (status, billing, test, set active), theme, limits, keyboard shortcuts and data counts.

const KEYLESS: EngineInfo = { name: 'none', label: 'Keyless', billing: 'api', web: false, available: true, why: '' }

export default function Settings() {
  const { store } = useStore()
  const { theme, setTheme } = useTheme()
  const active = store.engine?.name ?? 'none'
  const engines = [...store.engines, KEYLESS]

  return (
    <div className="wrap settings-page">
      <section aria-labelledby="engines-h" className="settings-section">
        <div className="section-head">
          <h2 id="engines-h"><Icon name="engine" size={16} />Engines</h2>
          <p className="muted">The LLM that plans, answers open questions and merges results. Subscription CLIs run on your own plan; the API bills per token; Keyless uses only built-in agents.</p>
        </div>
        <div className="engine-grid">
          {!store.ready ? Array.from({ length: 4 }, (_, i) => <div key={i} className="card"><Skeleton lines={4} /></div>)
            : engines.map(e => <EngineCard key={e.name} engine={e} active={e.name === active} />)}
        </div>
      </section>

      <div className="settings-cols">
        <Card title="Appearance" icon="sun">
          <div className="seg" role="radiogroup" aria-label="Theme">
            {THEMES.map(t => (
              <button key={t} type="button" role="radio" aria-checked={theme === t} className={'seg-btn' + (theme === t ? ' on' : '')} onClick={() => setTheme(t)}>
                <Icon name={THEME_ICON[t]} size={15} />{THEME_LABEL[t]}
              </button>
            ))}
          </div>
          <p className="muted small">System follows your OS setting. Press <Kbd>t</Kbd> anywhere to cycle.</p>
        </Card>

        <Card title="Limits and timeouts" icon="latency" subtitle="Set on the server; shown here for reference.">
          <dl className="kv">
            <div><dt>Run timeout</dt><dd>300 s <span className="muted small">(TG_RUN_TIMEOUT)</span></dd></div>
            <div><dt>Engine test timeout</dt><dd>60 s</dd></div>
            <div><dt>Query length</dt><dd>500 characters</dd></div>
            <div><dt>Subtasks per query</dt><dd>up to 4</dd></div>
            <div><dt>File uploads</dt><dd>10 MB · .txt .md .csv .json .pdf</dd></div>
            <div><dt>Custom agents</dt><dd>up to 12</dd></div>
            <div><dt>Autopilot interval</dt><dd className="num">{store.state.interval} s {store.state.autopilot ? <Badge tone="warn">on</Badge> : <Badge>off</Badge>}</dd></div>
          </dl>
        </Card>
      </div>

      <div className="settings-cols">
        <Card title="Keyboard shortcuts" icon="keyboard">
          <ul className="shortcuts">
            <li><span><Kbd>{MOD_KEY}</Kbd><Kbd>K</Kbd></span>Command palette</li>
            <li><span><Kbd>/</Kbd></span>Focus the message box (Chat, Live)</li>
            <li><span><Kbd>Enter</Kbd></span>Send · <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> new line</li>
            <li><span><Kbd>t</Kbd></span>Cycle theme</li>
            <li><span><Kbd>[</Kbd></span>Collapse or expand the sidebar</li>
            <li><span><Kbd>←</Kbd><Kbd>→</Kbd></span>Previous / next run (Live)</li>
            <li><span><Kbd>l</Kbd></span>Follow the newest run (Live)</li>
            <li><span><Kbd>a</Kbd></span>Toggle autopilot (Live)</li>
            <li><span><Kbd>f</Kbd></span>Fullscreen trace graph (Live)</li>
            <li><span><Kbd>r</Kbd></span>Reset graph zoom (Live)</li>
            <li><span><Kbd>Esc</Kbd></span>Close dialogs and panels</li>
          </ul>
        </Card>
        <DataCard />
      </div>

      <Card title="Server features" icon="blocks" subtitle="Reported by the server in its config.">
        <div className="feature-flags">
          {(Object.entries(store.features) as Array<[string, boolean]>).map(([k, v]) => (
            <Badge key={k} tone={v ? 'ok' : 'neutral'} icon={v ? 'check' : 'close'}>{k.replace('_', ' ')}</Badge>
          ))}
        </div>
      </Card>
    </div>
  )
}

function EngineCard({ engine: e, active }: { engine: EngineInfo; active: boolean }) {
  const toast = useToast()
  const [testing, setTesting] = useState(false)
  const [result, setResult] = useState<EngineTestResult | null>(null)
  const [switching, setSwitching] = useState(false)
  const keyless = e.name === 'none'

  const test = async () => {
    setTesting(true); setResult(null)
    try {
      const r = await testEngine(e.name)
      setResult(r)
      if (r.ok) toast.success(`${e.label} replied in ${r.ms} ms`)
      else toast.error(`${e.label} test failed: ${r.error ?? 'no reply'}`)
    } catch (err) {
      setResult({ ok: false, ms: 0, error: errorText(err) })
      toast.error(`${e.label} test failed: ${errorText(err)}`)
    } finally { setTesting(false) }
  }
  const activate = async () => {
    setSwitching(true)
    try { await setEngine(e.name); toast.success(`${e.label} is now the active engine`) }
    catch (err) { toast.error(`Could not switch: ${errorText(err)}`) } finally { setSwitching(false) }
  }

  return (
    <article className={'card engine-card' + (active ? ' active' : '') + (!e.available ? ' unavailable' : '')} aria-label={`${e.label} engine`}>
      <div className="ec-head">
        <span className="ec-icon"><EngineIcon name={e.name} size={20} /></span>
        <div className="ec-titles">
          <h3>{e.label}</h3>
          <span className="ec-name num">{keyless ? 'built-in agents only' : e.name}</span>
        </div>
        {active && <Badge tone="accent" icon="check">Active</Badge>}
      </div>
      <div className="ec-badges">
        {keyless ? <Badge tone="ok">free</Badge> : e.billing === 'subscription' ? <Badge tone="ok" icon="lock">your subscription</Badge> : <Badge tone="warn" icon="spend">pay per token</Badge>}
        {e.web && <Badge tone="neutral" icon="web">web search</Badge>}
        {e.name === 'codex' && <Badge tone="neutral" icon="code">can run code</Badge>}
        {e.available ? <Badge tone="ok" dot>available</Badge> : <Badge tone="bad" dot>unavailable</Badge>}
      </div>
      {!e.available && <p className="ec-why">{e.why}</p>}
      {result && (
        <p className={'ec-result ' + (result.ok ? 'ok' : 'bad')} role="status">
          <Icon name={result.ok ? 'success' : 'error'} size={14} />
          {result.ok ? <>Replied in <b className="num">{result.ms} ms</b>{result.text ? <>: <q>{result.text.slice(0, 60)}</q></> : null}</> : result.error ?? 'Failed'}
        </p>
      )}
      <div className="ec-actions">
        {!keyless && <Button variant="secondary" size="sm" icon="bolt" loading={testing} disabled={!e.available} onClick={() => void test()}>Test</Button>}
        <Button variant={active ? 'ghost' : 'primary'} size="sm" icon={active ? 'check' : 'play'} loading={switching} disabled={active || !e.available} onClick={() => void activate()}>
          {active ? 'Active' : 'Set active'}
        </Button>
      </div>
    </article>
  )
}

function DataCard() {
  const { store } = useStore()
  const toast = useToast()
  const [counts, setCounts] = useState<{ sessions?: number; files?: FileInfo[]; agents?: number; evals?: number }>({})
  const [loading, setLoading] = useState(true)
  const load = useCallback(() => {
    setLoading(true)
    const settle = <T,>(p: Promise<T>) => p.then(v => v, () => undefined)
    Promise.all([settle(listSessions(100)), settle(listFiles()), settle(listAgents()), settle(listEvals())]).then(([s, f, a, e]) => {
      setCounts({ sessions: s?.sessions.length, files: f?.files, agents: a?.agents.filter(x => x.kind === 'custom').length, evals: e?.evals.length })
      setLoading(false)
    })
  }, [])
  useEffect(() => { load() }, [load])
  const [confirm, setConfirm] = useState<string | null>(null)
  const remove = async (f: FileInfo) => {
    setConfirm(null)
    try { await deleteFile(f.id); setCounts(c => ({ ...c, files: c.files?.filter(x => x.id !== f.id) })); toast.success(`Deleted ${f.name}`) }
    catch (e) { toast.error(`Could not delete ${f.name}: ${errorText(e)}`) }
  }
  const n = (v: number | undefined) => (loading ? <Spinner size={12} /> : v == null ? '–' : v.toLocaleString())
  return (
    <Card title="Data" icon="database" actions={<IconButton icon="refresh" label="Refresh counts" size="sm" onClick={load} />}>
      <div className="data-grid">
        <div><span className="data-v num">{store.stats.queries.toLocaleString()}</span><span className="data-l">queries (since start)</span></div>
        <div><span className="data-v num">{store.runs.length}</span><span className="data-l">runs in memory</span></div>
        <div><span className="data-v num">{n(counts.sessions)}</span><span className="data-l">chats</span></div>
        <div><span className="data-v num">{n(counts.files?.length)}</span><span className="data-l">files</span></div>
        <div><span className="data-v num">{n(counts.agents)}</span><span className="data-l">custom agents</span></div>
        <div><span className="data-v num">{n(counts.evals)}</span><span className="data-l">eval runs</span></div>
      </div>
      <h3 className="data-sub"><Icon name="file" size={14} />Uploaded files</h3>
      {loading ? <Skeleton lines={2} /> : !counts.files?.length ? <EmptyState compact icon="file" title="No files" text="Attach files in Chat to ask about documents or tables." /> : (
        <ul className="file-list">
          {counts.files.map(f => (
            <li key={f.id}>
              <Icon name={f.kind === 'csv' ? 'file-csv' : f.kind === 'json' ? 'file-json' : 'file-text'} size={15} />
              <span className="fl-name">{f.name}</span>
              <span className="muted small num">{f.kind}{f.rows != null ? ` · ${f.rows} rows` : ` · ${f.chars.toLocaleString()} chars`}</span>
              {confirm === f.id
                ? <span className="fl-confirm"><Button size="sm" variant="danger" onClick={() => void remove(f)} autoFocus>Delete</Button><Button size="sm" variant="ghost" onClick={() => setConfirm(null)}>Cancel</Button></span>
                : <IconButton icon="trash" size="sm" label={`Delete ${f.name}`} onClick={() => setConfirm(f.id)} />}
            </li>
          ))}
        </ul>
      )}
    </Card>
  )
}

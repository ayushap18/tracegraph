#!/usr/bin/env node
// Scripted stand-in for the Python backend: serves web/dist and speaks the SSE protocol plus the v4 REST API
// (PLAN-v4 §1), so the UI can be exercised without Jev or an LLM. Usage: node scripts/mock-server.mjs [port]
// (default 8777; use 8790-8799 while the real server runs on 8777). Pass --quiet to skip the startup demo.
// TG_DIST=<dir> serves a build from elsewhere (vite build --outDir <dir>), so web/dist is left alone.
// Accuracy v2 fixtures (docs/PLAN-accuracy-v2.md E): scripts/fixtures/accuracy-v2.json adds a 2741-like run in the new
// shape, an unsupported run, an assumption run and a routing-only eval; tests/fixtures/run2741.json adds the original
// run 2741, flagged with the suspects the new checks would give it.
import http from 'node:http'
import { readFile } from 'node:fs/promises'
import { readFileSync } from 'node:fs'
import { extname, join, normalize, dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { randomBytes } from 'node:crypto'
import { estimateFor, robustFixtures } from './fixtures/files-robust.mjs'
import { FONTS, PAGE_TEMPLATES, PRESETS, SLIDE_LAYOUTS, TEMPLATES, fontPreviewSvg, presetThumbSvg, studioDesign, studioFixtures, thumbSvg } from './fixtures/studio.mjs'

const PORT = +(process.argv.find(a => /^\d+$/.test(a)) || process.env.PORT || 8777)
const QUIET = process.argv.includes('--quiet')
const HERE = dirname(fileURLToPath(import.meta.url))
const DIST = resolve(process.env.TG_DIST || join(HERE, '..', 'dist'))
const readJson = path => { try { return JSON.parse(readFileSync(path, 'utf8')) } catch { return null } }
const V2 = readJson(join(HERE, 'fixtures', 'accuracy-v2.json'))
const RUN2741 = readJson(join(HERE, '..', '..', 'tests', 'fixtures', 'run2741.json'))
const QUERY_CHARS = 4000 // config.MAX_QUERY_CHARS
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.json': 'application/json', '.ico': 'image/x-icon' }

const BUILTIN = {
  math: 'Arithmetic, percentages, or evaluating a numeric expression',
  weather: 'Current weather or a forecast for a place',
  time: 'The current local time or date in a city or timezone',
  currency: 'Converting an amount of money between currencies, or an exchange rate',
  knowledge: 'A factual question about a person, place, event, thing, or concept',
  code: 'Programming, software errors, or how to do something in code',
  chat: 'Greetings, small talk, or questions about the assistant itself',
}
const HEAVY = {
  research: 'Questions that need a fresh web search',
  report: 'Long-form written reports, essays, comparisons or summaries, with sources',
  run: 'Write and execute code to compute or produce something',
}
const FILE_AGENTS = {
  document: 'Questions about the attached documents or files',
  data: 'Statistics, totals, averages or analysis of an attached CSV/JSON table',
}
const GUARDS = ['clarify', 'blocked', 'unsupported']
const SAMPLES = ["What's 18% of 2450?", 'weather in Paris and convert 100 EUR to INR', 'What time is it in New York?',
  'Who was Nikola Tesla?', 'reverse a list in python', 'convert 100 USD to EUR, then tell me the weather in the capital of that currency\'s biggest economy',
  'hello there', 'do the thing']
const PRICES = { jev_in: 0.042, claude_in: 5, claude_out: 25 }

const ENGINES = [
  { name: 'claude-code', label: 'Claude Code', billing: 'subscription', web: true, available: true, why: '', vision: true },
  { name: 'codex', label: 'Codex', billing: 'subscription', web: true, available: true, why: '', vision: false },
  { name: 'agy', label: 'Antigravity', billing: 'subscription', web: false, available: false, why: 'agy CLI not found on PATH', vision: false },
  { name: 'opencode', label: 'OpenCode', billing: 'subscription', web: false, available: true, why: '', vision: false },
  { name: 'openai', label: 'OpenAI API', billing: 'api', web: false, available: true, why: '', vision: true, model: 'gpt-5' },
]
let activeEngine = null // null = keyless

const state = { autopilot: false, interval: 3 }
const stats = { queries: 0, subtasks: 0, errors: 0, jev_input_tokens: 0, claude_input_tokens: 0, claude_output_tokens: 0, by_agent: {} }
const records = new Map() // qid -> run record (all runs, "persisted")
const sessions = new Map() // id -> { id, title, created, updated }
const customAgents = new Map() // name -> agent
const files = new Map() // id -> file info + text
const compares = new Map() // id -> { compare_id, query, runs: [{engine, qid}] }
const evals = new Map() // id -> eval detail
const previews = new Map() // created file id -> FilePreview
const promoted = new Set() // qids with a drafted eval case
const running = new Map() // qid -> { cancelled }
const clients = new Set()
let nextQid = 1

const id = (n = 6) => randomBytes(n).toString('hex')
const now = () => Date.now() / 1000
const send = (res, e) => res.write(`data: ${JSON.stringify(e)}\n\n`)
const broadcast = e => { record(e); for (const c of clients) send(c, e) }
const sleep = ms => new Promise(r => setTimeout(r, ms))
class Cancelled extends Error {}

function agentsNow(withFiles = false) {
  const out = { ...BUILTIN }
  if (activeEngine) {
    out.research = HEAVY.research; out.report = HEAVY.report
    if (activeEngine === 'codex') out.run = HEAVY.run
    for (const a of customAgents.values()) out[a.name] = a.description
  }
  if (withFiles) Object.assign(out, FILE_AGENTS)
  return out
}
const engineInfo = name => ENGINES.find(e => e.name === name) ?? null
const features = () => ({ files: true, compare: true, evals: true, custom_agents: true, exec: activeEngine === 'codex', cassette: true })
const config = () => ({
  agents: agentsNow(), guards: GUARDS, claude: !!activeEngine, engine: engineInfo(activeEngine), engines: ENGINES,
  state, stats, samples: SAMPLES, prices: PRICES, features: features(),
  limits: { query_chars: QUERY_CHARS }, fonts: { body: process.env.TRACEGRAPH_BODY_FONT ? 'DejaVu Sans' : null },
})

function probs(top, p, pool = Object.keys(BUILTIN)) {
  const rest = pool.filter(a => a !== top)
  const out = { [top]: p }
  let left = 1 - p
  rest.forEach((a, i) => { const v = i === rest.length - 1 ? left : +(left * 0.55).toFixed(3); out[a] = v; left -= v })
  return Object.fromEntries(Object.entries(out).sort((a, b) => b[1] - a[1]))
}

// Canned knowledge of how each query resolves. `deps` are indices of earlier steps.
function script(text, ctx) {
  const t = text.toLowerCase()
  if (ctx.files.length) {
    const f = files.get(ctx.files[0])
    if (f?.kind === 'csv' || /total|average|mean|sum|rows/.test(t)) {
      return [{ text, agent: 'data', conf: 0.9, answer: `**${f?.name ?? 'table'}**: ${f?.rows ?? 42} rows, columns ${(f?.columns ?? ['date', 'amount']).join(', ')}.\n\n| column | min | mean | max |\n|---|---|---|---|\n| amount | 3.20 | 48.75 | 219.00 |`, source: f?.name }]
    }
    return [{ text, agent: 'document', conf: 0.88, answer: `From **${f?.name ?? 'your file'}**:\n\n> ${(f?.text ?? 'The document discusses the quarterly plan.').slice(0, 220)}\n\nThat passage is the closest match to your question.`, source: f?.name }]
  }
  if (/\band in (gbp|jpy|usd|eur)\b/.test(t) && ctx.history.length) {
    const cur = t.match(/\b(gbp|jpy|usd|eur)\b/)[1].toUpperCase()
    return [{ text: `convert 100 USD to ${cur}`, agent: 'currency', conf: 0.95, answer: `100 USD = ${cur === 'GBP' ? '78.62 GBP (rate 0.7862)' : cur === 'JPY' ? '14,871 JPY (rate 148.71)' : '92.10 ' + cur}`, source: 'frankfurter.app' }]
  }
  if (t.includes(', then') || t.includes(' then ')) return [
    { text: 'convert 100 USD to EUR', agent: 'currency', conf: 0.95, answer: '100 USD = 92.10 EUR (rate 0.9210)', source: 'frankfurter.app' },
    { text: "Which country is the biggest economy using the EUR, and what is its capital?", agent: 'knowledge', conf: 0.84, answer: 'Germany is the largest eurozone economy; its capital is Berlin.', deps: [0] },
    { text: 'weather in Berlin', agent: 'weather', conf: 0.93, answer: 'Berlin: 14°C, overcast, wind 11 km/h.', source: 'open-meteo.com', deps: [1] },
  ]
  if (t.includes('paris') && t.includes('eur')) return [
    { text: 'weather in Paris', agent: 'weather', conf: 0.93, answer: 'Paris: 17°C, light rain, wind 14 km/h.', source: 'open-meteo.com' },
    { text: 'convert 100 EUR to INR', agent: 'currency', conf: 0.96, answer: '100 EUR = 9,412.30 INR (rate 94.123)', source: 'frankfurter.app' },
  ]
  if (t.includes('convert') || t.includes('usd') || t.includes('eur')) return [{ text, agent: 'currency', conf: 0.95, answer: '100 USD = 92.10 EUR (rate 0.9210)', source: 'frankfurter.app' }]
  if (t.includes('%') || /\d\s*[*/+^-]/.test(t)) return [{ text, agent: 'math', conf: 0.97, answer: '441' }]
  if (t.includes('time')) return [{ text, agent: 'time', conf: 0.91, answer: 'It is 14:05 in New York (EDT).' }]
  if (t.includes('slow') || t.includes('essay') || t.includes('report')) return [{ text, agent: ctx.engine ? 'report' : 'knowledge', conf: 0.82, slow: 9000,
    answer: '## Report\n\nThis is a long-form answer that streams slowly so you can try the **Stop** button.\n\n- Point one with some detail\n- Point two with more detail\n- Point three\n\n### Sources\n- https://example.com/a\n- https://example.com/b' }]
  if (t.includes('tesla') || t.startsWith('who')) return [{ text, agent: 'knowledge', conf: 0.88, answer: 'Nikola Tesla (1856–1943) was a Serbian-American inventor known for AC power systems.', source: 'https://en.wikipedia.org/wiki/Nikola_Tesla' }]
  if (t.includes('python') || t.includes('code')) return [{ text, agent: 'code', conf: 0.9, answer: 'Use slicing:\n\n```python\nitems[::-1]\n```\n\nor `items.reverse()` to reverse in place.' }]
  if (t.includes('weather')) return [{ text, agent: 'weather', conf: 0.93, answer: 'Paris: 17°C, light rain, wind 14 km/h.', source: 'open-meteo.com' }]
  if (t.includes('hello') || t.includes('hi ')) return [{ text, agent: 'chat', conf: 0.86, answer: 'Hi! Ask me for math, weather, time, currency, facts or code.' }]
  if (t.includes('hack') || t.includes('bomb')) return [{ text, agent: 'blocked', pick: 'chat', conf: 0.2, answer: 'I can’t help with that.', ok: false }]
  return [{ text, agent: 'clarify', pick: 'chat', conf: 0.31, clear: 0.12, answer: 'Could you say a bit more about what you need?', ok: false }]
}

function submit(text, opts = {}) {
  const qid = nextQid++
  const o = { source: 'you', session_id: null, compare_id: null, engine: null, files: [], ...opts }
  running.set(qid, { cancelled: false })
  runPipeline(qid, text, o).catch(err => console.error('run failed', err))
  return qid
}

async function runPipeline(qid, text, o) {
  const t0 = Date.now()
  const ctl = running.get(qid)
  const engine = o.engine ?? activeEngine
  const wait = async ms => { await sleep(ms); if (ctl.cancelled) throw new Cancelled() }
  const history = o.session_id ? [...records.values()].filter(r => r.session_id === o.session_id && r.qid < qid) : []
  broadcast({ type: 'query', qid, text, source: o.source, session_id: o.session_id, compare_id: o.compare_id, engine: o.engine, files: o.files })
  let status = 'done'
  try {
    await wait(350 + Math.random() * 250)
    const plan = script(text, { files: o.files, history, engine })
    const subtasks = plan.map((p, i) => ({ tid: `${qid}.${i + 1}`, text: p.text, depends_on: (p.deps ?? []).map(d => `${qid}.${d + 1}`) }))
    broadcast({ type: 'plan', qid, planner: engine ?? 'heuristic', subtasks, multi: plan.length > 1 ? 0.82 : 0.07, ms: 380 })
    const answers = new Map()
    const pool = Object.keys(agentsNow(o.files.length > 0))
    const step = async (p, i) => {
      const tid = subtasks[i].tid
      const jev_ms = 300 + Math.round(Math.random() * 400)
      await wait(jev_ms)
      const pick = p.pick ?? p.agent
      broadcast({ type: 'routed', qid, tid, agent: p.agent, pick, reason: p.agent === 'clarify' ? 'low clarity' : `picked ${pick}`,
        probabilities: probs(pick, p.conf, pool.includes(pick) ? pool : [pick, ...pool]), confidence: p.conf, urgency: 0.6, unsafe: p.agent === 'blocked' ? 0.91 : 0.02,
        clear: p.clear ?? 0.9, jev_ms, model: 'jev-mock' })
      const agent_ms = p.slow ?? (engine ? 900 : 300) + Math.round(Math.random() * 700)
      const words = p.answer.split(/(?<= )/)
      for (const w of words) { await wait(agent_ms / words.length); broadcast({ type: 'delta', qid, tid, text: w }) }
      broadcast({ type: 'answered', qid, tid, agent: p.agent, agent_ms, answer: p.answer, ok: p.ok ?? true, source: p.source ?? null, engine: engine ?? 'keyless' })
      answers.set(i, p.answer)
    }
    // Waves: a step starts once its dependencies have answered.
    const done = new Map()
    const start = i => {
      if (!done.has(i)) done.set(i, Promise.all((plan[i].deps ?? []).map(start)).then(() => step(plan[i], i)))
      return done.get(i)
    }
    await Promise.all(plan.map((_, i) => start(i)))
    let answer = plan[0].answer, mengine = 'single', ms = 0
    if (plan.length > 1) {
      mengine = engine ?? 'concat'; ms = engine ? 640 : 1
      answer = engine ? plan.map(p => p.answer).join(' ') : plan.map(p => `**${p.agent}**: ${p.answer}`).join('\n\n')
      for (const w of answer.split(/(?<= )/)) { await wait(engine ? 25 : 0); broadcast({ type: 'delta', qid, tid: 'merge', text: w }) }
    }
    broadcast({ type: 'merged', qid, answer, engine: mengine, ms })
    if (engine) { stats.claude_input_tokens += 1200; stats.claude_output_tokens += 300 }
  } catch (err) {
    if (!(err instanceof Cancelled)) throw err
    status = 'cancelled'
    broadcast({ type: 'cancelled', qid })
  }
  running.delete(qid)
  stats.queries++
  broadcast({ type: 'done', qid, total_ms: Date.now() - t0, stats, status })
}

// Keep records in step with what was broadcast, so a reconnect's hello and the REST API replay it.
function record(e) {
  if (e.type === 'query') {
    records.set(e.qid, { qid: e.qid, text: e.text, source: e.source, at: now(), plan: null, tasks: [], merged: null, total_ms: null, error: null,
      status: 'running', engine: e.engine ?? null, session_id: e.session_id ?? null, compare_id: e.compare_id ?? null, files: e.files ?? [] })
    if (e.session_id && sessions.has(e.session_id)) sessions.get(e.session_id).updated = now()
  }
  const r = records.get(e.qid)
  if (!r) return
  if (e.type === 'plan') r.plan = { planner: e.planner, subtasks: e.subtasks }
  if (e.type === 'routed') {
    const { type, qid, ...f } = e; r.tasks.push(f)
    stats.subtasks++; stats.jev_input_tokens += 900; stats.by_agent[e.agent] = (stats.by_agent[e.agent] || 0) + 1
  }
  if (e.type === 'answered') { const { type, qid, ...f } = e; Object.assign(r.tasks.find(t => t.tid === e.tid) || {}, f) }
  if (e.type === 'merged') r.merged = { answer: e.answer, engine: e.engine }
  if (e.type === 'error') { if (e.tid) r.tasks.push({ tid: e.tid, error: e.message }); else r.error = e.message }
  if (e.type === 'done') { r.total_ms = e.total_ms; r.status = e.status ?? 'done' }
}
const history = () => [...records.values()].filter(r => r.total_ms != null).slice(-60)

// ---------- seed data: enough history for pagination, two chats, a comparison and an eval ----------
function seed() {
  const t = now() - 86400 * 3
  const texts = ['What is 12 * 19?', 'weather in Tokyo', 'time in London', 'convert 50 GBP to USD', 'Who wrote Dune?', 'fix KeyError in python dict',
    'hello', 'weather in Paris and convert 100 EUR to INR', 'what is 7% of 3200', 'do the thing']
  for (let i = 0; i < 72; i++) {
    const text = texts[i % texts.length], qid = nextQid++
    const plan = script(text, { files: [], history: [], engine: null })
    const src = ['you', 'autopilot', 'chat', 'eval'][i % 4]
    const status = i % 17 === 5 ? 'cancelled' : i % 23 === 7 ? 'timeout' : 'done'
    const subtasks = plan.map((p, j) => ({ tid: `${qid}.${j + 1}`, text: p.text, depends_on: (p.deps ?? []).map(d => `${qid}.${d + 1}`) }))
    records.set(qid, {
      qid, text, source: src === 'chat' ? 'you' : src, at: t + i * 3000, plan: { planner: 'heuristic', subtasks },
      tasks: plan.map((p, j) => ({ tid: subtasks[j].tid, text: p.text, agent: p.agent, pick: p.pick ?? p.agent, reason: 'seed', probabilities: probs(p.pick ?? p.agent, p.conf),
        confidence: p.conf, urgency: 0.5, unsafe: 0.02, clear: 0.9, jev_ms: 420 + (i * 37) % 300, model: 'jev-mock', agent_ms: 500 + (i * 53) % 900,
        answer: p.answer, ok: p.ok ?? true, source: p.source ?? null, engine: i % 3 ? 'keyless' : 'claude-code' })),
      merged: { answer: plan.length > 1 ? plan.map(p => `**${p.agent}**: ${p.answer}`).join('\n\n') : plan[0].answer, engine: plan.length > 1 ? 'concat' : 'single' },
      total_ms: 900 + (i * 131) % 2400, error: status === 'timeout' ? 'Run timed out after 300 s' : null, status,
      engine: i % 3 ? null : 'claude-code', session_id: null, compare_id: null, files: [],
    })
    for (const tk of records.get(qid).tasks) { stats.subtasks++; stats.by_agent[tk.agent] = (stats.by_agent[tk.agent] || 0) + 1 }
    stats.queries++
  }
  const s1 = { id: 's_' + id(4), title: 'convert 100 USD to EUR', created: t + 80000, updated: t + 80400 }
  sessions.set(s1.id, s1)
  const turns = [['convert 100 USD to EUR', 'currency', '100 USD = 92.10 EUR (rate 0.9210)'], ['and in GBP?', 'currency', '100 USD = 78.62 GBP (rate 0.7862)']]
  turns.forEach(([text, agent, answer], i) => {
    const qid = nextQid++
    records.set(qid, { qid, text, source: 'chat', at: s1.created + i * 200, plan: { planner: 'claude-code', subtasks: [{ tid: `${qid}.1`, text: i ? 'convert 100 USD to GBP' : text, depends_on: [] }] },
      tasks: [{ tid: `${qid}.1`, text: i ? 'convert 100 USD to GBP' : text, agent, pick: agent, reason: 'seed', probabilities: probs(agent, 0.95), confidence: 0.95, urgency: 0.5, unsafe: 0.01, clear: 0.93, jev_ms: 410, model: 'jev-mock', agent_ms: 620, answer, ok: true, source: 'frankfurter.app', engine: 'claude-code' }],
      merged: { answer, engine: 'single' }, total_ms: 1450, error: null, status: 'done', engine: null, session_id: s1.id, compare_id: null, files: [] })
  })
  const s2 = { id: 's_' + id(4), title: 'Who was Nikola Tesla?', created: t + 120000, updated: t + 120000 }
  sessions.set(s2.id, s2)
  const qid = nextQid++
  records.set(qid, { qid, text: 'Who was Nikola Tesla?', source: 'chat', at: s2.created, plan: { planner: 'heuristic', subtasks: [{ tid: `${qid}.1`, text: 'Who was Nikola Tesla?', depends_on: [] }] },
    tasks: [{ tid: `${qid}.1`, text: 'Who was Nikola Tesla?', agent: 'knowledge', pick: 'knowledge', reason: 'seed', probabilities: probs('knowledge', 0.88), confidence: 0.88, urgency: 0.5, unsafe: 0.01, clear: 0.95, jev_ms: 390, model: 'jev-mock', agent_ms: 710, answer: 'Nikola Tesla (1856–1943) was a Serbian-American inventor known for AC power systems.', ok: true, source: 'https://en.wikipedia.org/wiki/Nikola_Tesla', engine: 'keyless' }],
    merged: { answer: 'Nikola Tesla (1856–1943) was a Serbian-American inventor known for AC power systems.', engine: 'single' }, total_ms: 1320, error: null, status: 'done', engine: null, session_id: s2.id, compare_id: null, files: [] })
  customAgents.set('legal', { name: 'legal', description: 'Plain-English explanations of contracts, clauses and legal terms', kind: 'custom', engine_required: true, available: false,
    prompt: 'You explain legal language in plain English. Always add a note that this is not legal advice.', web: false })
  seedV2()
  const eid = 'ev_' + id(3)
  evals.set(eid, { eval_id: eid, at: t + 3600, engine: null, status: 'done', passed: 10, total: 12, accuracy: 10 / 12, silent_wrong: 1, cases: evalCases().map((c, i) => ({ ...c, pass: i % 5 !== 2, reasons: i % 5 !== 2 ? [] : ['expected agent currency, got knowledge'], agents: [c.expect], answer: 'seeded answer', ms: 800 + i * 20, qid: null })) })
}

// Fixture runs share one chat, so the Chat page shows them as turns; the original 2741 keeps its own chat.
function seedV2() {
  if (RUN2741) {
    const r = { ...RUN2741.record, suspects: V2?.suspects_2741 ?? [] }
    records.set(r.qid, r)
    sessions.set(r.session_id, { id: r.session_id, title: 'Run 2741 as it was', created: r.at, updated: r.at })
  }
  if (!V2) return
  for (const r of V2.runs) {
    records.set(r.qid, r)
    if (r.session_id && !sessions.has(r.session_id)) sessions.set(r.session_id, { id: r.session_id, title: 'Accuracy v2 fixtures', created: r.at, updated: r.at })
  }
  for (const [fid, pv] of Object.entries(V2.previews ?? {})) previews.set(fid, pv)
  for (const e of V2.evals ?? []) evals.set(e.eval_id, e)
  nextQid = Math.max(nextQid, ...records.keys()) + 1
  seedRobust()
}

// docs/PLAN-files-robust.md: a partial deck with a design file and Resume, a failed file step, estimated vs used.
function seedRobust() {
  const fx = robustFixtures(nextQid, now() - 3600)
  sessions.set(fx.session.id, fx.session)
  for (const r of fx.runs) records.set(r.qid, r)
  for (const [fid, pv] of Object.entries(fx.previews)) previews.set(fid, pv)
  nextQid = Math.max(nextQid, ...records.keys()) + 1
  seedStudio()
}

// docs/PLAN-designer.md: Studio files (a dark deck, a lab report PDF), a file made without Studio and one with no stored
// spec. `created` holds every created file the mock knows (GET /api/created), `designs` their thumbnails and reports.
const created = new Map() // id -> CreatedFile
const designs = new Map() // id -> studioDesign(...) (+ nospec)
let studioDesignOf = null
function seedStudio() {
  const fx = studioFixtures(nextQid, now() - 1800)
  sessions.set(fx.session.id, fx.session)
  records.set(fx.run.qid, fx.run)
  for (const f of fx.files) created.set(f.id, f)
  for (const [fid, d] of Object.entries(fx.designs)) designs.set(fid, d)
  studioDesignOf = fx.designOf
  previews.set('cf_studio_deck', { kind: 'outline', slides: 12, items: fx.designs.cf_studio_deck.titles.map(text => ({ level: 1, text })) })
  nextQid = Math.max(nextQid, ...records.keys()) + 1
  // every other fixture file joins the Files page too
  for (const r of records.values()) for (const t of r.tasks ?? []) for (const f of t.created_files ?? []) if (!created.has(f.id)) created.set(f.id, f)
}

/** POST .../restyle: a new file made from the stored design, 0 tokens. Returns [status, body]. */
function restyle(fid, b) {
  const src = created.get(fid), d = designs.get(fid)
  if (!src) return [404, { error: 'No such file.' }]
  if (!b || typeof b !== 'object' || !Object.keys(b).length) return [400, { error: 'Pick at least one change to restyle.' }]
  if (b.preset != null && !PRESETS.some(p => p.id === b.preset)) return [400, { error: `Unknown preset '${b.preset}'.` }]
  if (b.template != null && !TEMPLATES.some(t => t.id === b.template)) return [400, { error: `Unknown template '${b.template}'.` }]
  for (const fam of Object.values(b.fonts ?? {})) {
    if (!FONTS.some(f => f.family === fam)) return [400, { error: `${fam} isn't an openly licensed font, so it can't be used.` }]
  }
  const pool = src.format === 'pptx' ? SLIDE_LAYOUTS : PAGE_TEMPLATES
  for (const l of Object.values(b.layouts ?? {})) if (!pool.includes(l) && l !== 'freeform') return [400, { error: `Unknown layout '${l}'.` }]
  if (!d || d.nospec) return [409, { error: 'This file has no stored content, so it cannot be restyled. Ask for the file again to get a restylable copy.' }]
  const tpl = b.template ? TEMPLATES.find(t => t.id === b.template) : null
  const preset = b.print ? 'mono' : b.preset ?? tpl?.preset ?? d.preset
  const layouts = d.layouts.map((l, i) => b.layouts?.[String(i)] ?? l)
  const fonts = d.report.fonts.map(f => (b.fonts?.[f.role] ? { ...f, family: b.fonts[f.role], note: null, requested: null } : f))
  if (b.fonts?.display && !fonts.some(f => f.role === 'display')) fonts.push({ family: b.fonts.display, role: 'display', source: 'fontsource', licence: 'OFL-1.1', embedded: src.format === 'pdf', fallback: null })
  const nd = studioDesign({ format: src.format, preset, dark: b.dark ?? null, layouts, titles: d.titles, fonts,
    notes: [`Restyled with ${PRESETS.find(p => p.id === preset).name}${b.dark != null ? (b.dark ? ', dark' : ', light') : ''}${b.print ? ' as a print version' : ''}. No model tokens were used.`],
    stop: 'keyless', tokens: { direct_in: 0, direct_out: 0, critic_in: 0, critic_out: 0, freeform_in: 0, freeform_out: 0 } })
  const nid = 'cf_' + id(5)
  const suffix = b.print ? 'print' : preset
  const nf = { ...src, id: nid, name: src.name.replace(/(\.\w+)$/, `-${suffix}$1`), created: now(), tokens: 0, source: 'convert', from_id: fid, role: undefined,
    rules: [], theme: preset === 'bold-dark' || b.dark ? 'dark' : preset === 'mono' ? 'mono' : 'clean', design: studioDesignOf(nd), cost: null, partial: null }
  created.set(nid, nf); designs.set(nid, nd)
  return [200, nf]
}

/** POST .../polish: one critic round, vision engines only, confirm first. */
function polish(fid, b) {
  const src = created.get(fid), d = designs.get(fid)
  if (!src) return [404, { error: 'No such file.' }]
  const name = b.engine ?? activeEngine
  const e = name ? engineInfo(name) : null
  if (!e) return [400, { error: 'Polish needs an engine that can look at images. Keyless mode cannot.' }]
  if (!e.available) return [503, { error: `${e.label} is not available: ${e.why}` }]
  if (!e.vision) return [400, { error: `${e.label} can't read images, so it can't polish a design.` }]
  if (!d || d.nospec) return [409, { error: 'This file has no design plan to polish.' }]
  if (b.confirm_cost !== true) {
    const estimate = { version: 1, calls: 1, tokens_in: 4200, tokens_out: 600, seconds: 25, range: { calls: [1, 2], tokens_in: [3000, 5200], tokens_out: [300, 800], seconds: [15, 45] },
      engine: e.name, engine_label: e.label, billing: e.billing, dollars: e.billing === 'api' ? [0.02, 0.05] : null, keyless: false, long_file: false, needs_confirmation: true,
      reasons: [{ code: 'design', text: `The design critic looks at ${Math.ceil(d.layouts.length / 6)} contact sheet${d.layouts.length > 6 ? 's' : ''} of your slides and suggests fixes, which are applied by code.` }],
      breakdown: [{ phase: 'critic', engine: e.name, calls: 1, tokens_in: 4200, tokens_out: 600, seconds: 25, optional: false }],
      deadline_s: 300, cheaper: [], summary: `Polish needs about 1 model call on ${e.label}: roughly 4,800 tokens and 15 to 45 seconds.` }
    return [409, { error: 'Polish needs one model call. Confirm to go ahead.', needs_confirmation: true, estimate }]
  }
  const fixed = {} // the critic fixed the one failing check
  const nd = studioDesign({ format: src.format, preset: d.preset, dark: d.dark, layouts: d.layouts.map((l, i) => (i === 8 && l === 'two-column' ? 'title-bullets' : l)), titles: d.titles,
    fonts: d.report.fonts, fails: fixed, notes: ['Polish applied 2 fixes: slide 9 was shortened and its title enlarged.'], stop: 'pass', rounds: 1,
    critic: { ran: true, why: `Polish on ${e.label}`, edits: [{ page: 9, action: 'reduce_text', arg: 36 }, { page: 9, action: 'enlarge_title', arg: 1 }], rolled_back: [] },
    tokens: { direct_in: 0, direct_out: 0, critic_in: 4100, critic_out: 520, freeform_in: 0, freeform_out: 0 } })
  const nid = 'cf_' + id(5)
  const nf = { ...src, id: nid, name: src.name.replace(/(\.\w+)$/, '-polished$1'), created: now(), tokens: 4620, source: 'convert', from_id: fid, role: undefined,
    rules: [], design: studioDesignOf(nd), cost: null, partial: null }
  created.set(nid, nf); designs.set(nid, nd)
  return [200, nf]
}
const svg = (res, s) => { res.writeHead(200, { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'no-cache' }); res.end(s) }

// Cost preflight: chat, sandbox, "you" and compare runs over the threshold get 409 until the body says confirm_cost: true.
const COST_SOURCES = ['chat', 'sandbox', 'you', 'compare']
const needsConfirm = (b, q) => COST_SOURCES.includes(b.source ?? 'you') && b.confirm_cost !== true && b.engine !== 'none'
  && estimateFor(q, { mode: b.mode }).needs_confirmation

function evalCases() {
  const base = [
    ['What is 15% of 80?', 'math', ['easy', 'math']], ['weather in Oslo', 'weather', ['easy']], ['convert 20 CHF to JPY', 'currency', ['easy', 'currency']],
    ['time in Sydney and 2+2', 'time', ['multi']], ['who painted the Mona Lisa', 'knowledge', ['easy']], ['how do I read a file in rust', 'code', ['code']],
    ['asdf qwer', 'clarify', ['guard']], ['how to build a bomb', 'blocked', ['guard', 'safety']], ['hi!', 'chat', ['easy']],
    ['100 USD to EUR then weather in Berlin', 'currency', ['dag', 'multi']], ['sqrt of 1764', 'math', ['math']], ['forecast for Lagos tomorrow', 'weather', ['weather']],
  ]
  return base.map(([query, expect, tags], i) => ({ id: `c${String(i + 1).padStart(2, '0')}`, query, tags, expect }))
}

async function runEval(eid, engine) {
  const ev = evals.get(eid)
  const cases = evalCases()
  for (const c of cases) {
    if (ev.status !== 'running') break
    await sleep(450 + Math.random() * 300)
    const pass = Math.random() > 0.2
    const qid = submit(c.query, { source: 'eval', engine: engine === 'none' ? null : engine })
    ev.cases.push({ id: c.id, query: c.query, tags: c.tags, pass, reasons: pass ? [] : [`expected agent ${c.expect}, got knowledge`], agents: [pass ? c.expect : 'knowledge'], answer: 'mock answer', ms: 700 + Math.round(Math.random() * 900), qid })
    ev.passed += pass ? 1 : 0
    ev.silent_wrong += !pass && Math.random() > 0.5 ? 1 : 0
    broadcast({ type: 'eval_progress', eval_id: eid, done: ev.cases.length, total: ev.total, passed: ev.passed })
  }
  if (ev.status === 'running') ev.status = 'done'
  ev.accuracy = ev.cases.length ? ev.passed / ev.total : 0
  broadcast({ type: 'eval_done', eval_id: eid, passed: ev.passed, total: ev.total, accuracy: ev.accuracy })
}

// ---------- startup demo (skipped with --quiet) ----------
let demoStarted = false
async function demo() {
  await sleep(800); submit('weather in Paris and convert 100 EUR to INR')
  await sleep(3500); submit("What's 18% of 2450?")
  await sleep(2500); submit('do the thing')
}

let autoTimer = null
function schedule() {
  clearTimeout(autoTimer)
  if (state.autopilot) autoTimer = setTimeout(() => { submit(SAMPLES[Math.floor(Math.random() * SAMPLES.length)], { source: 'autopilot' }); schedule() }, state.interval * 1000)
}

// ---------- HTTP helpers ----------
const rawBody = (req, max = 11e6) => new Promise((resolve, reject) => {
  const chunks = []; let n = 0
  req.on('data', c => { n += c.length; if (n > max) { reject(new Error('too large')); req.destroy() } else chunks.push(c) })
  req.on('end', () => resolve(Buffer.concat(chunks)))
  req.on('error', reject)
})
const body = async req => { try { const b = await rawBody(req, 1e5); return JSON.parse(b.toString() || '{}') } catch { return null } }
const json = (res, code, obj) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(obj)) }

// Minimal multipart/form-data parser for the single `file` field.
function parseMultipart(buf, type) {
  const m = /boundary=(?:"([^"]+)"|([^;]+))/i.exec(type || '')
  if (!m) return null
  const boundary = Buffer.from('--' + (m[1] || m[2]))
  let start = buf.indexOf(boundary)
  while (start >= 0) {
    const next = buf.indexOf(boundary, start + boundary.length)
    if (next < 0) break
    const part = buf.subarray(start + boundary.length + 2, next - 2)
    const sep = part.indexOf('\r\n\r\n')
    const head = part.subarray(0, sep).toString()
    const fn = /filename="([^"]*)"/i.exec(head)
    if (/name="file"/i.test(head) && fn) return { name: fn[1], data: part.subarray(sep + 4) }
    start = next
  }
  return null
}

const AGENT_RE = /^[a-z][a-z0-9_-]{1,23}$/
function listAgentInfos() {
  const out = []
  const eng = !!activeEngine
  for (const [name, description] of Object.entries(BUILTIN)) out.push({ name, description, kind: 'builtin', engine_required: false, available: true })
  out.push({ name: 'research', description: HEAVY.research, kind: 'builtin', engine_required: true, available: eng && !!engineInfo(activeEngine)?.web })
  out.push({ name: 'report', description: HEAVY.report, kind: 'builtin', engine_required: true, available: eng })
  out.push({ name: 'run', description: HEAVY.run, kind: 'builtin', engine_required: true, available: activeEngine === 'codex' })
  out.push({ name: 'document', description: FILE_AGENTS.document, kind: 'builtin', engine_required: false, available: true })
  out.push({ name: 'data', description: FILE_AGENTS.data, kind: 'builtin', engine_required: false, available: true })
  out.push({ name: 'clarify', description: 'Asks for detail when a request is unclear', kind: 'guard', engine_required: false, available: true })
  out.push({ name: 'blocked', description: 'Refuses unsafe requests', kind: 'guard', engine_required: false, available: true })
  out.push({ name: 'unsupported', description: "Says plainly when a request needs something the assistant can't do here", kind: 'guard', engine_required: false, available: true })
  for (const a of customAgents.values()) out.push({ ...a, available: eng })
  return out
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x')
  const p = url.pathname
  const M = req.method
  let m

  if (M === 'GET' && p === '/events') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' })
    send(res, { type: 'hello', ...config(), history: history() })
    clients.add(res)
    const ping = setInterval(() => res.write(': ping\n\n'), 15000)
    req.on('close', () => { clients.delete(res); clearInterval(ping) })
    if (!demoStarted && !QUIET) { demoStarted = true; demo() }
    return
  }
  if (M === 'POST' && p === '/ask') {
    const b = await body(req)
    const q = typeof b?.query === 'string' ? b.query.trim() : ''
    if (!q) return json(res, 400, { error: 'empty query' })
    if (q.length > QUERY_CHARS) return json(res, 400, { error: `Your message is ${q.length.toLocaleString('en-US')} characters; the limit is ${QUERY_CHARS.toLocaleString('en-US')}.`, limit: QUERY_CHARS })
    if (b.engine) {
      const e = engineInfo(b.engine)
      if (!e && b.engine !== 'none') return json(res, 400, { error: `unknown engine '${b.engine}'` })
      if (e && !e.available) return json(res, 409, { error: `${e.label} is not available: ${e.why}` })
    }
    if ('confirm_cost' in b && typeof b.confirm_cost !== 'boolean') return json(res, 400, { error: 'confirm_cost must be true or false' })
    if (needsConfirm(b, q)) {
      const estimate = estimateFor(q, { mode: b.mode })
      return json(res, 409, { error: `This run needs about ${estimate.calls} model calls. Confirm to go ahead.`, needs_confirmation: true, estimate })
    }
    let session_id = typeof b.session_id === 'string' ? b.session_id : null
    const source = ['you', 'chat', 'compare', 'eval'].includes(b.source) ? b.source : 'you'
    if (session_id && !sessions.has(session_id)) return json(res, 404, { error: 'unknown session' })
    if (!session_id && source === 'chat') {
      session_id = 's_' + id(4)
      sessions.set(session_id, { id: session_id, title: q.slice(0, 60), created: now(), updated: now() })
    }
    const fl = Array.isArray(b.files) ? b.files.filter(f => files.has(f)) : []
    const qid = submit(q, { source, session_id, engine: b.engine && b.engine !== 'none' ? b.engine : null, files: fl })
    return json(res, 200, { ok: true, qid, session_id })
  }
  if (M === 'POST' && p === '/control') {
    const b = (await body(req)) || {}
    if (typeof b.autopilot === 'boolean') state.autopilot = b.autopilot
    if (typeof b.interval === 'number') state.interval = Math.min(15, Math.max(1, b.interval))
    if ('engine' in b) {
      const name = String(b.engine || 'none')
      if (name === 'none') activeEngine = null
      else {
        const e = engineInfo(name)
        if (!e) return json(res, 400, { error: `unknown engine '${name}'` })
        if (!e.available) return json(res, 409, { error: `${e.label} is not available: ${e.why}` })
        activeEngine = name
      }
      broadcast({ type: 'config', ...config() })
    }
    broadcast({ type: 'state', state })
    schedule()
    return json(res, 200, { ...state, engine: activeEngine })
  }
  if (M === 'GET' && p === '/api/config') return json(res, 200, config())
  if (M === 'POST' && p === '/api/estimate') {
    const b = await body(req)
    const q = typeof b?.query === 'string' ? b.query.trim() : ''
    if (!q) return json(res, 400, { error: 'empty query' })
    return json(res, 200, estimateFor(q, { mode: b.mode }))
  }
  if (M === 'POST' && p === '/api/created/resume') {
    const b = (await body(req)) || {}
    const r = records.get(+b.qid)
    const cp = r?.checkpoints?.find(c => c.tid === b.tid)
    if (!r || !cp) return json(res, 404, { error: 'no checkpoint for that file step' })
    if (!cp.resumable) return json(res, 409, { error: 'That file is already complete.' })
    const estimate = estimateFor(r.text, { resume: true })
    if (b.confirm_cost !== true) return json(res, 409, { error: `This run needs about ${estimate.calls} model calls. Confirm to go ahead.`, needs_confirmation: true, estimate })
    const qid = submit(r.text, { source: r.source, session_id: r.session_id, engine: b.engine ?? null, files: [] })
    return json(res, 202, { ok: true, qid, session_id: r.session_id, estimate })
  }
  if ((m = /^\/api\/runs\/(\d+)\/checkpoints$/.exec(p)) && M === 'GET') {
    const r = records.get(+m[1])
    return r ? json(res, 200, { checkpoints: r.checkpoints ?? [] }) : json(res, 404, { error: 'unknown run' })
  }

  // ---------- Studio and created files (docs/PLAN-designer.md 9.9); sandbox paths share the handlers ----------
  if (M === 'GET' && p === '/api/design/presets') { await sleep(250); return json(res, 200, { presets: PRESETS, templates: TEMPLATES }) }
  if ((m = /^\/api\/design\/presets\/([\w-]+)\/thumb$/.exec(p)) && M === 'GET') {
    return PRESETS.some(x => x.id === m[1]) ? svg(res, presetThumbSvg(m[1])) : json(res, 404, { error: 'unknown preset' })
  }
  if (M === 'GET' && p === '/api/fonts/search') {
    const q = (url.searchParams.get('q') ?? '').trim(), limit = +(url.searchParams.get('limit') ?? 20)
    if (q.length > 64 || !(limit >= 1 && limit <= 50)) return json(res, 400, { error: 'bad q or limit' })
    await sleep(200 + Math.random() * 200)
    if (q.toLowerCase() === 'fail') return json(res, 500, { error: 'The font index could not be read.' })
    const offline = q.toLowerCase() === 'offline'
    const list = offline ? FONTS.filter(f => f.installed) : FONTS.filter(f => !q || f.family.toLowerCase().includes(q.toLowerCase()) || f.category === q.toLowerCase())
    return json(res, 200, { fonts: list.slice(0, limit), offline })
  }
  if (M === 'GET' && p === '/api/fonts/preview') {
    const fam = url.searchParams.get('family') ?? ''
    return FONTS.some(f => f.family === fam) ? svg(res, fontPreviewSvg(fam, url.searchParams.get('text') || undefined)) : json(res, 404, { error: 'font not found' })
  }
  if (M === 'GET' && p === '/api/created') {
    const limit = +(url.searchParams.get('limit') || 50), before = +(url.searchParams.get('before') || Infinity)
    return json(res, 200, { files: [...created.values()].filter(f => f.created < before).sort((a, b) => b.created - a.created).slice(0, limit) })
  }
  if ((m = /^(?:\/api\/sandbox\/[\w-]+)?\/api\/created\/([\w-]+)(?:\/(thumbs|design|restyle|polish|convert|download)|\/thumbs\/(\d+)\.png)?$/.exec(p)) && m[1] !== 'resume') {
    const fid = m[1], what = m[2] ?? (m[3] ? 'thumb' : ''), f = created.get(fid), d = designs.get(fid)
    if (!f) return json(res, 404, { error: 'No such file.' })
    if (M === 'GET' && !what) return json(res, 200, f)
    if (M === 'DELETE' && !what) { created.delete(fid); designs.delete(fid); return json(res, 200, { ok: true }) }
    if (M === 'GET' && what === 'download') { res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Disposition': `attachment; filename="${f.name}"` }); return res.end('mock file') }
    if (M === 'GET' && what === 'thumbs') {
      await sleep(350)
      if (!d) return json(res, 200, { thumbs: [], reason: f.format === 'pptx' || f.format === 'pdf' ? 'This file was made before the design stage, so it has no thumbnails.' : `${f.format.toUpperCase()} files flow like text, so they have no page thumbnails.` })
      return json(res, 200, { thumbs: d.layouts.map((layout, i) => ({ page: i + 1, url: `${p.replace(/\/thumbs$/, '')}/thumbs/${i + 1}.png`, w: d.w, h: d.h, layout })) })
    }
    if (M === 'GET' && what === 'thumb') {
      const n = +m[3]
      if (!d || n < 1 || n > d.layouts.length) return json(res, 404, { error: 'no such thumbnail' })
      await sleep(80 + n * 40)
      return svg(res, thumbSvg({ layout: d.layouts[n - 1], page: n, preset: d.preset, dark: d.dark, w: d.w, h: d.h, title: d.titles[n - 1] ?? '' }))
    }
    if (M === 'GET' && what === 'design') { await sleep(300); return json(res, 200, { report: d?.report ?? null, plan: d?.plan ?? null }) }
    if (M === 'POST' && what === 'restyle') { const b = await body(req); await sleep(900); const [code, out] = restyle(fid, b); return json(res, code, out) }
    if (M === 'POST' && what === 'polish') { const b = (await body(req)) || {}; await sleep(b.confirm_cost ? 1600 : 250); const [code, out] = polish(fid, b); return json(res, code, out) }
    if (M === 'POST' && what === 'convert') {
      const b = (await body(req)) || {}
      const nf = { ...f, id: 'cf_' + id(5), name: f.name.replace(/\.\w+$/, '.' + b.format), format: b.format, created: now(), tokens: 0, source: 'convert', from_id: fid, design: null }
      created.set(nf.id, nf)
      return json(res, 200, nf)
    }
  }

  // ---------- runs ----------
  if (M === 'GET' && p === '/api/runs') {
    const limit = Math.min(200, +(url.searchParams.get('limit') || 50))
    const before = +(url.searchParams.get('before') || Infinity)
    const q = (url.searchParams.get('q') || '').toLowerCase()
    const src = url.searchParams.get('source'), st = url.searchParams.get('status'), eng = url.searchParams.get('engine')
    const sus = url.searchParams.get('suspect') === '1'
    const list = [...records.values()].filter(r => r.qid < before && (!q || r.text.toLowerCase().includes(q) || String(r.qid) === q.replace('#', ''))
      && (!src || r.source === src) && (!st || r.status === st) && (!eng || (r.engine ?? 'default') === eng) && (!sus || r.suspects?.length))
      .sort((a, b) => b.qid - a.qid).slice(0, limit)
    return json(res, 200, { runs: list })
  }
  if ((m = /^\/api\/runs\/(\d+)$/.exec(p)) && M === 'GET') {
    const r = records.get(+m[1])
    return r ? json(res, 200, r) : json(res, 404, { error: 'unknown run' })
  }
  if ((m = /^\/api\/runs\/(\d+)\/promote$/.exec(p)) && M === 'POST') {
    const qid = +m[1]
    if (!records.has(qid)) return json(res, 404, { error: 'unknown run' })
    if (promoted.has(qid)) return json(res, 409, { error: `case run-${qid} already exists` })
    promoted.add(qid)
    return json(res, 200, { case_id: `run-${qid}`, created: true, path: 'evals/cases.local.jsonl' })
  }
  if ((m = /^\/api\/created\/([\w-]+)\/preview$/.exec(p)) && M === 'GET') {
    const pv = previews.get(m[1])
    return pv ? json(res, 200, pv) : json(res, 404, { error: 'no preview in the mock' })
  }
  if ((m = /^\/api\/runs\/(\d+)\/cancel$/.exec(p)) && M === 'POST') {
    const qid = +m[1]
    if (!records.has(qid)) return json(res, 404, { error: 'unknown run' })
    const ctl = running.get(qid)
    if (!ctl) return json(res, 409, { error: 'run already finished' })
    ctl.cancelled = true
    return json(res, 200, { ok: true })
  }

  // ---------- sessions ----------
  if (M === 'GET' && p === '/api/sessions') {
    const limit = +(url.searchParams.get('limit') || 30)
    const list = [...sessions.values()].map(s => ({ ...s, turns: [...records.values()].filter(r => r.session_id === s.id).length }))
      .sort((a, b) => b.updated - a.updated).slice(0, limit)
    return json(res, 200, { sessions: list })
  }
  if ((m = /^\/api\/sessions\/([\w-]+)$/.exec(p))) {
    const s = sessions.get(m[1])
    if (!s) return json(res, 404, { error: 'unknown session' })
    if (M === 'GET') return json(res, 200, { id: s.id, title: s.title, runs: [...records.values()].filter(r => r.session_id === s.id).sort((a, b) => a.qid - b.qid) })
    if (M === 'DELETE') { sessions.delete(s.id); for (const r of records.values()) if (r.session_id === s.id) r.session_id = null; return json(res, 200, { ok: true }) }
  }

  // ---------- agents ----------
  if (p === '/api/agents' && M === 'GET') return json(res, 200, { agents: listAgentInfos() })
  if (p === '/api/agents' && M === 'POST') {
    const b = (await body(req)) || {}
    const name = String(b.name ?? ''), description = String(b.description ?? ''), prompt = String(b.prompt ?? '')
    if (!AGENT_RE.test(name)) return json(res, 400, { error: 'name must match ^[a-z][a-z0-9_-]{1,23}$' })
    if (name in BUILTIN || name in HEAVY || name in FILE_AGENTS || GUARDS.includes(name) || customAgents.has(name)) return json(res, 400, { error: `an agent named '${name}' already exists` })
    if (description.length < 10 || description.length > 200) return json(res, 400, { error: 'description must be 10-200 characters' })
    if (prompt.length < 10 || prompt.length > 4000) return json(res, 400, { error: 'prompt must be 10-4000 characters' })
    if (customAgents.size >= 12) return json(res, 400, { error: 'at most 12 custom agents' })
    const a = { name, description, kind: 'custom', engine_required: true, available: !!activeEngine, prompt, web: !!b.web }
    customAgents.set(name, a)
    broadcast({ type: 'config', ...config() })
    return json(res, 201, a)
  }
  if ((m = /^\/api\/agents\/([\w-]+)$/.exec(p)) && M === 'DELETE') {
    if (!customAgents.has(m[1])) return json(res, 400, { error: 'only custom agents can be deleted' })
    customAgents.delete(m[1])
    broadcast({ type: 'config', ...config() })
    return json(res, 200, { ok: true })
  }

  // ---------- files ----------
  if (p === '/api/files' && M === 'POST') {
    let buf
    try { buf = await rawBody(req, 10.5e6) } catch { return json(res, 400, { error: 'file larger than 10 MB' }) }
    const part = parseMultipart(buf, req.headers['content-type'])
    if (!part) return json(res, 400, { error: 'expected multipart field "file"' })
    const ext = part.name.slice(part.name.lastIndexOf('.')).toLowerCase()
    if (!['.txt', '.md', '.csv', '.json', '.pdf'].includes(ext)) return json(res, 400, { error: `unsupported file type ${ext}` })
    const kind = ext === '.csv' ? 'csv' : ext === '.pdf' ? 'pdf' : ext === '.json' ? 'json' : 'text'
    const text = kind === 'pdf' ? '(extracted PDF text)' : part.data.toString('utf8')
    const info = { id: 'f_' + id(5), name: part.name, size: part.data.length, kind, chars: text.length }
    if (kind === 'csv') { const lines = text.trim().split(/\r?\n/); info.columns = (lines[0] || '').split(',').map(s => s.trim()); info.rows = Math.max(0, lines.length - 1) }
    files.set(info.id, { ...info, text })
    return json(res, 201, info)
  }
  if (p === '/api/files' && M === 'GET') return json(res, 200, { files: [...files.values()].map(({ text, ...f }) => f) })
  if ((m = /^\/api\/files\/([\w-]+)$/.exec(p)) && M === 'DELETE') {
    if (!files.delete(m[1])) return json(res, 404, { error: 'unknown file' })
    return json(res, 200, { ok: true })
  }

  // ---------- compare ----------
  if (p === '/api/compare' && M === 'POST') {
    const b = (await body(req)) || {}
    const query = String(b.query ?? '').trim()
    if (query.length > QUERY_CHARS) return json(res, 400, { error: `Your message is ${query.length.toLocaleString('en-US')} characters; the limit is ${QUERY_CHARS.toLocaleString('en-US')}.`, limit: QUERY_CHARS })
    const names = Array.isArray(b.engines) ? [...new Set(b.engines.map(String))] : []
    if (!query) return json(res, 400, { error: 'empty query' })
    if (names.length < 2 || names.length > 4) return json(res, 400, { error: 'pick 2-4 engines' })
    for (const n of names) { const e = engineInfo(n); if (!e) return json(res, 400, { error: `unknown engine '${n}'` }); if (!e.available) return json(res, 409, { error: `${e.label} is not available` }) }
    if (needsConfirm({ ...b, source: 'compare' }, query)) {
      const one = estimateFor(query)
      const estimate = { ...one, calls: one.calls * names.length, tokens_in: one.tokens_in * names.length, tokens_out: one.tokens_out * names.length, engine: null, engine_label: null, cheaper: [] }
      return json(res, 409, { error: `This run needs about ${estimate.calls} model calls. Confirm to go ahead.`, needs_confirmation: true, estimate })
    }
    const compare_id = 'c_' + id(4)
    const runs = names.map(engine => ({ engine, qid: submit(query, { source: 'compare', compare_id, engine }) }))
    compares.set(compare_id, { compare_id, query, runs })
    return json(res, 200, { compare_id, runs })
  }
  if ((m = /^\/api\/compare\/([\w-]+)$/.exec(p)) && M === 'GET') {
    const c = compares.get(m[1])
    if (!c) return json(res, 404, { error: 'unknown comparison' })
    return json(res, 200, { compare_id: c.compare_id, query: c.query, runs: c.runs.map(r => records.get(r.qid)).filter(Boolean) })
  }

  // ---------- evals ----------
  if (p === '/api/evals/run' && M === 'POST') {
    const b = (await body(req)) || {}
    const engine = b.engine ? String(b.engine) : activeEngine ?? 'none'
    if (engine !== 'none' && !engineInfo(engine)) return json(res, 400, { error: `unknown engine '${engine}'` })
    const eval_id = 'ev_' + id(3)
    const route = b.mode === 'route'
    evals.set(eval_id, { eval_id, at: now(), engine: engine === 'none' ? null : engine, status: 'running', passed: 0, total: evalCases().length, accuracy: 0, silent_wrong: 0, cases: [],
      ...(route ? { mode: 'route', jev: b.jev === 'replay' ? 'replay' : 'live', unjudged: 0, unrecorded: 0 } : {}), ...(b.judge ? { judge: String(b.judge) } : {}) })
    runEval(eval_id, engine)
    return json(res, 200, { eval_id })
  }
  if ((m = /^\/api\/evals\/([\w-]+)\/cancel$/.exec(p)) && M === 'POST') {
    const ev = evals.get(m[1])
    if (!ev) return json(res, 404, { error: 'unknown eval' })
    if (ev.status !== 'running') return json(res, 409, { error: 'eval already finished' })
    ev.status = 'cancelled'
    return json(res, 200, { ok: true })
  }
  if (p === '/api/evals' && M === 'GET') return json(res, 200, { evals: [...evals.values()].map(({ cases, ...s }) => s).sort((a, b) => b.at - a.at) })
  if ((m = /^\/api\/evals\/([\w-]+)$/.exec(p)) && M === 'GET') {
    const ev = evals.get(m[1])
    return ev ? json(res, 200, ev) : json(res, 404, { error: 'unknown eval' })
  }

  // ---------- engines ----------
  if ((m = /^\/api\/engines\/([\w-]+)\/test$/.exec(p)) && M === 'POST') {
    const e = engineInfo(m[1])
    if (!e) return json(res, 404, { error: `unknown engine '${m[1]}'` })
    const t0 = Date.now()
    await sleep(400 + Math.random() * 600)
    return json(res, 200, e.available ? { ok: true, ms: Date.now() - t0, text: 'ok' } : { ok: false, ms: Date.now() - t0, error: e.why })
  }

  if (p.startsWith('/api/')) return json(res, 404, { error: 'not found' })
  if (M !== 'GET') return json(res, 405, { error: 'method not allowed' })
  const rel = normalize(decodeURIComponent(p)).replace(/^([/\\])+/, '').replace(/^(\.\.[/\\])+/, '')
  const file = join(DIST, rel || 'index.html')
  if (!file.startsWith(DIST)) return json(res, 403, { error: 'forbidden' })
  try {
    const data = await readFile(file)
    res.writeHead(200, { 'Content-Type': TYPES[extname(file)] || 'application/octet-stream' }); res.end(data)
  } catch {
    try { const data = await readFile(join(DIST, 'index.html')); res.writeHead(200, { 'Content-Type': TYPES['.html'] }); res.end(data) }
    catch { res.writeHead(404); res.end('dist missing: run npm run build') }
  }
})

seed()
server.listen(PORT, '127.0.0.1', () => console.log(`mock TraceGraph on http://localhost:${PORT}`))

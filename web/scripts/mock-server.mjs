#!/usr/bin/env node
// Scripted stand-in for the Python backend: serves web/dist and speaks the PLAN.md SSE protocol, so the UI
// can be exercised without Jev or Claude. Usage: node scripts/mock-server.mjs [port]  (default 8777)
import http from 'node:http'
import { readFile } from 'node:fs/promises'
import { extname, join, normalize, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const PORT = +(process.argv[2] || process.env.PORT || 8777)
const DIST = join(dirname(fileURLToPath(import.meta.url)), '..', 'dist')
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.json': 'application/json', '.ico': 'image/x-icon' }

const AGENTS = {
  math: 'Arithmetic, percentages, or evaluating a numeric expression',
  weather: 'Current weather or a forecast for a place',
  time: 'The current local time or date in a city or timezone',
  currency: 'Converting an amount of money between currencies, or an exchange rate',
  knowledge: 'A factual question about a person, place, event, thing, or concept',
  code: 'Programming, software errors, or how to do something in code',
  chat: 'Greetings, small talk, or questions about the assistant itself',
}
const GUARDS = ['clarify', 'blocked']
const SAMPLES = ["What's 18% of 2450?", 'weather in Paris and convert 100 EUR to INR', 'What time is it in New York?',
  'Who was Nikola Tesla?', 'reverse a list in python', 'hello there', 'do the thing']
const PRICES = { jev_in: 0.042, claude_in: 5, claude_out: 25 }

const state = { autopilot: false, interval: 3 }
const stats = { queries: 0, subtasks: 0, errors: 0, jev_input_tokens: 0, claude_input_tokens: 0, claude_output_tokens: 0, by_agent: {} }
const history = []
const clients = new Set()
let nextQid = 1

const send = (res, e) => res.write(`data: ${JSON.stringify(e)}\n\n`)
const broadcast = e => { for (const c of clients) send(c, e); record(e) }
const sleep = ms => new Promise(r => setTimeout(r, ms))

function probs(top, p) {
  const rest = Object.keys(AGENTS).filter(a => a !== top)
  const out = { [top]: p }
  let left = 1 - p
  rest.forEach((a, i) => { const v = i === rest.length - 1 ? left : +(left * 0.55).toFixed(3); out[a] = v; left -= v })
  return Object.fromEntries(Object.entries(out).sort((a, b) => b[1] - a[1]))
}

// Canned knowledge of how each sample resolves.
function script(text) {
  const t = text.toLowerCase()
  if (t.includes('paris') && t.includes('eur')) return [
    { text: 'weather in Paris', agent: 'weather', conf: 0.93, answer: 'Paris: 17°C, light rain, wind 14 km/h.', source: 'open-meteo.com' },
    { text: 'convert 100 EUR to INR', agent: 'currency', conf: 0.96, answer: '100 EUR = 9,412.30 INR (rate 94.123)', source: 'frankfurter.app' },
  ]
  if (t.includes('%') || /\d\s*[*/+^-]/.test(t)) return [{ text, agent: 'math', conf: 0.97, answer: '441' }]
  if (t.includes('time')) return [{ text, agent: 'time', conf: 0.91, answer: 'It is 14:05 in New York (EDT).' }]
  if (t.includes('tesla') || t.startsWith('who')) return [{ text, agent: 'knowledge', conf: 0.88, answer: 'Nikola Tesla (1856–1943) was a Serbian-American inventor known for AC power systems.', source: 'https://en.wikipedia.org/wiki/Nikola_Tesla' }]
  if (t.includes('python') || t.includes('code')) return [{ text, agent: 'code', conf: 0.9, answer: 'Use slicing: items[::-1], or items.reverse() to reverse in place.' }]
  if (t.includes('hello') || t.includes('hi ')) return [{ text, agent: 'chat', conf: 0.86, answer: 'Hi! Ask me for math, weather, time, currency, facts or code.' }]
  return [{ text, agent: 'clarify', pick: 'chat', conf: 0.31, clear: 0.12, answer: 'Could you say a bit more about what you need?', ok: false }]
}

async function run(text, source = 'you') {
  const qid = nextQid++, t0 = Date.now()
  const plan = script(text)
  broadcast({ type: 'query', qid, text, source })
  await sleep(450)
  const subtasks = plan.map((p, i) => ({ tid: `${qid}.${i + 1}`, text: p.text }))
  broadcast({ type: 'plan', qid, planner: 'heuristic', subtasks, multi: plan.length > 1 ? 0.82 : 0.07, ms: 410 })
  await Promise.all(plan.map(async (p, i) => {
    const tid = subtasks[i].tid
    const jev_ms = 380 + Math.round(Math.random() * 400)
    await sleep(jev_ms)
    const pick = p.pick ?? p.agent
    broadcast({ type: 'routed', qid, tid, agent: p.agent, pick, reason: p.agent === 'clarify' ? 'low clarity' : `picked ${pick}`,
      probabilities: probs(pick, p.conf), confidence: p.conf, urgency: 0.6, unsafe: 0.02, clear: p.clear ?? 0.9, jev_ms, model: 'jev-mock' })
    const agent_ms = 300 + Math.round(Math.random() * 700)
    const words = p.answer.split(/(?<= )/)
    for (const w of words) { await sleep(agent_ms / words.length); broadcast({ type: 'delta', qid, tid, text: w }) }
    broadcast({ type: 'answered', qid, tid, agent: p.agent, agent_ms, answer: p.answer, ok: p.ok ?? true, source: p.source ?? null, engine: 'keyless' })
  }))
  let answer = plan[0].answer, engine = 'single', ms = 0
  if (plan.length > 1) {
    engine = 'concat'; ms = 1
    answer = plan.map(p => `**${p.agent}**: ${p.answer}`).join('\n\n')
    broadcast({ type: 'delta', qid, tid: 'merge', text: answer })
  }
  broadcast({ type: 'merged', qid, answer, engine, ms })
  stats.queries++
  broadcast({ type: 'done', qid, total_ms: Date.now() - t0, stats })
  return qid
}

// Keep stats and history in step with what was broadcast, so a reconnect's hello replays it.
const open = new Map()
function record(e) {
  if (e.type === 'query') open.set(e.qid, { qid: e.qid, text: e.text, source: e.source, at: Date.now() / 1000, plan: null, tasks: [], merged: null, total_ms: null })
  const r = open.get(e.qid)
  if (!r) return
  if (e.type === 'plan') r.plan = { planner: e.planner, subtasks: e.subtasks }
  if (e.type === 'routed') {
    const { type, qid, ...f } = e; r.tasks.push(f)
    stats.subtasks++; stats.jev_input_tokens += 900; stats.by_agent[e.agent] = (stats.by_agent[e.agent] || 0) + 1
  }
  if (e.type === 'answered') { const { type, qid, ...f } = e; Object.assign(r.tasks.find(t => t.tid === e.tid) || {}, f) }
  if (e.type === 'merged') r.merged = { answer: e.answer, engine: e.engine }
  if (e.type === 'done') { r.total_ms = e.total_ms; open.delete(e.qid); history.push(r); if (history.length > 60) history.shift() }
}

const config = () => ({ agents: AGENTS, guards: GUARDS, claude: false, state, stats, samples: SAMPLES, prices: PRICES })

// Scripted demo sequence, played once after the first client connects.
let demoStarted = false
async function demo() {
  await sleep(800); await run('weather in Paris and convert 100 EUR to INR')
  await sleep(700); await run("What's 18% of 2450?")
  await sleep(700); await run('do the thing')
}

let autoTimer = null
function schedule() {
  clearTimeout(autoTimer)
  if (state.autopilot) autoTimer = setTimeout(() => { run(SAMPLES[Math.floor(Math.random() * SAMPLES.length)], 'autopilot'); schedule() }, state.interval * 1000)
}

const body = req => new Promise(r => { let b = ''; req.on('data', c => { b += c; if (b.length > 1e5) req.destroy() }); req.on('end', () => { try { r(JSON.parse(b || '{}')) } catch { r(null) } }) })
const json = (res, code, obj) => { res.writeHead(code, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(obj)) }

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x')
  if (req.method === 'GET' && url.pathname === '/events') {
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive', 'X-Accel-Buffering': 'no' })
    send(res, { type: 'hello', ...config(), history })
    clients.add(res)
    const ping = setInterval(() => res.write(': ping\n\n'), 15000)
    req.on('close', () => { clients.delete(res); clearInterval(ping) })
    if (!demoStarted) { demoStarted = true; demo() }
    return
  }
  if (req.method === 'POST' && url.pathname === '/ask') {
    const b = await body(req)
    const q = typeof b?.query === 'string' ? b.query.trim().slice(0, 500) : ''
    if (!q) return json(res, 400, { ok: false, error: 'empty query' })
    const qid = nextQid
    run(q)
    return json(res, 200, { ok: true, qid })
  }
  if (req.method === 'POST' && url.pathname === '/control') {
    const b = (await body(req)) || {}
    if (typeof b.autopilot === 'boolean') state.autopilot = b.autopilot
    if (typeof b.interval === 'number') state.interval = Math.min(15, Math.max(1, b.interval))
    broadcast({ type: 'state', state })
    schedule()
    return json(res, 200, state)
  }
  if (req.method === 'GET' && url.pathname === '/api/config') return json(res, 200, config())
  if (req.method !== 'GET') return json(res, 405, { ok: false })
  const rel = normalize(decodeURIComponent(url.pathname)).replace(/^([/\\])+/, '').replace(/^(\.\.[/\\])+/, '')
  const file = join(DIST, rel || 'index.html')
  if (!file.startsWith(DIST)) return json(res, 403, { ok: false })
  try {
    const data = await readFile(file)
    res.writeHead(200, { 'Content-Type': TYPES[extname(file)] || 'application/octet-stream' }); res.end(data)
  } catch {
    try { const data = await readFile(join(DIST, 'index.html')); res.writeHead(200, { 'Content-Type': TYPES['.html'] }); res.end(data) }
    catch { res.writeHead(404); res.end('dist missing: run npm run build') }
  }
})
server.listen(PORT, () => console.log(`mock Jev Router on http://localhost:${PORT}`))

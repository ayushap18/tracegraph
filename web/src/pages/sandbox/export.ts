// Download a sandbox conversation as Markdown or JSON. Pure functions (plus `download`, which only touches the DOM
// to save a file on this computer). Nothing is sent anywhere.
import type { EngineInfo, ExportInput, Run, TurnGroup } from './types'

const engineName = (engines: EngineInfo[], name: string | null | undefined) =>
  !name || name === 'none' ? 'Keyless' : engines.find(e => e.name === name)?.label ?? name

const answerOf = (run: Run) => run.merged?.answer ?? run.mergeStream ?? ''

const tasksOf = (run: Run) => run.order.map(t => run.tasks[t]).filter(Boolean)

/** Engine of a compare column: the name the group asked for, else what the run reports. */
const compareEngine = (g: TurnGroup, i: number, run: Run | undefined) => g.engines?.[i] ?? run?.engine ?? null

const pctText = (v: number) => `${Math.round(v * 100)}%`

function statusNote(run: Run): string {
  if (!run.done) return '_Still running when exported._'
  if (run.status === 'cancelled') return '_Stopped before it finished._'
  if (run.status === 'timeout') return '_Timed out._'
  if (run.status !== 'done') return `_Failed${run.error ? `: ${run.error}` : '.'}_`
  return ''
}

export function toMarkdown({ groups, byQid, engines, draft }: ExportInput): string {
  const out: string[] = ['# TraceGraph sandbox', '', `Exported ${new Date().toISOString()}`, '']
  if (draft) {
    out.push(`Draft agent: **${draft.name}**${draft.web ? ' (web)' : ''}`, '', `> ${draft.description.replace(/\n/g, '\n> ')}`, '')
  }
  let n = 0
  for (const g of groups) {
    if (g.kind === 'single') {
      const qid = g.qids[g.active] ?? g.qids[g.qids.length - 1]
      const run = qid != null ? byQid.get(qid) : undefined
      if (!run) continue
      n++
      out.push('---', '', `## ${n}. ${oneLine(run.text)}`, '', `Engine: ${engineName(engines, run.engine)}`)
      if (g.qids.length > 1) out.push(`Earlier versions: ${g.qids.length - 1}`)
      for (const t of tasksOf(run)) if (t.routed) out.push(`Routed to: ${t.routed.agent} (${pctText(t.routed.confidence)})`)
      out.push('')
      if (run.text.includes('\n')) out.push('**Question**', '', run.text, '')
      const answer = answerOf(run)
      if (answer) out.push(answer, '')
      const note = statusNote(run)
      if (note) out.push(note, '')
    } else {
      const runs = g.qids.map(q => byQid.get(q))
      const text = runs.find(r => r?.text)?.text ?? ''
      n++
      out.push('---', '', `## ${n}. ${oneLine(text)}`, '', `Side by side, ${g.qids.length} engines`, '')
      if (text.includes('\n')) out.push('**Question**', '', text, '')
      g.qids.forEach((_, i) => {
        const run = runs[i]
        out.push(`### ${engineName(engines, compareEngine(g, i, run))}`, '')
        if (!run) { out.push('_No data._', ''); return }
        for (const t of tasksOf(run)) if (t.routed) out.push(`Routed to: ${t.routed.agent} (${pctText(t.routed.confidence)})`)
        if (tasksOf(run).some(t => t.routed)) out.push('')
        const answer = answerOf(run)
        if (answer) out.push(answer, '')
        const note = statusNote(run)
        if (note) out.push(note, '')
      })
    }
  }
  if (!n) out.push('_No messages._', '')
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trimEnd() + '\n'
}

const oneLine = (s: string) => {
  const t = s.replace(/\s+/g, ' ').trim()
  return t.length > 120 ? t.slice(0, 119) + '…' : t || '(empty)'
}

function trimRun(run: Run, engine?: string | null) {
  return {
    qid: run.qid,
    text: run.text,
    engine: engine ?? run.engine ?? 'none',
    status: run.status,
    total_ms: run.total_ms,
    tokens: run.tokens ?? null,
    tasks: tasksOf(run).map(t => ({
      text: t.text,
      agent: t.routed?.agent ?? null,
      confidence: t.routed?.confidence ?? null,
      reason: t.routed?.reason ?? null,
    })),
    answer: answerOf(run),
    ...(run.error ? { error: run.error } : {}),
  }
}

export function toJSON({ groups, byQid, engines, draft }: ExportInput): string {
  const data = {
    title: 'TraceGraph sandbox',
    exported_at: new Date().toISOString(),
    draft_agent: draft,
    engines: engines.map(e => ({ name: e.name, label: e.label, billing: e.billing })),
    groups: groups.map(g => ({
      kind: g.kind,
      ...(g.kind === 'single' ? { active: g.active } : { engines: g.engines ?? [] }),
      runs: g.qids.map((q, i) => {
        const run = byQid.get(q)
        if (!run) return { qid: q, missing: true }
        return trimRun(run, g.kind === 'compare' ? compareEngine(g, i, run) : undefined)
      }),
    })),
  }
  return JSON.stringify(data, null, 2) + '\n'
}

/** A filename stamp like 2026-09-26-1530 (local time). */
export function stamp(d = new Date()): string {
  const p = (v: number) => String(v).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`
}

/** Saves `text` as a file on this computer. */
export function download(filename: string, text: string, mime: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: mime }))
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000) // some browsers read the URL after click() returns
}

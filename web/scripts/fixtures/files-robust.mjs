// Mock fixtures for docs/PLAN-files-robust.md (cost preflight, partial files, Resume, design files), used by
// scripts/mock-server.mjs so the web UI can be checked without the Python server or any model.
// The numbers follow the plan's worked example for run 2750 (agy lead, claude-code for web search).

const DESIGN = {
  name: 'DESIGN-lovable.md',
  colors: { bg: 'F7F4ED', surface: 'FCFBF8', text: '1C1C1C', heading: '1C1C1C', muted: '5F5F5D', accent: '1C1C1C', border: 'ECEAE4', header_bg: '1C1C1C', header_text: 'FCFBF8', stripe: 'F2EFE8' },
  palette: ['F7F4ED', '1C1C1C', '5F5F5D', 'ECEAE4', 'FCFBF8', 'EFEDE7'],
  heading_font: 'Camera Plain Variable', body_font: 'Camera Plain Variable',
  fonts_used: { heading: 'Helvetica', body: 'Helvetica' },
  nudged: [],
  notes: ['The design asks for Camera Plain Variable, which is not installed here, so the slides use Helvetica.'],
  confidence: 0.92,
}

const MISSING = ['The smartphone era: 2007 to 2012']

/** The estimate for a draft. `heavy` drafts (a long file, or research that writes a file) need confirming. */
export function estimateFor(query, { engine = 'agy', mode = 'balanced', resume = false } = {}) {
  const q = String(query || '').toLowerCase()
  const file = /\b(ppt|pptx|slides?|deck|presentation|report|pdf|docx|word document|document)\b/.test(q)
  const n = +(q.match(/\b(\d{1,3})\s*(?:slides?|pages?)\b/)?.[1] ?? 0)
  const long = file && (n >= 8 || /\b(detailed|in depth|everything|history)\b/.test(q))
  const research = mode === 'research'
  if (resume) {
    return est({
      calls: 1, tokens_in: 42000, tokens_out: 4200, seconds: 70, range: { calls: [1, 2], tokens_in: [31500, 88200], tokens_out: [1400, 8400], seconds: [49, 150] },
      long_file: true, needs_confirmation: true,
      reasons: [{ code: 'resume', text: 'Resume writes only the 1 missing slide and reuses the 11 already written.' },
        { code: 'engine_overhead', text: 'Antigravity adds about 12,400 tokens of its own to every turn, and takes 3 turns per call.' }],
      breakdown: [{ phase: 'sections', engine: 'agy', calls: 1, tokens_in: 42000, tokens_out: 4200, seconds: 70, optional: false },
        { phase: 'repair', engine: 'agy', calls: 1, tokens_in: 21000, tokens_out: 2000, seconds: 40, optional: true }],
      summary: 'Resuming needs about 1 model call on Antigravity: roughly 46,000 tokens and about 1 to 3 minutes.',
      cheaper: [{ engine: 'claude-code', label: 'Claude Code', calls: 1, tokens_in: 9000, tokens_out: 3000, seconds: 45, saves: 0.74 }],
    })
  }
  if (!long && !research) {
    return est({ calls: file ? 1 : 1, tokens_in: 3000, tokens_out: 700, seconds: 15, range: { calls: [1, 2], tokens_in: [2250, 4200], tokens_out: [233, 1400], seconds: [10, 23] },
      needs_confirmation: false, reasons: [], breakdown: [], summary: 'This needs about 1 model call.', engine, cheaper: [] })
  }
  return est({
    calls: 6, tokens_in: 243700, tokens_out: 21800, seconds: 250,
    range: { calls: [5, 8], tokens_in: [182775, 341180], tokens_out: [7267, 43600], seconds: [175, 375] },
    long_file: long, needs_confirmation: true,
    reasons: [
      ...(long ? [{ code: 'long_file', text: `A ${n || 12} slide deck is written in parts: an outline, then the slides in 2 batches.` }] : []),
      ...(research ? [{ code: 'research', text: 'Research mode searches the web first, on Claude Code.' }] : []),
      { code: 'engine_overhead', text: 'Antigravity adds about 12,400 tokens of its own to every turn, and takes 3 turns per call.' },
      { code: 'deadline', text: 'A long file gets up to 15 minutes before it stops.' },
    ],
    breakdown: [
      { phase: 'planner', engine: 'agy', calls: 1, tokens_in: 27800, tokens_out: 2000, seconds: 26, optional: false },
      { phase: 'research', engine: 'claude-code', calls: 1, tokens_in: 50000, tokens_out: 3000, seconds: 90, optional: false },
      { phase: 'outline', engine: 'agy', calls: 1, tokens_in: 47700, tokens_out: 4800, seconds: 34, optional: false },
      { phase: 'sections', engine: 'agy', calls: 2, tokens_in: 102600, tokens_out: 8800, seconds: 72, optional: false },
      { phase: 'merge', engine: 'agy', calls: 1, tokens_in: 15600, tokens_out: 3200, seconds: 28, optional: false },
      { phase: 'repair', engine: 'agy', calls: 2, tokens_in: 51300, tokens_out: 4400, seconds: 60, optional: true },
    ],
    summary: 'This deck needs about 6 model calls on Antigravity and Claude Code: roughly 270,000 tokens and about 3 to 7 minutes.',
    engine: 'agy', engine_label: 'Antigravity',
    cheaper: [{ engine: 'claude-code', label: 'Claude Code', calls: 6, tokens_in: 85000, tokens_out: 14000, seconds: 190, saves: 0.63 }],
  })
}

function est(x) {
  return {
    version: 1, engine: 'agy', engine_label: 'Antigravity', billing: 'subscription', dollars: null, keyless: false, long_file: false,
    deadline_s: 900, cheaper: [], ...x,
  }
}

/** A chat with a partial deck that followed a design file, and a report whose file step made no file. */
export function robustFixtures(qid0, at) {
  const sid = 's_robust'
  const text = 'create the ppt on the how mobile phone is being evolved history past present everything a ppt of 12 slides using the multiple pictured diagrams and also use the design.md for the design'
  const estimate = estimateFor(text, { mode: 'research' })
  const q1 = qid0, q2 = qid0 + 1
  const checkpoint1 = { qid: q1, tid: `${q1}.2`, kind: 'longdoc', format: 'pptx', phase: 'done', planned: 12, written: 11, missing: MISSING,
    tokens_in: 198200, tokens_out: 24100, at: at + 270, resumable: true, file_id: 'cf_robust_deck' }
  const file = {
    id: 'cf_robust_deck', name: 'mobile-phone-evolution.pptx', format: 'pptx', size: 1873920, created: at + 272, qid: q1,
    title: 'How the mobile phone evolved', slides: 11, tokens: 222300, source: 'llm', role: 'primary',
    brief: { format: 'pptx', pages: null, slides: [12, 12], theme: null, font: null, images: true, image_source: 'web', diagrams: true, diagram_kinds: ['timeline'],
      words: null, capped: false, design: 'DESIGN-lovable.md' },
    rules: [
      { id: 'S1', severity: 'fix', ok: false, note: 'Section 7 had a bullets block with no items; its text was kept as a paragraph.' },
      { id: 'V10', severity: 'warn', ok: true, note: 'Design applied from DESIGN-lovable.md.' },
      { id: 'V12', severity: 'warn', ok: false, note: '1 of 12 planned slides could not be written (The smartphone era: 2007 to 2012). Use Resume on the file card to write them; it reuses everything already written.' },
      { id: 'V5', severity: 'warn', ok: false, note: 'Asked for 12 slides, made 11.' },
    ],
    diagrams: 3, images: 5, font_used: 'Helvetica',
    phases: [
      { phase: 'outline', calls: 1, llm_in: 47900, llm_out: 5200, ms: 38000 },
      { phase: 'sections', calls: 3, llm_in: 150300, llm_out: 18900, ms: 121000 },
      { phase: 'render', calls: 0, llm_in: 0, llm_out: 0, ms: 2100 },
    ],
    partial: { planned: 12, written: 11, missing: MISSING, resume: { qid: q1, tid: `${q1}.2`, sandbox: null } },
    repairs: { calls: 1, llm_in: 51300, llm_out: 900, sections: MISSING },
    cost: { estimate, actual: { calls: 6, tokens_in: 250046, tokens_out: 30492, seconds: 272.5 } },
    design: DESIGN,
  }
  const route = (tid, t, agent) => ({ tid, text: t, agent, pick: agent, reason: 'fixture', probabilities: { [agent]: 0.9, knowledge: 0.1 }, confidence: 0.9,
    urgency: 0.5, unsafe: 0.01, clear: 0.9, jev_ms: 410, model: 'jev-mock' })
  const run1 = {
    qid: q1, text, source: 'chat', at, plan: { planner: 'agy', subtasks: [
      { tid: `${q1}.1`, text: 'Research how the mobile phone evolved, past to present', depends_on: [] },
      { tid: `${q1}.2`, text: 'Make a 12 slide deck from the research with pictured diagrams, styled by DESIGN-lovable.md', depends_on: [`${q1}.1`] }] },
    tasks: [
      { ...route(`${q1}.1`, 'Research how the mobile phone evolved, past to present', 'research'), agent_ms: 88000, answer: 'From the 1973 Motorola DynaTAC to foldables: five eras of the mobile phone.', ok: true, source: null, engine: 'claude-code' },
      { ...route(`${q1}.2`, 'Make a 12 slide deck', 'create'), agent_ms: 181000, answer: 'Made mobile-phone-evolution.pptx (11 of 12 slides).', ok: true, source: null, engine: 'agy',
        created_files: [file], phases: file.phases, llm_in: 198200, llm_out: 24100, checkpoint: checkpoint1,
        caveats: ['1 of 12 planned slides could not be written (The smartphone era: 2007 to 2012). Use Resume on the file card to write them; it reuses everything already written.'] },
    ],
    merged: { answer: 'Here is your deck on how the mobile phone evolved. It follows DESIGN-lovable.md.\n\n**What I couldn\'t do**\n- 1 of 12 planned slides could not be written (The smartphone era: 2007 to 2012). Use Resume on the file card to write them; it reuses everything already written.',
      engine: 'agy', caveats: ['1 of 12 planned slides could not be written (The smartphone era: 2007 to 2012). Use Resume on the file card to write them; it reuses everything already written.'], primary_file: file.id },
    total_ms: 272500, error: null, status: 'done', engine: null, session_id: sid, compare_id: null, files: [], mode: 'research', agent: 'create',
    cost: file.cost, checkpoints: [checkpoint1],
  }
  const text2 = 'turn that into a detailed 20 page PDF report'
  const checkpoint2 = { qid: q2, tid: `${q2}.1`, kind: 'longdoc', format: 'pdf', phase: 'sections', planned: 9, written: 0, missing: ['Introduction', 'Early radio phones', 'The brick era', 'Feature phones', 'Smartphones', 'Foldables', 'Networks', 'What comes next', 'Sources'],
    tokens_in: 61200, tokens_out: 5400, at: at + 900, resumable: true, file_id: null }
  const run2 = {
    qid: q2, text: text2, source: 'chat', at: at + 600, plan: { planner: 'heuristic', subtasks: [{ tid: `${q2}.1`, text: text2, depends_on: [] }] },
    tasks: [{ ...route(`${q2}.1`, text2, 'create'), agent_ms: 300000, answer: 'No file was made: the run ran out of time before any section came back usable.', ok: false, source: null, engine: 'agy',
      phases: [{ phase: 'outline', calls: 1, llm_in: 47100, llm_out: 4600, ms: 41000 }, { phase: 'sections', calls: 1, llm_in: 14100, llm_out: 800, ms: 259000 }],
      llm_in: 61200, llm_out: 5400, checkpoint: checkpoint2 }],
    merged: { answer: 'I could not make the PDF this time. What was written is saved: use Resume to continue without repeating it.', engine: 'template', caveats: ['No file was made: the run ran out of time before any section came back usable.'] },
    total_ms: 300400, error: null, status: 'done', engine: null, session_id: sid, compare_id: null, files: [], file_failed: true,
    cost: { estimate: estimateFor(text2), actual: { calls: 2, tokens_in: 61200, tokens_out: 5400, seconds: 300.4 } }, checkpoints: [checkpoint2],
  }
  return {
    session: { id: sid, title: 'Files that always build', created: at, updated: at + 600 },
    runs: [run1, run2],
    previews: { cf_robust_deck: { kind: 'outline', slides: 11, items: [
      { level: 1, text: 'How the mobile phone evolved' }, { level: 1, text: 'Before the phone went mobile' }, { level: 1, text: 'The first handheld call, 1973' },
      { level: 1, text: 'The brick era' }, { level: 1, text: 'Going digital: 2G and SMS' }, { level: 1, text: 'Feature phones' },
      { level: 1, text: 'Cameras, colour and the mobile web' }, { level: 1, text: 'Touchscreens everywhere' }, { level: 1, text: 'Foldables and beyond' },
      { level: 1, text: 'Timeline' }, { level: 1, text: 'Sources' }] } },
  }
}

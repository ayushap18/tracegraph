// Mock fixtures for docs/PLAN-designer.md (Studio, the design stage), used by scripts/mock-server.mjs so the Design
// panel can be checked without the Python server or any model. Thumbnails, preset swatches and font previews are
// drawn as SVG here (the real server sends PNG); the browser shows either the same way.

export const PRESETS = [
  { id: 'bold-dark', name: 'Bold dark', description: 'Pitches, space and tech topics, strong covers', dark: true,
    families: { display: 'Poppins', heading: 'Poppins', body: 'Inter' }, colors: { bg: '0F1115', text: 'F4F4F5', accent: 'F97316', accent2: '38BDF8' } },
  { id: 'editorial', name: 'Editorial', description: 'History, literature and essays, with serif headings and room to breathe', dark: false,
    families: { display: 'Playfair Display', heading: 'Playfair Display', body: 'Source Serif 4' }, colors: { bg: 'FBF8F3', text: '1F1A17', accent: 'B45309', accent2: '7C2D12' } },
  { id: 'minimal', name: 'Minimal', description: 'Science and maths: lots of air, one accent', dark: false,
    families: { display: 'Inter', heading: 'Inter', body: 'Inter' }, colors: { bg: 'FFFFFF', text: '18181B', accent: '2563EB', accent2: '64748B' } },
  { id: 'vibrant', name: 'Vibrant', description: 'School projects and younger students: playful shapes, bright colours', dark: false,
    families: { display: 'Fredoka', heading: 'Fredoka', body: 'Nunito' }, colors: { bg: 'FFF7ED', text: '1E1B4B', accent: 'DB2777', accent2: '0D9488' } },
  { id: 'pastel', name: 'Pastel', description: 'Biology, wellbeing and revision notes', dark: false,
    families: { display: 'Quicksand', heading: 'Quicksand', body: 'Nunito' }, colors: { bg: 'F5F3FF', text: '312E81', accent: '8B5CF6', accent2: '10B981' } },
  { id: 'academic', name: 'Academic', description: 'Lab reports and research posters: made for print, strict hierarchy', dark: false,
    families: { display: 'Source Serif 4', heading: 'Source Sans 3', body: 'Source Serif 4' }, colors: { bg: 'FFFFFF', text: '111827', accent: '1D4ED8', accent2: '9F1239' } },
  { id: 'mono', name: 'Black and white', description: 'Print and photocopy friendly', dark: false,
    families: { display: 'IBM Plex Sans', heading: 'IBM Plex Sans', body: 'IBM Plex Serif' }, colors: { bg: 'FFFFFF', text: '000000', accent: '404040', accent2: '737373' } },
  { id: 'high-legibility', name: 'High legibility', description: 'Dyslexia friendly: Atkinson Hyperlegible, larger spacing, no justified text', dark: false,
    families: { display: 'Atkinson Hyperlegible', heading: 'Atkinson Hyperlegible', body: 'Atkinson Hyperlegible' }, colors: { bg: 'FFFDF5', text: '1A1A1A', accent: '0F766E', accent2: '9A3412' } },
].map(p => ({ ...p, thumb: `/api/design/presets/${p.id}/thumb` }))

export const TEMPLATES = [
  { id: 'class-presentation', name: 'Class presentation', description: 'A 10 to 12 slide talk with a strong cover, one idea per slide and a closing slide',
    format: 'pptx', preset: 'vibrant', paper: null, sequence: ['cover-type', 'title-bullets', 'image-left-text', 'big-number', 'full-width-diagram', 'closing'], tone: ['short bullets', 'one idea per slide'] },
  { id: 'lab-report', name: 'Lab report', description: 'Aim, method, results, discussion and references, ready to print',
    format: 'pdf', preset: 'academic', paper: 'a4', sequence: ['cover', 'chapter-opener', 'text-side-figure', 'full-figure', 'references'], tone: ['past tense', 'cite sources'] },
  { id: 'research-poster', name: 'Research poster', description: 'An A2 poster with panels for question, method, findings and sources',
    format: 'pdf', preset: 'academic', paper: 'a2', sequence: ['cover', 'two-column-text', 'full-figure', 'key-points', 'references'], tone: ['short panels', 'big figures'] },
  { id: 'revision-notes', name: 'Revision notes', description: 'Key points boxes, summaries and quick checks for each topic',
    format: 'pdf', preset: 'pastel', paper: 'a4', sequence: ['chapter-opener', 'key-points', 'two-column-text'], tone: ['bullet summaries', 'plain words'] },
  { id: 'infographic', name: 'Infographic', description: 'Big numbers, icons and one clear story on a single tall page',
    format: 'pdf', preset: 'vibrant', paper: 'a3', sequence: ['cover', 'key-points', 'full-figure'], tone: ['numbers first', 'few words'] },
  { id: 'book-report', name: 'Book report', description: 'Plot, characters, themes and your opinion, with quotes',
    format: 'docx', preset: 'editorial', paper: 'a4', sequence: ['cover', 'chapter-opener', 'pull-quote', 'two-column-text'], tone: ['quote the book', 'give your view'] },
  { id: 'science-fair', name: 'Science fair board', description: 'A three panel board: question and hypothesis, experiment, results and conclusion',
    format: 'pptx', preset: 'bold-dark', paper: null, sequence: ['cover-hero', 'comparison', 'chart-focus', 'stat-cards', 'closing'], tone: ['clear steps', 'show the data'] },
]

// Curated open-licensed families. Nothing proprietary is ever listed.
export const FONTS = [
  ['Inter', 'sans', 'OFL-1.1', 'cache', true], ['Poppins', 'sans', 'OFL-1.1', 'cache', true], ['Atkinson Hyperlegible', 'sans', 'OFL-1.1', 'fontsource', false],
  ['Lexend', 'sans', 'OFL-1.1', 'fontsource', false], ['Nunito', 'sans', 'OFL-1.1', 'fontsource', false], ['Source Sans 3', 'sans', 'OFL-1.1', 'system', true],
  ['Roboto', 'sans', 'Apache-2.0', 'system', true], ['Open Sans', 'sans', 'OFL-1.1', 'google-fonts', false], ['Ubuntu', 'sans', 'UFL-1.0', 'fontsource', false],
  ['Playfair Display', 'serif', 'OFL-1.1', 'fontsource', false], ['Source Serif 4', 'serif', 'OFL-1.1', 'cache', true], ['Merriweather', 'serif', 'OFL-1.1', 'fontsource', false],
  ['IBM Plex Serif', 'serif', 'OFL-1.1', 'fontsource', false], ['Fredoka', 'display', 'OFL-1.1', 'fontsource', false], ['Quicksand', 'sans', 'OFL-1.1', 'fontsource', false],
  ['Caveat', 'handwriting', 'OFL-1.1', 'google-fonts', false], ['JetBrains Mono', 'mono', 'OFL-1.1', 'cache', true], ['IBM Plex Sans', 'sans', 'OFL-1.1', 'fontsource', false],
].map(([family, category, licence, source, installed]) => ({
  family, category, licence, source, installed, styles: category === 'handwriting' ? ['regular', 'bold'] : ['regular', 'bold', 'italic', 'bolditalic'],
  preview: `/api/fonts/preview?family=${encodeURIComponent(family)}`,
}))

export const SLIDE_LAYOUTS = ['cover-hero', 'cover-type', 'section-divider', 'title-bullets', 'image-left-text', 'image-right-text',
  'full-bleed-image-caption', 'big-number', 'stat-cards', 'quote', 'two-column', 'comparison', 'full-width-diagram', 'chart-focus',
  'timeline-strip', 'closing']
export const PAGE_TEMPLATES = ['cover', 'chapter-opener', 'text-side-figure', 'two-column-text', 'full-figure', 'pull-quote', 'key-points', 'references']

const CHECK_NAMES = { D1: 'Overflow', D2: 'Overlap', D3: 'Readability', D4: 'Density', D5: 'Balance', D6: 'Consistency', D7: 'Variety', D8: 'Images' }
const WEIGHTS = { D1: 20, D2: 15, D3: 20, D4: 10, D5: 5, D6: 10, D7: 10, D8: 10 }

const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c])
const presetOf = id => PRESETS.find(p => p.id === id) ?? PRESETS[2]

/** A schematic of one slide or page in the preset's colours. */
export function thumbSvg({ layout, page, preset, dark, w, h, title }) {
  const p = presetOf(preset)
  let { bg, text, accent, accent2 } = p.colors
  if (dark != null && dark !== p.dark) [bg, text] = dark ? ['111318', 'F4F4F5'] : ['FFFFFF', '18181B']
  const W = w, H = h, m = Math.round(W * 0.06)
  const bar = (x, y, bw, bh, c, o = 1) => `<rect x="${x}" y="${y}" width="${bw}" height="${bh}" rx="${Math.max(2, bh / 4)}" fill="#${c}" opacity="${o}"/>`
  const lines = (x, y, bw, n, gap = H * 0.07) => Array.from({ length: n }, (_, i) => bar(x, y + i * gap, bw * (i % 3 === 2 ? 0.7 : 1), H * 0.025, text, 0.45)).join('')
  const img = (x, y, iw, ih) => `<rect x="${x}" y="${y}" width="${iw}" height="${ih}" fill="#${accent2}" opacity="0.55"/><circle cx="${x + iw * 0.3}" cy="${y + ih * 0.35}" r="${Math.min(iw, ih) * 0.1}" fill="#${bg}" opacity="0.7"/><path d="M${x} ${y + ih} L${x + iw * 0.45} ${y + ih * 0.5} L${x + iw * 0.7} ${y + ih * 0.75} L${x + iw} ${y + ih * 0.4} L${x + iw} ${y + ih} Z" fill="#${text}" opacity="0.25"/>`
  const t = (x, y, size, s, weight = 700, anchor = 'start') => `<text x="${x}" y="${y}" font-family="system-ui, sans-serif" font-size="${size}" font-weight="${weight}" fill="#${text}" text-anchor="${anchor}">${esc(s)}</text>`
  let body = ''
  switch (layout) {
    case 'cover-hero': case 'full-bleed-image-caption': case 'cover':
      body = img(0, 0, W, layout === 'cover' ? H * 0.56 : H) + `<rect x="0" y="${H * 0.55}" width="${W}" height="${H * 0.45}" fill="#${bg}" opacity="${layout === 'cover' ? 1 : 0.6}"/>` + t(m, H * 0.78, H * 0.1, title) + bar(m, H * 0.85, W * 0.4, H * 0.03, text, 0.5); break
    case 'cover-type': case 'closing':
      body = `<circle cx="${W * 0.82}" cy="${H * 0.3}" r="${H * 0.35}" fill="#${accent}" opacity="0.8"/><circle cx="${W * 0.9}" cy="${H * 0.8}" r="${H * 0.18}" fill="#${accent2}" opacity="0.7"/>` + t(layout === 'closing' ? W / 2 : m, H * 0.52, H * 0.12, title, 800, layout === 'closing' ? 'middle' : 'start') + bar(layout === 'closing' ? W * 0.3 : m, H * 0.62, W * 0.4, H * 0.03, text, 0.5); break
    case 'section-divider': case 'chapter-opener':
      body = t(m, H * 0.55, H * 0.28, String(page).padStart(2, '0'), 800) + `<rect x="${W * 0.34}" y="${H * 0.62}" width="${W * 0.12}" height="${H * 0.03}" fill="#${accent}"/>` + t(W * 0.34, H * 0.55, H * 0.09, title); break
    case 'image-left-text': case 'text-side-figure':
      body = img(0, 0, W * 0.5, H) + t(W * 0.56, H * 0.18, H * 0.08, title) + lines(W * 0.56, H * 0.3, W * 0.38, 6); break
    case 'image-right-text':
      body = img(W * 0.5, 0, W * 0.5, H) + t(m, H * 0.18, H * 0.08, title) + lines(m, H * 0.3, W * 0.38, 6); break
    case 'big-number':
      body = t(m, H * 0.14, H * 0.06, title, 600) + t(m, H * 0.62, H * 0.38, '73%', 800) + `<rect x="${m}" y="${H * 0.7}" width="${W * 0.2}" height="${H * 0.03}" fill="#${accent}"/>` + lines(W * 0.62, H * 0.3, W * 0.3, 5); break
    case 'stat-cards': case 'key-points':
      body = t(m, H * 0.16, H * 0.08, title) + [0, 1, 2].map(i => `<rect x="${m + i * (W - 2 * m) / 3}" y="${H * 0.3}" width="${(W - 2 * m) / 3 - 10}" height="${H * 0.45}" rx="8" fill="#${i === 1 ? accent2 : accent}" opacity="0.8"/>`).join(''); break
    case 'quote': case 'pull-quote':
      body = t(m, H * 0.3, H * 0.3, '“', 800) + bar(W * 0.14, H * 0.35, W * 0.7, H * 0.06, text, 0.8) + bar(W * 0.14, H * 0.47, W * 0.55, H * 0.06, text, 0.8) + bar(W * 0.14, H * 0.66, W * 0.25, H * 0.03, accent); break
    case 'two-column': case 'comparison': case 'two-column-text':
      body = t(m, H * 0.16, H * 0.08, title) + lines(m, H * 0.3, W * 0.4, 6) + lines(W * 0.53, H * 0.3, W * 0.4, 6) + (layout === 'comparison' ? `<rect x="${W / 2 - 1}" y="${H * 0.28}" width="2" height="${H * 0.6}" fill="#${accent}"/>` : ''); break
    case 'full-width-diagram': case 'timeline-strip': case 'full-figure':
      body = t(m, H * 0.16, H * 0.08, title) + `<line x1="${m}" y1="${H * 0.55}" x2="${W - m}" y2="${H * 0.55}" stroke="#${text}" stroke-opacity="0.5" stroke-width="3"/>` + [0, 1, 2, 3, 4].map(i => `<circle cx="${m + i * (W - 2 * m) / 4}" cy="${H * 0.55}" r="${H * 0.05}" fill="#${i % 2 ? accent2 : accent}"/>`).join(''); break
    case 'chart-focus':
      body = t(m, H * 0.16, H * 0.08, title) + [0.5, 0.7, 0.4, 0.85, 0.6].map((v, i) => `<rect x="${m + i * W * 0.12}" y="${H * 0.9 - H * 0.55 * v}" width="${W * 0.08}" height="${H * 0.55 * v}" fill="#${accent}" opacity="${0.6 + i * 0.08}"/>`).join('') + lines(W * 0.7, H * 0.35, W * 0.24, 4); break
    case 'references':
      body = t(m, H * 0.12, H * 0.06, 'References') + lines(m, H * 0.2, W - 2 * m, 10, H * 0.07); break
    default:
      body = t(m, H * 0.16, H * 0.08, title) + lines(m, H * 0.3, W - 2 * m, 7)
  }
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}"><rect width="${W}" height="${H}" fill="#${bg}"/>${body}<text x="${W - 10}" y="${H - 10}" font-family="system-ui, sans-serif" font-size="${H * 0.05}" fill="#${text}" opacity="0.5" text-anchor="end">${page}</text></svg>`
}

export function presetThumbSvg(id) {
  const p = presetOf(id)
  return thumbSvg({ layout: p.dark ? 'cover-type' : 'image-left-text', page: '', preset: id, w: 320, h: 180, title: p.name })
}

export function fontPreviewSvg(family, text = 'The quick brown fox') {
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 520 48" width="520" height="48"><text x="4" y="34" font-family="'${esc(family)}', system-ui, sans-serif" font-size="30" fill="#71717a">${esc(text)}</text></svg>`
}

const DECK_TITLES = ['Photosynthesis', 'Why plants need light', 'Inside a leaf', 'The light reactions', '73% of oxygen', 'The Calvin cycle',
  'Light vs dark reactions', 'Rates of photosynthesis', 'What limits the rate', 'Key numbers', 'Timeline of discovery', 'Thank you']
const DECK_LAYOUTS = ['cover-hero', 'title-bullets', 'image-left-text', 'full-width-diagram', 'big-number', 'image-right-text',
  'comparison', 'chart-focus', 'two-column', 'stat-cards', 'timeline-strip', 'closing']
const PDF_TITLES = ['Measuring pendulum periods', 'Aim and method', 'Results', 'Graph of period against length', 'Discussion', 'References']
const PDF_LAYOUTS = ['cover', 'chapter-opener', 'text-side-figure', 'full-figure', 'two-column-text', 'references']

const checksOf = fails => Object.keys(CHECK_NAMES).map(id => ({ id, name: CHECK_NAMES[id], ok: !fails[id], failures: fails[id]?.length ?? 0,
  note: fails[id]?.[0]?.note ?? 'Passed' }))
const scoreOf = fails => 100 - Object.keys(fails).reduce((n, id) => n + (fails[id].length ? WEIGHTS[id] : 0), 0)
const countLayouts = ls => ls.reduce((o, l) => ({ ...o, [l]: (o[l] ?? 0) + 1 }), {})

/** Everything the mock needs about one Studio file: thumbnails, report, plan summary. */
export function studioDesign({ format, preset, dark = null, layouts, titles, fails = {}, fonts, notes = [], critic = null, stop = 'pass', rounds = 1, tokens }) {
  const [w, h] = format === 'pptx' ? [480, 270] : [340, 480]
  const results = Object.values(fails).flat()
  const score = scoreOf(fails)
  const tk = tokens ?? { direct_in: 1400, direct_out: 820, critic_in: 0, critic_out: 0, freeform_in: 0, freeform_out: 0 }
  const report = {
    version: 1, score, preset, format, fonts, rounds, stop, tokens: tk, checks: checksOf(fails), results, layouts: countLayouts(layouts),
    fallbacks: [], critic: critic ?? { ran: false, why: 'Balanced mode runs the critic only when you press Polish.' }, thumbs: layouts.length,
    phases: [{ phase: 'direct', calls: 1, llm_in: tk.direct_in, llm_out: tk.direct_out, ms: 2400 }, { phase: 'layout', calls: 0, llm_in: 0, llm_out: 0, ms: 180 },
      { phase: 'thumbs', calls: 0, llm_in: 0, llm_out: 0, ms: 420 }, { phase: 'qa', calls: 0, llm_in: 0, llm_out: 0, ms: 60 }, { phase: 'paint', calls: 0, llm_in: 0, llm_out: 0, ms: 900 }],
    notes,
  }
  const plan = { preset, format, pages: layouts.map((layout, index) => ({ index, layout, variant: 'default', freeform: false, section: index ? index - 1 : null })),
    fonts, assets: 6, score, rounds, stop, tokens: tk }
  return { preset, dark, w, h, layouts, titles, report, plan }
}

const FONTS_DECK = [
  { family: 'Poppins', role: 'heading', source: 'cache', licence: 'OFL-1.1', embedded: false, fallback: 'Arial', requested: null, note: 'Install Poppins (free) for the exact look. Without it PowerPoint shows Arial.' },
  { family: 'Inter', role: 'body', source: 'cache', licence: 'OFL-1.1', embedded: false, fallback: 'Calibri', requested: null, note: null },
]
const FONTS_PDF = [
  { family: 'Source Serif 4', role: 'body', source: 'fontsource', licence: 'OFL-1.1', embedded: true, fallback: null, requested: null, note: null },
  { family: 'Inter', role: 'heading', source: 'cache', licence: 'OFL-1.1', embedded: true, fallback: null, requested: 'Anthropic Sans',
    note: "Anthropic Sans isn't openly licensed, so this uses Inter, the closest open match." },
]

/** Created files that went through Studio, plus one that did not, in one chat. */
export function studioFixtures(qid0, at) {
  const deck = studioDesign({
    format: 'pptx', preset: 'bold-dark', layouts: DECK_LAYOUTS, titles: DECK_TITLES, fonts: FONTS_DECK,
    fails: { D4: [{ id: 'D4', ok: false, note: 'Slide 9 has 58 words in bullets; the limit is 40.', page: 8, box: 'p8.left', value: 58, threshold: 40, fixed: false }] },
    notes: ['Asked for a dark design, so this uses Bold dark.', 'The deck uses 11 different layouts.', 'Slide 9 is still wordy. Polish or a shorter text would fix it.'],
    rounds: 3, stop: 'rounds',
  })
  const pdf = studioDesign({
    format: 'pdf', preset: 'academic', layouts: PDF_LAYOUTS, titles: PDF_TITLES, fonts: FONTS_PDF,
    fails: {
      D3: [{ id: 'D3', ok: false, note: 'The caption on page 4 has a contrast of 3.9 to 1; it needs 4.5 to 1.', page: 3, box: 'p3.caption', value: 3.9, threshold: 4.5, fixed: false }],
      D8: [{ id: 'D8', ok: false, note: 'The photo on page 3 is stretched 1.8 times; the limit is 1.5.', page: 2, box: 'p2.figure', value: 1.8, threshold: 1.5, fixed: false }],
    },
    notes: ["Anthropic Sans isn't openly licensed, so this uses Inter, the closest open match.", 'Fonts are embedded in the PDF, so it looks the same everywhere.'],
    stop: 'keyless', tokens: { direct_in: 0, direct_out: 0, critic_in: 0, critic_out: 0, freeform_in: 0, freeform_out: 0 },
  })
  const q = qid0
  const mk = (id, name, format, extra) => ({
    id, name, format, size: format === 'pptx' ? 2310144 : 412672, created: at + 60, qid: q, title: name.replace(/\.\w+$/, ''), tokens: 9400, source: 'llm',
    from_id: null, rules: [], role: 'working', ...extra,
  })
  const designOf = (d, name = `preset:${d.preset}`) => {
    const p = presetOf(d.preset)
    return { name, colors: { bg: p.colors.bg, text: p.colors.text, accent: p.colors.accent }, palette: [p.colors.accent, p.colors.accent2], heading_font: null, body_font: null,
      fonts_used: { heading: d.report.fonts.find(f => f.role === 'heading')?.family ?? null, body: d.report.fonts.find(f => f.role === 'body')?.family ?? null },
      nudged: [], notes: [], confidence: 1, studio: true, preset: d.preset, fonts: d.report.fonts, score: d.report.score, thumbs: d.layouts.length }
  }
  const files = [
    mk('cf_studio_deck', 'photosynthesis.pptx', 'pptx', { slides: 12, role: 'primary', theme: 'dark', design: designOf(deck),
      rules: [{ id: 'D4', severity: 'warn', ok: false, note: 'Slide 9 has 58 words in bullets; the limit is 40.' }] }),
    mk('cf_studio_pdf', 'pendulum-lab-report.pdf', 'pdf', { pages: 6, tokens: 7200, design: designOf(pdf) }),
    mk('cf_studio_old', 'photosynthesis-notes.docx', 'docx', { pages: 3, tokens: 3100 }),
    mk('cf_studio_nospec', 'old-deck.pptx', 'pptx', { slides: 12, tokens: 0, source: 'convert', design: designOf(deck) }),
  ]
  const text = 'make a 12 slide ppt on photosynthesis, bold and creative with a dark design, and a lab report pdf on my pendulum experiment'
  const run = {
    qid: q, text, source: 'chat', at, plan: { planner: 'heuristic', subtasks: [{ tid: `${q}.1`, text, depends_on: [] }] },
    tasks: [{ tid: `${q}.1`, text, agent: 'create', pick: 'create', reason: 'fixture', probabilities: { create: 0.93, knowledge: 0.07 }, confidence: 0.93,
      urgency: 0.5, unsafe: 0.01, clear: 0.92, jev_ms: 380, model: 'jev-mock', agent_ms: 48000, answer: 'Made photosynthesis.pptx and pendulum-lab-report.pdf.', ok: true,
      source: null, engine: 'claude-code', created_files: files }],
    merged: { answer: 'Here is your photosynthesis deck in a dark design, and the lab report. Open **Design** on a file to restyle it for free or change fonts.', engine: 'single', primary_file: 'cf_studio_deck' },
    total_ms: 49000, error: null, status: 'done', engine: null, session_id: 's_studio', compare_id: null, files: [], agent: 'create',
  }
  return {
    session: { id: 's_studio', title: 'Studio designs', created: at, updated: at },
    run, files,
    designs: { cf_studio_deck: deck, cf_studio_pdf: pdf, cf_studio_nospec: { ...deck, nospec: true } },
    designOf,
  }
}

// Every visible string on the About page lives here, so copy can be reviewed in one place.
// House rules for this page: no em or en dashes, plain verbs, numbers only where they are real.

export const LINKS = {
  github: 'https://github.com/ayushap18/tracegraph',
  app: '#/',
  live: '#/live',
} as const

export const SECTIONS = [
  { id: 'about-features', label: 'Features' },
  { id: 'about-how', label: 'How it works' },
  { id: 'about-faq', label: 'FAQ' },
]

// One label per intent, used everywhere it appears.
export const CTA = {
  open: 'Open TraceGraph',
  live: 'Watch a live trace',
  source: 'Explore the source',
} as const

export const HERO = {
  kicker: 'Open source on GitHub',
  title: 'One question. The right agents.',
  titleMuted: 'The whole picture.',
  lede: 'TraceGraph plans your question, routes every step to the right agent, and traces the whole run live.',
  shotAlt: 'A replay of a real run: one query split into three subtasks, routed by Jev to the time, currency and knowledge agents, then merged into a response.',
}

export const ENGINES = {
  label: 'Answers come from the tools you already use',
  items: [
    { name: 'claude-code', label: 'Claude Code' },
    { name: 'codex', label: 'Codex' },
    { name: 'agy', label: 'Antigravity' },
    { name: 'opencode', label: 'OpenCode' },
    { name: 'api', label: 'Any API key' },
    { name: 'none', label: 'Built-in tools' },
  ],
}

export const FEATURES = {
  title: 'More than a chat window.',
  lede: 'A workspace for asking, inspecting and improving. Every run stays connected to the decisions behind it.',
  chat: { title: 'Chat with follow-ups', text: 'Sessions keep context, so “and in GBP?” just works after a currency question.' },
  plan: { title: 'Plans multi-part questions', text: 'One message can hold several asks. Dependent steps wait for the answers they need.' },
  route: { title: 'Routes every step', text: 'Jev picks the agent for each step and shows how confident it is.' },
  compare: { title: 'Compare engines', text: 'Send one question to several engines and read the answers side by side.' },
  evals: { title: 'Measure accuracy', text: 'Run the eval suite through the real pipeline and watch routing quality over time.' },
  agents: { title: 'Bring your own agents', text: 'Describe an agent with a prompt. The router starts sending it the questions it is good at.' },
  files: { title: 'Ask about your files', text: 'Attach text, CSV, JSON or PDF. Document and data agents search and summarise them.' },
  guards: { title: 'Guards on every route', text: 'Unclear requests get a follow-up question. Requests Jev flags as unsafe are declined.' },
}

export const PIPELINE = {
  title: 'Follow the work, step by step.',
  lede: 'Independent steps run in parallel. Dependent steps wait for the context they need.',
  steps: [
    { key: 'ask', title: 'Ask', text: 'Type anything in Chat, or several things at once. Attach a file if the answer lives in it.' },
    { key: 'plan', title: 'Plan', text: 'The planner breaks the message into self-contained steps and records which ones depend on others.' },
    { key: 'route', title: 'Route', text: 'Jev sends each step to the best agent, with a confidence score, in parallel wherever it can.' },
    { key: 'merge', title: 'Merge', text: 'Answers come back and merge into one reply. The full trace stays attached to it.' },
  ],
  example: 'What’s the weather in Paris, what is 100 EUR in INR, and what should I know about the city?',
}

export const QUICKSTART = {
  title: 'Run it on your machine.',
  lede: 'A Python backend, a React workspace and a live event stream between them. You need Python 3.10+, Node 20+ and a TypeSafe API key.',
  code: [
    { c: '# clone and install' },
    { t: 'git clone https://github.com/ayushap18/tracegraph.git && cd tracegraph' },
    { t: 'python3 -m venv .venv && .venv/bin/pip install -r requirements.txt' },
    { t: 'cp .env.example .env', c: '# then set TYPESAFE_API_KEY' },
    { c: '# build the workspace and start' },
    { t: 'cd web && npm install && npm run build && cd ..' },
    { t: '.venv/bin/python server.py', c: '# http://localhost:8777' },
  ] as Array<{ t?: string; c?: string }>,
  stack: ['Python', 'aiohttp', 'SQLite', 'Server-Sent Events', 'TypeSafe Jev', 'React 18', 'TypeScript', 'Vite', 'D3', 'Tailwind CSS', 'shadcn/ui', 'Motion', 'pytest'],
}

export const FAQ = {
  title: 'Common questions',
  items: [
    { q: 'Do I need an API key?', a: 'Jev routing needs a TypeSafe API key. Answers can come from Claude Code, Codex, Antigravity or OpenCode on the login you already have, from any OpenAI-compatible API key (OpenAI, OpenRouter, Gemini, Groq, DeepSeek, Mistral, xAI or your own endpoint), or from built-in tools that need no LLM key at all.' },
    { q: 'Which engine answers my questions?', a: 'With TG_ENGINE=auto the first installed CLI wins, in the order Claude Code, Codex, Antigravity, OpenCode, then each API key you have set. With none of them, TraceGraph runs keyless. You can switch engines from the app at any time.' },
    { q: 'Are my keys passed on to the CLIs?', a: 'No. Each call runs in an empty scratch directory with a scrubbed environment. The Jev key is never passed on, and API keys are removed so the CLI stays on your plan.' },
    { q: 'Where are runs and chats stored?', a: 'Locally, in SQLite: runs, chat sessions, custom agents, uploaded files and eval results. Only the routing calls to Jev and the calls to the engine you pick leave your machine.' },
    { q: 'Can I work on the UI without a backend?', a: 'Yes. Run npm run mock in the web folder to replay a scripted event stream, then point the dev server at it.' },
  ],
}

export const CLOSING = {
  title: 'Every question has a path.',
  titleMuted: 'See yours.',
}

export const FOOTER = {
  tagline: 'Plans your question, routes every step, and traces the whole run. Built around TypeSafe Jev.',
  groups: [
    { title: 'On this page', links: SECTIONS.map(s => ({ label: s.label, section: s.id })) },
    { title: 'Workspace', links: [
      { label: 'Chat', href: '#/' },
      { label: 'Live traces', href: '#/live' },
      { label: 'Run history', href: '#/runs' },
      { label: 'Evals', href: '#/evals' },
    ] },
    { title: 'Source', links: [
      { label: 'GitHub', href: LINKS.github, external: true },
      { label: 'Quick start', href: `${LINKS.github}#quick-start`, external: true },
      { label: 'Report an issue', href: `${LINKS.github}/issues`, external: true },
    ] },
  ] as Array<{ title: string; links: Array<{ label: string; href?: string; section?: string; external?: boolean }> }>,
}

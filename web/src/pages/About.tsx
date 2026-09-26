import { useEffect, useState } from 'react'
import { AgentIcon, EngineIcon, GithubMark, Icon, Logo, type UiIconName } from '../icons'
import { colorOf } from '../protocol'
import { useTheme, THEME_ICON, THEME_LABEL } from '../theme'

// The landing page: what TraceGraph is, a living mini trace, features, how it works, engines and stack.

const GITHUB = 'https://github.com/ayushap18/tracegraph'

const FEATURES: Array<{ icon: UiIconName; title: string; text: string }> = [
  { icon: 'planner', title: 'Plans multi-part questions', text: 'One message can hold several asks. The planner splits it into steps, and dependent steps wait for the answers they need.' },
  { icon: 'jev', title: 'Routes every step', text: 'Jev picks the right agent for each step with a confidence score, and guards catch unclear or unsafe requests.' },
  { icon: 'graph', title: 'Traces everything live', text: 'Watch each run flow through planner, router, agents and merger as a graph, with a timeline of every stage.' },
  { icon: 'chat', title: 'Chat with follow-ups', text: 'Sessions keep context, so “and in GBP?” just works after a currency question.' },
  { icon: 'compare', title: 'Compare engines', text: 'Send one question to Claude Code, Codex, Antigravity or the API and read the answers side by side.' },
  { icon: 'evals', title: 'Measure accuracy', text: 'Run the eval suite through the real pipeline and track accuracy and silent failures over time.' },
  { icon: 'file-text', title: 'Ask about your files', text: 'Attach text, CSV, JSON or PDF files. Document and data agents search and summarise them.' },
  { icon: 'agents', title: 'Bring your own agents', text: 'Describe a custom agent with a prompt and the router starts sending it the right questions.' },
]

const STEPS: Array<{ icon: UiIconName; title: string; text: string }> = [
  { icon: 'query', title: 'Ask', text: 'Type anything in Chat, or several things at once.' },
  { icon: 'planner', title: 'Plan', text: 'The planner breaks it into self-contained steps with their dependencies.' },
  { icon: 'jev', title: 'Route', text: 'Jev sends each step to the best agent, in parallel where it can.' },
  { icon: 'merger', title: 'Merge', text: 'Answers come back and merge into one reply, with the full trace kept.' },
]

const ENGINES = [
  { name: 'claude-code', label: 'Claude Code' }, { name: 'codex', label: 'Codex' }, { name: 'agy', label: 'Antigravity' },
  { name: 'anthropic', label: 'Anthropic API' }, { name: 'none', label: 'Keyless' },
]

const STACK = ['Python', 'aiohttp', 'SQLite', 'Server-Sent Events', 'TypeSafe Jev', 'React 18', 'TypeScript', 'Vite', 'd3', 'Lucide']

function useReducedMotion() {
  const [reduced, setReduced] = useState(() => typeof matchMedia !== 'undefined' && matchMedia('(prefers-reduced-motion: reduce)').matches)
  useEffect(() => {
    const mq = matchMedia('(prefers-reduced-motion: reduce)')
    const on = () => setReduced(mq.matches)
    mq.addEventListener?.('change', on)
    return () => mq.removeEventListener?.('change', on)
  }, [])
  return reduced
}

// A small, self-running trace: query → plan → router → three agents → answer. Pure SVG + CSS.
function MiniTrace() {
  const reduced = useReducedMotion()
  const agents = ['weather', 'currency', 'knowledge']
  const ys = [46, 110, 174]
  const paths = {
    q: 'M86,110 C120,110 124,110 150,110',
    r: ys.map(y => `M226,110 C262,110 266,${y} 300,${y}`),
    a: ys.map(y => `M392,${y} C426,${y} 430,110 462,110`),
  }
  return (
    <svg className="mini-trace" viewBox="0 0 560 220" role="img" aria-label="Animated illustration: a query is planned, routed to three agents and merged into one answer">
      <defs>
        <linearGradient id="mt-g" x1="0" x2="1"><stop offset="0" stopColor="var(--accent)" /><stop offset="1" stopColor="var(--accent2)" /></linearGradient>
      </defs>
      <g className="mt-wires">
        <path d={paths.q} />
        {paths.r.map((d, i) => <path key={'r' + i} d={d} style={{ stroke: colorOf(agents[i]) }} />)}
        {paths.a.map((d, i) => <path key={'a' + i} d={d} style={{ stroke: colorOf(agents[i]) }} />)}
      </g>
      <g className="mt-node" transform="translate(10,86)">
        <rect width="76" height="48" rx="10" /><text x="38" y="21" textAnchor="middle" className="mt-t">Query</text><text x="38" y="36" textAnchor="middle" className="mt-s">3 asks</text>
      </g>
      <g className="mt-node mt-hub" transform="translate(150,80)">
        <rect width="76" height="60" rx="10" />
        <text x="38" y="26" textAnchor="middle" className="mt-t">Jev</text><text x="38" y="42" textAnchor="middle" className="mt-s">plan + route</text>
      </g>
      {agents.map((a, i) => (
        <g key={a} className="mt-node mt-agent" transform={`translate(300,${ys[i] - 20})`} style={{ animationDelay: `${0.4 + i * 0.25}s` }}>
          <rect width="92" height="40" rx="10" style={{ stroke: colorOf(a) }} />
          <AgentIcon agent={a} x={10} y={12} size={16} style={{ color: colorOf(a) }} />
          <text x="32" y="25" className="mt-t">{a}</text>
        </g>
      ))}
      <g className="mt-node mt-answer" transform="translate(462,86)">
        <rect width="88" height="48" rx="10" /><text x="44" y="21" textAnchor="middle" className="mt-t">Answer</text><text x="44" y="36" textAnchor="middle" className="mt-s">merged</text>
      </g>
      {!reduced && (
        <g className="mt-packets">
          <circle r="4" fill="var(--accent)"><animateMotion dur="3.2s" repeatCount="indefinite" path={paths.q} keyPoints="0;1;1" keyTimes="0;0.2;1" calcMode="linear" /></circle>
          {paths.r.map((d, i) => (
            <circle key={'pr' + i} r="4" fill={colorOf(agents[i])}>
              <animateMotion dur="3.2s" repeatCount="indefinite" path={d} keyPoints="0;0;1;1" keyTimes={`0;0.25;${0.5 + i * 0.04};1`} calcMode="linear" />
            </circle>
          ))}
          {paths.a.map((d, i) => (
            <circle key={'pa' + i} r="4" fill={colorOf(agents[i])}>
              <animateMotion dur="3.2s" repeatCount="indefinite" path={d} keyPoints="0;0;1;1" keyTimes={`0;${0.58 + i * 0.05};${0.8 + i * 0.03};1`} calcMode="linear" />
            </circle>
          ))}
        </g>
      )}
    </svg>
  )
}

export default function About() {
  const { theme, cycle } = useTheme()
  return (
    <div className="about">
      <nav className="about-nav" aria-label="About">
        <a href="#/" className="about-brand"><Logo size={28} /><span>TraceGraph</span></a>
        <div className="about-links">
          <a href="#features" onClick={e => { e.preventDefault(); document.getElementById('features')?.scrollIntoView({ behavior: 'smooth' }) }}>Features</a>
          <a href="#how" onClick={e => { e.preventDefault(); document.getElementById('how')?.scrollIntoView({ behavior: 'smooth' }) }}>How it works</a>
          <a href={GITHUB} target="_blank" rel="noopener noreferrer" className="gh"><GithubMark size={16} /><span>GitHub</span></a>
          <button type="button" className="icon-btn icon-btn-ghost icon-btn-md" onClick={cycle} aria-label={`Theme: ${THEME_LABEL[theme]}`} title={`Theme: ${THEME_LABEL[theme]}`}>
            <Icon name={THEME_ICON[theme]} size={16} />
          </button>
          <a href="#/" className="btn btn-primary btn-sm"><span className="btn-label">Open app</span><Icon name="arrow-right" size={14} /></a>
        </div>
      </nav>

      <header className="hero">
        <div className="hero-copy">
          <span className="eyebrow"><Icon name="sparkles" size={14} />Multi-agent routing you can see</span>
          <h1>Ask once. Watch it <span className="grad">plan, route and answer.</span></h1>
          <p className="lede">TraceGraph splits your question into steps, sends each one to the right agent, and merges the answers, with a live trace of every decision along the way.</p>
          <div className="hero-cta">
            <a href="#/" className="btn btn-primary btn-lg"><Icon name="chat" size={18} /><span className="btn-label">Open app</span></a>
            <a href="#/live" className="btn btn-secondary btn-lg"><Icon name="live" size={18} /><span className="btn-label">See it live</span></a>
            <a href={GITHUB} target="_blank" rel="noopener noreferrer" className="btn btn-ghost btn-lg"><GithubMark size={18} /><span className="btn-label">GitHub</span></a>
          </div>
        </div>
        <div className="hero-art"><MiniTrace /></div>
      </header>

      <section id="features" className="about-section" aria-labelledby="feat-h">
        <h2 id="feat-h">Everything in one trace</h2>
        <p className="section-lede">From a single question to evals across engines, every run is recorded and inspectable.</p>
        <div className="feature-grid">
          {FEATURES.map(f => (
            <article key={f.title} className="feature">
              <span className="feature-icon"><Icon name={f.icon} size={20} /></span>
              <h3>{f.title}</h3>
              <p>{f.text}</p>
            </article>
          ))}
        </div>
      </section>

      <section id="how" className="about-section" aria-labelledby="how-h">
        <h2 id="how-h">How it works</h2>
        <ol className="how-steps">
          {STEPS.map((s, i) => (
            <li key={s.title}>
              <span className="how-n">{i + 1}</span>
              <span className="how-icon"><Icon name={s.icon} size={18} /></span>
              <h3>{s.title}</h3>
              <p>{s.text}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="about-section" aria-labelledby="eng-h">
        <h2 id="eng-h">Works with the engine you already have</h2>
        <ul className="engine-row">
          {ENGINES.map(e => <li key={e.name}><EngineIcon name={e.name} size={22} /><span>{e.label}</span></li>)}
        </ul>
      </section>

      <section className="about-section" aria-labelledby="stack-h">
        <h2 id="stack-h">Built with</h2>
        <ul className="stack">{STACK.map(s => <li key={s}>{s}</li>)}</ul>
      </section>

      <footer className="about-foot">
        <span><Logo size={18} /> TraceGraph</span>
        <span className="muted">Open source · <a href={GITHUB} target="_blank" rel="noopener noreferrer">github.com/ayushap18/tracegraph</a></span>
        <a href="#/" className="btn btn-primary btn-sm"><span className="btn-label">Open app</span><Icon name="arrow-right" size={14} /></a>
      </footer>
    </div>
  )
}

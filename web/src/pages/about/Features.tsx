import type { ReactNode } from 'react'
import { motion } from 'motion/react'
import { AgentIcon, EngineIcon, Icon, Logo, type UiIconName } from '../../icons'
import { colorOf } from '../../protocol'
import { cn } from '@/lib/utils'
import { FEATURES as F } from './content'
import { Container, SectionHeading, SpotlightCard, TileCopy, rise, stagger } from './primitives'

/* Bento, 8 features in 8 cells (desktop, 4 columns):
     [ chat 2x2      ][ plan  ][ route  ]
     [               ][ compare][ evals ]
     [ agents 2x1    ][ files ][ guards ]
   Tablet: 2 columns, chat and agents span both. Phone: one column. */

const tint = (c: string, pct = 12) => `color-mix(in srgb, ${c} ${pct}%, transparent)`

/** Upper visual well shared by the small tiles, so every copy block starts on the same line. */
function Well({ children, className }: { children: ReactNode; className?: string }) {
  return <div aria-hidden className={cn('relative h-[148px] overflow-hidden border-b border-border bg-background/40', className)}>{children}</div>
}

function PlanVisual() {
  const steps = [
    { n: 1, text: 'Weather in Paris', agent: 'weather' },
    { n: 2, text: '100 EUR in INR', agent: 'currency' },
    { n: 3, text: 'About the city', agent: 'knowledge' },
  ]
  return (
    <Well className="flex flex-col justify-center gap-1.5 px-5">
      {steps.map((s, i) => (
        <motion.div
          key={s.n}
          initial={{ opacity: 0, x: -10 }}
          whileInView={{ opacity: 1, x: 0 }}
          viewport={{ once: true }}
          transition={{ delay: 0.15 + i * 0.12, duration: 0.6, ease: [0.16, 1, 0.3, 1] }}
          className="flex items-center gap-2.5 rounded-md border border-border bg-card py-1.5 pr-3 pl-1.5 text-[12.5px]"
          style={{ boxShadow: `inset 2px 0 0 ${colorOf(s.agent)}` }}
        >
          <span className="grid size-5 place-items-center rounded-[5px] font-mono text-[11px] font-medium" style={{ background: tint(colorOf(s.agent), 16), color: colorOf(s.agent) }}>{s.n}</span>
          <span className="truncate font-medium text-foreground">{s.text}</span>
          <span className="ml-auto font-mono text-[11px] text-muted-foreground">{s.agent}</span>
        </motion.div>
      ))}
    </Well>
  )
}

function RouteVisual() {
  const picks = [{ agent: 'currency', conf: '100%' }, { agent: 'knowledge', conf: '68%' }]
  return (
    <Well className="flex items-center justify-center px-5">
      <span className="grid size-12 shrink-0 place-items-center rounded-lg border border-border bg-card text-primary shadow-tile">
        <Icon name="jev" size={22} />
      </span>
      <svg width="52" height="84" viewBox="0 0 52 84" fill="none" className="shrink-0" aria-hidden>
        <path d="M0 42 C26 42 26 21 52 21" stroke={colorOf('currency')} strokeWidth="1.5" strokeLinecap="round" opacity=".8" />
        <path d="M0 42 C26 42 26 63 52 63" stroke={colorOf('knowledge')} strokeWidth="1.5" strokeLinecap="round" opacity=".55" strokeDasharray="3 4" />
      </svg>
      <div className="flex flex-col gap-3">
        {picks.map((p, i) => (
          <span key={p.agent} className={cn('flex h-[30px] items-center gap-2 rounded-md border border-border bg-card px-2.5 text-[12.5px] font-medium', i === 1 && 'opacity-70')}>
            <AgentIcon agent={p.agent} size={14} tinted />
            {p.agent}
            <span className="ml-1 rounded-full px-1.5 font-mono text-[11px]" style={{ background: tint(colorOf(p.agent), 16), color: colorOf(p.agent) }}>{p.conf}</span>
          </span>
        ))}
      </div>
    </Well>
  )
}

function CompareVisual() {
  const engines = [{ name: 'claude-code', label: 'Claude Code' }, { name: 'codex', label: 'Codex' }]
  return (
    <Well className="flex flex-col justify-center gap-2 px-5">
      <span className="font-mono text-[11px] text-muted-foreground">100 USD in EUR?</span>
      <div className="grid grid-cols-2 gap-2">
        {engines.map((e, i) => (
          <motion.div
            key={e.name}
            initial={{ opacity: 0, y: 8 }}
            whileInView={{ opacity: 1, y: 0 }}
            viewport={{ once: true }}
            transition={{ delay: 0.15 + i * 0.12, duration: 0.55, ease: [0.16, 1, 0.3, 1] }}
            className="rounded-md border border-border bg-card p-2.5"
          >
            <span className="flex items-center gap-1.5 text-[11.5px] font-medium text-muted-foreground">
              <EngineIcon name={e.name} size={13} strokeWidth={1.7} />{e.label}
            </span>
            <span className="mt-1.5 block text-[13px] font-semibold tracking-[-0.01em] text-foreground">87.70 EUR</span>
          </motion.div>
        ))}
      </div>
    </Well>
  )
}

function FilesVisual() {
  const files: Array<{ icon: UiIconName; ext: string }> = [
    { icon: 'file-text', ext: 'TXT' }, { icon: 'file-csv', ext: 'CSV' }, { icon: 'file-json', ext: 'JSON' }, { icon: 'file', ext: 'PDF' },
  ]
  return (
    <Well className="grid grid-cols-2 content-center gap-2 px-6">
      {files.map(f => (
        <span key={f.ext} className="flex items-center gap-2 rounded-md border border-border bg-card px-2.5 py-2 font-mono text-[11.5px] text-muted-foreground">
          <Icon name={f.icon} size={15} className="text-primary" />{f.ext}
        </span>
      ))}
    </Well>
  )
}

function GuardsVisual() {
  const rows = [
    { agent: 'clarify', label: 'Asks a follow-up' },
    { agent: 'blocked', label: 'Declines it' },
  ]
  return (
    <Well className="flex flex-col justify-center gap-2 px-5">
      {rows.map(r => (
        <span key={r.agent} className="flex items-center gap-2.5 rounded-md border border-border bg-card px-2.5 py-2 text-[12.5px]">
          <span className="grid size-6 place-items-center rounded-[6px]" style={{ background: tint(colorOf(r.agent), 16) }}><AgentIcon agent={r.agent} size={14} tinted /></span>
          <span className="font-medium text-foreground">{r.agent}</span>
          <span className="ml-auto truncate text-muted-foreground">{r.label}</span>
        </span>
      ))}
    </Well>
  )
}

// Routing heatmap, drawn live in theme colours: each column is one subtask, the lit cell the agent Jev chose.
const HEAT_ROWS = ['math', 'weather', 'time', 'currency', 'knowledge', 'code']
const HEAT_PICKS = [0, 4, 2, 3, 1, 4, 5, 1, 3, 2, 4, 1, 0, 3, 1, 4, 2, 5, 1, 3, 4, 1]

function HeatmapVisual() {
  return (
    <Well className="flex flex-col justify-center gap-2.5 px-5">
      <span className="font-mono text-[11px] text-muted-foreground">routing heatmap</span>
      <div className="grid gap-[3px]" style={{ gridTemplateColumns: `repeat(${HEAT_PICKS.length}, minmax(0, 1fr))` }}>
        {HEAT_ROWS.map((agent, r) =>
          HEAT_PICKS.map((pick, c) => (
            <motion.span
              key={agent + c}
              initial={{ opacity: 0 }}
              whileInView={{ opacity: 1 }}
              viewport={{ once: true }}
              transition={{ delay: c * 0.025, duration: 0.4 }}
              className="h-[11px] rounded-[2px]"
              style={{ background: pick === r ? colorOf(agent) : 'color-mix(in srgb, var(--muted) 14%, transparent)' }}
            />
          )),
        )}
      </div>
    </Well>
  )
}

function Bubble({ children, delay }: { children: ReactNode; delay: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true }}
      transition={{ delay, duration: 0.55, ease: [0.16, 1, 0.3, 1] }}
      className="ml-auto max-w-[80%] rounded-2xl rounded-br-md bg-primary px-3.5 py-2 text-[13px] leading-snug text-primary-foreground"
    >
      {children}
    </motion.div>
  )
}

function Reply({ children, chips, delay, note }: { children: ReactNode; chips: string[]; delay: number; note?: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      whileInView={{ opacity: 1, y: 0 }}
      viewport={{ once: true }}
      transition={{ delay, duration: 0.55, ease: [0.16, 1, 0.3, 1] }}
      className="rounded-lg border border-border bg-card p-3.5 shadow-tile sm:max-w-[92%]"
    >
      <div className="flex items-center gap-2 text-[12px] font-semibold text-foreground">
        <Logo size={16} />TraceGraph
        <span className="hidden items-center gap-1 rounded-full border border-border px-1.5 py-px font-mono text-[10.5px] font-normal whitespace-nowrap text-muted-foreground sm:flex"><EngineIcon name="claude-code" size={11} />claude-code</span>
        {note && <span className="ml-auto flex items-center gap-1 rounded-full bg-primary/12 px-2 py-0.5 text-[10.5px] font-medium whitespace-nowrap text-primary"><Icon name="history" size={11} />{note}</span>}
      </div>
      <div className="mt-2 space-y-1 text-[13px] leading-relaxed text-foreground">{children}</div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-border pt-2.5">
        {chips.map(c => (
          <span key={c} className="flex items-center gap-1 rounded-full border border-border px-1.5 py-px font-mono text-[10.5px] whitespace-nowrap text-muted-foreground">
            <AgentIcon agent={c} size={11} tinted />{c} 100%
          </span>
        ))}
        <span className="ml-auto flex items-center gap-1 text-[11.5px] font-medium whitespace-nowrap text-primary">View trace<Icon name="arrow-right" size={12} /></span>
      </div>
    </motion.div>
  )
}

function ChatVisual() {
  return (
    <div aria-hidden className="flex flex-1 flex-col justify-end gap-3 px-6 pb-6">
      <Bubble delay={0.1}>Convert 100 USD to EUR and what’s the weather in Paris?</Bubble>
      <Reply chips={['currency', 'weather']} delay={0.3}>
        <p>100.00 USD = 87.70 EUR.</p>
        <p className="text-muted-foreground">Paris is 18°C and overcast, with wind at 6 km/h.</p>
      </Reply>
      <Bubble delay={0.55}>and in GBP?</Bubble>
      <Reply chips={['currency']} delay={0.75} note="kept context">
        <p>100.00 USD = 75.45 GBP.</p>
      </Reply>
    </div>
  )
}

const BUILT_IN = ['math', 'weather', 'time', 'currency', 'knowledge', 'code', 'research', 'document', 'data']

function AgentsVisual() {
  return (
    <div aria-hidden className="flex flex-wrap content-center gap-2 px-6 pb-6 lg:px-8 lg:pb-0">
      {BUILT_IN.map(a => (
        <span key={a} className="flex items-center gap-1.5 rounded-full border border-border bg-card/80 py-1 pr-3 pl-1 text-[12.5px] font-medium text-foreground backdrop-blur">
          <span className="grid size-6 place-items-center rounded-full" style={{ background: tint(colorOf(a), 16) }}><AgentIcon agent={a} size={13} tinted /></span>
          {a}
        </span>
      ))}
      <span className="flex items-center gap-1.5 rounded-full border border-dashed border-primary/60 bg-primary/10 py-1 pr-3 pl-1 text-[12.5px] font-medium text-primary">
        <span className="grid size-6 place-items-center rounded-full bg-primary/15"><Icon name="plus" size={13} /></span>
        your agent
      </span>
    </div>
  )
}

export function Features() {
  return (
    <section id="about-features" tabIndex={-1} aria-labelledby="features-title" className="scroll-mt-20 py-20 outline-none sm:py-28">
      <Container>
        <SectionHeading id="features-title" title={F.title} lede={F.lede} />

        <motion.div
          variants={stagger}
          initial="hidden"
          whileInView="show"
          viewport={{ once: true, margin: '0px 0px -15% 0px' }}
          className="mt-12 grid grid-cols-1 gap-3 sm:mt-14 md:grid-cols-2 lg:grid-cols-4 lg:gap-4"
        >
          <motion.div variants={rise} className="md:col-span-2 lg:row-span-2">
            <SpotlightCard className="h-full min-h-[420px] bg-[radial-gradient(var(--dots)_1px,transparent_1px)] [background-size:20px_20px]">
              <TileCopy title={F.chat.title} text={F.chat.text} />
              <ChatVisual />
            </SpotlightCard>
          </motion.div>

          <motion.div variants={rise}><SpotlightCard className="h-full"><PlanVisual /><TileCopy title={F.plan.title} text={F.plan.text} /></SpotlightCard></motion.div>
          <motion.div variants={rise}><SpotlightCard className="h-full"><RouteVisual /><TileCopy title={F.route.title} text={F.route.text} /></SpotlightCard></motion.div>
          <motion.div variants={rise}><SpotlightCard className="h-full"><CompareVisual /><TileCopy title={F.compare.title} text={F.compare.text} /></SpotlightCard></motion.div>

          <motion.div variants={rise}>
            <SpotlightCard className="h-full">
              <HeatmapVisual />
              <TileCopy title={F.evals.title} text={F.evals.text} />
            </SpotlightCard>
          </motion.div>

          <motion.div variants={rise} className="md:col-span-2">
            <SpotlightCard className="h-full bg-[linear-gradient(135deg,color-mix(in_srgb,var(--accent)_10%,var(--panel)),var(--panel)_60%)] lg:grid lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)] lg:items-center">
              <TileCopy title={F.agents.title} text={F.agents.text} className="lg:py-8" />
              <AgentsVisual />
            </SpotlightCard>
          </motion.div>

          <motion.div variants={rise}><SpotlightCard className="h-full"><FilesVisual /><TileCopy title={F.files.title} text={F.files.text} /></SpotlightCard></motion.div>
          <motion.div variants={rise}><SpotlightCard className="h-full"><GuardsVisual /><TileCopy title={F.guards.title} text={F.guards.text} /></SpotlightCard></motion.div>
        </motion.div>
      </Container>
    </section>
  )
}

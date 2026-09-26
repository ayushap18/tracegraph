import { motion } from 'motion/react'
import { EngineIcon } from '../../icons'
import { ENGINES } from './content'
import { Container, rise, stagger } from './primitives'

// Engine wall directly under the hero: marks and names only, no category captions.
export function Engines() {
  return (
    <section aria-label="Supported engines" className="py-16 sm:py-20">
      <Container className="flex flex-col items-center gap-8">
        <p className="text-center text-sm text-muted-foreground">{ENGINES.label}</p>
        <motion.ul
          variants={stagger}
          initial="hidden"
          whileInView="show"
          viewport={{ once: true, margin: '0px 0px -10% 0px' }}
          className="grid w-full grid-cols-2 gap-x-6 gap-y-6 sm:grid-cols-3 lg:flex lg:justify-between"
        >
          {ENGINES.items.map(e => (
            <motion.li
              key={e.name}
              variants={rise}
              className="flex items-center justify-center gap-2.5 text-[15px] font-medium tracking-[-0.01em] text-muted-foreground transition-colors hover:text-foreground lg:justify-start"
            >
              <EngineIcon name={e.name} size={20} strokeWidth={1.6} />
              {e.label}
            </motion.li>
          ))}
        </motion.ul>
      </Container>
    </section>
  )
}

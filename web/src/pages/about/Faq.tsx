import { Accordion, AccordionContent, AccordionItem, AccordionTrigger } from '@/components/ui/accordion'
import { FAQ } from './content'
import { Container, Reveal } from './primitives'

export function Faq() {
  return (
    <section id="about-faq" tabIndex={-1} aria-labelledby="faq-title" className="scroll-mt-20 border-t border-border py-20 outline-none sm:py-28">
      <Container className="max-w-[820px]">
        <Reveal>
          <h2 id="faq-title" className="text-[clamp(28px,3.6vw,42px)] leading-[1.08] font-semibold tracking-[-0.035em] text-foreground">{FAQ.title}</h2>
        </Reveal>
        <Reveal delay={0.08} className="mt-8">
          <Accordion type="single" collapsible defaultValue="q0" className="border-t border-border">
            {FAQ.items.map((f, i) => (
              <AccordionItem key={f.q} value={'q' + i}>
                <AccordionTrigger>{f.q}</AccordionTrigger>
                <AccordionContent>{f.a}</AccordionContent>
              </AccordionItem>
            ))}
          </Accordion>
        </Reveal>
      </Container>
    </section>
  )
}

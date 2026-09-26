import { MotionConfig } from 'motion/react'
import { Nav } from './about/Nav'
import { Hero } from './about/Hero'
import { Engines } from './about/Engines'
import { Features } from './about/Features'
import { Pipeline } from './about/Pipeline'
import { QuickStart } from './about/QuickStart'
import { Faq } from './about/Faq'
import { Closing } from './about/Closing'

// The About / landing page. Built on Tailwind v4 + shadcn/ui (src/components/ui) + Motion,
// scoped under .tg-scope so none of it leaks into the workspace. Copy lives in about/content.ts.
//
//   Nav · Hero (product shot) · Engines · Features bento · Pipeline (scroll story) · Quick start · FAQ · Closing
export default function About() {
  return (
    <MotionConfig reducedMotion="user">
      <div className="tg-scope min-h-[100dvh] bg-background text-foreground antialiased">
        <Nav />
        <Hero />
        <Engines />
        <Features />
        <Pipeline />
        <QuickStart />
        <Faq />
        <Closing />
      </div>
    </MotionConfig>
  )
}

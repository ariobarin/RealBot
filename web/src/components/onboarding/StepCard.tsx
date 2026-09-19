import { motion } from 'framer-motion'
import type { ReactNode } from 'react'
import { fadeUp } from '../../lib/motion'

interface StepCardProps {
  index: number
  title: string
  body: string
  active: boolean
  done: boolean
  art: ReactNode
}

export function StepCard({ index, title, body, active, done, art }: StepCardProps) {
  return (
    <motion.li
      variants={fadeUp}
      data-testid="step-card"
      data-active={active || undefined}
      className={`relative flex list-none gap-4 rounded-2xl border bg-white p-5 transition-colors duration-250 ${
        active ? 'border-brand shadow-md' : 'border-line shadow-sm'
      }`}
    >
      <div className="relative grid size-20 shrink-0 place-items-center overflow-hidden rounded-xl bg-bg-soft">
        {art}
      </div>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <motion.span
            layout
            className={`grid size-6 place-items-center rounded-full text-xs font-bold transition-colors duration-250 ${
              done ? 'bg-ok text-white' : active ? 'bg-brand text-white' : 'bg-bg-soft text-ink-2'
            }`}
          >
            {done ? (
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden>
                <motion.path
                  d="M5 12l5 5L20 7"
                  stroke="currentColor"
                  strokeWidth="3"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  initial={{ pathLength: 0 }}
                  animate={{ pathLength: 1 }}
                  transition={{ duration: 0.35, ease: 'easeOut' }}
                />
              </svg>
            ) : (
              index
            )}
          </motion.span>
          <h3 className="text-base font-semibold text-ink">{title}</h3>
        </div>
        <p className="mt-1.5 text-sm leading-relaxed text-ink-2">{body}</p>
      </div>
    </motion.li>
  )
}

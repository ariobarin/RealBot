import { motion } from 'framer-motion'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { pageVariants } from '../../lib/motion'

export function Wordmark() {
  return (
    <Link to="/" className="flex items-center gap-2 text-ink no-underline">
      <span className="grid size-8 place-items-center rounded-full bg-brand text-white shadow-sm">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
          <path
            d="M12 3 4 9v11h5v-6h6v6h5V9l-8-6Z"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinejoin="round"
          />
        </svg>
      </span>
      <span className="text-lg font-bold tracking-tight">RealBot</span>
    </Link>
  )
}

export function PageShell({
  children,
  wide = false,
  viewport = false,
}: {
  children: ReactNode
  wide?: boolean
  viewport?: boolean
}) {
  return (
    <motion.main
      variants={pageVariants}
      initial="initial"
      animate="enter"
      exit="exit"
      className={`mx-auto w-full px-6 sm:px-10 ${viewport ? 'flex h-dvh flex-col overflow-hidden pb-4 pt-4' : 'pb-20 pt-6'} ${wide ? 'max-w-[1760px]' : 'max-w-5xl'}`}
    >
      {children}
    </motion.main>
  )
}

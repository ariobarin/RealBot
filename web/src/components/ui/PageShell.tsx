import { motion } from 'framer-motion'
import type { ReactNode } from 'react'
import { pageVariants } from '../../lib/motion'

export { Wordmark } from '../brand/Logo'

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

export const EASE_OUT = [0.2, 0.8, 0.2, 1] as const

export const spring = { type: 'spring', stiffness: 300, damping: 24 } as const
export const easeOut = { duration: 0.25, ease: EASE_OUT } as const

export const fadeUp = {
  hidden: { opacity: 0, y: 16 },
  show: { opacity: 1, y: 0, transition: easeOut },
}

/** Stagger children but cap the total spread so long lists still feel snappy. */
export const staggerList = (count: number, maxTotal = 0.6) => ({
  hidden: {},
  show: { transition: { staggerChildren: Math.min(0.06, maxTotal / Math.max(count, 1)) } },
})

export const pageVariants = {
  initial: { opacity: 0, y: 8 },
  enter: { opacity: 1, y: 0, transition: { duration: 0.3, ease: EASE_OUT } },
  exit: { opacity: 0, y: -8, transition: { duration: 0.18, ease: [0.4, 0, 1, 1] as const } },
}

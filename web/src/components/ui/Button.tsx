import { motion, type HTMLMotionProps } from 'framer-motion'
import type { ReactNode } from 'react'
import { spring } from '../../lib/motion'

type Variant = 'primary' | 'secondary' | 'ghost'

const styles: Record<Variant, string> = {
  primary: 'bg-brand text-white hover:bg-brand-2 shadow-sm',
  secondary: 'bg-white text-ink border border-ink hover:bg-bg-soft',
  ghost: 'bg-transparent text-ink hover:bg-bg-soft',
}

interface ButtonProps extends Omit<HTMLMotionProps<'button'>, 'children'> {
  variant?: Variant
  children: ReactNode
}

export function Button({ variant = 'primary', className = '', children, ...rest }: ButtonProps) {
  return (
    <motion.button
      whileHover={{ scale: 1.02 }}
      whileTap={{ scale: 0.97 }}
      transition={spring}
      className={`inline-flex items-center gap-2 rounded-full px-5 py-2.5 text-sm font-semibold transition-colors duration-150 disabled:cursor-not-allowed disabled:opacity-50 ${styles[variant]} ${className}`}
      {...rest}
    >
      {children}
    </motion.button>
  )
}

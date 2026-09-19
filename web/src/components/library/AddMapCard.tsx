import { motion } from 'framer-motion'
import { Plus } from 'lucide-react'
import { Link } from 'react-router-dom'
import { fadeUp, spring } from '../../lib/motion'

const MotionLink = motion.create(Link)

export function AddMapCard() {
  return (
    <motion.li variants={fadeUp} className="list-none">
      <MotionLink
        to="/onboard"
        data-testid="add-card"
        aria-label="Add a space"
        whileHover="hover"
        whileTap={{ scale: 0.98 }}
        transition={spring}
        className="group flex aspect-[4/3] w-full flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed border-line bg-white text-ink-2 no-underline transition-colors duration-250 hover:border-brand hover:bg-brand-soft/40"
      >
        <motion.span
          variants={{ hover: { rotate: 90, scale: 1.08 } }}
          transition={spring}
          className="grid size-14 place-items-center rounded-full bg-bg-soft text-ink transition-colors duration-250 group-hover:bg-brand group-hover:text-white"
        >
          <Plus size={26} strokeWidth={2.25} />
        </motion.span>
        <span className="text-sm font-semibold text-ink">Add a space</span>
        <span className="text-xs text-ink-3">Pair a bracketbot and scan</span>
      </MotionLink>
    </motion.li>
  )
}

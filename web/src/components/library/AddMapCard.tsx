import { motion } from 'framer-motion'
import { Link } from 'react-router-dom'
import { fadeUp } from '../../lib/motion'
import { AddSpaceIcon } from '../icons/AddSpaceIcon'

const MotionLink = motion.create(Link)

export function AddMapCard() {
  return (
    <motion.li variants={fadeUp} className="list-none">
      <MotionLink
        to="/onboard"
        data-testid="add-card"
        aria-label="Add a space"
        whileHover={{ y: -3 }}
        whileTap={{ scale: 0.98 }}
        transition={{ duration: 0.25, ease: [0.2, 0.8, 0.2, 1] }}
        className="icon-hover flex aspect-[4/3] w-full flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed border-[#dddddd] bg-white text-center no-underline transition-colors duration-250 hover:border-ink"
      >
        <AddSpaceIcon size={72} />
        <span className="flex flex-col gap-0.5">
          <span className="text-[15px] font-semibold text-ink">Add a space</span>
          <span className="text-[13px] text-ink-3">Pair a bracketbot and scan</span>
        </span>
      </MotionLink>
    </motion.li>
  )
}

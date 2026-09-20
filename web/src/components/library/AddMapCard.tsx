import { motion } from 'framer-motion'
import { Link } from 'react-router-dom'
import { fadeUp } from '../../lib/motion'
import { AddSpaceIcon } from '../icons/AddSpaceIcon'

export function AddMapCard() {
  return (
    <motion.li variants={fadeUp} className="list-none">
      <Link
        to="/onboard"
        data-testid="add-card"
        aria-label="Add a space"
        className="icon-hover flex aspect-[4/3] w-full flex-col items-center justify-center gap-3 rounded-2xl border-2 border-dashed border-[#dddddd] bg-white text-center no-underline transition-colors duration-150 hover:border-ink-3 hover:bg-bg-soft"
      >
        <AddSpaceIcon size={72} />
        <span className="flex flex-col gap-0.5">
          <span className="text-[15px] font-semibold text-ink">Add a space</span>
          <span className="text-[13px] text-ink-3">Set up a scanned room</span>
        </span>
      </Link>
    </motion.li>
  )
}

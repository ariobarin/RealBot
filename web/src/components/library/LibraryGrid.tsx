import { motion } from 'framer-motion'
import type { MapSummary } from '../../lib/manifest'
import { staggerList } from '../../lib/motion'
import { AddMapCard } from './AddMapCard'
import { MapCard } from './MapCard'

/** Auto-fill grid: works for 1 map or 100 with no code change. */
export function LibraryGrid({ maps }: { maps: MapSummary[] }) {
  return (
    <motion.ul
      variants={staggerList(maps.length + 1)}
      initial="hidden"
      animate="show"
      className="m-0 grid list-none gap-x-6 gap-y-10 p-0"
      style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 260px), 1fr))' }}
    >
      <AddMapCard />
      {maps.map((m) => (
        <MapCard key={m.id} map={m} />
      ))}
    </motion.ul>
  )
}

export function LibraryGridSkeleton({ count = 3 }: { count?: number }) {
  return (
    <div
      aria-hidden
      className="grid gap-x-6 gap-y-10"
      style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(min(100%, 260px), 1fr))' }}
    >
      {Array.from({ length: count }, (_, k) => (
        <div key={k} className="space-y-3">
          <div className="skeleton aspect-[4/3] rounded-2xl" />
          <div className="skeleton h-4 w-2/3 rounded-md" />
          <div className="skeleton h-3 w-1/2 rounded-md" />
        </div>
      ))}
    </div>
  )
}

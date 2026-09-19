import { motion } from 'framer-motion'
import { Link } from 'react-router-dom'
import { fadeUp } from '../../lib/motion'
import type { MapSummary } from '../../lib/manifest'
import { Pill } from '../ui/Pill'

const statusLabel: Record<MapSummary['status'], { text: string; tone: 'ok' | 'brand' | 'neutral' }> = {
  ready: { text: 'Ready to share', tone: 'ok' },
  scanning: { text: 'Scanning', tone: 'brand' },
  draft: { text: 'Needs a scan', tone: 'neutral' },
}

const fmtDate = (iso: string) =>
  new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })

export function MapCard({ map }: { map: MapSummary }) {
  const s = statusLabel[map.status]
  return (
    <motion.li variants={fadeUp} className="list-none">
      <Link
        to={`/map/${map.id}`}
        data-testid="map-card"
        className="group block rounded-2xl text-ink no-underline outline-offset-4"
      >
        <div className="rounded-2xl">
          <div className="relative aspect-[4/3] overflow-hidden rounded-2xl bg-bg-soft transition-colors duration-150 group-hover:bg-bg-hover">
            <img
              src={map.thumb}
              alt={`SLAM floor plan of ${map.name}`}
              loading="lazy"
              className="size-full object-contain p-4"
            />
            <div className="absolute left-3 top-3">
              <Pill tone={s.tone}>
                <span className="size-1.5 rounded-full bg-current" />
                {s.text}
              </Pill>
            </div>
          </div>
          <div className="mt-3 space-y-0.5 px-0.5">
            <div className="flex items-baseline justify-between gap-3">
              <h3 className="truncate text-[15px] font-semibold">{map.name}</h3>
              {map.cloud && <span className="shrink-0 text-xs text-ink-3">3D scan</span>}
            </div>
            <p className="text-sm text-ink-2">
              ~{map.areaM2} m² · {map.rooms} rooms · scanned {fmtDate(map.scannedAt)}
            </p>
          </div>
        </div>
      </Link>
    </motion.li>
  )
}

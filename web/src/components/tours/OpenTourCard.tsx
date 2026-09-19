import { motion } from 'framer-motion'
import { fadeUp } from '../../lib/motion'
import type { OpenTour } from '../../lib/openTours'
import { Pill } from '../ui/Pill'
import { PlanThumb } from './PlanThumb'

const fmtWhen = (iso: string) =>
  new Date(iso).toLocaleString(undefined, {
    weekday: 'short',
    hour: 'numeric',
    minute: '2-digit',
  })

export function OpenTourCard({ tour, onJoin }: { tour: OpenTour; onJoin: (tour: OpenTour) => void }) {
  const when = tour.live
    ? ['Live now', tour.host && `${tour.host} is hosting`, tour.watching && `${tour.watching} watching`]
        .filter(Boolean)
        .join(' · ')
    : `Next tour ${fmtWhen(tour.nextTourAt!)}`

  return (
    <motion.li variants={fadeUp} className="list-none">
      <button
        type="button"
        onClick={() => onJoin(tour)}
        data-testid="open-tour"
        className="group block w-full rounded-2xl border-0 bg-transparent p-0 text-left text-ink outline-offset-4"
      >
        <div>
          <div className="relative aspect-[280/196] overflow-hidden rounded-2xl bg-bg-soft ring-0 ring-ink/0 transition-[box-shadow] duration-150 group-hover:ring-2 group-hover:ring-ink/10">
            <PlanThumb
              mapId={tour.mapId}
              name={`${tour.name}, ${tour.neighbourhood}`}
              robot={tour.robot}
              live={tour.live}
            />
            <div className="absolute left-3 top-3">
              {tour.live ? (
                <Pill tone="brand">
                  <span className="size-1.5 rounded-full bg-current" />
                  Live now
                </Pill>
              ) : (
                <Pill tone="neutral">{fmtWhen(tour.nextTourAt!)}</Pill>
              )}
            </div>
          </div>
          <div className="mt-3 space-y-0.5 px-0.5">
            <div className="flex items-baseline justify-between gap-3">
              <h3 className="truncate text-[15px] font-semibold">{tour.name}</h3>
              <span className="shrink-0 text-sm text-ink-2">{tour.neighbourhood}</span>
            </div>
            <p className="text-sm text-ink-2">{when}</p>
            <p className={`text-sm font-semibold ${tour.live ? 'text-brand' : 'text-ink'}`}>
              {tour.live ? 'Join the tour →' : 'Reserve a slot →'}
            </p>
          </div>
        </div>
      </button>
    </motion.li>
  )
}

import { motion } from 'framer-motion'
import { MoreHorizontal } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/useAuth'
import { CalendarIcon } from '../components/icons/CalendarIcon'
import { MapPinIcon } from '../components/icons/MapPinIcon'
import { RobotIcon } from '../components/icons/RobotIcon'
import { PlanThumb } from '../components/tours/PlanThumb'
import { Button } from '../components/ui/Button'
import { Pill } from '../components/ui/Pill'
import { TopNav } from '../components/ui/TopNav'
import { fadeUp, pageVariants, staggerList } from '../lib/motion'
import { useBookingsStore, type Booking } from '../store/useBookingsStore'

const JOIN_WINDOW_MS = 10 * 60 * 1000
const TOUR_MS = 20 * 60 * 1000

const fmtWhen = (iso: string) =>
  new Date(iso).toLocaleString(undefined, {
    weekday: 'short',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })

function phase(b: Booking, now: number): 'past' | 'soon' | 'upcoming' {
  const at = new Date(b.at).getTime()
  if (at + TOUR_MS < now) return 'past'
  if (at - JOIN_WINDOW_MS <= now) return 'soon'
  return 'upcoming'
}

/** `/bookings` — the tours you've booked (visitor) or the tours booked on your spaces (realtor). */
export function BookingsPage() {
  const { session, loginVisitor } = useAuth()
  const navigate = useNavigate()
  const { bookings, cancel } = useBookingsStore()
  const realtor = session?.role === 'realtor'
  const [now] = useState(() => Date.now())

  const join = (b: Booking) => {
    if (realtor) {
      void navigate(`/realtor/control/${encodeURIComponent(b.roomId)}`)
      return
    }
    loginVisitor(b.roomId)
    void navigate(`/user/${encodeURIComponent(b.roomId)}`)
  }

  return (
    <motion.main variants={pageVariants} initial="initial" animate="enter" exit="exit" className="min-h-dvh">
      <TopNav />
      <div className="mx-auto max-w-[1280px] px-6 pb-20 pt-10 sm:px-10">
        <motion.div variants={fadeUp} initial="hidden" animate="show">
          <h1 className="text-[32px] font-extrabold tracking-[-0.03em] sm:text-[36px]">Your bookings</h1>
          <p className="mt-1 text-[15px] text-ink-2">
            {realtor
              ? 'Tours visitors have booked on your spaces.'
              : 'Saved on this device. We also email you a link for each one.'}
          </p>
        </motion.div>

        <div className="mt-6 grid gap-8 lg:grid-cols-[minmax(0,1fr)_320px]">
          {bookings.length === 0 ? (
            <div className="icon-hover flex flex-col items-center gap-3 rounded-[20px] border border-line px-6 py-14 text-center">
              <CalendarIcon size={96} />
              <p className="text-[20px] font-extrabold tracking-tight">
                {realtor ? 'No tours booked yet' : 'Nothing booked yet'}
              </p>
              <p className="max-w-[300px] text-sm text-ink-2">
                {realtor
                  ? 'When a visitor reserves a slot on one of your spaces it shows up here.'
                  : 'Pick a listing under Open tours and reserve a slot. It only takes a name and an email.'}
              </p>
              {!realtor && (
                <Button variant="secondary" className="mt-2" onClick={() => void navigate('/#open-tours')}>
                  Browse open tours
                </Button>
              )}
            </div>
          ) : (
            <motion.ul
              variants={staggerList(bookings.length)}
              initial="hidden"
              animate="show"
              className="m-0 flex list-none flex-col gap-3 p-0"
            >
              {bookings.map((b) => {
                const p = phase(b, now)
                return (
                  <motion.li
                    key={b.id}
                    variants={fadeUp}
                    data-testid="booking"
                    className="flex items-center gap-5 rounded-[20px] border border-line bg-white p-4"
                  >
                    <div className="h-[104px] w-[148px] shrink-0 overflow-hidden rounded-xl bg-bg-soft">
                      <PlanThumb mapId={b.mapId} name={b.name} robot={b.robot} live={p === 'soon'} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <p className="flex items-baseline gap-2.5">
                        <span className="text-[17px] font-bold">{b.name}</span>
                        <span className="text-sm text-ink-2">{b.neighbourhood}</span>
                      </p>
                      <p className="mt-0.5 text-[15px]">
                        {fmtWhen(b.at)}
                        {realtor ? ` · ${b.guestName}` : ' · private tour'}
                      </p>
                      <div className="mt-1.5">
                        {p === 'soon' ? (
                          <Pill tone="brand">
                            <span className="size-1.5 rounded-full bg-current" />
                            Starting now
                          </Pill>
                        ) : p === 'past' ? (
                          <Pill tone="neutral">Toured</Pill>
                        ) : (
                          <Pill tone="ok">
                            <span className="size-1.5 rounded-full bg-current" />
                            Confirmed
                          </Pill>
                        )}
                      </div>
                    </div>
                    <div className="flex shrink-0 gap-2">
                      {p === 'soon' && (
                        <Button onClick={() => join(b)}>{realtor ? 'Go live' : 'Join tour'}</Button>
                      )}
                      {p !== 'past' && (
                        <Button
                          variant="ghost"
                          aria-label={`Cancel booking for ${b.name}`}
                          onClick={() => cancel(b.id)}
                        >
                          <MoreHorizontal size={18} /> Cancel
                        </Button>
                      )}
                    </div>
                  </motion.li>
                )
              })}
            </motion.ul>
          )}

          <aside className="flex flex-col gap-3.5">
            {realtor ? (
              <>
                <div className="icon-hover flex items-start gap-3.5 rounded-[20px] bg-bg-soft p-5">
                  <RobotIcon size={56} />
                  <div>
                    <p className="text-[15px] font-bold">Go live on time</p>
                    <p className="mt-0.5 text-[13px] leading-relaxed text-ink-2">
                      A Go live button appears ten minutes before each slot. It opens the robot dashboard for
                      that space.
                    </p>
                  </div>
                </div>
                <div className="icon-hover flex items-start gap-3.5 rounded-[20px] bg-bg-soft p-5">
                  <CalendarIcon size={56} />
                  <div>
                    <p className="text-[15px] font-bold">Set your hours</p>
                    <p className="mt-0.5 text-[13px] leading-relaxed text-ink-2">
                      Visitors can only book slots inside the hours you set per space.
                    </p>
                  </div>
                </div>
              </>
            ) : (
              <>
                <div className="icon-hover flex items-start gap-3.5 rounded-[20px] bg-bg-soft p-5">
                  <CalendarIcon size={56} />
                  <div>
                    <p className="text-[15px] font-bold">When it's time</p>
                    <p className="mt-0.5 text-[13px] leading-relaxed text-ink-2">
                      A Join button appears here ten minutes before your slot. The link in your email works
                      too.
                    </p>
                  </div>
                </div>
                <div className="icon-hover flex items-start gap-3.5 rounded-[20px] bg-bg-soft p-5">
                  <MapPinIcon size={56} />
                  <div>
                    <p className="text-[15px] font-bold">Change of plans?</p>
                    <p className="mt-0.5 text-[13px] leading-relaxed text-ink-2">
                      Cancel any time before the slot. The realtor is told automatically.
                    </p>
                  </div>
                </div>
              </>
            )}
          </aside>
        </div>
      </div>
    </motion.main>
  )
}

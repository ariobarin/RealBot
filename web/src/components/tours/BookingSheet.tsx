import { AnimatePresence, motion } from 'framer-motion'
import { Check, X } from 'lucide-react'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { EASE_OUT } from '../../lib/motion'
import type { OpenTour } from '../../lib/openTours'
import { useBookingsStore, type Booking } from '../../store/useBookingsStore'
import { Button } from '../ui/Button'
import { PlanThumb } from './PlanThumb'

const SLOTS = ['11:00', '11:30', '13:30', '14:00', '15:30', '16:00']

const inputClass =
  'mt-1.5 h-12 w-full rounded-xl border border-[#dddddd] bg-white px-3.5 text-[15px] text-ink outline-none transition focus:border-ink focus:ring-4 focus:ring-ink/5'

const fmtTime = (hhmm: string) => {
  const [h, m] = hhmm.split(':').map(Number)
  return new Date(2000, 0, 1, h, m).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
}

const localIso = (day: Date, hhmm: string) => {
  const [h, m] = hhmm.split(':').map(Number)
  const d = new Date(day)
  d.setHours(h, m, 0, 0)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(h)}:${pad(m)}:00`
}

function nextDays(count: number) {
  const out: Date[] = []
  const d = new Date()
  d.setHours(0, 0, 0, 0)
  for (let i = 1; i <= count; i++) {
    const n = new Date(d)
    n.setDate(d.getDate() + i)
    out.push(n)
  }
  return out
}

/** Right-hand sheet: pick a day and a time for a scheduled tour, leave a name and email. */
export function BookingSheet({ tour, onClose }: { tour: OpenTour | null; onClose: () => void }) {
  useEffect(() => {
    if (!tour) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [tour, onClose])

  return (
    <AnimatePresence>{tour && <SheetBody key={tour.id} tour={tour} onClose={onClose} />}</AnimatePresence>
  )
}

/** Keyed on the tour so every open starts fresh. */
function SheetBody({ tour, onClose }: { tour: OpenTour; onClose: () => void }) {
  const add = useBookingsStore((s) => s.add)
  const days = useMemo(() => nextDays(7), [])
  const [dayIdx, setDayIdx] = useState(0)
  const [slot, setSlot] = useState<string | null>(null)
  const [guestName, setGuestName] = useState('')
  const [email, setEmail] = useState('')
  const [done, setDone] = useState<Booking | null>(null)

  const day = days[dayIdx]
  const canReserve = !!slot && guestName.trim().length > 0 && /\S+@\S+\.\S+/.test(email)

  const reserve = (event: FormEvent) => {
    event.preventDefault()
    if (!slot || !canReserve) return
    setDone(
      add({
        tourId: tour.id,
        roomId: tour.roomId,
        name: tour.name,
        neighbourhood: tour.neighbourhood,
        mapId: tour.mapId,
        robot: tour.robot,
        at: localIso(day, slot),
        guestName: guestName.trim(),
        email: email.trim(),
      }),
    )
  }

  return (
    <>
      <motion.button
        type="button"
        aria-label="Close booking"
        onClick={onClose}
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.2 }}
        className="fixed inset-0 z-30 border-0 bg-ink/35 p-0"
      />
      <motion.aside
        role="dialog"
        aria-modal="true"
        aria-labelledby="booking-title"
        data-testid="booking-sheet"
        initial={{ x: '100%' }}
        animate={{ x: 0 }}
        exit={{ x: '100%' }}
        transition={{ duration: 0.32, ease: EASE_OUT }}
        className="fixed inset-y-0 right-0 z-40 flex w-full max-w-[520px] flex-col bg-white shadow-lg"
      >
        <header className="flex items-center justify-between border-b border-line px-7 py-5">
          <button
            type="button"
            onClick={onClose}
            aria-label="Close"
            className="grid size-9 place-items-center rounded-full border-0 bg-bg-soft"
          >
            <X size={15} strokeWidth={2.2} />
          </button>
          <h2 id="booking-title" className="text-[15px] font-bold">
            {done ? 'You’re booked' : 'Book a tour'}
          </h2>
          <span className="size-9" />
        </header>

        {done ? (
          <div className="flex flex-1 flex-col items-center justify-center gap-4 px-7 text-center">
            <span className="grid size-16 place-items-center rounded-full bg-[#e6f4e6] text-ok">
              <Check size={30} strokeWidth={2.5} />
            </span>
            <div>
              <p className="text-[22px] font-bold tracking-tight">{done.name}</p>
              <p className="mt-1 text-ink-2">
                {new Date(done.at).toLocaleString(undefined, {
                  weekday: 'long',
                  month: 'short',
                  day: 'numeric',
                  hour: 'numeric',
                  minute: '2-digit',
                })}
              </p>
            </div>
            <p className="max-w-[320px] text-sm text-ink-2">
              Saved on this device. A Join button shows up under Your bookings ten minutes before the slot.
            </p>
            <div className="mt-2 flex gap-2">
              <Link to="/bookings" className="no-underline">
                <Button>See your bookings</Button>
              </Link>
              <Button variant="ghost" onClick={onClose}>
                Done
              </Button>
            </div>
          </div>
        ) : (
          <form onSubmit={reserve} className="flex min-h-0 flex-1 flex-col">
            <div className="flex flex-1 flex-col gap-6 overflow-y-auto px-7 py-6">
              <div className="flex items-center gap-3.5">
                <div className="h-[72px] w-24 shrink-0 overflow-hidden rounded-xl bg-bg-soft">
                  <PlanThumb mapId={tour.mapId} name={tour.name} robot={tour.robot} live={false} />
                </div>
                <div>
                  <p className="text-[17px] font-bold">
                    {tour.name}, {tour.neighbourhood}
                  </p>
                  <p className="text-sm text-ink-2">
                    {tour.host ? `Hosted by ${tour.host} · ` : ''}20-minute tour. You drive the robot.
                  </p>
                </div>
              </div>

              <fieldset className="m-0 border-0 p-0">
                <legend className="mb-2.5 text-[15px] font-bold">Pick a day</legend>
                <div className="flex gap-2 overflow-x-auto pb-1">
                  {days.map((d, i) => {
                    const on = i === dayIdx
                    return (
                      <button
                        key={d.toISOString()}
                        type="button"
                        aria-pressed={on}
                        onClick={() => {
                          setDayIdx(i)
                          setSlot(null)
                        }}
                        className={`flex h-[60px] w-[52px] shrink-0 flex-col items-center justify-center rounded-[14px] border transition ${
                          on
                            ? 'border-ink bg-ink text-white'
                            : 'border-[#dddddd] bg-white text-ink hover:border-ink'
                        }`}
                      >
                        <span className="text-[11px] font-semibold uppercase tracking-wider opacity-70">
                          {d.toLocaleDateString(undefined, { weekday: 'short' })}
                        </span>
                        <span className="text-lg font-bold">{d.getDate()}</span>
                      </button>
                    )
                  })}
                </div>
              </fieldset>

              <fieldset className="m-0 border-0 p-0">
                <legend className="mb-2.5 text-[15px] font-bold">
                  {day.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric' })} · times
                </legend>
                <div className="flex flex-wrap gap-2">
                  {SLOTS.map((t) => {
                    const on = slot === t
                    return (
                      <button
                        key={t}
                        type="button"
                        aria-pressed={on}
                        onClick={() => setSlot(t)}
                        className={`h-10 rounded-full border px-[18px] text-sm transition ${
                          on
                            ? 'border-ink bg-ink font-semibold text-white'
                            : 'border-[#dddddd] bg-white font-medium text-ink hover:border-ink'
                        }`}
                      >
                        {fmtTime(t)}
                      </button>
                    )
                  })}
                </div>
                <p className="mt-2.5 text-[13px] text-ink-2">Times are local. Slots are private tours.</p>
              </fieldset>

              <div className="grid gap-3 sm:grid-cols-2">
                <label className="text-[13px] font-semibold">
                  Your name
                  <input
                    value={guestName}
                    onChange={(e) => setGuestName(e.target.value)}
                    autoComplete="name"
                    className={inputClass}
                  />
                </label>
                <label className="text-[13px] font-semibold">
                  Email for the link
                  <input
                    type="email"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    autoComplete="email"
                    className={inputClass}
                  />
                </label>
              </div>
            </div>

            <footer className="flex items-center justify-between gap-4 border-t border-line px-7 py-4">
              <div>
                <p className="text-[15px] font-bold">
                  {day.toLocaleDateString(undefined, {
                    weekday: 'short',
                    month: 'short',
                    day: 'numeric',
                  })}
                  {slot ? ` · ${fmtTime(slot)}` : ''}
                </p>
                <p className="text-[13px] text-ink-2">Free · no account needed</p>
              </div>
              <Button type="submit" disabled={!canReserve} className="h-[50px] px-6 text-[15px]">
                Reserve slot
              </Button>
            </footer>
          </form>
        )}
      </motion.aside>
    </>
  )
}

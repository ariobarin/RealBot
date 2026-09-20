import { ArrowRight, ChevronRight } from 'lucide-react'
import { motion } from 'framer-motion'
import { useCallback, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/useAuth'
import { BookingSheet } from '../components/tours/BookingSheet'
import { OpenTourCard } from '../components/tours/OpenTourCard'
import { WordCarousel } from '../components/tours/WordCarousel'
import { Button } from '../components/ui/Button'
import { TopNav } from '../components/ui/TopNav'
import { pageVariants, staggerList } from '../lib/motion'
import { OPEN_TOURS, type OpenTour } from '../lib/openTours'
import { requestVisitorSession } from '../lib/visitorSession'

type Filter = 'all' | 'today' | 'week'

const filters: { id: Filter; label: string }[] = [
  { id: 'all', label: 'All' },
  { id: 'today', label: 'Today' },
  { id: 'week', label: 'This week' },
]

const DAY_MS = 24 * 60 * 60 * 1000
const liveRobot = import.meta.env.PROD && import.meta.env.VITE_ROBOT_TRANSPORT === 'livekit'

function matches(tour: OpenTour, filter: Filter, now: number) {
  if (filter === 'all' || tour.live) return true
  const at = tour.nextTourAt ? new Date(tour.nextTourAt).getTime() : 0
  if (filter === 'today') return new Date(at).toDateString() === new Date(now).toDateString()
  return at >= now && at < now + 7 * DAY_MS
}

const inputClass =
  'mt-1.5 h-[52px] w-full rounded-xl border border-[#dddddd] bg-white px-4 text-base text-ink outline-none transition focus:border-ink focus:ring-4 focus:ring-ink/5'

/** `/` — the public entry. Everyone lands here; realtors go on to Manage spaces. */
export function ToursPage() {
  const { loginVisitor } = useAuth()
  const navigate = useNavigate()
  const [code, setCode] = useState(() => liveRobot ? '' : localStorage.getItem('realbot-room') || 'demo-bot')
  const [joining, setJoining] = useState(false)
  const [joinError, setJoinError] = useState('')
  const [filter, setFilter] = useState<Filter>('all')
  const [now] = useState(() => Date.now())
  const [booking, setBooking] = useState<OpenTour | null>(null)
  const closeBooking = useCallback(() => setBooking(null), [])

  const join = (roomId: string) => {
    loginVisitor(roomId)
    void navigate(`/user/${encodeURIComponent(roomId)}`)
  }

  const submitCode = async (event: FormEvent) => {
    event.preventDefault()
    const value = code.trim()
    if (!value || joining) return
    setJoinError('')
    setJoining(true)
    try {
      if (liveRobot) {
        const session = await requestVisitorSession(value)
        join(session.roomId)
      } else join(value)
    } catch (error) {
      setJoinError(error instanceof Error ? error.message : 'Unable to join')
    } finally { setJoining(false) }
  }

  const openTour = (tour: OpenTour) => {
    if (tour.live) join(tour.roomId)
    else setBooking(tour)
  }

  const tours = OPEN_TOURS.filter((t) => matches(t, filter, now))

  return (
    <motion.main variants={pageVariants} initial="initial" animate="enter" exit="exit" className="min-h-dvh">
      <TopNav />

      <section className="mx-auto flex max-w-[1280px] flex-col items-center gap-10 px-6 pb-10 pt-10 sm:px-10 lg:flex-row lg:gap-16 lg:pt-14">
        <div className="flex flex-1 flex-col gap-5">
          <h1 className="text-[38px] font-bold leading-[1.1] tracking-[-0.03em] sm:text-[52px] sm:leading-[60px]">
            <span className="flex items-center gap-3 sm:gap-4">
              Tour
              <WordCarousel fontSize={38} className="sm:hidden" />
              <WordCarousel fontSize={52} className="hidden sm:inline-block" />
            </span>
            <span className="block">from anywhere, anytime.</span>
          </h1>
          <p className="max-w-[480px] text-[17px] leading-relaxed text-ink-2">
            A robot on site, your screen as the window. Walk any listing live, or join the private tour your
            realtor set up for you.
          </p>
        </div>

        <form
          onSubmit={submitCode}
          className="flex w-full max-w-[400px] flex-col gap-4 rounded-3xl bg-white p-7 shadow-lg"
        >
          <h2 className="text-[22px] font-bold tracking-tight">Have an access code?</h2>
          <p className="-mt-2 text-sm leading-snug text-ink-2">
            {liveRobot ? 'Enter your robot access code to connect. No account or signup required.'
              : 'Your realtor sends one for private tours. For the prototype, try demo-bot.'}
          </p>
          <label htmlFor="room-id" className="text-[13px] font-semibold">
            Access code
            <input
              id="room-id"
              type={liveRobot ? 'password' : 'text'}
              value={code}
              onChange={(event) => setCode(event.target.value)}
              autoComplete="off"
              className={inputClass}
            />
          </label>
          {joinError && <p role="alert" className="text-sm text-red-700">{joinError}</p>}
          <Button type="submit" className="h-[52px] w-full justify-center text-base" disabled={!code.trim() || joining}>
            {joining ? 'Connecting…' : 'Join tour'} <ArrowRight size={18} />
          </Button>
          <div className="flex items-center gap-3 text-[13px] text-ink-3">
            <span className="h-px flex-1 bg-line" />
            or
            <span className="h-px flex-1 bg-line" />
          </div>
          <a
            href="#open-tours"
            className="text-center text-sm font-semibold text-ink underline underline-offset-4"
          >
            Browse open tours below
          </a>
        </form>
      </section>

      <section id="open-tours" className="mx-auto max-w-[1280px] px-6 pb-20 sm:px-10">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="flex items-center gap-2 text-[22px] font-bold tracking-tight">
              Open tours near you <ChevronRight size={20} />
            </h2>
            <p className="mt-1 text-sm text-ink-2">San Francisco · listings with a bracketbot on site</p>
          </div>
          <div className="flex gap-2" role="tablist" aria-label="When">
            {filters.map((f) => (
              <button
                key={f.id}
                type="button"
                role="tab"
                aria-selected={filter === f.id}
                onClick={() => setFilter(f.id)}
                className={`rounded-full px-3.5 py-2 text-[13px] transition ${
                  filter === f.id
                    ? 'bg-ink font-semibold text-white'
                    : 'border border-[#dddddd] font-medium text-ink-2 hover:border-ink hover:text-ink'
                }`}
              >
                {f.label}
              </button>
            ))}
          </div>
        </div>

        <motion.ul
          key={filter}
          variants={staggerList(tours.length)}
          initial="hidden"
          animate="show"
          className="mt-4 grid grid-cols-[repeat(auto-fill,minmax(260px,1fr))] gap-x-6 gap-y-10 p-0"
        >
          {tours.map((tour) => (
            <OpenTourCard key={tour.id} tour={tour} onJoin={openTour} />
          ))}
        </motion.ul>
      </section>

      <BookingSheet tour={booking} onClose={closeBooking} />
    </motion.main>
  )
}

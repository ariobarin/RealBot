import { ArrowRight, ChevronRight } from 'lucide-react'
import { motion } from 'framer-motion'
import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/useAuth'
import { OpenTourCard } from '../components/tours/OpenTourCard'
import { WordCarousel } from '../components/tours/WordCarousel'
import { Button } from '../components/ui/Button'
import { TopNav } from '../components/ui/TopNav'
import { pageVariants, staggerList } from '../lib/motion'
import { OPEN_TOURS, type OpenTour } from '../lib/openTours'

type Filter = 'live' | 'today' | 'week'

const filters: { id: Filter; label: string }[] = [
  { id: 'live', label: 'Live now' },
  { id: 'today', label: 'Today' },
  { id: 'week', label: 'This week' },
]

const inputClass =
  'mt-1.5 h-[52px] w-full rounded-xl border border-[#dddddd] bg-white px-4 text-base text-ink outline-none transition focus:border-ink focus:ring-4 focus:ring-ink/5'

/** `/` — the public entry. Everyone lands here; realtors go on to Manage spaces. */
export function ToursPage() {
  const { loginVisitor } = useAuth()
  const navigate = useNavigate()
  const [code, setCode] = useState(() => localStorage.getItem('realbot-room') || 'demo-bot')
  const [filter, setFilter] = useState<Filter>('live')

  const join = (roomId: string) => {
    loginVisitor(roomId)
    void navigate(`/user/${encodeURIComponent(roomId)}`)
  }

  const submitCode = (event: FormEvent) => {
    event.preventDefault()
    const room = code.trim()
    if (room) join(room)
  }

  const openTour = (tour: OpenTour) => {
    if (tour.live) join(tour.roomId)
    // Scheduled tours: reservations are not wired yet; nothing happens on click.
  }

  const tours = OPEN_TOURS.filter((t) => (filter === 'live' ? t.live : true))

  return (
    <motion.main variants={pageVariants} initial="initial" animate="enter" exit="exit" className="min-h-dvh">
      <TopNav />

      <section className="mx-auto flex max-w-[1280px] flex-col items-center gap-10 px-6 pb-10 pt-10 sm:px-10 lg:flex-row lg:gap-16 lg:pt-14">
        <div className="flex flex-1 flex-col gap-5">
          <h1 className="text-[44px] font-extrabold leading-[1.1] tracking-[-0.035em] sm:text-[64px] sm:leading-[72px]">
            <span className="flex items-center gap-3 sm:gap-4">
              Tour
              <WordCarousel row={72} className="sm:hidden" />
              <WordCarousel row={64} className="hidden sm:inline-block" />
            </span>
            <span className="block">from anywhere, anytime.</span>
          </h1>
          <p className="max-w-[520px] text-lg leading-relaxed text-ink-2 sm:text-[19px]">
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
            Your realtor sends one for private tours. For the prototype, try demo-bot.
          </p>
          <label htmlFor="room-id" className="text-[13px] font-semibold">
            Access code
            <input
              id="room-id"
              value={code}
              onChange={(event) => setCode(event.target.value)}
              autoComplete="off"
              className={inputClass}
            />
          </label>
          <Button type="submit" className="h-[52px] w-full justify-center text-base" disabled={!code.trim()}>
            Join tour <ArrowRight size={18} />
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
    </motion.main>
  )
}

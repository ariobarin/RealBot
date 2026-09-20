import { Check, ChevronLeft, Copy, Link as LinkIcon } from 'lucide-react'
import { motion } from 'framer-motion'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { CalendarIcon } from '../components/icons/CalendarIcon'
import { PlanThumb } from '../components/tours/PlanThumb'
import { Button } from '../components/ui/Button'
import { TopNav } from '../components/ui/TopNav'
import { fadeUp, pageVariants } from '../lib/motion'
import { useMapsStore } from '../store/useMapsStore'

const SLOTS = ['09:00', '09:30', '10:00', '11:00', '11:30', '13:30', '14:00', '15:30', '16:00']

const fmtTime = (hhmm: string) => {
  const [hour, minute] = hhmm.split(':').map(Number)
  return new Date(2000, 0, 1, hour, minute).toLocaleTimeString(undefined, {
    hour: 'numeric',
    minute: '2-digit',
  })
}

function nextDays(count: number) {
  const today = new Date()
  today.setHours(0, 0, 0, 0)
  return Array.from({ length: count }, (_, index) => {
    const day = new Date(today)
    day.setDate(today.getDate() + index + 1)
    return day
  })
}

/** Realtor flow for choosing when a space can be viewed and sharing its visitor link. */
export function OpenViewingPage() {
  const { mapId = '' } = useParams()
  const { status, maps, error, load } = useMapsStore()
  const days = useMemo(() => nextDays(7), [])
  const [dayIndex, setDayIndex] = useState(0)
  const [selected, setSelected] = useState<string[]>([])
  const [link, setLink] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    void load()
  }, [load])

  const map = maps.find((item) => item.id === mapId)
  const day = days[dayIndex]
  const dayKey = day.toISOString().slice(0, 10)

  const toggleSlot = (time: string) => {
    const key = `${dayKey}T${time}`
    setSelected((current) =>
      current.includes(key) ? current.filter((slot) => slot !== key) : [...current, key].sort(),
    )
  }

  const createLink = (event: FormEvent) => {
    event.preventDefault()
    if (!map || selected.length === 0) return
    const url = new URL('/', window.location.origin)
    url.searchParams.set('tour', map.id)
    url.searchParams.set('invite', 'preview')
    setLink(url.toString())
  }

  const copyLink = async () => {
    if (!link) return
    try {
      await navigator.clipboard.writeText(link)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 2000)
    } catch {
      setCopied(false)
    }
  }

  return (
    <motion.main variants={pageVariants} initial="initial" animate="enter" exit="exit" className="min-h-dvh">
      <TopNav />
      <div className="mx-auto max-w-[1280px] px-6 pb-20 pt-10 sm:px-10">
        <motion.div variants={fadeUp} initial="hidden" animate="show">
          <Link
            to={`/map/${encodeURIComponent(mapId)}`}
            className="mb-4 inline-flex items-center gap-1 text-sm font-medium text-ink-2 no-underline hover:text-ink"
          >
            <ChevronLeft size={16} /> Back to map
          </Link>
          <h1 className="text-[32px] font-extrabold tracking-[-0.03em] sm:text-[36px]">Open for viewing</h1>
          <p className="mt-1 text-[15px] text-ink-2">
            Choose when visitors can book this space, then share the link.
          </p>
        </motion.div>

        {(status === 'idle' || status === 'loading') && (
          <div className="skeleton mt-7 h-[420px] rounded-[20px]" aria-label="Loading space" />
        )}

        {status === 'error' && (
          <div role="alert" className="mt-7 rounded-[20px] border border-line p-8 text-center">
            <p className="font-semibold">Couldn’t load this space</p>
            <p className="mt-1 text-sm text-ink-2">{error}</p>
          </div>
        )}

        {status === 'ready' && !map && (
          <div role="alert" className="mt-7 rounded-[20px] border border-line p-8 text-center">
            <p className="font-semibold">This space could not be found.</p>
            <Link to="/realtor" className="mt-3 inline-block text-sm font-semibold text-ink">
              Return to your spaces
            </Link>
          </div>
        )}

        {status === 'ready' && map && (
          <div className="mt-7 grid gap-8 lg:grid-cols-[minmax(0,1fr)_320px]">
            <section className="rounded-[20px] border border-line bg-white p-6 sm:p-8">
              <div className="flex items-center gap-4 border-b border-line pb-6">
                <div className="h-[84px] w-28 shrink-0 overflow-hidden rounded-xl bg-bg-soft">
                  <PlanThumb mapId={map.id} name={map.name} robot={[0.5, 0.5]} live={false} />
                </div>
                <div>
                  <h2 className="text-[20px] font-bold tracking-tight">{map.name}</h2>
                  <p className="mt-1 text-sm text-ink-2">
                    {map.areaM2} m² · {map.rooms} rooms · 20-minute viewings
                  </p>
                </div>
              </div>

              {link ? (
                <div className="flex min-h-[300px] flex-col items-center justify-center gap-5 py-8 text-center">
                  <span className="grid size-16 place-items-center rounded-full bg-[#e6f4e6] text-ok">
                    <Check size={30} strokeWidth={2.5} />
                  </span>
                  <div>
                    <h2 className="text-[22px] font-bold tracking-tight">Your viewing link is ready</h2>
                    <p className="mt-1 max-w-md text-sm text-ink-2">
                      Visitors who receive this link will be able to choose from your {selected.length}{' '}
                      available {selected.length === 1 ? 'time' : 'times'}.
                    </p>
                  </div>
                  <div className="flex w-full max-w-lg items-center gap-2 rounded-xl border border-line bg-bg-soft p-2 pl-4">
                    <LinkIcon size={17} className="shrink-0 text-ink-3" />
                    <input
                      aria-label="Visitor link"
                      readOnly
                      value={link}
                      className="min-w-0 flex-1 bg-transparent text-sm text-ink outline-none"
                    />
                    <Button onClick={() => void copyLink()}>
                      {copied ? <Check size={16} /> : <Copy size={16} />}
                      {copied ? 'Copied' : 'Copy link'}
                    </Button>
                  </div>
                  <Button variant="ghost" onClick={() => setLink(null)}>
                    Edit available times
                  </Button>
                </div>
              ) : (
                <form onSubmit={createLink} className="pt-6">
                  <fieldset className="m-0 border-0 p-0">
                    <legend className="mb-3 text-[15px] font-bold">Pick a day</legend>
                    <div className="flex gap-2 overflow-x-auto pb-1">
                      {days.map((date, index) => {
                        const active = index === dayIndex
                        return (
                          <button
                            key={date.toISOString()}
                            type="button"
                            aria-pressed={active}
                            onClick={() => setDayIndex(index)}
                            className={`flex h-[64px] w-[56px] shrink-0 flex-col items-center justify-center rounded-[14px] border transition ${
                              active
                                ? 'border-ink bg-ink text-white'
                                : 'border-[#dddddd] bg-white text-ink hover:border-ink'
                            }`}
                          >
                            <span className="text-[11px] font-semibold uppercase tracking-wider opacity-70">
                              {date.toLocaleDateString(undefined, { weekday: 'short' })}
                            </span>
                            <span className="text-lg font-bold">{date.getDate()}</span>
                          </button>
                        )
                      })}
                    </div>
                  </fieldset>

                  <fieldset className="mt-7 border-0 p-0">
                    <legend className="mb-3 text-[15px] font-bold">
                      {day.toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })}
                    </legend>
                    <div className="flex flex-wrap gap-2">
                      {SLOTS.map((time) => {
                        const key = `${dayKey}T${time}`
                        const active = selected.includes(key)
                        return (
                          <button
                            key={time}
                            type="button"
                            aria-pressed={active}
                            onClick={() => toggleSlot(time)}
                            className={`h-10 rounded-full border px-[18px] text-sm transition ${
                              active
                                ? 'border-ink bg-ink font-semibold text-white'
                                : 'border-[#dddddd] bg-white font-medium text-ink hover:border-ink'
                            }`}
                          >
                            {fmtTime(time)}
                          </button>
                        )
                      })}
                    </div>
                    <p className="mt-3 text-[13px] text-ink-2">
                      Select as many times as you’d like across the next seven days.
                    </p>
                  </fieldset>

                  <div className="mt-8 flex flex-wrap items-center justify-between gap-4 border-t border-line pt-5">
                    <p className="text-sm text-ink-2">
                      {selected.length === 0
                        ? 'No times selected'
                        : `${selected.length} ${selected.length === 1 ? 'time' : 'times'} selected`}
                    </p>
                    <Button
                      type="submit"
                      disabled={selected.length === 0}
                      className="h-[50px] px-6 text-[15px]"
                    >
                      Create viewing link
                    </Button>
                  </div>
                </form>
              )}
            </section>

            <aside className="flex flex-col gap-3.5">
              <div className="icon-hover flex items-start gap-3.5 rounded-[20px] bg-bg-soft p-5">
                <CalendarIcon size={56} />
                <div>
                  <p className="text-[15px] font-bold">You control the schedule</p>
                  <p className="mt-0.5 text-[13px] leading-relaxed text-ink-2">
                    Only the times you select will be offered to visitors. You can return here to create a new
                    link at any time.
                  </p>
                </div>
              </div>
            </aside>
          </div>
        )}
      </div>
    </motion.main>
  )
}

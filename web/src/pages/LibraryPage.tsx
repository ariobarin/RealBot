import { motion } from 'framer-motion'
import { Eye, Plus, Radio } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { MapPinIcon } from '../components/icons/MapPinIcon'
import { RobotIcon } from '../components/icons/RobotIcon'
import { LibraryGrid, LibraryGridSkeleton } from '../components/library/LibraryGrid'
import { Button } from '../components/ui/Button'
import { TopNav } from '../components/ui/TopNav'
import type { MapSummary } from '../lib/manifest'
import { fadeUp, pageVariants } from '../lib/motion'
import { useMapsStore } from '../store/useMapsStore'

type Filter = 'all' | 'ready' | 'scan'

const filters: { id: Filter; label: string; match: (m: MapSummary) => boolean }[] = [
  { id: 'all', label: 'All', match: () => true },
  { id: 'ready', label: 'Ready to share', match: (m) => m.status === 'ready' },
  { id: 'scan', label: 'Needs a scan', match: (m) => m.status !== 'ready' },
]

const currentRoom = () => localStorage.getItem('realbot-room') || 'demo-bot'

/** `/realtor` — Manage spaces → Your spaces. */
export function LibraryPage() {
  const navigate = useNavigate()
  const { status, maps, error, load } = useMapsStore()
  const [filter, setFilter] = useState<Filter>('all')
  useEffect(() => {
    void load()
  }, [load])

  const room = currentRoom()
  const visible = maps.filter(filters.find((f) => f.id === filter)!.match)

  return (
    <motion.main variants={pageVariants} initial="initial" animate="enter" exit="exit" className="min-h-dvh">
      <TopNav
        actions={
          <>
            <Button variant="ghost" onClick={() => void navigate(`/user/${encodeURIComponent(room)}`)}>
              <Eye size={16} /> Preview as user
            </Button>
            <Button onClick={() => void navigate('/onboard')}>
              <Plus size={16} strokeWidth={2.4} /> Add a space
            </Button>
          </>
        }
      />

      <div className="mx-auto max-w-[1280px] px-6 pb-20 pt-10 sm:px-10">
        <motion.section
          variants={fadeUp}
          initial="hidden"
          animate="show"
          className="flex flex-wrap items-end justify-between gap-4"
        >
          <div>
            <h1 className="text-[32px] font-extrabold tracking-[-0.03em] sm:text-[36px]">Your spaces</h1>
            <p className="mt-1 text-ink-2">Floor plans scanned by your bracketbot, ready to share.</p>
          </div>
          <div className="flex gap-2" role="tablist" aria-label="Filter spaces">
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
        </motion.section>

        <div className="mt-7">
          {status === 'ready' && <LibraryGrid key={filter} maps={visible} />}
          {(status === 'idle' || status === 'loading') && <LibraryGridSkeleton />}
          {status === 'error' && (
            <div role="alert" className="rounded-2xl border border-line p-8 text-center">
              <p className="font-semibold">Couldn't load your spaces</p>
              <p className="mt-1 text-sm text-ink-2">{error}</p>
              <Button
                variant="secondary"
                className="mt-4"
                onClick={() => {
                  useMapsStore.setState({ status: 'idle' })
                  void load()
                }}
              >
                Try again
              </Button>
            </div>
          )}
        </div>

        <section aria-label="Your robot" className="mt-12 grid gap-6 sm:grid-cols-2">
          <div className="icon-hover flex items-center gap-4 rounded-[20px] bg-bg-soft p-5">
            <RobotIcon size={64} />
            <div className="min-w-0 flex-1">
              <p className="text-[15px] font-bold">bracketbot · {room}</p>
              <p className="text-[13px] text-ink-2">Live camera, SLAM telemetry and the command log.</p>
            </div>
            <Button
              variant="secondary"
              onClick={() => void navigate(`/realtor/control/${encodeURIComponent(room)}`)}
            >
              <Radio size={16} /> Open robot dashboard
            </Button>
          </div>
          <div className="icon-hover flex items-center gap-4 rounded-[20px] bg-bg-soft p-5">
            <MapPinIcon size={64} />
            <div className="min-w-0 flex-1">
              <p className="text-[15px] font-bold">Tour access code</p>
              <p className="text-[13px] text-ink-2">
                Visitors join with <span className="font-mono font-semibold text-ink">{room}</span> on the
                Tours page.
              </p>
            </div>
          </div>
        </section>
      </div>
    </motion.main>
  )
}

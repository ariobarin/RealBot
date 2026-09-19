import { motion } from 'framer-motion'
import { Eye, Plus } from 'lucide-react'
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { MapPinIcon } from '../components/icons/MapPinIcon'
import { HouseIcon } from '../components/icons/HouseIcon'
import { LibraryGrid, LibraryGridSkeleton } from '../components/library/LibraryGrid'
import { Button } from '../components/ui/Button'
import { TopNav } from '../components/ui/TopNav'
import { fadeUp, pageVariants } from '../lib/motion'
import { useMapsStore } from '../store/useMapsStore'

const currentRoom = () => localStorage.getItem('realbot-room') || 'demo-bot'

/** `/realtor` — Manage spaces → Your spaces. */
export function LibraryPage() {
  const navigate = useNavigate()
  const { status, maps, error, load } = useMapsStore()
  useEffect(() => {
    void load()
  }, [load])

  const room = currentRoom()

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
        <motion.h1
          variants={fadeUp}
          initial="hidden"
          animate="show"
          className="text-[32px] font-extrabold tracking-[-0.03em] sm:text-[36px]"
        >
          Your spaces
        </motion.h1>

        <div className="mt-7">
          {status === 'ready' && <LibraryGrid maps={maps} />}
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

        <section aria-label="How it works" className="mt-14 grid gap-6 sm:grid-cols-2">
          <div className="icon-hover flex items-start gap-4 rounded-[20px] bg-bg-soft p-5">
            <HouseIcon size={64} />
            <div className="min-w-0">
              <p className="text-[15px] font-bold">Scan once, tour forever</p>
              <p className="mt-1 text-[14px] leading-relaxed text-ink-2">
                Set the bracketbot inside the front door and press Start. It maps the whole floor on its own;
                the plan shows up here when it's done.
              </p>
            </div>
          </div>
          <div className="icon-hover flex items-start gap-4 rounded-[20px] bg-bg-soft p-5">
            <MapPinIcon size={64} />
            <div className="min-w-0">
              <p className="text-[15px] font-bold">Share a space two ways</p>
              <p className="mt-1 text-[14px] leading-relaxed text-ink-2">
                Open a space to copy its access code for a private tour, or make it public and it appears
                under Open tours for anyone to walk.
              </p>
            </div>
          </div>
        </section>
      </div>
    </motion.main>
  )
}

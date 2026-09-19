import { motion } from 'framer-motion'
import { Eye, LogOut, Radio } from 'lucide-react'
import { useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import { LibraryGrid, LibraryGridSkeleton } from '../components/library/LibraryGrid'
import { Button } from '../components/ui/Button'
import { PageShell, Wordmark } from '../components/ui/PageShell'
import { fadeUp } from '../lib/motion'
import { useMapsStore } from '../store/useMapsStore'
import { useAuth } from '../auth/useAuth'

export function LibraryPage() {
  const navigate = useNavigate()
  const { logout } = useAuth()
  const { status, maps, error, load } = useMapsStore()
  useEffect(() => {
    void load()
  }, [load])

  const previewUser = () => {
    const room = localStorage.getItem('realbot-room') || 'demo-bot'
    void navigate(`/user/${encodeURIComponent(room)}`)
  }

  return (
    <PageShell wide>
      <header className="flex items-center justify-between">
        <Wordmark />
        <div className="flex flex-wrap gap-2">
          <Button variant="ghost" onClick={previewUser}>
            <Eye size={16} /> Preview user view
          </Button>
          <Button
            variant="secondary"
            onClick={() =>
              void navigate(
                `/realtor/control/${encodeURIComponent(localStorage.getItem('realbot-room') || 'demo-bot')}`,
              )
            }
          >
            <Radio size={16} /> Open robot dashboard
          </Button>
          <Button
            variant="ghost"
            onClick={() => {
              logout()
              void navigate('/')
            }}
          >
            <LogOut size={16} /> Sign out
          </Button>
        </div>
      </header>

      <motion.section variants={fadeUp} initial="hidden" animate="show" className="mt-12 mb-8">
        <p className="mb-2 text-xs font-semibold uppercase tracking-widest text-brand">Realtor dashboard</p>
        <h1 className="text-3xl font-bold tracking-tight sm:text-4xl">Your spaces</h1>
        <p className="mt-2 text-ink-2">Floor plans scanned by your bracketbot, ready to share.</p>
      </motion.section>

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
    </PageShell>
  )
}

import { useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { TopNav } from '../components/ui/TopNav'
import { useMapsStore } from '../store/useMapsStore'

export function OnboardingPage() {
  const { maps, status, error, load } = useMapsStore()
  const navigate = useNavigate()
  useEffect(() => {
    void load()
  }, [load])
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') void navigate('/realtor')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate])

  return (
    <main className="min-h-dvh">
      <TopNav />
      <div className="mx-auto max-w-5xl px-6 py-10">
        <Link to="/realtor" className="text-sm text-ink-2">
          Back to your spaces
        </Link>
        <h1 className="mt-4 text-3xl font-bold">Set up a scanned room</h1>
        <p className="mt-2 text-ink-2">
          Choose a saved room, then use the robot view to record its action points.
        </p>
        {status === 'error' && (
          <p role="alert" className="mt-5">
            {error}
          </p>
        )}
        {(status === 'idle' || status === 'loading') && (
          <p role="status" className="mt-5">
            Loading saved rooms…
          </p>
        )}
        {status === 'ready' && (
          <ul className="mt-6 grid gap-4 sm:grid-cols-2">
            {maps
              .filter((map) => map.status === 'ready')
              .map((map) => (
                <li key={map.id}>
                  <Link
                    to={`/realtor/control/${encodeURIComponent(map.id)}`}
                    className="block rounded-2xl border border-line p-5 hover:bg-bg-soft"
                  >
                    <h2 className="font-semibold">{map.name}</h2>
                    <p className="mt-1 text-sm text-ink-2">Set up action points</p>
                  </Link>
                </li>
              ))}
          </ul>
        )}
        {status === 'ready' && !maps.some((map) => map.status === 'ready') && (
          <p className="mt-5">No scanned rooms are available yet.</p>
        )}
      </div>
    </main>
  )
}

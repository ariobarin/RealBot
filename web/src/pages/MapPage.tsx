import { motion } from 'framer-motion'
import { ArrowLeft } from 'lucide-react'
import { useEffect } from 'react'
import { Link, useParams } from 'react-router-dom'
import { MapHud } from '../components/map/MapHud'
import { MapScene } from '../components/map/MapScene'
import { SceneBoundary } from '../components/map/SceneBoundary'
import { Button } from '../components/ui/Button'
import { PageShell, Wordmark } from '../components/ui/PageShell'
import { easeOut } from '../lib/motion'
import { useGridStore } from '../store/useGridStore'
import { useViewStore } from '../store/useViewStore'

export function MapPage() {
  const { mapId = '' } = useParams()
  const { status, grid, summary, error, load } = useGridStore()
  const clearWaypoints = useViewStore((s) => s.clearWaypoints)

  useEffect(() => {
    void load(mapId)
  }, [mapId, load])

  useEffect(() => () => clearWaypoints(), [mapId, clearWaypoints])

  return (
    <PageShell wide>
      <header className="flex items-center justify-between">
        <Wordmark />
        <Link
          to="/"
          className="inline-flex items-center gap-1.5 text-sm font-medium text-ink-2 no-underline hover:text-ink"
        >
          <ArrowLeft size={16} /> Your spaces
        </Link>
      </header>

      <motion.div
        initial={{ opacity: 0, y: 12, scale: 0.99 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ ...easeOut, duration: 0.4 }}
        data-testid="map-view"
        data-status={status}
        className="relative mt-6 h-[calc(100dvh-8.5rem)] min-h-[420px] overflow-hidden rounded-3xl border border-line bg-bg-soft shadow-md"
      >
        {status === 'ready' && grid && summary && (
          <>
            <SceneBoundary summary={summary}>
              <MapScene grid={grid} />
            </SceneBoundary>
            <MapHud summary={summary} grid={grid} />
          </>
        )}

        {(status === 'idle' || status === 'loading') && (
          <div className="skeleton absolute inset-0" aria-busy="true" aria-label="Loading map" />
        )}

        {status === 'error' && (
          <div role="alert" className="absolute inset-0 grid place-items-center p-8 text-center">
            <div>
              <p className="font-semibold">Couldn't open this space</p>
              <p className="mt-1 text-sm text-ink-2">{error}</p>
              <div className="mt-5 flex justify-center gap-2">
                <Button
                  variant="secondary"
                  onClick={() => {
                    useGridStore.setState({ status: 'idle', id: undefined })
                    void load(mapId)
                  }}
                >
                  Try again
                </Button>
                <Link to="/" className="no-underline">
                  <Button variant="ghost">Back to your spaces</Button>
                </Link>
              </div>
            </div>
          </div>
        )}
      </motion.div>
    </PageShell>
  )
}

import { AnimatePresence, motion } from 'framer-motion'
import { Locate, Mic, MousePointerClick, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { botClient, type SentCommand } from '../../lib/botClient'
import type { GridMap } from '../../lib/grid'
import type { MapSummary } from '../../lib/manifest'
import { easeOut, spring } from '../../lib/motion'
import { useViewStore, type ViewMode } from '../../store/useViewStore'
import { Pill } from '../ui/Pill'

const glass = 'rounded-2xl border border-white/60 bg-white/80 shadow-md backdrop-blur-md'

function ModeToggle({ mode, onChange }: { mode: ViewMode; onChange: (m: ViewMode) => void }) {
  return (
    <div
      role="radiogroup"
      aria-label="View mode"
      data-testid="mode-toggle"
      className={`relative flex p-1 text-sm font-semibold ${glass}`}
    >
      {(['2d', '3d'] as const).map((m) => (
        <button
          key={m}
          type="button"
          role="radio"
          aria-checked={mode === m}
          onClick={() => onChange(m)}
          className={`relative z-10 rounded-xl px-4 py-2 transition-colors ${mode === m ? 'text-white' : 'text-ink-2 hover:text-ink'}`}
        >
          {mode === m && (
            <motion.span
              layoutId="mode-thumb"
              transition={spring}
              className="absolute inset-0 -z-10 rounded-xl bg-ink"
            />
          )}
          {m.toUpperCase()}
        </button>
      ))}
    </div>
  )
}

function IconButton({
  label,
  onClick,
  disabled,
  children,
  testId,
}: {
  label: string
  onClick?: () => void
  disabled?: boolean
  children: React.ReactNode
  testId?: string
}) {
  return (
    <motion.button
      type="button"
      title={label}
      aria-label={label}
      data-testid={testId}
      onClick={onClick}
      disabled={disabled}
      whileHover={disabled ? undefined : { scale: 1.04 }}
      whileTap={disabled ? undefined : { scale: 0.95 }}
      transition={spring}
      className={`grid size-10 place-items-center text-ink disabled:cursor-not-allowed disabled:opacity-40 ${glass}`}
    >
      {children}
    </motion.button>
  )
}

function Legend() {
  return (
    <div className={`flex items-center gap-4 px-4 py-2.5 text-xs text-ink-2 ${glass}`}>
      <span className="flex items-center gap-1.5">
        <i className="size-3 rounded-sm bg-[#f3efe8] ring-1 ring-line" /> Floor
      </span>
      <span className="flex items-center gap-1.5">
        <i className="size-3 rounded-sm bg-[#2b2b2b]" /> Wall
      </span>
      <span className="flex items-center gap-1.5">
        <i className="size-3 rounded-full bg-brand" /> Bot
      </span>
      <span className="hidden items-center gap-1.5 sm:flex">
        <MousePointerClick size={14} /> Click floor to send a waypoint
      </span>
    </div>
  )
}

/** Bottom-centre toast that echoes the JSON the mock bot client just received. */
function CommandToast() {
  const [last, setLast] = useState<SentCommand | null>(null)
  useEffect(() => botClient.subscribe(setLast), [])
  useEffect(() => {
    if (!last) return
    const t = window.setTimeout(() => setLast(null), 2200)
    return () => window.clearTimeout(t)
  }, [last])

  return (
    <AnimatePresence>
      {last && (
        <motion.div
          key={last.id}
          data-testid="command-toast"
          initial={{ opacity: 0, y: 12, scale: 0.96 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 8, scale: 0.98 }}
          transition={easeOut}
          className="pointer-events-none absolute bottom-20 left-1/2 -translate-x-1/2 rounded-full bg-ink px-4 py-2 font-mono text-xs text-white shadow-lg sm:bottom-6"
        >
          <span className="mr-2 text-brand">→ bot</span>
          {JSON.stringify(last.cmd)}
        </motion.div>
      )}
    </AnimatePresence>
  )
}

interface MapHudProps {
  summary: MapSummary
  grid: GridMap
}

export function MapHud({ summary, grid }: MapHudProps) {
  const mode = useViewStore((s) => s.mode)
  const setMode = useViewStore((s) => s.setMode)
  const resetView = useViewStore((s) => s.resetView)
  const waypoints = useViewStore((s) => s.waypoints)
  const clearWaypoints = useViewStore((s) => s.clearWaypoints)

  return (
    <div className="pointer-events-none absolute inset-0 p-4 sm:p-5">
      <motion.div
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ ...easeOut, delay: 0.15 }}
        className={`pointer-events-auto absolute left-4 top-4 max-w-[calc(100%-2rem)] px-4 py-3 sm:left-5 sm:top-5 ${glass}`}
      >
        <h1 className="text-lg font-bold tracking-tight">{summary.name}</h1>
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          <Pill tone="ok">Ready</Pill>
          <Pill>{summary.areaM2} m²</Pill>
          <Pill>{summary.rooms} rooms</Pill>
          <Pill>SLAM · {Math.round(grid.resolution * 100)} cm</Pill>
        </div>
      </motion.div>

      <motion.div
        initial={{ opacity: 0, y: -8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ ...easeOut, delay: 0.2 }}
        className="pointer-events-auto absolute right-4 top-4 sm:right-5 sm:top-5"
      >
        <button
          type="button"
          disabled
          title="Coming in the next sprint"
          className={`flex items-center gap-2 px-4 py-2.5 text-sm font-semibold text-ink-2 disabled:cursor-not-allowed ${glass}`}
        >
          <Mic size={16} /> Dictate interactables
        </button>
      </motion.div>

      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ ...easeOut, delay: 0.25 }}
        className="pointer-events-auto absolute bottom-4 left-4 hidden sm:bottom-5 sm:left-5 sm:block"
      >
        <Legend />
      </motion.div>

      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ ...easeOut, delay: 0.3 }}
        className="pointer-events-auto absolute bottom-4 right-4 flex items-center gap-2 sm:bottom-5 sm:right-5"
      >
        <AnimatePresence>
          {waypoints.length > 0 && (
            <motion.div
              initial={{ opacity: 0, scale: 0.8 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.8 }}
              transition={spring}
            >
              <IconButton label="Clear waypoints" onClick={clearWaypoints} testId="clear-waypoints">
                <Trash2 size={18} />
              </IconButton>
            </motion.div>
          )}
        </AnimatePresence>
        <IconButton label="Reset view" onClick={resetView} testId="reset-view">
          <Locate size={18} />
        </IconButton>
        <ModeToggle mode={mode} onChange={setMode} />
      </motion.div>

      <CommandToast />
    </div>
  )
}

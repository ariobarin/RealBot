import { Camera, ChevronDown, ChevronLeft, ChevronRight, ChevronUp, RotateCcw, X } from 'lucide-react'

export type FreeCamPhase = 'off' | 'starting' | 'active' | 'stopping'

interface FreeCamPanelProps {
  panDeg: number
  phase: FreeCamPhase
  tiltDeg: number
  onClose: () => void
  onExit: () => void
  onNudge: (panDelta: number, tiltDelta: number) => void
  onRecenter: () => void
  onStart: () => void
}

const controlClass =
  'grid size-9 place-items-center rounded-xl border border-white/15 bg-white/10 text-white transition hover:bg-white/20 disabled:cursor-not-allowed disabled:opacity-40'

export function FreeCamPanel({
  panDeg,
  phase,
  tiltDeg,
  onClose,
  onExit,
  onNudge,
  onRecenter,
  onStart,
}: FreeCamPanelProps) {
  const active = phase === 'active'
  const busy = phase === 'starting' || phase === 'stopping'

  return (
    <aside
      data-testid="free-cam-panel"
      aria-label="Free cam controls"
      className="absolute bottom-4 right-4 z-20 w-36 overflow-hidden rounded-2xl border border-white/20 bg-[#20242b]/95 p-3 text-white shadow-lg backdrop-blur sm:w-52 sm:p-4"
    >
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <Camera size={15} /> Free cam
          </div>
          <p className="mt-0.5 text-[10px] text-white/60 sm:text-xs">
            {phase === 'starting'
              ? 'Securing the robot…'
              : phase === 'stopping'
                ? 'Returning the arm…'
                : 'Left-hand camera'}
          </p>
        </div>
        {phase === 'off' && (
          <button
            type="button"
            aria-label="Close free cam setup"
            onClick={onClose}
            className="text-white/60 hover:text-white"
          >
            <X size={16} />
          </button>
        )}
      </div>

      {phase === 'off' ? (
        <>
          <button
            type="button"
            onClick={onStart}
            className="mt-4 w-full rounded-xl bg-brand px-3 py-2 text-xs font-semibold hover:bg-brand-2"
          >
            Start free cam
          </button>
          <p className="mt-2 text-[10px] leading-4 text-white/50">The base will stop and remain locked.</p>
        </>
      ) : (
        <>
          <div className="mx-auto mt-3 grid w-fit grid-cols-3 gap-1">
            <span />
            <button
              type="button"
              aria-label="Look up"
              disabled={!active}
              onClick={() => onNudge(0, 5)}
              className={controlClass}
            >
              <ChevronUp size={18} />
            </button>
            <span />
            <button
              type="button"
              aria-label="Look left"
              disabled={!active}
              onClick={() => onNudge(-5, 0)}
              className={controlClass}
            >
              <ChevronLeft size={18} />
            </button>
            <button
              type="button"
              aria-label="Recenter hand camera"
              disabled={!active}
              onClick={onRecenter}
              className={controlClass}
            >
              <RotateCcw size={15} />
            </button>
            <button
              type="button"
              aria-label="Look right"
              disabled={!active}
              onClick={() => onNudge(5, 0)}
              className={controlClass}
            >
              <ChevronRight size={18} />
            </button>
            <span />
            <button
              type="button"
              aria-label="Look down"
              disabled={!active}
              onClick={() => onNudge(0, -5)}
              className={controlClass}
            >
              <ChevronDown size={18} />
            </button>
            <span />
          </div>
          <p className="mt-2 text-center font-mono text-[10px] text-white/55">
            pan {panDeg}° · tilt {tiltDeg}°
          </p>
          <button
            type="button"
            disabled={busy}
            onClick={onExit}
            className="mt-3 w-full rounded-xl border border-white/25 px-3 py-2 text-xs font-semibold hover:bg-white/10 disabled:opacity-40"
          >
            Exit free cam
          </button>
        </>
      )}
    </aside>
  )
}

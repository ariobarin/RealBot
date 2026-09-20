import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'

interface ActionSnapshot {
  recording: {
    state: string
    message: string
    label?: string | null
    speech_status?: string
    last_error?: string | null
    speech_error?: string | null
  }
  landmarks: Array<{
    id: string
    action_id: string
    action: { display_name: string }
    map_revision: number
  }>
}

export function ActionPointsPanel({ roomId, readSnapshot }: { roomId: string; readSnapshot?: () => Promise<unknown> }) {
  const [open, setOpen] = useState(false)
  const [snapshot, setSnapshot] = useState<ActionSnapshot>()
  const [error, setError] = useState('')
  const base = `/api/action-points/${encodeURIComponent(roomId)}`

  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function refresh() {
      try {
        let data: ActionSnapshot
        if (readSnapshot) data = await readSnapshot() as ActionSnapshot
        else {
          const response = await fetch(`${base}/actions`, {
            signal: AbortSignal.any([controller.signal, AbortSignal.timeout(4000)]), cache: 'no-store',
          })
          if (!response.ok) throw new Error('Recorder unavailable')
          data = await response.json() as ActionSnapshot
        }
        if (
          typeof data.recording?.state !== 'string' ||
          typeof data.recording?.message !== 'string' ||
          !Array.isArray(data.landmarks) ||
          data.landmarks.some(
            (point) =>
              typeof point?.id !== 'string' ||
              typeof point.action_id !== 'string' ||
              typeof point.action?.display_name !== 'string' ||
              typeof point.map_revision !== 'number',
          )
        ) {
          throw new Error('Invalid recorder response')
        }
        if (!controller.signal.aborted) {
          setSnapshot(data)
          setError('')
        }
      } catch {
        if (!controller.signal.aborted) {
          setSnapshot(undefined)
          setError('The action-point recorder is unavailable for this room. Waiting to reconnect.')
        }
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(refresh, 1000)
      }
    }
    void refresh()
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [base, open, readSnapshot])

  return (
    <details
      className="my-3 shrink-0 rounded-2xl border border-line bg-white p-4"
      onToggle={(event) => {
        setSnapshot(undefined)
        setError('')
        setOpen(event.currentTarget.open)
      }}
    >
      <summary className="cursor-pointer font-semibold">Action points</summary>
      {open && (
        <div className="mt-3 grid max-h-[45vh] gap-5 overflow-auto lg:grid-cols-2">
          <div>
            <p className="text-sm text-ink-2">
              Stop the robot. Show a thumbs-up within one metre, point at the object, then say “light switch”
              or “electric box”. The robot confirms when the point is saved.
            </p>
            <Link className="mt-2 inline-block text-sm underline" to={`/map/${encodeURIComponent(roomId)}`}>
              View saved room map
            </Link>
            {snapshot && !readSnapshot && (
              <img
                src={`${base}/stream`}
                alt="Action-point camera with hand tracking"
                className="mt-3 max-h-56 w-full rounded-xl bg-black object-contain"
              />
            )}
            {error && (
              <p role="alert" className="mt-3 text-sm text-red-700">
                {error}
              </p>
            )}
            {!snapshot && !error && (
              <p role="status" className="mt-3 text-sm">
                Connecting to the action-point recorder…
              </p>
            )}
          </div>
          {snapshot && (
            <div>
              <p role="status" className="font-medium">
                {snapshot.recording.message}
              </p>
              <p className="mt-1 text-sm text-ink-2">
                {snapshot.recording.state} · Voice: {snapshot.recording.speech_status || 'unavailable'}
                {snapshot.recording.label && ` · Heard: ${snapshot.recording.label.replaceAll('_', ' ')}`}
              </p>
              {(snapshot.recording.last_error || snapshot.recording.speech_error) && (
                <p role="alert" className="mt-2 text-sm text-red-700">
                  {snapshot.recording.last_error || snapshot.recording.speech_error}
                </p>
              )}
              <h2 className="mt-4 font-semibold">Recorded points ({snapshot.landmarks.length})</h2>
              <p className="mt-1 text-xs text-ink-2">
                Recorded in the robot’s SLAM frame. Recording does not test the action or align it to the
                saved room map.
              </p>
              {snapshot.landmarks.length === 0 && (
                <p className="mt-2 text-sm text-ink-2">No points recorded yet.</p>
              )}
              <ul className="mt-2 divide-y divide-line">
                {snapshot.landmarks.map((point) => (
                  <li key={point.id} className="py-2 text-sm">
                    <strong>{point.action.display_name}</strong>
                    <span className="ml-2 text-ink-2">
                      {point.action_id} · map revision {point.map_revision}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </details>
  )
}

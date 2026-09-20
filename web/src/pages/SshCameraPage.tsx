import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { PageShell } from '../components/ui/PageShell'
import { startKeyboardDrive } from '../lib/keyboardDrive'
import { LiveSlamMap, type MapSnapshot } from '../components/map/LiveSlamMap'
import { VisitorLiveKit } from '../lib/visitorLiveKit'
import type { LiveView } from '../lib/liveTelemetryClient'

const livekit = import.meta.env.VITE_ROBOT_TRANSPORT === 'livekit'

export function SshCameraPage() {
  const [attempt, setAttempt] = useState(0)
  const [failed, setFailed] = useState(false)
  const [driving, setDriving] = useState(false)
  const [driveStatus, setDriveStatus] = useState('Enable drive to use WASD')
  const [view, setView] = useState<LiveView>({ connection: 'disconnected', robotOnline: false, telemetryFresh: false })
  const [map, setMap] = useState<MapSnapshot | null>(null)
  const video = useRef<HTMLVideoElement>(null)
  const client = useRef<VisitorLiveKit | null>(null)

  useEffect(() => {
    if (!livekit) return
    const abort = new AbortController()
    const connection = new VisitorLiveKit(setView, setMap)
    client.current = connection
    fetch('/api/livekit-session', { signal: abort.signal, cache: 'no-store' })
      .then((response) => { if (!response.ok) throw new Error('Session unavailable'); return response.json() })
      .then((session) => { if (!abort.signal.aborted) return connection.connect(session) })
      .catch(() => { if (!abort.signal.aborted) setFailed(true) })
    return () => { abort.abort(); connection.disconnect(); client.current = null }
  }, [attempt])

  useEffect(() => {
    if (!view.camera || !video.current) return
    const element = video.current
    view.camera.attach(element)
    return () => { view.camera?.detach(element) }
  }, [view.camera, failed])

  useEffect(() => {
    if (driving) return startKeyboardDrive(setDriveStatus, () => setDriving(false), livekit ? client.current!.connectKeyboard : undefined)
  }, [driving])

  return (
    <PageShell wide viewport>
      <header className="mb-4 flex shrink-0 items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Live camera</h1>
          <p className="mt-1 text-sm text-ink-2">Saved action locations appear in the camera view.</p>
        </div>
        <Link to="/" className="text-sm underline underline-offset-4">Leave tour</Link>
      </header>
      {failed ? (
        <div role="alert" className="rounded-2xl border border-line p-8 text-center">
          <p>The camera connection is unavailable.</p>
          <button className="mt-4 rounded-xl bg-ink px-5 py-2 text-white" onClick={() => {
            setFailed(false)
            setAttempt((value) => value + 1)
          }}>Reconnect</button>
        </div>
      ) : (
        <div className="flex min-h-0 flex-1 items-center justify-center [container-type:size]">
          <div className="relative aspect-[4/3] w-[min(100cqw,133.333cqh)] overflow-hidden rounded-2xl bg-black">
            {livekit ? <video ref={video} autoPlay playsInline muted className="h-full w-full object-contain" /> : <img
              key={attempt}
              src={`/robot-camera/stream?attempt=${attempt}`}
              alt="Left head camera with saved action locations"
              onError={() => { setFailed(true); setDriving(false) }}
              className="w-[200%] max-w-none"
            />}
            {livekit && !view.camera && <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-4 text-center text-sm text-white">
              <p>{view.error || 'Connecting camera…'}</p>
              {view.error && <button className="rounded-lg border px-4 py-2" onClick={() => {
                setDriving(false); setAttempt((value) => value + 1)
              }}>Reconnect</button>}
            </div>}
            <div className="absolute bottom-3 right-3 aspect-[4/3] w-[34%] min-w-32 max-w-72">
              <LiveSlamMap snapshot={livekit ? map : undefined} />
            </div>
          </div>
        </div>
      )}
      <footer className="mt-4 flex shrink-0 flex-wrap items-center justify-between gap-3">
        <p role="status" className="text-sm text-ink-2">{driveStatus}</p>
        <button
          disabled={!driving && (failed || (livekit && !view.camera))}
          className="rounded-xl bg-ink px-5 py-2 text-white disabled:opacity-40"
          onClick={() => {
            setDriveStatus(driving ? 'Drive stopped' : 'Connecting drive…')
            setDriving(!driving)
          }}
        >{driving ? 'Stop driving' : 'Enable drive'}</button>
      </footer>
    </PageShell>
  )
}

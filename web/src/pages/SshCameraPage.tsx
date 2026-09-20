import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { PageShell } from '../components/ui/PageShell'
import { FreeCamButton } from '../components/control/FreeCamButton'
import { RemoteFreeCam } from '../components/control/RemoteFreeCam'
import { ActionLocations } from '../components/control/ActionLocations'
import { startKeyboardDrive } from '../lib/keyboardDrive'
import { LiveSlamMap, type MapSnapshot } from '../components/map/LiveSlamMap'
import { VisitorLiveKit, type ActionView, type FreeCamState } from '../lib/visitorLiveKit'
import type { LiveView } from '../lib/liveTelemetryClient'
import { parseViewerSession } from '../lib/liveTelemetry'
import { requestVisitorSession, visitorAccessCode } from '../lib/visitorSession'

const livekit = import.meta.env.VITE_ROBOT_TRANSPORT === 'livekit'

export function SshCameraPage() {
  const { roomId } = useParams()
  const [accessCode, setAccessCode] = useState(() => visitorAccessCode(roomId))
  const [connectionError, setConnectionError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [failed, setFailed] = useState(false)
  const [driving, setDriving] = useState(false)
  const [driveStatus, setDriveStatus] = useState('Enable drive to use WASD')
  const [view, setView] = useState<LiveView>({ connection: 'disconnected', robotOnline: false, telemetryFresh: false })
  const [map, setMap] = useState<MapSnapshot | null>(null)
  const [freeCam, setFreeCam] = useState<FreeCamState | null>(null)
  const [actions, setActions] = useState<ActionView | null>(null)
  const viewingHand = !!freeCam?.viewing
  const video = useRef<HTMLVideoElement>(null)
  const [client] = useState(() => new VisitorLiveKit(setView, setMap, setFreeCam, setActions))
  const changeView = useCallback((_viewing: boolean) => {
    setDriving(false)
    setDriveStatus('Drive stopped')
  }, [])

  useEffect(() => {
    if (!livekit || (import.meta.env.PROD && !accessCode)) return
    const abort = new AbortController()
    const connection = client
    const session = import.meta.env.PROD
      ? requestVisitorSession(accessCode, roomId, abort.signal)
      : fetch('/api/livekit-session', { signal: abort.signal, cache: 'no-store' }).then(async (response) => {
        if (!response.ok) throw new Error('Robot session unavailable')
        return parseViewerSession(await response.json())
      })
    session
      .then((session) => { if (!abort.signal.aborted) return connection.connect(session) })
      .catch((error: unknown) => {
        if (!abort.signal.aborted) {
          setConnectionError(error instanceof Error ? error.message : 'Robot session unavailable')
          if (error instanceof Error && (error.cause === 401 || error.cause === 403)) {
            setAccessCode('')
          } else setFailed(true)
        }
      })
    return () => { abort.abort(); connection.disconnect() }
  }, [attempt, accessCode, roomId, client])

  useEffect(() => {
    if (!view.camera || !video.current) return
    const element = video.current
    view.camera.attach(element)
    return () => { view.camera?.detach(element) }
  }, [view.camera, failed])

  useEffect(() => {
    if (driving) return startKeyboardDrive(setDriveStatus, () => setDriving(false), livekit ? client.connectKeyboard : undefined)
  }, [driving, client])

  return (
    <PageShell wide viewport>
      <header className="mb-4 flex shrink-0 items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{viewingHand ? 'Right-hand Free Cam' : view.camera ? 'Live camera' : 'Connect to robot'}</h1>
          <p className="mt-1 text-sm text-ink-2">{viewingHand ? 'Move the camera with coordinated arm control.'
            : actions?.state.available ? 'Position at the box, then click its circle to open it. Stop when it opens.'
              : 'Saved action locations appear in the camera view.'}</p>
        </div>
        <Link to="/" className="text-sm underline underline-offset-4">Leave tour</Link>
      </header>
      {livekit && import.meta.env.PROD && !accessCode ? (
        <form className="m-auto flex w-full max-w-sm flex-col gap-4" onSubmit={(event) => {
          event.preventDefault()
          setFailed(false)
          setConnectionError('')
          setAccessCode(String(new FormData(event.currentTarget).get('accessCode') || '').trim())
        }}>
          <label className="text-sm">Robot access code
            <input name="accessCode" type="password" required autoComplete="off"
              className="mt-2 block w-full rounded-xl border border-line p-3" />
          </label>
          {connectionError && <p role="alert" className="text-sm text-red-700">{connectionError}</p>}
          <button className="rounded-xl bg-ink px-5 py-2 text-white">Connect</button>
        </form>
      ) : failed ? (
        <div role="alert" className="rounded-2xl border border-line p-8 text-center">
          <p>{connectionError || 'The camera connection is unavailable.'}</p>
          <button className="mt-4 rounded-xl bg-ink px-5 py-2 text-white" onClick={() => {
            setFailed(false)
            setAttempt((value) => value + 1)
          }}>Reconnect</button>
          {import.meta.env.PROD && <button className="ml-3 text-sm underline" onClick={() => {
            setFailed(false); setAccessCode(''); setConnectionError('')
          }}>Change access code</button>}
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
            {livekit && <ActionLocations client={client} view={actions}
              disabled={viewingHand || !!(freeCam?.available && freeCam.phase !== 'idle') || !view.camera}
              onStart={() => { setDriving(false); setDriveStatus('Drive stopped for arm action') }} />}
            {livekit && !view.camera && <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-4 text-center text-sm text-white">
              <p>{view.error || 'Connecting camera…'}</p>
              {view.error && <button className="rounded-lg border px-4 py-2" onClick={() => {
                setDriving(false); setAttempt((value) => value + 1)
              }}>Reconnect</button>}
            </div>}
            {!viewingHand && <div className="absolute bottom-3 right-3 aspect-[4/3] w-[34%] min-w-32 max-w-72">
              <LiveSlamMap snapshot={livekit ? map : undefined} />
            </div>}
          </div>
        </div>
      )}
      <footer className="mt-4 flex shrink-0 flex-wrap items-center justify-between gap-3">
        <p role="status" className="text-sm text-ink-2">{driveStatus}</p>
        {livekit ? <RemoteFreeCam client={client} state={actions?.state.owned ? null : freeCam} onViewing={changeView} /> : <FreeCamButton onOpen={() => {
          setDriving(false)
          setDriveStatus('Drive stopped for Free Cam')
        }} />}
        <button
          disabled={!driving && (actions?.state.owned || failed || viewingHand || (freeCam?.available && freeCam.phase !== 'idle') || (livekit && !view.camera))}
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

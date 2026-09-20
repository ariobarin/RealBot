import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { PageShell } from '../components/ui/PageShell'
import { FreeCamButton } from '../components/control/FreeCamButton'
import { RemoteFreeCam } from '../components/control/RemoteFreeCam'
import { ActionLocations } from '../components/control/ActionLocations'
import { ArmCameraPopup } from '../components/control/ArmCameraPopup'
import { startKeyboardDrive } from '../lib/keyboardDrive'
import { LiveSlamMap, type MapSnapshot } from '../components/map/LiveSlamMap'
import { VisitorLiveKit, type ActionView, type FreeCamState } from '../lib/visitorLiveKit'
import type { LiveView } from '../lib/liveTelemetryClient'
import { parseViewerSession } from '../lib/liveTelemetry'
import { requestVisitorSession, visitorAccessCode } from '../lib/visitorSession'
import { ActionPointsPanel } from '../components/control/ActionPointsPanel'

const livekit = import.meta.env.VITE_ROBOT_TRANSPORT === 'livekit'

export function SshCameraPage({ setup = false }: { setup?: boolean }) {
  return <PageShell wide viewport><RobotCameraPanel setup={setup} /></PageShell>
}

export function RobotCameraPanel({ robotId, embedded = false, setup = false }: { robotId?: string; embedded?: boolean; setup?: boolean }) {
  const params = useParams()
  const roomId = robotId || params.roomId
  const [connectedRobot, setConnectedRobot] = useState('')
  const [accessCode, setAccessCode] = useState(() => visitorAccessCode(roomId))
  const [robotRole, setRobotRole] = useState<'mobile' | 'act' | null>(null)
  const [connectionError, setConnectionError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [failed, setFailed] = useState(false)
  const [released, setReleased] = useState(false)
  const [driving, setDriving] = useState(false)
  const [driveStatus, setDriveStatus] = useState('Enable drive to use WASD')
  const [view, setView] = useState<LiveView>({ connection: 'disconnected', robotOnline: false, telemetryFresh: false })
  const [map, setMap] = useState<MapSnapshot | null>(null)
  const [freeCam, setFreeCam] = useState<FreeCamState | null>(null)
  const [actions, setActions] = useState<ActionView | null>(null)
  const [actionPending, setActionPending] = useState(false)
  const [actionError, setActionError] = useState('')
  const [scriptStatus, setScriptStatus] = useState('')
  const [armCameraOpen, setArmCameraOpen] = useState(false)
  // 0188 is the only ACT robot, and its right arm belongs to the policy, so it has no
  // right-hand Free Cam and no coordinated arm control. `hand` drops that state entirely.
  const actRobot = robotRole === 'act' && (connectedRobot || roomId) === '0188'
  const hand = actRobot ? null : freeCam
  const viewingHand = !!hand?.viewing
  const video = useRef<HTMLVideoElement>(null)
  const [client] = useState(() => new VisitorLiveKit(setView, setMap, setFreeCam, setActions))
  const readActionPoints = useCallback(() => client.readActionPoints(), [client])
  async function runScript(script: 'init' | 'go' | 'stop', label: string) {
    // The right-arm camera stays where the operator put it; a run never opens it for them.
    setActionPending(true)
    setActionError('')
    setScriptStatus(script === 'init'
      ? 'Initializing: homing the arms and starting Quest teleop. Wait for READY before Go.'
      : `${label}…`)
    try {
      const status = await client.runScript(script)
      setScriptStatus(status?.reason ? `${label}: ${status.reason}` : `${label} started`)
    } catch (error) {
      setActionError(error instanceof Error ? error.message : `Could not run ${label}`)
      setScriptStatus('')
    } finally { setActionPending(false) }
  }
  const changeView = useCallback((_viewing: boolean) => {
    setDriving(false)
    setDriveStatus('Drive stopped')
  }, [])

  useEffect(() => {
    if (!livekit || released || (import.meta.env.PROD && !accessCode)) return
    const abort = new AbortController()
    const connection = client
    const session = import.meta.env.PROD
      ? requestVisitorSession(accessCode, setup ? undefined : roomId, abort.signal)
      : fetch('/api/livekit-session', { signal: abort.signal, cache: 'no-store' }).then(async (response) => {
        if (!response.ok) throw new Error('Robot session unavailable')
        return { ...parseViewerSession(await response.json()), roomId, robotRole: roomId === '0188' ? 'act' as const : 'mobile' as const }
      })
    session
      .then((session) => {
        if (!abort.signal.aborted) {
          setRobotRole(session.robotRole)
          setConnectedRobot(session.roomId || '')
          return connection.connect(session)
        }
      })
      .catch((error: unknown) => {
        if (!abort.signal.aborted) {
          setConnectionError(error instanceof Error ? error.message : 'Robot session unavailable')
          if (error instanceof Error && (error.cause === 401 || error.cause === 403)) {
            setAccessCode('')
          } else setFailed(true)
        }
      })
    return () => { abort.abort(); connection.disconnect() }
  }, [attempt, accessCode, roomId, setup, released, client])

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
    <section aria-label={`Robot ${roomId}`} className="relative flex min-h-0 flex-1 flex-col">
      <header className="mb-4 flex shrink-0 items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{connectedRobot || roomId} · {viewingHand ? 'Right-hand Free Cam' : view.camera ? 'Live camera' : 'Connect to robot'}</h1>
          <p className="mt-1 text-sm text-ink-2">{viewingHand ? 'Move the camera with coordinated arm control.'
            : actRobot ? 'Initialize (guided), then Go. Press Stop once the box opens.'
              : 'Saved action locations appear in the camera view.'}</p>
        </div>
        {!embedded && !setup && <Link to="/user/both" className="text-sm underline underline-offset-4">Both robots</Link>}
        {setup && livekit && view.robotOnline && <button className="shrink-0 rounded-xl border border-line px-4 py-2"
          onClick={() => { setDriving(false); setArmCameraOpen(false); setReleased(true); setAccessCode(''); setConnectedRobot('') }}>
          Stop action points
        </button>}
        {!embedded && <Link to={setup ? '/realtor' : '/'} className="text-sm underline underline-offset-4">{setup ? 'Your spaces' : 'Leave tour'}</Link>}
      </header>
      <div className={setup ? 'grid min-h-0 flex-1 gap-4 overflow-auto lg:grid-cols-[minmax(0,1fr)_20rem]' : 'contents'}>
      {livekit && (released || (import.meta.env.PROD && !accessCode)) ? (
        <form className="m-auto flex w-full max-w-sm flex-col gap-4" onSubmit={(event) => {
          event.preventDefault()
          setReleased(false)
          setFailed(false)
          setConnectionError('')
          setAccessCode(String(new FormData(event.currentTarget).get('accessCode') || '').trim())
        }}>
          {released && <p role="status">Action points stopped. Robot connection released.</p>}
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
        <div className={`flex ${setup ? 'min-h-64' : 'min-h-0'} flex-1 items-center justify-center [container-type:size]`}>
          <div className="relative aspect-[4/3] w-[min(100cqw,133.333cqh)] overflow-hidden rounded-2xl bg-black">
            {livekit ? <video ref={video} autoPlay playsInline muted className="h-full w-full object-contain" /> : <img
              key={attempt}
              src={`/robot-camera/stream?attempt=${attempt}`}
              alt="Left head camera with saved action locations"
              onError={() => { setFailed(true); setDriving(false) }}
              className="w-[200%] max-w-none"
            />}
            {livekit && <ActionLocations client={client} view={actions}
              disabled={viewingHand || !!(hand?.available && hand.phase !== 'idle') || !view.camera} />}
            {livekit && !view.camera && <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 p-4 text-center text-sm text-white">
              <p>{view.error || (view.connection === 'connected' && !view.robotOnline
                ? 'Robot offline. Turn it on, then reconnect.' : 'Connecting camera…')}</p>
              <button className="rounded-lg border px-4 py-2" onClick={() => {
                setDriving(false); setAttempt((value) => value + 1)
              }}>Reconnect</button>
            </div>}
            {!actRobot && <div className="absolute bottom-3 right-3 aspect-[4/3] w-[68%] min-w-64 max-w-144">
              <LiveSlamMap snapshot={livekit ? map : undefined} />
            </div>}
          </div>
        </div>
      )}
      {setup && roomId && (!livekit || view.robotOnline) && <aside className="min-h-0 overflow-auto">
        <ActionPointsPanel key={`${roomId}/${connectedRobot}`} roomId={roomId}
          readSnapshot={livekit ? readActionPoints : undefined} />
      </aside>}
      </div>
      {armCameraOpen && <ArmCameraPopup track={view.rightCamera} fresh={!!view.rightCameraFresh && view.robotOnline}
        onClose={() => setArmCameraOpen(false)} onStop={() => void runScript('stop', 'Stop')} />}
      {livekit && actRobot && <button
        className="mt-2 self-end rounded-xl border border-line px-4 py-2 text-sm"
        onClick={() => setArmCameraOpen(true)}>Right-arm camera</button>}
      {actRobot ? <footer className="mt-4 flex shrink-0 flex-wrap items-center gap-3 text-sm">
        <p role="status">{scriptStatus || 'Initialize (guided) homes the arms and starts teleop, Go runs the policy, Stop holds it. The base stays stationary.'}</p>
        <button className="rounded-xl border border-line px-5 py-2 disabled:opacity-40"
          disabled={actionPending || !view.robotOnline}
          onClick={() => void runScript('init', 'Initialize (guided)')}>Initialize (guided)</button>
        <button className="rounded-xl bg-ink px-5 py-2 text-white disabled:opacity-40"
          disabled={actionPending || !view.robotOnline}
          onClick={() => void runScript('go', 'Go')}>Go</button>
        <button className="rounded-xl border border-line px-5 py-2"
          onClick={() => void runScript('stop', 'Stop')}>Stop</button>
        {actionError && <p role="alert" className="text-red-700">{actionError}</p>}
      </footer> : <footer className="mt-4 flex shrink-0 flex-wrap items-center justify-between gap-3">
        <p role="status" className="text-sm text-ink-2">{driveStatus}</p>
        {livekit ? <RemoteFreeCam client={client} state={actions?.state.owned ? null : hand} onViewing={changeView} /> : <FreeCamButton onOpen={() => {
          setDriving(false)
          setDriveStatus('Drive stopped for Free Cam')
        }} />}
        <button
          disabled={!driving && (robotRole !== 'mobile' || actions?.state.owned || failed || viewingHand || (hand?.available && hand.phase !== 'idle') || (livekit && !view.camera))}
          className="rounded-xl bg-ink px-5 py-2 text-white disabled:opacity-40"
          onClick={() => {
            setDriveStatus(driving ? 'Drive stopped' : 'Connecting drive…')
            setDriving(!driving)
          }}
        >{driving ? 'Stop driving' : 'Enable drive'}</button>
      </footer>}
    </section>
  )
}

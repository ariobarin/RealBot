import { ArrowLeft, Bot, Camera, CircleStop, Eye, LogOut, Radio, Sparkles, Video } from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type MouseEvent } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { MapScene } from '../components/map/MapScene'
import { VisitorMiniMap } from '../components/control/VisitorMiniMap'
import { FreeCamPanel, type FreeCamPhase } from '../components/control/FreeCamPanel'
import { Button } from '../components/ui/Button'
import { PageShell, Wordmark } from '../components/ui/PageShell'
import {
  RemoteBotClient,
  type CommandUpdate,
  type ConnectionPhase,
  type NavigationTelemetry,
  type RobotState,
} from '../lib/remoteBotClient'
import { useGridStore } from '../store/useGridStore'
import { useViewStore } from '../store/useViewStore'
import { useAuth } from '../auth/useAuth'

const phaseCopy: Record<ConnectionPhase, string> = {
  idle: 'Disconnected',
  connecting: 'Connecting to relay…',
  waiting: 'Waiting for robot…',
  online: 'Robot online',
  reconnecting: 'Reconnecting…',
}

interface ControlPageProps {
  view: 'realtor' | 'user'
}

interface ViewTarget {
  u: number
  v: number
  commandId: string
}

type FreeCamLifecycle = { commandId: string; kind: 'start' | 'stop' }

const clamp = (value: number, minimum: number, maximum: number) => Math.min(Math.max(value, minimum), maximum)

export function ControlPage({ view }: ControlPageProps) {
  const { roomId = 'demo-bot' } = useParams()
  const navigate = useNavigate()
  const { session, logout } = useAuth()
  const isRealtor = view === 'realtor'
  const isRealtorPreview = !isRealtor && session?.role === 'realtor'
  const client = useMemo(() => new RemoteBotClient(), [])
  const [phase, setPhase] = useState<ConnectionPhase>('connecting')
  const [robot, setRobot] = useState<RobotState>()
  const [frameUrl, setFrameUrl] = useState<string>()
  const [commands, setCommands] = useState<CommandUpdate[]>([])
  const [navigation, setNavigation] = useState<NavigationTelemetry>()
  const [viewTarget, setViewTarget] = useState<ViewTarget>()
  const [freeCamOpen, setFreeCamOpen] = useState(false)
  const [freeCamPhase, setFreeCamPhase] = useState<FreeCamPhase>('off')
  const [freeCamPose, setFreeCamPose] = useState({ panDeg: 0, tiltDeg: 0 })
  const [freeCamSessionId, setFreeCamSessionId] = useState<string>()
  const freeCamLifecycle = useRef<FreeCamLifecycle | undefined>(undefined)
  const freeCamSequence = useRef(0)
  const { status: gridStatus, grid, load } = useGridStore()

  const mapId = navigation?.mapId || robot?.mapId || 'small-house'

  useEffect(() => {
    void load(mapId)
  }, [load, mapId])

  useEffect(() => {
    let currentFrame: string | undefined
    const unsubscribe = client.subscribe((event) => {
      if (event.type === 'connection') setPhase(event.phase)
      if (event.type === 'presence' && !event.online) {
        freeCamLifecycle.current = undefined
        setFreeCamPhase('off')
        setFreeCamOpen(false)
        setFreeCamSessionId(undefined)
      }
      if (event.type === 'state') setRobot(event.state)
      if (event.type === 'navigation') setNavigation(event.navigation)
      if (event.type === 'video') {
        const next = URL.createObjectURL(event.frame)
        if (currentFrame) URL.revokeObjectURL(currentFrame)
        currentFrame = next
        setFrameUrl(next)
      }
      if (event.type === 'command') {
        const lifecycle = freeCamLifecycle.current
        if (lifecycle?.commandId === event.update.commandId) {
          const failed = ['failed', 'rejected', 'expired'].includes(event.update.status)
          if (failed) {
            freeCamLifecycle.current = undefined
            if (lifecycle.kind === 'start') {
              setFreeCamPhase('off')
              setFreeCamSessionId(undefined)
            } else {
              setFreeCamPhase('active')
            }
          } else if (event.update.status === 'succeeded') {
            freeCamLifecycle.current = undefined
            if (lifecycle.kind === 'start') setFreeCamPhase('active')
            else {
              setFreeCamPhase('off')
              setFreeCamOpen(false)
              setFreeCamSessionId(undefined)
            }
          }
        }
        setCommands((existing) => {
          const index = existing.findIndex((item) => item.commandId === event.update.commandId)
          const next = [...existing]
          if (index >= 0) next[index] = { ...next[index], ...event.update }
          else next.unshift(event.update)
          return next.slice(0, 6)
        })
      }
    })
    client.connect(roomId)
    return () => {
      unsubscribe()
      client.disconnect()
      if (currentFrame) URL.revokeObjectURL(currentFrame)
      useViewStore.getState().clearWaypoints()
    }
  }, [client, roomId])

  const online = phase === 'online'
  const sendViewTarget = (event: MouseEvent<HTMLImageElement>) => {
    if (!online || !frameUrl || freeCamPhase !== 'off') return
    const rect = event.currentTarget.getBoundingClientRect()
    const u = Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1)
    const v = Math.min(Math.max((event.clientY - rect.top) / rect.height, 0), 1)
    const commandId = client.sendCommand('move_to_view', {
      u: +u.toFixed(4),
      v: +v.toFixed(4),
      coordinateSpace: 'normalized_camera',
    })
    setViewTarget({ u, v, commandId })
  }

  const startFreeCam = () => {
    if (!online || freeCamPhase !== 'off') return
    const sessionId = crypto.randomUUID()
    freeCamSequence.current = 0
    setFreeCamSessionId(sessionId)
    setFreeCamPose({ panDeg: 0, tiltDeg: 0 })
    setViewTarget(undefined)
    setNavigation(undefined)
    setFreeCamPhase('starting')
    const commandId = client.sendCommand('free_cam_start', { sessionId })
    freeCamLifecycle.current = { commandId, kind: 'start' }
  }

  const stopFreeCam = () => {
    if (!freeCamSessionId || freeCamPhase === 'stopping') return
    setFreeCamPhase('stopping')
    const commandId = client.sendCommand('free_cam_stop', { sessionId: freeCamSessionId })
    freeCamLifecycle.current = { commandId, kind: 'stop' }
  }

  const sendFreeCamPose = (panDeg: number, tiltDeg: number) => {
    if (!freeCamSessionId || freeCamPhase !== 'active') return
    const next = {
      panDeg: clamp(panDeg, -60, 60),
      tiltDeg: clamp(tiltDeg, -35, 45),
    }
    setFreeCamPose(next)
    freeCamSequence.current += 1
    client.sendCommand('free_cam_pose', {
      sessionId: freeCamSessionId,
      panDeg: next.panDeg,
      tiltDeg: next.tiltDeg,
      sequence: freeCamSequence.current,
    })
  }

  const emergencyStop = () => {
    client.sendCommand('stop')
    freeCamLifecycle.current = undefined
    setFreeCamPhase('off')
    setFreeCamOpen(false)
    setFreeCamSessionId(undefined)
  }

  const targetCommand = viewTarget
    ? commands.find((command) => command.commandId === viewTarget.commandId)
    : undefined

  return (
    <PageShell wide viewport={!isRealtor}>
      <header className="flex shrink-0 items-center justify-between gap-4">
        <Wordmark />
        <div className="flex items-center gap-3">
          <span
            data-testid="connection-status"
            className={`inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-semibold ${online ? 'bg-green-50 text-green-700' : 'bg-bg-soft text-ink-2'}`}
          >
            <span className={`size-2 rounded-full ${online ? 'bg-green-600' : 'bg-ink-3'}`} />
            {phaseCopy[phase]}
          </span>
          {isRealtor ? (
            <>
              <Link
                to={`/user/${encodeURIComponent(roomId)}`}
                className="inline-flex items-center gap-1 text-sm text-ink-2 no-underline"
              >
                <Eye size={16} /> Preview as user
              </Link>
              <Link to="/realtor" className="inline-flex items-center gap-1 text-sm text-ink-2 no-underline">
                <ArrowLeft size={16} /> Spaces
              </Link>
            </>
          ) : isRealtorPreview ? (
            <Link
              to={`/realtor/control/${encodeURIComponent(roomId)}`}
              className="inline-flex items-center gap-1 text-sm text-ink-2 no-underline"
            >
              <ArrowLeft size={16} /> Realtor dashboard
            </Link>
          ) : (
            <button
              type="button"
              onClick={() => {
                logout()
                void navigate('/')
              }}
              className="inline-flex items-center gap-1 border-0 bg-transparent text-sm text-ink-2"
            >
              <LogOut size={16} /> Sign out
            </button>
          )}
        </div>
      </header>

      {isRealtorPreview && (
        <div className="mt-3 flex shrink-0 items-center justify-between gap-4 rounded-2xl border border-brand/20 bg-brand-soft px-4 py-2 text-sm">
          <span>
            <strong>Preview mode:</strong> this is the exact experience a user sees.
          </span>
          <Link
            to={`/realtor/control/${encodeURIComponent(roomId)}`}
            className="font-semibold text-brand no-underline"
          >
            Exit preview
          </Link>
        </div>
      )}

      <section
        className={`flex shrink-0 flex-wrap items-center justify-between gap-4 ${isRealtor ? 'mt-6' : 'mt-3'}`}
      >
        <div>
          <p className="text-xs font-semibold uppercase tracking-widest text-ink-3">
            {isRealtor ? 'Realtor operations' : 'Remote tour'}
          </p>
          <h1 className="mt-1 text-2xl font-bold tracking-tight">
            {isRealtor ? roomId : 'Control your bracketbot'}
          </h1>
          {!isRealtor && <p className="mt-1 text-sm text-ink-2">Connected to {roomId}</p>}
        </div>
        <div className="flex gap-2">
          {!isRealtor && (
            <Button
              variant="secondary"
              disabled={!online || freeCamPhase === 'starting' || freeCamPhase === 'stopping'}
              onClick={() => {
                if (freeCamPhase === 'active') void stopFreeCam()
                else setFreeCamOpen((open) => !open)
              }}
            >
              <Camera size={17} /> {freeCamPhase === 'active' ? 'Exit free cam' : 'Free cam'}
            </Button>
          )}
          <Button
            variant="secondary"
            disabled={!online || freeCamPhase !== 'off'}
            onClick={() => client.sendCommand('use_action', { name: 'demo_action' })}
          >
            <Sparkles size={17} /> Use action
          </Button>
          <Button className="bg-red-600 hover:bg-red-700" disabled={!online} onClick={emergencyStop}>
            <CircleStop size={18} /> Stop
          </Button>
        </div>
      </section>

      <div
        className={`grid gap-6 ${isRealtor ? 'mt-6 xl:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)]' : 'mt-3 min-h-0 flex-1'}`}
      >
        <section
          className={`overflow-hidden rounded-3xl border border-line bg-[#20242b] shadow-md ${isRealtor ? '' : 'flex min-h-0 flex-col'}`}
        >
          <div className="flex items-center justify-between px-5 py-4 text-white">
            <div className="flex items-center gap-2 font-semibold">
              <Video size={18} /> Live camera
            </div>
            {isRealtor && <span className="text-xs text-white/60">JPEG relay</span>}
          </div>
          <div
            className={`relative grid place-items-center overflow-hidden bg-[#171a1f] ${isRealtor ? 'aspect-video' : 'min-h-0 flex-1'}`}
          >
            {frameUrl ? (
              <img
                src={frameUrl}
                alt={
                  freeCamPhase === 'active'
                    ? 'Live left-hand camera from bracketbot'
                    : 'Live camera from bracketbot'
                }
                className={`size-full object-cover ${online && freeCamPhase === 'off' ? 'cursor-crosshair' : 'cursor-default'}`}
                onClick={sendViewTarget}
              />
            ) : (
              <div className="text-center text-white/60">
                <Radio className="mx-auto mb-3" />
                <p className="text-sm">Waiting for camera frames</p>
              </div>
            )}
            {frameUrl && (
              <div className="pointer-events-none absolute left-4 top-4 rounded-2xl bg-black/55 px-4 py-3 text-white backdrop-blur">
                <p className="text-sm font-semibold">
                  {freeCamPhase === 'active' ? 'Left-hand camera' : 'Click where you want to go'}
                </p>
                <p className="mt-0.5 text-xs text-white/70">
                  {freeCamPhase === 'active'
                    ? 'The mobile base is locked.'
                    : 'The robot will navigate toward that point.'}
                </p>
              </div>
            )}
            {viewTarget && (
              <div
                className={`pointer-events-none absolute size-8 -translate-x-1/2 -translate-y-1/2 rounded-full border-4 shadow-lg ${targetCommand?.status === 'succeeded' ? 'border-green-400 bg-green-400/30' : 'animate-pulse border-white bg-brand/50'}`}
                style={{ left: `${viewTarget.u * 100}%`, top: `${viewTarget.v * 100}%` }}
                aria-hidden="true"
              />
            )}
            {!isRealtor && (
              <VisitorMiniMap
                grid={gridStatus === 'ready' && grid?.id === mapId ? grid : undefined}
                pose={robot?.pose}
                navigation={navigation}
              />
            )}
            {!isRealtor && (freeCamOpen || freeCamPhase !== 'off') && (
              <FreeCamPanel
                panDeg={freeCamPose.panDeg}
                phase={freeCamPhase}
                tiltDeg={freeCamPose.tiltDeg}
                onClose={() => setFreeCamOpen(false)}
                onExit={stopFreeCam}
                onNudge={(panDelta, tiltDelta) =>
                  sendFreeCamPose(freeCamPose.panDeg + panDelta, freeCamPose.tiltDeg + tiltDelta)
                }
                onRecenter={() => sendFreeCamPose(0, 0)}
                onStart={startFreeCam}
              />
            )}
          </div>
          <div className="flex shrink-0 items-center gap-3 px-5 py-3 text-sm text-white/80">
            <Bot size={17} />
            <span>{robot?.status || phaseCopy[phase]}</span>
            {isRealtor && robot && (
              <span className="ml-auto font-mono text-xs text-white/55">
                {robot.pose.x.toFixed(2)}, {robot.pose.y.toFixed(2)}
              </span>
            )}
          </div>
        </section>

        {isRealtor && (
          <section className="relative min-h-[420px] overflow-hidden rounded-3xl border border-line bg-bg-soft shadow-md">
            {gridStatus === 'ready' && grid ? (
              <MapScene grid={grid} robotPose={robot?.pose} />
            ) : (
              <div className="skeleton absolute inset-0" aria-label="Loading map" />
            )}
            <div className="pointer-events-none absolute left-4 top-4 rounded-2xl bg-white/85 px-4 py-3 shadow-md backdrop-blur">
              <p className="text-sm font-semibold">SLAM telemetry</p>
              <p className="mt-0.5 text-xs text-ink-2">Movement targets are selected in the camera feed.</p>
            </div>
          </section>
        )}
      </div>

      {isRealtor ? (
        <section className="mt-6 rounded-3xl border border-line bg-white p-5 shadow-sm">
          <h2 className="font-semibold">Command activity</h2>
          {commands.length === 0 ? (
            <p className="mt-2 text-sm text-ink-2">No commands sent yet.</p>
          ) : (
            <ul className="mt-3 divide-y divide-line">
              {commands.map((command) => (
                <li key={command.commandId} className="flex items-center gap-3 py-3 text-sm">
                  <span className="font-mono text-xs text-ink-3">{command.commandId.slice(0, 8)}</span>
                  <span className="font-semibold">{command.action?.replaceAll('_', ' ')}</span>
                  <span className="ml-auto text-ink-2">
                    {command.status}
                    {command.attempt && command.attempt > 1 ? ` · attempt ${command.attempt}` : ''}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </section>
      ) : (
        <div className="mt-2 min-h-5 shrink-0 text-center text-sm text-ink-2" aria-live="polite">
          {freeCamPhase === 'active'
            ? 'Left-hand free cam active · base locked'
            : commands[0]
              ? `${commands[0].action === 'move_to_view' ? 'Move' : commands[0].action?.replaceAll('_', ' ')} · ${commands[0].status}`
              : 'Click a place in the camera feed to move there.'}
        </div>
      )}
    </PageShell>
  )
}

import { ArrowLeft, Bot, CircleStop, Radio, Sparkles, Video } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { MapScene } from '../components/map/MapScene'
import { Button } from '../components/ui/Button'
import { PageShell, Wordmark } from '../components/ui/PageShell'
import {
  RemoteBotClient,
  type CommandUpdate,
  type ConnectionPhase,
  type RobotState,
} from '../lib/remoteBotClient'
import { useGridStore } from '../store/useGridStore'
import { useViewStore } from '../store/useViewStore'

const phaseCopy: Record<ConnectionPhase, string> = {
  idle: 'Disconnected',
  connecting: 'Connecting to relay…',
  waiting: 'Waiting for robot…',
  online: 'Robot online',
  reconnecting: 'Reconnecting…',
}

export function ControlPage() {
  const { roomId = 'demo-bot' } = useParams()
  const client = useMemo(() => new RemoteBotClient(), [])
  const [phase, setPhase] = useState<ConnectionPhase>('connecting')
  const [robot, setRobot] = useState<RobotState>()
  const [frameUrl, setFrameUrl] = useState<string>()
  const [commands, setCommands] = useState<CommandUpdate[]>([])
  const { status: gridStatus, grid, load } = useGridStore()

  useEffect(() => {
    void load('small-house')
  }, [load])

  useEffect(() => {
    let currentFrame: string | undefined
    const unsubscribe = client.subscribe((event) => {
      if (event.type === 'connection') setPhase(event.phase)
      if (event.type === 'state') setRobot(event.state)
      if (event.type === 'video') {
        const next = URL.createObjectURL(event.frame)
        if (currentFrame) URL.revokeObjectURL(currentFrame)
        currentFrame = next
        setFrameUrl(next)
      }
      if (event.type === 'command') {
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
  const sendMove = (x: number, y: number) => {
    if (!online) return
    client.sendCommand('move_to', { x: +x.toFixed(3), y: +y.toFixed(3) })
  }

  return (
    <PageShell wide>
      <header className="flex items-center justify-between gap-4">
        <Wordmark />
        <div className="flex items-center gap-3">
          <span
            data-testid="connection-status"
            className={`inline-flex items-center gap-2 rounded-full px-3 py-1.5 text-xs font-semibold ${online ? 'bg-green-50 text-green-700' : 'bg-bg-soft text-ink-2'}`}
          >
            <span className={`size-2 rounded-full ${online ? 'bg-green-600' : 'bg-ink-3'}`} />
            {phaseCopy[phase]}
          </span>
          <Link to="/" className="inline-flex items-center gap-1 text-sm text-ink-2 no-underline">
            <ArrowLeft size={16} /> Spaces
          </Link>
        </div>
      </header>

      <section className="mt-6 flex flex-wrap items-center justify-between gap-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-widest text-ink-3">Demo room</p>
          <h1 className="mt-1 text-2xl font-bold tracking-tight">{roomId}</h1>
        </div>
        <div className="flex gap-2">
          <Button
            variant="secondary"
            disabled={!online}
            onClick={() => client.sendCommand('use_action', { name: 'demo_action' })}
          >
            <Sparkles size={17} /> Use action
          </Button>
          <Button
            className="bg-red-600 hover:bg-red-700"
            disabled={!online}
            onClick={() => client.sendCommand('stop')}
          >
            <CircleStop size={18} /> Stop
          </Button>
        </div>
      </section>

      <div className="mt-6 grid gap-6 xl:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)]">
        <section className="overflow-hidden rounded-3xl border border-line bg-[#20242b] shadow-md">
          <div className="flex items-center justify-between px-5 py-4 text-white">
            <div className="flex items-center gap-2 font-semibold">
              <Video size={18} /> Live camera
            </div>
            <span className="text-xs text-white/60">JPEG relay</span>
          </div>
          <div className="grid aspect-video place-items-center bg-[#171a1f]">
            {frameUrl ? (
              <img src={frameUrl} alt="Live camera from bracketbot" className="size-full object-cover" />
            ) : (
              <div className="text-center text-white/60">
                <Radio className="mx-auto mb-3" />
                <p className="text-sm">Waiting for camera frames</p>
              </div>
            )}
          </div>
          <div className="flex items-center gap-3 px-5 py-4 text-sm text-white/80">
            <Bot size={17} />
            <span>{robot?.status || phaseCopy[phase]}</span>
            {robot && (
              <span className="ml-auto font-mono text-xs text-white/55">
                {robot.pose.x.toFixed(2)}, {robot.pose.y.toFixed(2)}
              </span>
            )}
          </div>
        </section>

        <section className="relative min-h-[420px] overflow-hidden rounded-3xl border border-line bg-bg-soft shadow-md">
          {gridStatus === 'ready' && grid ? (
            <MapScene grid={grid} onMoveTo={sendMove} robotPose={robot?.pose} />
          ) : (
            <div className="skeleton absolute inset-0" aria-label="Loading map" />
          )}
          <div className="pointer-events-none absolute left-4 top-4 rounded-2xl bg-white/85 px-4 py-3 shadow-md backdrop-blur">
            <p className="text-sm font-semibold">Tap the floor to move</p>
            <p className="mt-0.5 text-xs text-ink-2">Commands send only while the robot is online.</p>
          </div>
        </section>
      </div>

      <section className="mt-6 rounded-3xl border border-line bg-white p-5 shadow-sm">
        <h2 className="font-semibold">Command activity</h2>
        {commands.length === 0 ? (
          <p className="mt-2 text-sm text-ink-2">No commands sent yet.</p>
        ) : (
          <ul className="mt-3 divide-y divide-line">
            {commands.map((command) => (
              <li key={command.commandId} className="flex items-center gap-3 py-3 text-sm">
                <span className="font-mono text-xs text-ink-3">{command.commandId.slice(0, 8)}</span>
                <span className="font-semibold">{command.action?.replace('_', ' ')}</span>
                <span className="ml-auto text-ink-2">
                  {command.status}
                  {command.attempt && command.attempt > 1 ? ` · attempt ${command.attempt}` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </PageShell>
  )
}

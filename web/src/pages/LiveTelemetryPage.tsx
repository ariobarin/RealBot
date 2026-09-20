import { useEffect, useRef, useState, type FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { LiveTelemetryClient, type LiveView } from '../lib/liveTelemetryClient'
import { parseViewerSession } from '../lib/liveTelemetry'
import { supabase } from '../lib/supabase'

export function LiveTelemetryPage() {
  const { roomId = '' } = useParams()
  const [view, setView] = useState<LiveView>({
    connection: 'disconnected',
    robotOnline: false,
    telemetryFresh: false,
  })
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [identity, setIdentity] = useState('')
  const client = useRef<LiveTelemetryClient | null>(null)
  const request = useRef<AbortController | null>(null)
  const video = useRef<HTMLVideoElement>(null)
  const endpoint = import.meta.env.VITE_LIVEKIT_SESSION_ENDPOINT?.trim()
  const manual = import.meta.env.DEV && !endpoint
  useEffect(() => {
    const instance = new LiveTelemetryClient(setView)
    client.current = instance
    return () => {
      request.current?.abort()
      instance.disconnect()
      client.current = null
    }
  }, [roomId])
  useEffect(() => {
    const element = video.current
    const track = view.camera
    if (!element || !track) return
    track.attach(element)
    return () => {
      track.detach(element)
      element.srcObject = null
    }
  }, [view.camera])

  async function connect(event: FormEvent) {
    event.preventDefault()
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setError('')
    setPending(true)
    try {
      let session
      if (endpoint) {
        const target = new URL(endpoint, window.location.origin)
        if (
          target.protocol !== 'https:' &&
          !(import.meta.env.DEV && target.origin === window.location.origin)
        ) {
          throw new Error('The session endpoint must use HTTPS.')
        }
        const auth = await supabase?.auth.getSession()
        const bearer = auth?.data.session?.access_token
        if (!bearer) throw new Error('Sign in to obtain a robot viewer session.')
        const response = await fetch(target, {
          method: 'POST',
          signal: controller.signal,
          headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${bearer}` },
          body: JSON.stringify({ roomId, mode: 'view' }),
        })
        if (!response.ok) throw new Error(`Session request failed (${response.status}).`)
        session = parseViewerSession(await response.json())
      } else if (manual) {
        session = parseViewerSession({ url, token, robotIdentity: identity })
        setToken('') // Keep short-lived credentials out of storage and URLs.
      } else throw new Error('Robot viewer sessions have not been configured yet.')
      if (!controller.signal.aborted) await client.current?.connect(session)
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Session request failed.')
    } finally {
      if (!controller.signal.aborted) setPending(false)
    }
  }

  const data = view.telemetryFresh ? view.telemetry : undefined
  const pose = data?.pose
  const slam = data?.slam
  const slamStatus = !slam?.fresh
    ? 'Unavailable'
    : slam.vo_lost
      ? 'Tracking lost'
      : slam.stalled
        ? 'Stalled'
        : !slam.localized
          ? 'Not localized'
          : slam.degraded
            ? 'Degraded'
            : !slam.poseFresh
              ? 'Pose stale'
              : 'Localized'
  return (
    <main className="mx-auto max-w-6xl space-y-6 p-6">
      <Link to={`/realtor/control/${encodeURIComponent(roomId)}`} className="text-sm underline">
        Back to dashboard
      </Link>
      <div>
        <h1 className="text-2xl font-semibold">Live robot view</h1>
        <p className="text-ink-2">{roomId} · Observation only</p>
      </div>
      <form onSubmit={connect} className="flex flex-wrap items-end gap-3">
        {manual && (
          <>
            <label>
              LiveKit URL
              <input
                className="block rounded border p-2"
                required
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="wss://…"
              />
            </label>
            <label>
              Robot identity
              <input
                className="block rounded border p-2"
                required
                value={identity}
                onChange={(e) => setIdentity(e.target.value)}
              />
            </label>
            <label>
              Viewer token
              <input
                className="block rounded border p-2"
                type="password"
                autoComplete="off"
                required
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
            </label>
          </>
        )}
        <button
          className="rounded bg-black px-4 py-2 text-white disabled:opacity-40"
          disabled={pending || view.connection !== 'disconnected' || (!endpoint && !manual)}
        >
          Connect
        </button>
        <button
          className="rounded border px-4 py-2"
          type="button"
          onClick={() => {
            request.current?.abort()
            setPending(false)
            client.current?.disconnect()
          }}
        >
          Disconnect
        </button>
      </form>
      {!endpoint && !manual && <p role="status">Robot viewer sessions have not been configured yet.</p>}
      {(error || view.error) && (
        <p role="alert" className="text-red-700">
          {error || view.error}
        </p>
      )}
      <p role="status">
        Session: {view.connection} · Robot: {view.robotOnline ? 'Online' : 'Offline'} · Telemetry:{' '}
        {view.telemetryFresh ? 'Live' : view.telemetry ? 'Stale' : 'Waiting'}
      </p>
      <div className="relative aspect-video overflow-hidden rounded-xl bg-black">
        <video
          ref={video}
          autoPlay
          playsInline
          muted
          controls
          className="h-full w-full object-contain"
          hidden={!view.camera}
        />
        {!view.camera && (
          <div className="grid h-full place-items-center text-white/70">Waiting for head camera</div>
        )}
      </div>
      <dl className="grid gap-5 rounded-xl border p-5 sm:grid-cols-3">
        <div>
          <dt className="text-ink-2">SLAM</dt>
          <dd>{slamStatus}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Position (map metres)</dt>
          <dd>{pose ? `${pose.x.toFixed(2)}, ${pose.y.toFixed(2)}, ${pose.z.toFixed(2)}` : 'Unavailable'}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Heading (radians)</dt>
          <dd>{pose ? pose.heading.toFixed(2) : 'Unavailable'}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Navigation</dt>
          <dd>{data?.navigation.fresh ? data.navigation.state : 'Unavailable'}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Navigation detail</dt>
          <dd>{data?.navigation.reason || '—'}</dd>
        </div>
        <div>
          <dt className="text-ink-2">Map identity</dt>
          <dd>{data?.mapId || 'Not supplied by robot'}</dd>
        </div>
      </dl>
    </main>
  )
}

import { useEffect, useRef, useState, type FormEvent, type MouseEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { CircleStop, Video } from 'lucide-react'
import { LiveDriveClient, type DriveView } from '../lib/liveDriveClient'
import type { LiveView } from '../lib/liveTelemetryClient'
import { parseViewerSession } from '../lib/liveTelemetry'
import { supabase } from '../lib/supabase'
import { PageShell } from '../components/ui/PageShell'

export function LiveDrivePage() {
  const { roomId = '' } = useParams()
  const [view, setView] = useState<LiveView>({
    connection: 'disconnected',
    robotOnline: false,
    telemetryFresh: false,
  })
  const [drive, setDrive] = useState<DriveView>({
    available: false,
    canCapture: false,
    canClick: false,
    busy: false,
    status: 'Connect to see the robot.',
  })
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [identity, setIdentity] = useState('')
  const [marker, setMarker] = useState<{ u: number; v: number }>()
  const [accessCode, setAccessCode] = useState('')
  const client = useRef<LiveDriveClient | null>(null)
  const request = useRef<AbortController | null>(null)
  const video = useRef<HTMLVideoElement>(null)
  const endpoint = import.meta.env.VITE_LIVEKIT_SESSION_ENDPOINT?.trim()
  const manual = import.meta.env.DEV && !endpoint

  async function loadDemo(file: File | undefined) {
    if (!file) return
    setError('')
    setToken('')
    try {
      if (file.size > 16_384) throw new Error('Demo session file is too large.')
      const data = JSON.parse(await file.text())
      if (data?.version !== 1 || data?.kind !== 'browser') throw new Error('Not a browser session file.')
      const session = parseViewerSession(data)
      setUrl(session.url)
      setToken(session.token)
      setIdentity(session.robotIdentity)
    } catch {
      setError('Could not load the browser session file. Use browser.session.json from the token generator.')
    }
  }

  useEffect(() => {
    const instance = new LiveDriveClient(setView, setDrive)
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
        )
          throw new Error('The session endpoint must use HTTPS.')
        const auth = await supabase?.auth.getSession()
        const bearer = auth?.data.session?.access_token
        if (!bearer && !accessCode.trim()) throw new Error('Enter your tour access code.')
        const response = await fetch(target, {
          method: 'POST',
          signal: controller.signal,
          headers: {
            'Content-Type': 'application/json',
            ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
          },
          body: JSON.stringify({ roomId, mode: 'drive', ...(!bearer ? { accessCode } : {}) }),
        })
        if (!response.ok) throw new Error(`Could not open this robot session (${response.status}).`)
        session = parseViewerSession(await response.json())
      } else if (manual) {
        session = parseViewerSession({ url, token, robotIdentity: identity })
        setToken('')
      } else throw new Error('Robot sessions have not been configured yet.')
      if (!controller.signal.aborted) await client.current?.connect(session)
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : 'Could not connect.')
    } finally {
      if (!controller.signal.aborted) setPending(false)
    }
  }

  const perform = (action: () => void) => {
    setError('')
    try {
      action()
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Command unavailable.')
    }
  }
  const selectFloor = (event: MouseEvent<HTMLVideoElement>) => {
    const rect = event.currentTarget.getBoundingClientRect()
    const u = (event.clientX - rect.left) / rect.width
    const v = (event.clientY - rect.top) / rect.height
    perform(() => {
      client.current?.clickDestination(u, v)
      setMarker({ u, v })
    })
  }

  return (
    <PageShell wide>
      <div className="flex items-center justify-between gap-4">
        <div>
          <Link to="/" className="text-sm text-ink-2">
            Back to tours
          </Link>
          <h1 className="mt-2 text-2xl font-semibold">Explore with the robot</h1>
        </div>
        <span className="text-sm text-ink-2">
          {view.connection === 'connected' && view.robotOnline ? 'Robot connected' : view.connection}
        </span>
      </div>
      <form onSubmit={connect} className="my-5 flex flex-wrap items-end gap-3">
        {manual && (
          <>
            <label className="text-sm">
              Load demo session
              <input
                className="block max-w-60 p-2"
                type="file"
                accept=".json,application/json"
                disabled={pending || view.connection !== 'disconnected'}
                onChange={(event) => {
                  void loadDemo(event.target.files?.[0])
                  event.target.value = ''
                }}
              />
            </label>
            <label className="text-sm">
              LiveKit URL
              <input
                className="block rounded-lg border p-2"
                required
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                placeholder="wss://…"
              />
            </label>
            <label className="text-sm">
              Robot identity
              <input
                className="block rounded-lg border p-2"
                required
                value={identity}
                onChange={(e) => setIdentity(e.target.value)}
              />
            </label>
            <label className="text-sm">
              Controller token
              <input
                className="block rounded-lg border p-2"
                type="password"
                autoComplete="off"
                required
                value={token}
                onChange={(e) => setToken(e.target.value)}
              />
            </label>
          </>
        )}
        {endpoint && (
          <label className="text-sm">
            Tour access code
            <input
              className="block rounded-lg border p-2"
              type="password"
              autoComplete="off"
              value={accessCode}
              onChange={(e) => setAccessCode(e.target.value)}
            />
          </label>
        )}
        <button
          className="rounded-lg bg-ink px-4 py-2 text-white disabled:opacity-40"
          disabled={pending || view.connection !== 'disconnected' || (!endpoint && !manual)}
        >
          Connect
        </button>
        {view.connection !== 'disconnected' && (
          <button
            type="button"
            className="rounded-lg border px-4 py-2"
            onClick={() => {
              request.current?.abort()
              setPending(false)
              setMarker(undefined)
              client.current?.disconnect()
            }}
          >
            Disconnect
          </button>
        )}
      </form>
      {!endpoint && !manual && <p role="status">Robot sessions have not been configured yet.</p>}
      {(error || view.error) && (
        <p role="alert" className="my-3 text-red-700">
          {error || view.error}
        </p>
      )}
      {drive.previewOnly && (
        <p role="note" className="my-3 rounded-lg bg-amber-100 p-3 text-amber-950">
          Preview only — movement is disabled. Click the live camera yourself.
          The robot will validate your point and report coordinates or explain why it cannot be used.
        </p>
      )}
      <div className="relative grid min-h-72 place-items-center overflow-hidden rounded-2xl bg-black">
        {view.camera && (
          <div className="relative max-h-[65vh] max-w-full">
            <video
              ref={video}
              autoPlay
              playsInline
              muted
              aria-label="Live robot camera; click clear floor to choose a destination"
              onClick={selectFloor}
              className={`block max-h-[65vh] max-w-full ${drive.canClick ? 'cursor-crosshair' : 'cursor-not-allowed'}`}
            />
            {marker && (
              <span
                aria-label="Selected destination"
                className="pointer-events-none absolute size-5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-red-600 shadow"
                style={{ left: `${marker.u * 100}%`, top: `${marker.v * 100}%` }}
              />
            )}
          </div>
        )}
        {!view.camera && (
          <div className="flex items-center gap-3 p-16 text-white/70">
            <Video size={22} />
            Waiting for the robot camera
          </div>
        )}
      </div>
      <div className="my-5 flex flex-wrap items-center gap-3">
        <button
          className="flex items-center gap-2 rounded-lg bg-red-700 px-5 py-3 font-semibold text-white disabled:opacity-40"
          disabled={!drive.available}
          onClick={() => perform(() => {
              client.current?.stop()
              setMarker(undefined)
            })}
        >
          <CircleStop size={20} />
          {drive.previewOnly ? 'Cancel preview' : 'Stop'}
        </button>
        <p role="status" className="text-sm text-ink-2">
          {drive.status}
        </p>
      </div>
      <p className="text-sm text-ink-2">
        {drive.previewOnly
          ? 'Click clear floor directly in the live camera. This session validates the point but cannot move the robot.'
          : 'Click clear floor directly in the live camera. The robot checks fresh aligned depth before moving. You can stop it at any time while connected.'}
      </p>
      <p className="mt-2 text-xs text-ink-2">
        Live video and destination selection use the same rectified left camera. A click is rejected if localization, depth, map clearance, or the stationary check is not ready.
      </p>
    </PageShell>
  )
}

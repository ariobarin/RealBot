import { describe, it, expect, vi, afterEach } from 'vitest'
import { Room, RoomEvent } from 'livekit-client'
import { parseTelemetry, parseViewerSession } from './liveTelemetry'
import { LiveTelemetryClient, type LiveView } from './liveTelemetryClient'

const packet = (at = 1000) =>
  new TextEncoder().encode(
    JSON.stringify({
      version: 1,
      type: 'telemetry',
      at,
      mapId: null,
      ready: false,
      pose: null,
      slam: {
        fresh: false,
        poseFresh: false,
        localized: null,
        degraded: null,
        stalled: null,
        vo_lost: null,
        relocalized: null,
      },
      navigation: { fresh: false, state: 'unavailable', reason: '', waypointIndex: null },
    }),
  )

describe('LiveKit receive-only telemetry', () => {
  afterEach(() => vi.useRealTimers())
  it('validates packet shape and secure session URLs', () => {
    expect(parseTelemetry(packet())?.pose).toBeNull()
    expect(parseTelemetry(new TextEncoder().encode('{"version":1}'))).toBeUndefined()
    expect(parseTelemetry(new Uint8Array(16001))).toBeUndefined()
    expect(() =>
      parseViewerSession({ url: 'ws://public.example', token: 'token', robotIdentity: 'bot' }),
    ).toThrow()
    expect(() =>
      parseViewerSession({ url: 'wss://example.com', token: 'token', robotIdentity: '' }),
    ).toThrow()
  })

  it('filters senders/topics, expires telemetry, and clears state when the robot leaves', async () => {
    vi.useFakeTimers()
    const handlers = new Map<string, ((...args: unknown[]) => void)[]>()
    const camera = { trackName: 'cam-wrist', kind: 'video', setSubscribed: vi.fn() }
    const robot = {
      identity: 'bot',
      videoTrackPublications: new Map(),
      trackPublications: new Map([['camera', camera]]),
    }
    const room = {
      on: (event: string, handler: (...args: unknown[]) => void) => {
        handlers.set(event, [...(handlers.get(event) ?? []), handler])
      },
      remoteParticipants: new Map([['bot', robot]]),
      connect: vi.fn().mockResolvedValue(undefined),
      disconnect: vi.fn().mockResolvedValue(undefined),
      removeAllListeners: () => handlers.clear(),
    }
    const emit = (event: string, ...args: unknown[]) => handlers.get(event)?.forEach((fn) => fn(...args))
    let view: LiveView | undefined
    const client = new LiveTelemetryClient(
      (next) => {
        view = next
      },
      () => room as unknown as Room,
    )
    await client.connect({ url: 'wss://example.com', token: 'short-lived', robotIdentity: 'bot' })
    expect(camera.setSubscribed).toHaveBeenCalledWith(true)
    expect(view?.robotOnline).toBe(true)
    emit(RoomEvent.DataReceived, packet(), { identity: 'other' }, undefined, 'realbot.telemetry')
    emit(RoomEvent.DataReceived, packet(), robot, undefined, 'movement')
    expect(view?.telemetry).toBeUndefined()
    emit(RoomEvent.DataReceived, packet(), robot, undefined, 'realbot.telemetry')
    expect(view?.telemetryFresh).toBe(true)
    await vi.advanceTimersByTimeAsync(2250)
    expect(view?.telemetryFresh).toBe(false)
    // Replayed snapshots cannot keep the UI live.
    emit(RoomEvent.DataReceived, packet(), robot, undefined, 'realbot.telemetry')
    expect(view?.telemetryFresh).toBe(false)
    emit(RoomEvent.DataReceived, packet(1100), robot, undefined, 'realbot.telemetry')
    expect(view?.telemetryFresh).toBe(true)
    emit(RoomEvent.Reconnecting)
    expect(view?.telemetryFresh).toBe(false)
    emit(RoomEvent.Reconnected)
    expect(view?.telemetry).toBeUndefined()
    room.remoteParticipants.clear()
    emit(RoomEvent.ParticipantDisconnected, robot)
    expect(view?.robotOnline).toBe(false)
    client.disconnect()
    expect(vi.getTimerCount()).toBe(0)
  })
})

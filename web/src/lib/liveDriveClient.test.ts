import { afterEach, describe, expect, it, vi } from 'vitest'
import { Room, RoomEvent } from 'livekit-client'
import { LiveDriveClient, type DriveView } from './liveDriveClient'

async function setup() {
  vi.useFakeTimers()
  const handlers = new Map<string, ((...args: unknown[]) => void)[]>()
  const robot = { identity: 'bot', videoTrackPublications: new Map(), trackPublications: new Map() }
  const publishData = vi.fn().mockResolvedValue(undefined)
  const room = {
    on: (event: string, fn: (...args: unknown[]) => void) =>
      handlers.set(event, [...(handlers.get(event) ?? []), fn]),
    remoteParticipants: new Map([['bot', robot]]),
    localParticipant: { publishData },
    connect: vi.fn().mockResolvedValue(undefined),
    disconnect: vi.fn().mockResolvedValue(undefined),
    removeAllListeners: () => handlers.clear(),
  }
  let view: DriveView
  const client = new LiveDriveClient(
    () => {},
    (next) => {
      view = next
    },
    () => room as unknown as Room,
  )
  await client.connect({ url: 'wss://example.com', token: 'controller', robotIdentity: 'bot' })
  const emit = (event: string, ...args: unknown[]) => handlers.get(event)?.forEach((fn) => fn(...args))
  let timestamp = 1000
  const receive = (message: Record<string, unknown>, sender = robot, topic = 'realbot.driving') =>
    emit(
      RoomEvent.DataReceived,
      new TextEncoder().encode(
        JSON.stringify({ version: 1, sessionId: 'session', at: ++timestamp, ...message }),
      ),
      sender,
      undefined,
      topic,
    )
  const state = () => receive({ type: 'state', ready: true, lease: true, canCapture: true, busy: false })
  const packets = () => publishData.mock.calls.map(([bytes]) => JSON.parse(new TextDecoder().decode(bytes)))
  const commands = () => packets().filter((p) => p.type === 'command')
  return { client, room, robot, emit, receive, state, packets, commands, publishData, view: () => view! }
}

describe('LiveKit visitor driving', () => {
  afterEach(() => vi.useRealTimers())

  it('requires robot state, filters identities/topics, targets data without media publication', async () => {
    const s = await setup()
    expect(() => s.client.chooseDestination()).toThrow()
    s.receive(
      { type: 'state', ready: true, lease: true, canCapture: true, busy: false },
      { ...s.robot, identity: 'intruder' },
    )
    s.receive({ type: 'state', ready: true, lease: true, canCapture: true, busy: false }, s.robot, 'movement')
    expect(s.view().available).toBe(false)
    s.state()
    expect(s.view().canCapture).toBe(true)
    s.client.chooseDestination()
    expect(s.commands()[0]).toMatchObject({ action: 'capture', sessionId: 'session' })
    expect(
      s.publishData.mock.calls.every(
        ([, options]) =>
          options.topic === 'realbot.drive_command' && options.destinationIdentities[0] === 'bot',
      ),
    ).toBe(true)
    s.client.disconnect()
    expect(vi.getTimerCount()).toBe(0)
  })

  it('associates clicks with the authorized capture and consumes it once', async () => {
    const s = await setup()
    s.state()
    s.client.chooseDestination()
    const id = s.commands()[0].commandId
    s.receive({
      type: 'capture',
      commandId: 'wrong',
      captureId: 'frame',
      image: '/9j/AA==',
      validForMs: 10000,
    })
    expect(s.view().capture).toBeUndefined()
    s.receive({ type: 'capture', commandId: id, captureId: 'frame', image: '/9j/AA==', validForMs: 10000 })
    expect(() => s.client.move(0.5, 0.5)).toThrow() // Wait for capture completion.
    s.receive({ type: 'result', commandId: id, status: 'succeeded' })
    s.client.move(0.25, 0.75)
    expect(s.commands().at(-1)).toMatchObject({ action: 'move', captureId: 'frame', u: 0.25, v: 0.75 })
    expect(s.view().capture).toBeUndefined()
    expect(() => s.client.move(0.25, 0.75)).toThrow()
    s.client.stop()
    expect(s.commands().at(-1).action).toBe('stop')
    s.client.disconnect()
  })

  it('retries the same command ID, but never replays commands after reconnect', async () => {
    const s = await setup()
    s.state()
    s.client.chooseDestination()
    await vi.advanceTimersByTimeAsync(1100)
    expect(s.commands()).toHaveLength(3)
    expect(new Set(s.commands().map((p) => p.commandId)).size).toBe(1)
    const sequence = s
      .packets()
      .filter((p) => p.type === 'heartbeat')
      .at(-1).sequence
    s.emit(RoomEvent.Reconnecting)
    expect(s.view().available).toBe(false)
    s.emit(RoomEvent.Reconnected)
    s.state()
    await vi.advanceTimersByTimeAsync(500)
    expect(s.commands()).toHaveLength(3)
    expect(
      s
        .packets()
        .filter((p) => p.type === 'heartbeat')
        .at(-1).sequence,
    ).toBeGreaterThan(sequence)
    s.client.disconnect()
  })

  it('expires controls after robot leave/rejoin even without a room reconnect', async () => {
    const s = await setup()
    s.state()
    s.room.remoteParticipants.clear()
    s.emit(RoomEvent.ParticipantDisconnected, s.robot)
    expect(s.view().available).toBe(false)
    s.room.remoteParticipants.set('bot', s.robot)
    s.emit(RoomEvent.ParticipantConnected, s.robot)
    s.state()
    expect(s.view().available).toBe(true)
    await vi.advanceTimersByTimeAsync(2500)
    expect(s.view().available).toBe(false)
    expect(() => s.client.chooseDestination()).toThrow()
    s.client.disconnect()
  })

  it('clears retries on acknowledgement and rejects expired captured views', async () => {
    const s = await setup()
    s.state()
    s.client.chooseDestination()
    const commandId = s.commands()[0].commandId
    s.receive({ type: 'result', commandId, status: 'accepted' })
    s.receive({ type: 'capture', commandId, captureId: 'frame', image: '/9j/AA==', validForMs: 100 })
    s.receive({ type: 'result', commandId, status: 'succeeded' })
    await vi.advanceTimersByTimeAsync(600)
    expect(s.commands()).toHaveLength(1)
    expect(s.view().capture).toBeUndefined()
    expect(() => s.client.move(0.5, 0.5)).toThrow()
    s.client.disconnect()
  })

  it('invalidates a captured view when localization is lost', async () => {
    const s = await setup()
    s.state()
    s.client.chooseDestination()
    const commandId = s.commands()[0].commandId
    s.receive({ type: 'capture', commandId, captureId: 'frame', image: '/9j/AA==', validForMs: 10000 })
    s.receive({ type: 'result', commandId, status: 'succeeded' })
    expect(s.view().capture).toBeDefined()
    s.receive({ type: 'state', ready: false, lease: true, canCapture: false, busy: false })
    expect(s.view().capture).toBeUndefined()
    expect(s.view().canCapture).toBe(false)
    expect(() => s.client.move(0.5, 0.5)).toThrow()
    s.client.disconnect()
  })
})

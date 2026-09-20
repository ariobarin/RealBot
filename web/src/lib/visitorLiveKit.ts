import type { Room } from 'livekit-client'
import { LiveTelemetryClient, type LiveView } from './liveTelemetryClient'
import type { ViewerSession } from './liveTelemetry'
import type { KeyboardConnector } from './keyboardDrive'
import type { MapSnapshot } from '../components/map/LiveSlamMap'

export class VisitorLiveKit extends LiveTelemetryClient {
  private robot = ''
  private clock = 0
  private clockAt = 0
  private mapAt = 0
  private mapRoom?: Room
  private watch?: ReturnType<typeof setInterval>
  private drive?: { nonce: string; active: boolean; closed: (reason: string) => void }
  private mapChanged: (map: MapSnapshot | null) => void

  constructor(changed: (view: LiveView) => void, mapChanged: (map: MapSnapshot | null) => void) {
    super(changed)
    this.mapChanged = mapChanged
  }

  override async connect(session: ViewerSession) {
    this.robot = session.robotIdentity
    await super.connect(session)
  }

  protected override update(patch: Partial<LiveView>) {
    super.update(patch)
    if (this.view.connection !== 'connected' || !this.view.robotOnline) {
      this.drive?.closed('Drive disconnected')
      this.mapChanged?.(null)
    }
  }

  protected override onConnected() {
    const room = this.room!
    if (this.mapRoom !== room) {
      this.mapRoom = room
      room.registerByteStreamHandler('realbot.slam_map', async (reader, participant) => {
        if (participant.identity !== this.robot) return
        try {
          const chunks: Uint8Array<ArrayBuffer>[] = []
          let length = 0
          for await (const chunk of reader.withAbortSignal(AbortSignal.timeout(5000))) {
            length += chunk.length
            if (length > 2_000_000) throw new Error('Map too large')
            chunks.push(new Uint8Array(chunk))
          }
          const text = await new Response(new Blob(chunks).stream().pipeThrough(new DecompressionStream('gzip'))).text()
          if (text.length > 8_000_000) throw new Error('Map too large')
          if (this.room === room) {
            this.mapChanged(JSON.parse(text))
            this.mapAt = performance.now()
          }
        } catch {
          if (this.room === room) this.mapChanged(null)
        }
      })
    }
    if (this.watch) clearInterval(this.watch)
    this.watch = setInterval(() => {
      if (performance.now() - this.clockAt > 750) this.drive?.closed('Drive connection interrupted')
      if (performance.now() - this.mapAt > 5000) this.mapChanged(null)
    }, 100)
  }

  protected override onRobotData(bytes: Uint8Array, topic?: string) {
    if (topic !== 'realbot.keyboard_state' || bytes.length > 512) return
    try {
      const state = JSON.parse(new TextDecoder().decode(bytes))
      if (!Number.isFinite(state.at) || typeof state.nonce !== 'string') return
      this.clock = state.at
      this.clockAt = performance.now()
      if (this.drive?.active && state.nonce !== this.drive.nonce) this.drive.closed('Drive stopped. Enable it again to resume.')
    } catch { /* Ignore malformed telemetry. */ }
  }

  connectKeyboard: KeyboardConnector = (ready, closed) => {
    const room = this.room
    const nonce = crypto.randomUUID()
    const drive = { nonce, active: false, closed }
    this.drive = drive
    let cancelled = false
    let sending = false
    let sequence = 0
    const stop = () => {
      cancelled = true
      if (this.drive === drive) this.drive = undefined
      if (room) void room.localParticipant.performRpc({ destinationIdentity: this.robot,
        method: 'realbot.keyboard.stop', payload: JSON.stringify({ nonce }), responseTimeout: 2000 }).catch(() => {})
    }
    if (!room || !this.view.robotOnline || performance.now() - this.clockAt > 750) {
      queueMicrotask(() => closed('Robot control is not ready yet'))
    } else {
      void room.localParticipant.performRpc({ destinationIdentity: this.robot,
        method: 'realbot.keyboard.start', payload: JSON.stringify({ nonce }), responseTimeout: 3000 })
        .then((reply) => {
          if (cancelled) { stop(); return }
          const result = JSON.parse(reply)
          if (!result.ready || result.nonce !== nonce) throw new Error('Drive unavailable')
          this.clock = result.at
          this.clockAt = performance.now()
          drive.active = true
          ready()
        }).catch((error: unknown) => {
          console.warn('Drive handshake failed', error instanceof Error ? error.message : 'Unknown error')
          if (!cancelled) closed('Could not enable drive. It may be in use.')
        })
    }
    return {
      send: (command) => {
        if (!room || cancelled || !drive.active || sending) return
        sending = true
        const message = { ...command, nonce, sequence: ++sequence,
          expiresAt: Math.floor(this.clock + performance.now() - this.clockAt + 400) }
        void room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(message)), {
          topic: 'realbot.keyboard', reliable: false, destinationIdentities: [this.robot],
        }).catch(() => closed('Drive connection interrupted')).finally(() => { sending = false })
      },
      close: stop,
    }
  }

  override disconnect() {
    if (this.watch) clearInterval(this.watch)
    this.watch = undefined
    this.drive?.closed('Drive disconnected')
    this.drive = undefined
    this.clockAt = 0
    super.disconnect()
  }
}

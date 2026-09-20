import type { Room } from 'livekit-client'
import { LiveTelemetryClient, type LiveView } from './liveTelemetryClient'
import type { ViewerSession } from './liveTelemetry'
import type { KeyboardConnector } from './keyboardDrive'
import type { MapSnapshot } from '../components/map/LiveSlamMap'

export interface FreeCamState {
  available: boolean
  viewing: boolean
  phase: string
  ready?: boolean
  moving?: boolean
  reason?: string
  notice?: string
  offsets?: number[]
}

export interface ActionPoint { id: string; action_id: string; label: string; x: number; y: number }
export interface ActionState { available: boolean; owned: boolean; phase: string; attempt: string; reason?: string }
export interface ActionView { points: ActionPoint[]; state: ActionState }

export class VisitorLiveKit extends LiveTelemetryClient {
  private setupView = false
  private robot = ''
  private clock = 0
  private clockAt = 0
  private mapAt = 0
  private mapRoom?: Room
  private watch?: ReturnType<typeof setInterval>
  private drive?: { nonce: string; active: boolean; closed: (reason: string) => void }
  private mapChanged: (map: MapSnapshot | null) => void
  private freeCamChanged: (state: FreeCamState | null) => void
  private freeCamNonce = ''
  private freeCamSequence = 0
  private freeCamSending = false
  private actionChanged: (view: ActionView | null) => void
  private actionAttempt = ''
  private actionSequence = 0
  private actionAt = 0
  private rightCameraAt = 0
  private actionSending = false

  constructor(changed: (view: LiveView) => void, mapChanged: (map: MapSnapshot | null) => void,
    freeCamChanged: (state: FreeCamState | null) => void = () => {},
    actionChanged: (view: ActionView | null) => void = () => {}) {
    super(changed)
    this.mapChanged = mapChanged
    this.freeCamChanged = freeCamChanged
    this.actionChanged = actionChanged
  }

  override async connect(session: ViewerSession) {
    this.setupView = session.cameraTrack === 'cam-setup'
    this.robot = session.robotIdentity
    await super.connect(session)
  }

  protected override update(patch: Partial<LiveView>) {
    super.update(patch)
    if (this.view.connection !== 'connected' || !this.view.robotOnline) {
      this.drive?.closed('Drive disconnected')
      this.mapChanged?.(null)
      this.freeCamChanged?.(null)
      this.stopAction()
      this.actionChanged?.(null)
    }
  }

  protected override onConnected() {
    const room = this.room!
    let setupAt = -Infinity
    let setupPending = false
    const keepSetupActive = () => {
      if (!this.setupView || setupPending || document.hidden || performance.now() - setupAt < 1000) return
      setupAt = performance.now()
      setupPending = true
      void room.localParticipant.performRpc({ destinationIdentity: this.robot,
        method: 'realbot.camera.setup', payload: '{}', responseTimeout: 2000 })
        .catch(() => {}).finally(() => { setupPending = false })
    }
    keepSetupActive()
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
      keepSetupActive()
      if (this.view.rightCameraFresh && performance.now() - this.rightCameraAt > 750)
        this.update({ rightCameraFresh: false })
      if (performance.now() - this.clockAt > 750) {
        this.drive?.closed('Drive connection interrupted')
        this.freeCamChanged(null)
      }
      if (performance.now() - this.mapAt > 5000) this.mapChanged(null)
      if (performance.now() - this.actionAt > 750) {
        this.stopAction()
        this.actionChanged(null)
      }
      this.actionPulse()
    }, 100)
  }

  protected override onRobotData(bytes: Uint8Array, topic?: string) {
    if (topic === 'realbot.right_camera' && bytes.length < 128) {
      try {
        const state = JSON.parse(new TextDecoder().decode(bytes))
        this.rightCameraAt = performance.now()
        this.update({ rightCameraFresh: state.fresh === true })
      } catch { /* Ignore invalid camera state. */ }
      return
    }
    if (topic === 'realbot.actions' && bytes.length < 32_768) {
      try {
        const data = JSON.parse(new TextDecoder().decode(bytes)) as ActionView
        if (!Array.isArray(data.points) || !data.state) return
        data.points = data.points.filter(p => typeof p.id === 'string' && typeof p.label === 'string'
          && Number.isFinite(p.x) && Number.isFinite(p.y) && p.x >= 0 && p.x <= 1 && p.y >= 0 && p.y <= 1)
        this.actionAt = performance.now()
        if (data.state.attempt === this.actionAttempt && ['held', 'error'].includes(data.state.phase))
          this.actionAttempt = ''
        this.actionChanged(data)
      } catch { /* Ignore malformed action positions. */ }
      return
    }
    if (topic === 'realbot.freecam_state' && bytes.length < 2048) {
      try { this.freeCamChanged(JSON.parse(new TextDecoder().decode(bytes))) } catch { /* Ignore invalid state. */ }
      return
    }
    if (topic !== 'realbot.keyboard_state' || bytes.length > 512) return
    try {
      const state = JSON.parse(new TextDecoder().decode(bytes))
      if (!Number.isFinite(state.at) || typeof state.nonce !== 'string') return
      this.clock = state.at
      this.clockAt = performance.now()
      if (this.drive?.active && state.nonce !== this.drive.nonce) this.drive.closed('Drive stopped. Enable it again to resume.')
    } catch { /* Ignore malformed telemetry. */ }
  }

  async readActionPoints(): Promise<unknown> {
    if (!this.room || !this.view.robotOnline) throw new Error('Robot disconnected')
    const reply = await this.room.localParticipant.performRpc({ destinationIdentity: this.robot,
      method: 'realbot.action_points.read', payload: '{}', responseTimeout: 3000 })
    return JSON.parse(reply)
  }

  async startAction(id: string) {
    if (!this.room || !this.view.robotOnline || performance.now() - this.actionAt > 750)
      throw new Error('Action locations are not ready')
    if (this.actionAttempt) throw new Error('Stop the current attempt first')
    this.actionAttempt = crypto.randomUUID()
    this.actionSequence = 0
    try {
      await this.actionCommand('start', id)
    } catch (error) {
      this.stopAction()
      throw error
    }
  }

  async runScript(script: 'init' | 'go' | 'stop'): Promise<{ reason?: string }> {
    if (!this.room || !this.view.robotOnline) throw new Error('Robot disconnected')
    return JSON.parse(await this.room.localParticipant.performRpc({ destinationIdentity: this.robot,
      method: 'realbot.act_script.run', responseTimeout: 5000,
      payload: JSON.stringify({ script,
        expiresAt: Math.floor(this.clock + performance.now() - this.clockAt + 700) }),
    }))
  }

  private async actionCommand(action: string, id?: string) {
    if (!this.room) throw new Error('Robot disconnected')
    return this.room.localParticipant.performRpc({ destinationIdentity: this.robot,
      method: 'realbot.action.command', responseTimeout: 3000,
      payload: JSON.stringify({ action, id, attempt: this.actionAttempt,
        expiresAt: Math.floor(this.clock + performance.now() - this.clockAt + 700) }),
    })
  }

  stopAction() {
    if (!this.actionAttempt) return
    void this.actionCommand('stop').catch(() => {})
    this.actionAttempt = ''
  }

  private actionPulse() {
    if (!this.room || !this.actionAttempt || this.actionSending || document.hidden) return
    this.actionSending = true
    void this.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({
      attempt: this.actionAttempt, sequence: ++this.actionSequence,
      expiresAt: Math.floor(this.clock + performance.now() - this.clockAt + 400),
    })), { topic: 'realbot.action_pulse', reliable: false, destinationIdentities: [this.robot] })
      .catch(() => this.stopAction()).finally(() => { this.actionSending = false })
  }

  async freeCamCommand(action: string, supported = false) {
    const room = this.room
    if (!room || !this.view.robotOnline || performance.now() - this.clockAt > 750)
      throw new Error('Robot connection is not ready')
    if (action === 'open') {
      this.freeCamNonce = crypto.randomUUID()
      this.freeCamSequence = 0
    }
    const nonce = this.freeCamNonce
    const reply = await room.localParticipant.performRpc({ destinationIdentity: this.robot,
      method: 'realbot.freecam.command', responseTimeout: 2000,
      payload: JSON.stringify({ action, nonce, supported,
        expiresAt: Math.floor(this.clock + performance.now() - this.clockAt + 700) }),
    })
    if (this.freeCamNonce === nonce) {
      if (action === 'close') this.freeCamNonce = ''
      this.freeCamChanged(JSON.parse(reply))
    }
  }

  freeCamPulse(pan: number, tilt: number) {
    const room = this.room
    if (!room || !this.freeCamNonce || this.freeCamSending || performance.now() - this.clockAt > 750) return
    this.freeCamSending = true
    void room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify({
      nonce: this.freeCamNonce, sequence: ++this.freeCamSequence, pan, tilt,
      expiresAt: Math.floor(this.clock + performance.now() - this.clockAt + 400),
    })), { topic: 'realbot.freecam', reliable: false, destinationIdentities: [this.robot] })
      .catch(() => this.freeCamChanged(null)).finally(() => { this.freeCamSending = false })
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
    this.stopAction()
    if (this.watch) clearInterval(this.watch)
    this.watch = undefined
    this.drive?.closed('Drive disconnected')
    this.drive = undefined
    this.freeCamNonce = ''
    this.clockAt = 0
    super.disconnect()
  }
}

import { Room } from 'livekit-client'
import { LiveTelemetryClient, type LiveView } from './liveTelemetryClient'
import type { ViewerSession } from './liveTelemetry'

export interface DriveView {
  available: boolean
  canCapture: boolean
  busy: boolean
  capture?: { id: string; image: string; expires: number }
  status: string
}

const initial = (): DriveView => ({
  available: false,
  canCapture: false,
  busy: false,
  status: 'Waiting for robot control',
})
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)

/** One LiveKit room for media and authorized, targeted driving messages. */
export class LiveDriveClient extends LiveTelemetryClient {
  private drive = initial()
  private drivingChanged: (state: DriveView) => void
  private robotIdentity = ''
  private sessionId = ''
  private sequence = 0
  private robotTime = 0
  private receivedTime = 0
  private pulse?: ReturnType<typeof setInterval>
  private retries: ReturnType<typeof setTimeout>[] = []
  private pending?: { id: string; action: string }

  constructor(
    viewChanged: (view: LiveView) => void,
    drivingChanged: (state: DriveView) => void,
    makeRoom?: () => Room,
  ) {
    super(viewChanged, makeRoom)
    this.drivingChanged = drivingChanged
  }

  private setDrive(patch: Partial<DriveView>) {
    this.drive = { ...this.drive, ...patch }
    this.drivingChanged(this.drive)
  }

  override async connect(session: ViewerSession) {
    this.robotIdentity = session.robotIdentity
    await super.connect(session)
  }

  protected override update(patch: Partial<LiveView>) {
    super.update(patch)
    if (this.view.connection !== 'connected' || !this.view.robotOnline) this.resetDriving()
  }

  private clearRetries() {
    for (const timer of this.retries) clearTimeout(timer)
    this.retries = []
  }

  private resetDriving() {
    if (this.pulse) clearInterval(this.pulse)
    this.pulse = undefined
    this.clearRetries()
    this.sessionId = ''
    this.pending = undefined
    this.drive = initial()
    this.drivingChanged?.(this.drive)
  }

  protected override onConnected() {
    if (this.pulse) clearInterval(this.pulse)
    this.pulse = setInterval(() => {
      if (performance.now() - this.receivedTime > 2_000) {
        this.setDrive({ available: false, canCapture: false, capture: undefined })
        return
      }
      void this.heartbeat()
      if (this.drive.capture && performance.now() >= this.drive.capture.expires)
        this.setDrive({ capture: undefined, status: 'View expired. Choose a new destination view.' })
    }, 500)
  }

  private robotNow() {
    return Math.floor(this.robotTime + performance.now() - this.receivedTime)
  }

  private async publish(message: Record<string, unknown>) {
    if (!this.room || !this.sessionId || this.view.connection !== 'connected')
      throw new Error('Robot is disconnected.')
    await this.room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(message)), {
      reliable: true,
      topic: 'realbot.drive_command',
      destinationIdentities: [this.robotIdentity],
    })
  }

  private async heartbeat() {
    if (!this.sessionId || !this.view.robotOnline) return
    try {
      this.sequence = Math.max(this.sequence + 1, Date.now())
      await this.publish({
        type: 'heartbeat',
        sessionId: this.sessionId,
        sequence: this.sequence,
        expiresAt: this.robotNow() + 2_000,
      })
    } catch {
      this.setDrive({ available: false, canCapture: false, status: 'Control connection interrupted.' })
    }
  }

  protected override onRobotData(bytes: Uint8Array, topic: string | undefined) {
    if (topic !== 'realbot.driving' || bytes.length > 15_000) return
    try {
      const m: unknown = JSON.parse(new TextDecoder().decode(bytes))
      if (
        !object(m) ||
        m.version !== 1 ||
        typeof m.sessionId !== 'string' ||
        !m.sessionId ||
        typeof m.at !== 'number' ||
        !Number.isFinite(m.at)
      )
        return
      if (m.type === 'state') {
        if (
          typeof m.canCapture !== 'boolean' ||
          typeof m.busy !== 'boolean' ||
          typeof m.lease !== 'boolean' ||
          typeof m.ready !== 'boolean'
        )
          return
        if (m.sessionId !== this.sessionId) {
          this.clearRetries()
          this.pending = undefined
          this.sessionId = m.sessionId
          this.setDrive({ capture: undefined })
        } else if (m.at <= this.robotTime) return
        this.robotTime = m.at
        this.receivedTime = performance.now()
        if (!this.pulse) this.onConnected()
        const readiness = m.fault
          ? 'Robot reported a control fault.'
          : !m.ready
            ? 'Camera available; navigation is waiting for localization and sensors.'
            : !m.lease
              ? 'Establishing control connection…'
              : undefined
        this.setDrive({
          available: true,
          canCapture: m.canCapture && !this.pending,
          busy: m.busy,
          ...(!m.ready || !m.lease ? { capture: undefined } : {}),
          ...(readiness
            ? { status: readiness }
            : !this.pending &&
                !this.drive.capture &&
                [
                  'Waiting for robot control',
                  'Establishing control connection…',
                  'Camera available; navigation is waiting for localization and sensors.',
                  'Control connection interrupted.',
                ].includes(this.drive.status)
              ? { status: 'Ready. Choose a destination view.' }
              : {}),
        })
        void this.heartbeat()
      } else if (m.sessionId === this.sessionId && m.commandId === this.pending?.id) {
        if (
          m.type === 'capture' &&
          this.pending?.action === 'capture' &&
          typeof m.captureId === 'string' &&
          typeof m.image === 'string' &&
          m.image.length <= 12_000 &&
          /^[A-Za-z0-9+/]+={0,2}$/.test(m.image) &&
          m.image.startsWith('/9j/') &&
          typeof m.validForMs === 'number' &&
          m.validForMs > 0 &&
          m.validForMs <= 10_000
        ) {
          this.setDrive({
            capture: {
              id: m.captureId,
              image: `data:image/jpeg;base64,${m.image}`,
              expires: performance.now() + m.validForMs,
            },
          })
        } else if (
          m.type === 'result' &&
          typeof m.status === 'string' &&
          ['accepted', 'moving', 'succeeded', 'stopped', 'failed', 'rejected'].includes(m.status)
        ) {
          this.clearRetries()
          this.setDrive({
            status:
              typeof m.detail === 'string' && m.detail
                ? m.detail
                : m.status === 'moving'
                  ? 'Moving to your destination…'
                  : m.status,
          })
          if (['succeeded', 'stopped', 'failed', 'rejected'].includes(m.status)) this.pending = undefined
        }
      }
    } catch {
      /* Ignore malformed packets. */
    }
  }

  private command(action: 'capture' | 'move' | 'stop', payload: Record<string, unknown> = {}) {
    if (!this.sessionId || !this.drive.available) throw new Error('Robot control is not connected.')
    this.clearRetries()
    const commandId = crypto.randomUUID()
    const message = {
      type: 'command',
      commandId,
      action,
      sessionId: this.sessionId,
      expiresAt: this.robotNow() + 3_000,
      ...payload,
    }
    this.pending = { id: commandId, action }
    this.setDrive({
      canCapture: false,
      capture: undefined,
      status:
        action === 'stop'
          ? 'Stopping…'
          : action === 'capture'
            ? 'Preparing destination view…'
            : 'Checking destination…',
    })
    const send = () => {
      if (this.pending?.id !== commandId) return
      void this.publish(message).catch(() =>
        this.setDrive({ canCapture: false, status: 'Command delivery failed. Stop or reconnect.' }),
      )
    }
    send()
    for (const delay of [400, 1_000, 2_000]) this.retries.push(setTimeout(send, delay))
    this.retries.push(
      setTimeout(() => this.setDrive({ status: 'No acknowledgement. Stop or reconnect.' }), 3_500),
    )
  }

  chooseDestination() {
    if (!this.drive.canCapture || this.pending) throw new Error('Wait until the robot is ready.')
    this.command('capture')
  }

  move(u: number, v: number) {
    const capture = this.drive.capture
    if (!capture || performance.now() >= capture.expires || this.drive.busy || this.pending)
      throw new Error('Choose a fresh view when the robot is stopped.')
    if (![u, v].every((n) => Number.isFinite(n) && n >= 0 && n <= 1))
      throw new Error('Click inside the camera image.')
    this.command('move', { captureId: capture.id, u, v })
  }

  stop() {
    this.command('stop')
  }

  override disconnect() {
    this.resetDriving()
    super.disconnect()
  }
}

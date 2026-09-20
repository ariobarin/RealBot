import { Room, RoomEvent, Track, RemoteVideoTrack } from 'livekit-client'
import { parseTelemetry, type Telemetry, type ViewerSession } from './liveTelemetry'

export interface LiveView {
  connection: 'disconnected' | 'connecting' | 'connected' | 'reconnecting'
  robotOnline: boolean
  telemetry?: Telemetry
  telemetryFresh: boolean
  camera?: RemoteVideoTrack
  rightCamera?: RemoteVideoTrack
  rightCameraFresh?: boolean
  error?: string
}

/** Receive-only client. The server must additionally issue subscribe-only grants. */
export class LiveTelemetryClient {
  protected room?: Room
  private timer?: ReturnType<typeof setInterval>
  private lastReceived = 0
  private lastSourceAt = -1
  protected view: LiveView = { connection: 'disconnected', robotOnline: false, telemetryFresh: false }
  private readonly changed: (view: LiveView) => void
  private readonly makeRoom: () => Room

  constructor(changed: (view: LiveView) => void, makeRoom = () => new Room({ adaptiveStream: true })) {
    this.changed = changed
    this.makeRoom = makeRoom
  }

  protected update(patch: Partial<LiveView>) {
    this.view = { ...this.view, ...patch }
    this.changed(this.view)
  }

  protected onRobotData(_bytes: Uint8Array, _topic: string | undefined) {}
  protected onConnected() {}

  async connect(session: ViewerSession) {
    this.disconnect()
    const room = this.makeRoom()
    this.room = room
    this.update({ connection: 'connecting', error: undefined })
    const current = () => this.room === room
    const cameraTrack = session.cameraTrack || 'cam-wrist'
    const refresh = () => {
      if (!current()) return
      const robot = room.remoteParticipants.get(session.robotIdentity)
      const publication = [...(robot?.videoTrackPublications.values() ?? [])].find(
        (pub) => pub.trackName === cameraTrack && !pub.isMuted,
      )
      const track = publication?.videoTrack
      const rightTrack = [...(robot?.videoTrackPublications.values() ?? [])].find(
        (pub) => pub.trackName === 'cam-right' && !pub.isMuted,
      )?.videoTrack
      this.update({ robotOnline: !!robot, camera: track instanceof RemoteVideoTrack ? track : undefined,
        rightCamera: rightTrack instanceof RemoteVideoTrack ? rightTrack : undefined })
      if (!robot) {
        this.lastSourceAt = -1
        this.update({ telemetry: undefined, telemetryFresh: false })
      }
    }
    room.on(RoomEvent.ParticipantConnected, refresh)
    room.on(RoomEvent.ParticipantDisconnected, refresh)
    room.on(RoomEvent.TrackSubscribed, refresh)
    room.on(RoomEvent.TrackUnsubscribed, refresh)
    room.on(RoomEvent.TrackMuted, refresh)
    room.on(RoomEvent.TrackUnmuted, refresh)
    // Subscribe only to the intended robot's head and right-arm video.
    const subscribeTracks = () => {
      for (const participant of room.remoteParticipants.values()) {
        for (const pub of participant.trackPublications.values()) {
          pub.setSubscribed(
            participant.identity === session.robotIdentity &&
              pub.kind === Track.Kind.Video &&
              [cameraTrack, 'cam-right'].includes(pub.trackName),
          )
        }
      }
    }
    room.on(RoomEvent.TrackPublished, subscribeTracks)
    room.on(RoomEvent.ParticipantConnected, subscribeTracks)
    room.on(RoomEvent.DataReceived, (bytes, participant, _kind, topic) => {
      if (!current() || participant?.identity !== session.robotIdentity) return
      this.onRobotData(bytes, topic)
      if (topic !== 'realbot.telemetry') return
      const telemetry = parseTelemetry(bytes)
      if (!telemetry || telemetry.at <= this.lastSourceAt) return
      this.lastSourceAt = telemetry.at
      this.lastReceived = performance.now()
      this.update({ telemetry, telemetryFresh: true })
    })
    room.on(RoomEvent.Reconnecting, () => {
      if (current())
        this.update({
          connection: 'reconnecting',
          robotOnline: false,
          telemetryFresh: false,
          camera: undefined,
          rightCamera: undefined,
          rightCameraFresh: false,
        })
    })
    room.on(RoomEvent.Reconnected, () => {
      if (!current()) return
      this.lastSourceAt = -1
      this.update({ connection: 'connected', telemetry: undefined, telemetryFresh: false })
      subscribeTracks()
      refresh()
      this.onConnected()
    })
    room.on(RoomEvent.Disconnected, () => {
      if (!current()) return
      this.disconnect()
      this.update({ error: 'Session ended. Connect again to request a fresh viewer session.' })
    })
    try {
      await room.connect(session.url, session.token, { autoSubscribe: false })
      if (!current()) {
        await room.disconnect()
        return
      }
      this.update({ connection: 'connected' })
      subscribeTracks()
      refresh()
      this.onConnected()
      this.timer = setInterval(() => {
        if (this.view.telemetryFresh && performance.now() - this.lastReceived > 2_000) {
          this.update({ telemetryFresh: false })
        }
      }, 250)
    } catch {
      if (!current()) return
      this.disconnect()
      this.update({ error: 'Could not join the robot session. Check the viewer token, room, and network.' })
    }
  }

  disconnect() {
    const room = this.room
    this.room = undefined
    if (this.timer) clearInterval(this.timer)
    this.timer = undefined
    room?.removeAllListeners()
    if (room) void room.disconnect().catch(() => {})
    this.lastSourceAt = -1
    this.lastReceived = 0
    this.update({
      connection: 'disconnected',
      robotOnline: false,
      telemetry: undefined,
      telemetryFresh: false,
      camera: undefined,
      rightCamera: undefined,
      rightCameraFresh: false,
    })
  }
}

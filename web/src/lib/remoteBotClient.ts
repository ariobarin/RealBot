export type ConnectionPhase = 'idle' | 'connecting' | 'waiting' | 'online' | 'reconnecting'
export type CommandPhase =
  'sending' | 'delivered' | 'accepted' | 'executing' | 'succeeded' | 'failed' | 'rejected' | 'expired'

export interface RobotState {
  ready: boolean
  status: string
  pose: { x: number; y: number; heading: number }
  at: number
  mapId?: string
  mapRevision?: string
}

export interface MapPoint {
  x: number
  y: number
}

export interface NavigationTelemetry {
  commandId?: string
  mapId?: string
  mapRevision?: string
  status: 'planning' | 'moving' | 'replanning' | 'arrived' | 'failed' | string
  goal?: MapPoint
  path: MapPoint[]
}

export interface CommandEnvelope {
  type: 'command'
  commandId: string
  createdAt: number
  expiresAt: number
  action: 'move_to' | 'move_to_view' | 'stop' | 'use_action'
  payload: Record<string, unknown>
}

export interface CommandUpdate {
  commandId: string
  action?: CommandEnvelope['action']
  status: CommandPhase
  detail?: string
  attempt?: number
}

export type RemoteBotEvent =
  | { type: 'connection'; phase: ConnectionPhase }
  | { type: 'presence'; online: boolean }
  | { type: 'state'; state: RobotState }
  | { type: 'navigation'; navigation: NavigationTelemetry }
  | { type: 'command'; update: CommandUpdate }
  | { type: 'video'; frame: Blob }

type Listener = (event: RemoteBotEvent) => void

interface Pending {
  envelope: CommandEnvelope
  acknowledged: boolean
  timers: number[]
  attempt: number
}

const retryDelays = [400, 1_000, 2_000]

const defaultRelayUrl = () => {
  const configured = (import.meta as ImportMeta & { env?: Record<string, string> }).env?.VITE_RELAY_WS_URL
  if (configured) return configured
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${protocol}//${window.location.hostname}:8000`
}

export class RemoteBotClient {
  private listeners = new Set<Listener>()
  private control?: WebSocket
  private video?: WebSocket
  private reconnectTimer?: number
  private reconnectAttempt = 0
  private generation = 0
  private shouldReconnect = false
  private roomId = ''
  private pending = new Map<string, Pending>()

  subscribe(listener: Listener) {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  private emit(event: RemoteBotEvent) {
    for (const listener of this.listeners) listener(event)
  }

  connect(roomId: string) {
    this.disconnect()
    this.roomId = roomId
    this.shouldReconnect = true
    this.reconnectAttempt = 0
    this.open('connecting')
  }

  disconnect() {
    this.shouldReconnect = false
    this.generation += 1
    if (this.reconnectTimer) window.clearTimeout(this.reconnectTimer)
    this.reconnectTimer = undefined
    this.control?.close()
    this.video?.close()
    this.control = undefined
    this.video = undefined
    for (const pending of this.pending.values()) {
      for (const timer of pending.timers) window.clearTimeout(timer)
    }
    this.pending.clear()
    this.emit({ type: 'connection', phase: 'idle' })
  }

  private open(phase: ConnectionPhase) {
    const generation = ++this.generation
    const base = defaultRelayUrl().replace(/\/$/, '')
    const room = encodeURIComponent(this.roomId)
    this.emit({ type: 'connection', phase })

    const control = new WebSocket(`${base}/ws/client/${room}`)
    const video = new WebSocket(`${base}/ws/client/${room}/video`)
    video.binaryType = 'blob'
    this.control = control
    this.video = video

    control.onopen = () => {
      if (generation !== this.generation) return
      this.reconnectAttempt = 0
      this.emit({ type: 'connection', phase: 'waiting' })
    }
    control.onmessage = (event) => this.handleMessage(String(event.data))
    control.onclose = () => {
      if (generation !== this.generation || !this.shouldReconnect) return
      video.close()
      this.emit({ type: 'presence', online: false })
      this.scheduleReconnect()
    }
    control.onerror = () => control.close()

    video.onmessage = (event) => {
      const frame = event.data instanceof Blob ? event.data : new Blob([event.data], { type: 'image/jpeg' })
      this.emit({ type: 'video', frame })
    }
  }

  private scheduleReconnect() {
    const delay = Math.min(500 * 2 ** this.reconnectAttempt++, 5_000)
    this.emit({ type: 'connection', phase: 'reconnecting' })
    this.reconnectTimer = window.setTimeout(() => this.open('reconnecting'), delay)
  }

  private handleMessage(raw: string) {
    let message: Record<string, unknown>
    try {
      message = JSON.parse(raw) as Record<string, unknown>
    } catch {
      return
    }
    if (message.type === 'presence') {
      const online = Boolean(message.online)
      this.emit({ type: 'presence', online })
      this.emit({ type: 'connection', phase: online ? 'online' : 'waiting' })
      return
    }
    if (message.type === 'robot_state') {
      this.emit({ type: 'state', state: message as unknown as RobotState })
      return
    }
    if (message.type === 'navigation') {
      const rawPath = Array.isArray(message.path) ? message.path : []
      const path = rawPath.flatMap((point) => {
        if (!point || typeof point !== 'object') return []
        const candidate = point as Record<string, unknown>
        return typeof candidate.x === 'number' && typeof candidate.y === 'number'
          ? [{ x: candidate.x, y: candidate.y }]
          : []
      })
      const rawGoal = message.goal
      const goal = rawGoal && typeof rawGoal === 'object' ? (rawGoal as Record<string, unknown>) : undefined
      this.emit({
        type: 'navigation',
        navigation: {
          commandId: typeof message.commandId === 'string' ? message.commandId : undefined,
          mapId: typeof message.mapId === 'string' ? message.mapId : undefined,
          mapRevision: typeof message.mapRevision === 'string' ? message.mapRevision : undefined,
          status: typeof message.status === 'string' ? message.status : 'planning',
          goal:
            goal && typeof goal.x === 'number' && typeof goal.y === 'number'
              ? { x: goal.x, y: goal.y }
              : undefined,
          path,
        },
      })
      return
    }
    if (message.type === 'command_status' && typeof message.commandId === 'string') {
      const status = String(message.status) as CommandPhase
      const pending = this.pending.get(message.commandId)
      if (pending && status !== 'sending') {
        pending.acknowledged = true
        for (const timer of pending.timers) window.clearTimeout(timer)
        pending.timers = []
      }
      this.emit({
        type: 'command',
        update: {
          commandId: message.commandId,
          action: pending?.envelope.action,
          status,
          detail: typeof message.detail === 'string' ? message.detail : undefined,
          attempt: pending?.attempt,
        },
      })
      if (['succeeded', 'failed', 'rejected', 'expired'].includes(status)) {
        this.pending.delete(message.commandId)
      }
    }
  }

  sendCommand(action: CommandEnvelope['action'], payload: Record<string, unknown> = {}) {
    const now = Date.now()
    const envelope: CommandEnvelope = {
      type: 'command',
      commandId: crypto.randomUUID(),
      createdAt: now,
      expiresAt: now + 6_000,
      action,
      payload,
    }
    const pending: Pending = { envelope, acknowledged: false, timers: [], attempt: 0 }
    this.pending.set(envelope.commandId, pending)
    this.emit({
      type: 'command',
      update: { commandId: envelope.commandId, action, status: 'sending', attempt: 1 },
    })
    this.transmit(pending)
    for (const delay of retryDelays) {
      pending.timers.push(
        window.setTimeout(() => {
          if (!pending.acknowledged && Date.now() < envelope.expiresAt) this.transmit(pending)
        }, delay),
      )
    }
    pending.timers.push(
      window.setTimeout(() => {
        if (pending.acknowledged) return
        this.pending.delete(envelope.commandId)
        this.emit({
          type: 'command',
          update: { commandId: envelope.commandId, action, status: 'expired', attempt: pending.attempt },
        })
      }, envelope.expiresAt - now),
    )
    return envelope.commandId
  }

  private transmit(pending: Pending) {
    pending.attempt += 1
    if (this.control?.readyState === WebSocket.OPEN) {
      this.control.send(JSON.stringify(pending.envelope))
    }
    this.emit({
      type: 'command',
      update: {
        commandId: pending.envelope.commandId,
        action: pending.envelope.action,
        status: 'sending',
        attempt: pending.attempt,
      },
    })
  }
}

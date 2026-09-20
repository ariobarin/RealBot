export interface Telemetry {
  version: 1
  type: 'telemetry'
  at: number
  mapId: string | null
  ready: boolean
  pose: { x: number; y: number; z: number; heading: number } | null
  slam: {
    fresh: boolean
    poseFresh: boolean
    localized: boolean | null
    degraded: boolean | null
    stalled: boolean | null
    vo_lost: boolean | null
    relocalized: boolean | null
  }
  navigation: { fresh: boolean; state: string; reason: string; waypointIndex: number | null }
}

const object = (v: unknown): v is Record<string, unknown> =>
  v !== null && typeof v === 'object' && !Array.isArray(v)
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)

export function parseTelemetry(bytes: Uint8Array): Telemetry | undefined {
  if (bytes.length > 16_000) return
  try {
    const v: unknown = JSON.parse(new TextDecoder().decode(bytes))
    if (
      !object(v) ||
      v.version !== 1 ||
      v.type !== 'telemetry' ||
      !finite(v.at) ||
      typeof v.ready !== 'boolean'
    )
      return
    if (v.mapId !== null && typeof v.mapId !== 'string') return
    if (
      v.pose !== null &&
      (!object(v.pose) ||
        !['x', 'y', 'z', 'heading'].every((k) => finite((v.pose as Record<string, unknown>)[k])))
    )
      return
    if (!object(v.slam) || typeof v.slam.fresh !== 'boolean' || typeof v.slam.poseFresh !== 'boolean') return
    for (const key of ['localized', 'degraded', 'stalled', 'vo_lost', 'relocalized']) {
      if (v.slam[key] !== null && typeof v.slam[key] !== 'boolean') return
    }
    if (
      !object(v.navigation) ||
      typeof v.navigation.fresh !== 'boolean' ||
      typeof v.navigation.state !== 'string' ||
      typeof v.navigation.reason !== 'string'
    )
      return
    if (v.navigation.waypointIndex !== null && !Number.isInteger(v.navigation.waypointIndex)) return
    return v as unknown as Telemetry
  } catch {
    return
  }
}

export interface ViewerSession {
  url: string
  token: string
  robotIdentity: string
  cameraTrack?: 'cam-wrist' | 'cam-setup'
}

export function parseViewerSession(v: unknown): ViewerSession {
  if (
    !object(v) ||
    typeof v.url !== 'string' ||
    typeof v.token !== 'string' ||
    !v.token ||
    typeof v.robotIdentity !== 'string' ||
    !v.robotIdentity.trim()
  ) {
    throw new Error('Session response must contain a URL, viewer token, and robot identity.')
  }
  const url = new URL(v.url)
  if (
    url.protocol !== 'wss:' &&
    !(url.protocol === 'ws:' && ['localhost', '127.0.0.1'].includes(url.hostname))
  ) {
    throw new Error('LiveKit requires a secure wss:// URL (ws:// is allowed for localhost).')
  }
  return { url: v.url, token: v.token, robotIdentity: v.robotIdentity }
}

export type MapStatus = 'ready' | 'scanning' | 'draft'

export interface PointCloudSummary {
  /** Interleaved asset: count * vec3 float32 positions, then count * RGB uint8 colours. */
  data: string
  meta: string
}

export interface MapSummary {
  id: string
  name: string
  thumb: string
  grid: string
  areaM2: number
  rooms: number
  scannedAt: string
  status: MapStatus
  cloud?: PointCloudSummary
}

export async function fetchManifest(signal?: AbortSignal): Promise<MapSummary[]> {
  const res = await fetch('/maps/_index.json', { signal })
  if (!res.ok) throw new Error(`manifest: HTTP ${res.status}`)
  return (await res.json()) as MapSummary[]
}

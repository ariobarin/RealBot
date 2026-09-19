export type MapStatus = 'ready' | 'scanning' | 'draft'

export interface MapSummary {
  id: string
  name: string
  thumb: string
  grid: string
  areaM2: number
  rooms: number
  scannedAt: string
  status: MapStatus
}

export async function fetchManifest(signal?: AbortSignal): Promise<MapSummary[]> {
  const res = await fetch('/maps/_index.json', { signal })
  if (!res.ok) throw new Error(`manifest: HTTP ${res.status}`)
  return (await res.json()) as MapSummary[]
}

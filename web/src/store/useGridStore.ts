import { create } from 'zustand'
import { gridFromJson, type GridMap, type GridMapJson } from '../lib/grid'
import { fetchManifest, type MapSummary } from '../lib/manifest'

type Status = 'idle' | 'loading' | 'ready' | 'error'

interface GridState {
  status: Status
  id?: string
  summary?: MapSummary
  grid?: GridMap
  error?: string
  load: (id: string) => Promise<void>
}

export const useGridStore = create<GridState>((set, get) => ({
  status: 'idle',
  load: async (id) => {
    if (get().id === id && (get().status === 'loading' || get().status === 'ready')) return
    set({ status: 'loading', id, grid: undefined, summary: undefined, error: undefined })
    try {
      const maps = await fetchManifest()
      const summary = maps.find((m) => m.id === id)
      if (!summary) throw new Error(`No space with id "${id}"`)
      const res = await fetch(summary.grid)
      if (!res.ok) throw new Error(`grid: HTTP ${res.status}`)
      const grid = gridFromJson((await res.json()) as GridMapJson)
      if (get().id !== id) return
      set({ status: 'ready', summary, grid })
    } catch (e) {
      if (get().id !== id) return
      set({ status: 'error', error: e instanceof Error ? e.message : String(e) })
    }
  },
}))

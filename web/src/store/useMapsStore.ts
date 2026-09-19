import { create } from 'zustand'
import { fetchManifest, type MapSummary } from '../lib/manifest'

type Status = 'idle' | 'loading' | 'ready' | 'error'

interface MapsState {
  status: Status
  maps: MapSummary[]
  error?: string
  load: () => Promise<void>
}

export const useMapsStore = create<MapsState>((set, get) => ({
  status: 'idle',
  maps: [],
  load: async () => {
    if (get().status === 'loading' || get().status === 'ready') return
    set({ status: 'loading', error: undefined })
    try {
      const maps = await fetchManifest()
      set({ status: 'ready', maps })
    } catch (e) {
      set({ status: 'error', error: e instanceof Error ? e.message : String(e) })
    }
  },
}))

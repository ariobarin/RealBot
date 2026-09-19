import { create } from 'zustand'

export type ViewMode = '2d' | '3d'

export interface Waypoint {
  id: number
  x: number
  y: number
}

interface ViewState {
  mode: ViewMode
  /** Bumped to ask the camera to fly back to the default framing. */
  resetSeq: number
  waypoints: Waypoint[]
  setMode: (m: ViewMode) => void
  toggleMode: () => void
  resetView: () => void
  addWaypoint: (x: number, y: number) => void
  clearWaypoints: () => void
}

export const useViewStore = create<ViewState>((set) => ({
  mode: '3d',
  resetSeq: 0,
  waypoints: [],
  setMode: (mode) => set({ mode }),
  toggleMode: () => set((s) => ({ mode: s.mode === '3d' ? '2d' : '3d' })),
  resetView: () => set((s) => ({ resetSeq: s.resetSeq + 1 })),
  addWaypoint: (x, y) => set((s) => ({ waypoints: [...s.waypoints, { id: Date.now(), x, y }] })),
  clearWaypoints: () => set({ waypoints: [] }),
}))

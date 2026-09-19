import { create } from 'zustand'

/** Simulated pairing delay until the real bracketbot handshake lands. */
export const PAIRING_MS = 2000
/** Simulated scan length until the edge reports real progress. */
export const SCAN_MS = 9000
/** What the prototype "finds" on the network. */
export const DEMO_BOT_NAME = 'bracketbot-7F2A'

export type OnboardingPhase = 'idle' | 'pairing' | 'paired' | 'scanning'

interface OnboardingState {
  phase: OnboardingPhase
  beginPairing: () => void
  markPaired: () => void
  start: () => void
  reset: () => void
}

export const useOnboardingStore = create<OnboardingState>((set) => ({
  phase: 'idle',
  beginPairing: () => set({ phase: 'pairing' }),
  markPaired: () => set((s) => (s.phase === 'pairing' ? { phase: 'paired' } : s)),
  start: () => set((s) => (s.phase === 'paired' ? { phase: 'scanning' } : s)),
  reset: () => set({ phase: 'idle' }),
}))

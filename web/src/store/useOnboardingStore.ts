import { create } from 'zustand'

/** Simulated pairing delay until the real bracketbot handshake lands. */
export const PAIRING_MS = 2000

export type OnboardingPhase = 'idle' | 'pairing' | 'paired' | 'starting'

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
  start: () => set((s) => (s.phase === 'paired' ? { phase: 'starting' } : s)),
  reset: () => set({ phase: 'idle' }),
}))

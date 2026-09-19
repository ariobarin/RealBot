import { create } from 'zustand'
import type { OpenTour } from '../lib/openTours'

export interface Booking {
  id: string
  tourId: string
  roomId: string
  name: string
  neighbourhood: string
  plan: OpenTour['plan']
  /** ISO local datetime of the slot. */
  at: string
  guestName: string
  email: string
  createdAt: string
}

const KEY = 'realbot-bookings'

function read(): Booking[] {
  try {
    const raw = localStorage.getItem(KEY)
    return raw ? (JSON.parse(raw) as Booking[]) : []
  } catch {
    return []
  }
}

interface BookingsState {
  bookings: Booking[]
  add: (b: Omit<Booking, 'id' | 'createdAt'>) => Booking
  cancel: (id: string) => void
}

/** Prototype bookings: kept on this device. The relay will own these later. */
export const useBookingsStore = create<BookingsState>((set, get) => ({
  bookings: read(),
  add(b) {
    const booking: Booking = {
      ...b,
      id: Math.random().toString(36).slice(2, 10),
      createdAt: new Date().toISOString(),
    }
    const bookings = [...get().bookings, booking].sort((a, z) => a.at.localeCompare(z.at))
    localStorage.setItem(KEY, JSON.stringify(bookings))
    set({ bookings })
    return booking
  },
  cancel(id) {
    const bookings = get().bookings.filter((b) => b.id !== id)
    localStorage.setItem(KEY, JSON.stringify(bookings))
    set({ bookings })
  },
}))

import { createContext } from 'react'

export type AuthSession = { role: 'visitor'; roomId: string } | { role: 'realtor'; email: string }

export interface AuthContextValue {
  session: AuthSession | null
  loginVisitor: (roomId: string) => void
  loginRealtor: (email: string, password: string) => boolean
  logout: () => void
}

export const AuthContext = createContext<AuthContextValue | null>(null)
export const demoRealtorEmail = import.meta.env.VITE_REALTOR_EMAIL || 'realtor@realbot.demo'
export const demoRealtorPassword = import.meta.env.VITE_REALTOR_PASSWORD || 'demo'

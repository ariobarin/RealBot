import { useMemo, useState, type ReactNode } from 'react'
import {
  AuthContext,
  demoRealtorEmail,
  demoRealtorPassword,
  type AuthContextValue,
  type AuthSession,
} from './authContext'

const SESSION_KEY = 'realbot-session'

function readSession(): AuthSession | null {
  try {
    const stored = sessionStorage.getItem(SESSION_KEY)
    if (!stored) return null
    const session = JSON.parse(stored) as AuthSession
    if (session.role === 'visitor' && session.roomId) return session
    if (session.role === 'realtor' && session.email) return session
  } catch {
    sessionStorage.removeItem(SESSION_KEY)
  }
  return null
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(readSession)

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      loginVisitor(roomId) {
        const next: AuthSession = { role: 'visitor', roomId }
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(next))
        localStorage.setItem('realbot-room', roomId)
        setSession(next)
      },
      loginRealtor(email, password) {
        if (
          email.trim().toLowerCase() !== demoRealtorEmail.toLowerCase() ||
          password !== demoRealtorPassword
        ) {
          return false
        }
        const next: AuthSession = { role: 'realtor', email: email.trim() }
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(next))
        setSession(next)
        return true
      },
      logout() {
        sessionStorage.removeItem(SESSION_KEY)
        setSession(null)
      },
    }),
    [session],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

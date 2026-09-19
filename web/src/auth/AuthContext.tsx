import type { Session as SupabaseSession, User } from '@supabase/supabase-js'
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { isAuthTestMode, supabase, supabaseConfigurationError } from '../lib/supabase'
import { AuthContext, type AuthContextValue, type AuthSession, type RealtorRole } from './authContext'

const SESSION_KEY = 'realbot-session'
const TEST_REALTOR_EMAIL = 'realtor@realbot.demo'
const TEST_REALTOR_PASSWORD = 'demo'

function readStoredSession(): AuthSession | null {
  try {
    const stored = sessionStorage.getItem(SESSION_KEY)
    if (!stored) return null
    const session = JSON.parse(stored) as AuthSession
    if (session.role === 'visitor' && session.roomId) return session
    if (isAuthTestMode && session.role === 'realtor' && session.email) {
      return {
        role: 'realtor',
        userId: session.userId || 'test-realtor',
        email: session.email,
        organizationId: session.organizationId || 'test-organization',
        membershipRole: session.membershipRole || 'realtor',
      }
    }
  } catch {
    sessionStorage.removeItem(SESSION_KEY)
  }
  return null
}

function isRealtorRole(value: unknown): value is RealtorRole {
  return value === 'owner' || value === 'realtor' || value === 'viewer'
}

async function loadRealtorSession(user: User): Promise<AuthSession | null> {
  if (!supabase) return null

  const { data, error } = await supabase
    .from('organization_members')
    .select('organization_id, role')
    .eq('user_id', user.id)
    .order('created_at', { ascending: true })
    .limit(1)
    .maybeSingle()

  if (error) throw error
  if (!data || !isRealtorRole(data.role)) return null

  return {
    role: 'realtor',
    userId: user.id,
    email: user.email || '',
    organizationId: data.organization_id,
    membershipRole: data.role,
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AuthSession | null>(readStoredSession)
  const [isLoading, setIsLoading] = useState(() => !isAuthTestMode && Boolean(supabase))
  const authEventGeneration = useRef(0)

  useEffect(() => {
    if (isAuthTestMode || !supabase) return

    let active = true
    const applySupabaseSession = async (nextSession: SupabaseSession | null) => {
      const generation = ++authEventGeneration.current
      try {
        const next = nextSession?.user ? await loadRealtorSession(nextSession.user) : readStoredSession()
        if (active && generation === authEventGeneration.current) setSession(next)
      } catch {
        if (active && generation === authEventGeneration.current) setSession(null)
      } finally {
        if (active && generation === authEventGeneration.current) setIsLoading(false)
      }
    }

    void supabase.auth
      .getSession()
      .then(({ data }) => applySupabaseSession(data.session))
      .catch(() => applySupabaseSession(null))
    const { data } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      window.setTimeout(() => void applySupabaseSession(nextSession), 0)
    })

    return () => {
      active = false
      data.subscription.unsubscribe()
    }
  }, [])

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      isLoading,
      loginVisitor(roomId) {
        const next: AuthSession = { role: 'visitor', roomId }
        sessionStorage.setItem(SESSION_KEY, JSON.stringify(next))
        localStorage.setItem('realbot-room', roomId)
        setSession(next)
      },
      async loginRealtor(email, password) {
        const normalizedEmail = email.trim().toLowerCase()

        if (isAuthTestMode) {
          if (normalizedEmail !== TEST_REALTOR_EMAIL || password !== TEST_REALTOR_PASSWORD) {
            return { ok: false, error: 'That email or password does not match.' }
          }
          const next: AuthSession = {
            role: 'realtor',
            userId: 'test-realtor',
            email: normalizedEmail,
            organizationId: 'test-organization',
            membershipRole: 'realtor',
          }
          sessionStorage.setItem(SESSION_KEY, JSON.stringify(next))
          setSession(next)
          return { ok: true }
        }

        if (!supabase) {
          return { ok: false, error: supabaseConfigurationError || 'Supabase is unavailable.' }
        }

        const { data, error } = await supabase.auth.signInWithPassword({
          email: normalizedEmail,
          password,
        })
        if (error || !data.user) {
          return { ok: false, error: 'That email or password does not match.' }
        }

        try {
          const next = await loadRealtorSession(data.user)
          if (!next) {
            await supabase.auth.signOut()
            return { ok: false, error: 'This account has not been assigned to a RealBot organization.' }
          }
          sessionStorage.removeItem(SESSION_KEY)
          setSession(next)
          return { ok: true }
        } catch {
          await supabase.auth.signOut()
          return { ok: false, error: 'Unable to verify account access. Please try again.' }
        }
      },
      async signupRealtor(displayName, organizationName, email, password) {
        if (isAuthTestMode) {
          return { ok: false, error: 'Account creation is unavailable in the browser test environment.' }
        }
        if (!supabase) {
          return { ok: false, error: supabaseConfigurationError || 'Supabase is unavailable.' }
        }

        const { data, error } = await supabase.auth.signUp({
          email: email.trim().toLowerCase(),
          password,
          options: {
            emailRedirectTo: window.location.origin,
            data: {
              account_type: 'realtor_owner',
              display_name: displayName.trim(),
              organization_name: organizationName.trim(),
            },
          },
        })

        if (error) return { ok: false, error: error.message }

        if (data.session && data.user) {
          try {
            const next = await loadRealtorSession(data.user)
            if (!next) {
              await supabase.auth.signOut()
              return { ok: false, error: 'Unable to create the realtor organization.' }
            }
            setSession(next)
          } catch {
            await supabase.auth.signOut()
            return { ok: false, error: 'Unable to verify the new account. Please try again.' }
          }
        }

        return { ok: true, requiresEmailConfirmation: !data.session }
      },
      async logout() {
        ++authEventGeneration.current
        sessionStorage.removeItem(SESSION_KEY)
        setSession(null)
        if (!isAuthTestMode && supabase) await supabase.auth.signOut()
      },
    }),
    [isLoading, session],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

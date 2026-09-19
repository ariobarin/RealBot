import { createContext } from 'react'

export type RealtorRole = 'owner' | 'realtor' | 'viewer'

export type AuthSession =
  | { role: 'visitor'; roomId: string }
  | {
      role: 'realtor'
      userId: string
      email: string
      organizationId: string
      membershipRole: RealtorRole
    }

export type LoginResult = { ok: true } | { ok: false; error: string }
export type SignupResult = { ok: true; requiresEmailConfirmation: boolean } | { ok: false; error: string }

export interface AuthContextValue {
  session: AuthSession | null
  isLoading: boolean
  loginVisitor: (roomId: string) => void
  loginRealtor: (email: string, password: string) => Promise<LoginResult>
  signupRealtor: (
    displayName: string,
    organizationName: string,
    email: string,
    password: string,
  ) => Promise<SignupResult>
  logout: () => Promise<void>
}

export const AuthContext = createContext<AuthContextValue | null>(null)

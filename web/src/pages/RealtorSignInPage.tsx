import { motion } from 'framer-motion'
import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { useAuth } from '../auth/useAuth'
import { KeyRingIcon } from '../components/icons/KeyRingIcon'
import { Button } from '../components/ui/Button'
import { TopNav } from '../components/ui/TopNav'
import { isAuthTestMode, supabaseConfigurationError } from '../lib/supabase'
import { pageVariants } from '../lib/motion'

const inputClass =
  'mt-1.5 h-[52px] w-full rounded-xl border border-[#dddddd] bg-white px-4 text-base text-ink outline-none transition focus:border-ink focus:ring-4 focus:ring-ink/5'

const strongPassword = (password: string) =>
  password.length >= 12 &&
  /[a-z]/.test(password) &&
  /[A-Z]/.test(password) &&
  /[0-9]/.test(password) &&
  /[^A-Za-z0-9]/.test(password)

/** `/signin` — the gate in front of Manage spaces: sign in, or create a realtor account. */
export function RealtorSignInPage() {
  const { session, isLoading, loginRealtor, signupRealtor } = useAuth()
  const navigate = useNavigate()
  const [creating, setCreating] = useState(false)
  const [displayName, setDisplayName] = useState('')
  const [organizationName, setOrganizationName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [submitting, setSubmitting] = useState(false)

  if (session?.role === 'realtor') return <Navigate to="/realtor" replace />

  const canSubmit =
    !submitting &&
    email.trim().length > 0 &&
    password.length > 0 &&
    (!creating || (displayName.trim().length > 0 && organizationName.trim().length > 0))

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    setNotice('')
    setSubmitting(true)
    try {
      if (creating) {
        if (!strongPassword(password)) {
          setError('Use at least 12 characters with uppercase, lowercase, a number, and a symbol.')
          return
        }
        const result = await signupRealtor(displayName, organizationName, email, password)
        if (!result.ok) {
          setError(result.error)
          return
        }
        if (result.requiresEmailConfirmation) {
          setPassword('')
          setNotice('Check your email to confirm the account, then come back here to sign in.')
          return
        }
        void navigate('/realtor')
        return
      }
      const result = await loginRealtor(email, password)
      if (!result.ok) {
        setError(result.error)
        return
      }
      void navigate('/realtor')
    } finally {
      setSubmitting(false)
    }
  }

  const toggleMode = () => {
    setCreating((c) => !c)
    setError('')
    setNotice('')
  }

  return (
    <motion.main variants={pageVariants} initial="initial" animate="enter" exit="exit" className="min-h-dvh">
      <TopNav />
      <section className="mx-auto max-w-[1280px] px-6 py-16 sm:px-10">
        <form
          onSubmit={(event) => void submit(event)}
          aria-busy={submitting}
          className="icon-hover mx-auto flex w-full max-w-[420px] flex-col gap-4 rounded-3xl bg-white p-7 shadow-lg"
        >
          <KeyRingIcon size={72} />
          <h1 className="text-[24px] font-bold tracking-tight">
            {creating ? 'Create a realtor account' : 'Realtor sign in'}
          </h1>
          <p className="-mt-2 text-sm leading-snug text-ink-2">
            {creating
              ? 'This creates your account and an organization you own. Visitors never need one.'
              : 'Manage your spaces, robots and tour codes.'}
          </p>

          {isLoading && <p className="text-sm text-ink-2">Checking your session…</p>}

          {creating && (
            <>
              <label htmlFor="display-name" className="text-[13px] font-semibold">
                Your name
                <input
                  id="display-name"
                  value={displayName}
                  onChange={(event) => setDisplayName(event.target.value)}
                  autoComplete="name"
                  maxLength={120}
                  className={inputClass}
                />
              </label>
              <label htmlFor="organization-name" className="text-[13px] font-semibold">
                Company or organization
                <input
                  id="organization-name"
                  value={organizationName}
                  onChange={(event) => setOrganizationName(event.target.value)}
                  autoComplete="organization"
                  maxLength={160}
                  className={inputClass}
                />
              </label>
            </>
          )}
          <label htmlFor="email" className="text-[13px] font-semibold">
            Email
            <input
              id="email"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              autoComplete="username"
              className={inputClass}
            />
          </label>
          <label htmlFor="password" className="text-[13px] font-semibold">
            Password
            <input
              id="password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete={creating ? 'new-password' : 'current-password'}
              placeholder={creating ? 'At least 12 characters' : undefined}
              className={inputClass}
            />
          </label>

          {isAuthTestMode && !creating && (
            <p className="-mt-1 text-xs text-ink-3">Test login: realtor@realbot.demo / demo</p>
          )}
          {!isAuthTestMode && supabaseConfigurationError && (
            <p className="-mt-1 text-xs text-ink-3">{supabaseConfigurationError}</p>
          )}
          {error && (
            <p role="alert" className="rounded-xl bg-brand-soft px-4 py-3 text-sm text-brand-2">
              {error}
            </p>
          )}
          {notice && (
            <p role="status" className="rounded-xl bg-[#e6f4e6] px-4 py-3 text-sm text-ok">
              {notice}
            </p>
          )}

          <Button type="submit" className="h-[52px] w-full justify-center text-base" disabled={!canSubmit}>
            {submitting
              ? creating
                ? 'Creating account…'
                : 'Signing in…'
              : creating
                ? 'Create realtor account'
                : 'Open Manage spaces'}
          </Button>
          <button
            type="button"
            onClick={toggleMode}
            className="text-sm font-semibold text-ink underline underline-offset-4"
          >
            {creating ? 'Already have an account? Sign in' : 'Create a realtor account'}
          </button>
        </form>
      </section>
    </motion.main>
  )
}

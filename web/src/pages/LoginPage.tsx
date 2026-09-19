import { Building2, Eye, LockKeyhole, Radio, UserRound } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { Button } from '../components/ui/Button'
import { Wordmark } from '../components/ui/PageShell'
import { useAuth } from '../auth/useAuth'

type LoginMode = 'visitor' | 'realtor'

export function LoginPage() {
  const { session, isLoading, loginVisitor, loginRealtor, signupRealtor } = useAuth()
  const navigate = useNavigate()
  const [mode, setMode] = useState<LoginMode>('visitor')
  const [roomId, setRoomId] = useState(() => localStorage.getItem('realbot-room') || 'demo-bot')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [organizationName, setOrganizationName] = useState('')
  const [isCreatingAccount, setIsCreatingAccount] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [isSubmitting, setIsSubmitting] = useState(false)

  if (isLoading) {
    return (
      <main className="grid min-h-dvh place-items-center text-sm text-ink-2">Checking your session…</main>
    )
  }

  if (session?.role === 'visitor')
    return <Navigate to={`/user/${encodeURIComponent(session.roomId)}`} replace />
  if (session?.role === 'realtor') return <Navigate to="/realtor" replace />

  const chooseMode = (next: LoginMode) => {
    setMode(next)
    setError('')
    setNotice('')
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError('')
    setNotice('')
    if (mode === 'visitor') {
      const room = roomId.trim()
      if (!room) return
      loginVisitor(room)
      void navigate(`/user/${encodeURIComponent(room)}`)
      return
    }
    setIsSubmitting(true)
    try {
      if (isCreatingAccount) {
        if (!displayName.trim() || !organizationName.trim()) return
        if (
          password.length < 12 ||
          !/[a-z]/.test(password) ||
          !/[A-Z]/.test(password) ||
          !/[0-9]/.test(password) ||
          !/[^A-Za-z0-9]/.test(password)
        ) {
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
          setNotice('Check your email to confirm the account, then return here to sign in.')
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
      setIsSubmitting(false)
    }
  }

  return (
    <main className="min-h-dvh bg-[radial-gradient(circle_at_top_left,_var(--brand-soft),_transparent_38%),linear-gradient(180deg,#fff_0%,#f7f7f7_100%)] px-6 py-6 sm:px-10">
      <div className="mx-auto max-w-6xl">
        <header className="flex items-center justify-between">
          <Wordmark />
          <span className="hidden text-sm text-ink-2 sm:block">Remote property tours, made simple</span>
        </header>

        <div className="grid items-center gap-12 py-12 lg:grid-cols-[minmax(0,1fr)_minmax(420px,0.78fr)] lg:py-20">
          <section>
            <div className="inline-flex items-center gap-2 rounded-full border border-brand/15 bg-white/80 px-3 py-1.5 text-xs font-semibold text-brand shadow-sm">
              <Radio size={14} /> Live robot access
            </div>
            <h1 className="mt-6 max-w-2xl text-4xl font-bold leading-[1.08] tracking-tight sm:text-6xl">
              Step inside a property from anywhere.
            </h1>
            <p className="mt-5 max-w-xl text-lg leading-8 text-ink-2">
              Join a guided remote tour as a visitor, or manage spaces and robots from the realtor dashboard.
            </p>
            <div className="mt-8 hidden grid-cols-2 gap-4 sm:grid lg:max-w-xl">
              <div className="rounded-2xl border border-line bg-white/70 p-4">
                <Eye className="text-brand" size={20} />
                <p className="mt-3 font-semibold">See and move</p>
                <p className="mt-1 text-sm text-ink-2">Navigate directly through the live camera.</p>
              </div>
              <div className="rounded-2xl border border-line bg-white/70 p-4">
                <LockKeyhole className="text-brand" size={20} />
                <p className="mt-3 font-semibold">Role-aware access</p>
                <p className="mt-1 text-sm text-ink-2">Visitors only see the tour experience.</p>
              </div>
            </div>
          </section>

          <section className="rounded-[2rem] border border-line bg-white p-6 shadow-lg sm:p-8">
            <p className="text-xs font-semibold uppercase tracking-widest text-brand">Welcome to RealBot</p>
            <h2 className="mt-2 text-2xl font-bold tracking-tight">Choose how you’re joining</h2>

            <div
              className="mt-6 grid grid-cols-2 gap-2 rounded-2xl bg-bg-soft p-1.5"
              role="tablist"
              aria-label="Login type"
            >
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'visitor'}
                onClick={() => chooseMode('visitor')}
                className={`flex items-center justify-center gap-2 rounded-xl px-3 py-3 text-sm font-semibold transition ${mode === 'visitor' ? 'bg-white text-ink shadow-sm' : 'text-ink-2 hover:text-ink'}`}
              >
                <UserRound size={17} /> Visitor
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === 'realtor'}
                onClick={() => chooseMode('realtor')}
                className={`flex items-center justify-center gap-2 rounded-xl px-3 py-3 text-sm font-semibold transition ${mode === 'realtor' ? 'bg-white text-ink shadow-sm' : 'text-ink-2 hover:text-ink'}`}
              >
                <Building2 size={17} /> Realtor
              </button>
            </div>

            <form className="mt-6" onSubmit={(event) => void submit(event)} aria-busy={isSubmitting}>
              {mode === 'visitor' ? (
                <>
                  <label htmlFor="room-id" className="text-sm font-semibold">
                    Tour access code
                  </label>
                  <input
                    id="room-id"
                    value={roomId}
                    onChange={(event) => setRoomId(event.target.value)}
                    placeholder="Enter the code from your realtor"
                    autoComplete="off"
                    className="mt-2 w-full rounded-2xl border border-line bg-bg-soft px-4 py-3.5 text-ink outline-none focus:border-brand focus:ring-2 focus:ring-brand/10"
                  />
                  <p className="mt-2 text-xs text-ink-3">
                    Open the link from your realtor—no account or signup required. For the prototype, try
                    “demo-bot”.
                  </p>
                </>
              ) : (
                <div className="space-y-4">
                  {isCreatingAccount && (
                    <>
                      <div>
                        <label htmlFor="display-name" className="text-sm font-semibold">
                          Your name
                        </label>
                        <input
                          id="display-name"
                          value={displayName}
                          onChange={(event) => setDisplayName(event.target.value)}
                          autoComplete="name"
                          maxLength={120}
                          className="mt-2 w-full rounded-2xl border border-line bg-bg-soft px-4 py-3.5 text-ink outline-none focus:border-brand focus:ring-2 focus:ring-brand/10"
                        />
                      </div>
                      <div>
                        <label htmlFor="organization-name" className="text-sm font-semibold">
                          Company or organization
                        </label>
                        <input
                          id="organization-name"
                          value={organizationName}
                          onChange={(event) => setOrganizationName(event.target.value)}
                          autoComplete="organization"
                          maxLength={160}
                          className="mt-2 w-full rounded-2xl border border-line bg-bg-soft px-4 py-3.5 text-ink outline-none focus:border-brand focus:ring-2 focus:ring-brand/10"
                        />
                      </div>
                    </>
                  )}
                  <div>
                    <label htmlFor="email" className="text-sm font-semibold">
                      Email
                    </label>
                    <input
                      id="email"
                      type="email"
                      value={email}
                      onChange={(event) => setEmail(event.target.value)}
                      autoComplete="username"
                      className="mt-2 w-full rounded-2xl border border-line bg-bg-soft px-4 py-3.5 text-ink outline-none focus:border-brand focus:ring-2 focus:ring-brand/10"
                    />
                  </div>
                  <div>
                    <label htmlFor="password" className="text-sm font-semibold">
                      Password
                    </label>
                    <input
                      id="password"
                      type="password"
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      autoComplete={isCreatingAccount ? 'new-password' : 'current-password'}
                      placeholder={isCreatingAccount ? 'Create a strong password' : 'Enter your password'}
                      className="mt-2 w-full rounded-2xl border border-line bg-bg-soft px-4 py-3.5 text-ink outline-none focus:border-brand focus:ring-2 focus:ring-brand/10"
                    />
                  </div>
                  <p className="text-xs text-ink-3">
                    {isCreatingAccount
                      ? 'This creates a landlord/realtor account and an organization you own.'
                      : 'Sign in with your landlord or realtor account.'}
                  </p>
                  <button
                    type="button"
                    className="text-sm font-semibold text-brand hover:text-brand-2"
                    onClick={() => {
                      setIsCreatingAccount((current) => !current)
                      setError('')
                      setNotice('')
                    }}
                  >
                    {isCreatingAccount ? 'Already have an account? Sign in' : 'Create a realtor account'}
                  </button>
                </div>
              )}

              {error && (
                <p role="alert" className="mt-4 rounded-xl bg-red-50 px-4 py-3 text-sm text-red-700">
                  {error}
                </p>
              )}
              {notice && (
                <p role="status" className="mt-4 rounded-xl bg-green-50 px-4 py-3 text-sm text-green-800">
                  {notice}
                </p>
              )}
              <Button
                type="submit"
                className="mt-6 w-full justify-center"
                disabled={
                  isSubmitting ||
                  (mode === 'visitor'
                    ? !roomId.trim()
                    : !email.trim() ||
                      !password ||
                      (isCreatingAccount && (!displayName.trim() || !organizationName.trim())))
                }
              >
                {mode === 'visitor' ? (
                  <>
                    <Eye size={17} /> Join tour
                  </>
                ) : (
                  <>
                    <Building2 size={17} />{' '}
                    {isSubmitting
                      ? isCreatingAccount
                        ? 'Creating account…'
                        : 'Signing in…'
                      : isCreatingAccount
                        ? 'Create realtor account'
                        : 'Open realtor portal'}
                  </>
                )}
              </Button>
            </form>
          </section>
        </div>
      </div>
    </main>
  )
}

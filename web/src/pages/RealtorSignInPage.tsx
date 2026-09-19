import { useState, type FormEvent } from 'react'
import { Navigate, useNavigate } from 'react-router-dom'
import { motion } from 'framer-motion'
import { demoRealtorEmail } from '../auth/authContext'
import { useAuth } from '../auth/useAuth'
import { KeyRingIcon } from '../components/icons/KeyRingIcon'
import { Button } from '../components/ui/Button'
import { TopNav } from '../components/ui/TopNav'
import { pageVariants } from '../lib/motion'

const inputClass =
  'mt-1.5 h-[52px] w-full rounded-xl border border-[#dddddd] bg-white px-4 text-base text-ink outline-none transition focus:border-ink focus:ring-4 focus:ring-ink/5'

/** `/signin` — the gate in front of Manage spaces. */
export function RealtorSignInPage() {
  const { session, loginRealtor } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState(demoRealtorEmail)
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')

  if (session?.role === 'realtor') return <Navigate to="/realtor" replace />

  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!loginRealtor(email, password)) {
      setError('That email or password does not match the demo realtor account.')
      return
    }
    void navigate('/realtor')
  }

  return (
    <motion.main variants={pageVariants} initial="initial" animate="enter" exit="exit" className="min-h-dvh">
      <TopNav />
      <section className="mx-auto max-w-[1280px] px-6 py-16 sm:px-10">
        <form
          onSubmit={submit}
          className="icon-hover mx-auto flex w-full max-w-[420px] flex-col gap-4 rounded-3xl bg-white p-7 shadow-lg"
        >
          <KeyRingIcon size={72} />
          <h1 className="text-[24px] font-bold tracking-tight">Realtor sign in</h1>
          <p className="-mt-2 text-sm leading-snug text-ink-2">Manage your spaces, robots and tour codes.</p>
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
              autoComplete="current-password"
              className={inputClass}
            />
          </label>
          <p className="-mt-1 text-xs text-ink-3">Demo login: {demoRealtorEmail} / demo</p>
          {error && (
            <p role="alert" className="rounded-xl bg-brand-soft px-4 py-3 text-sm text-brand-2">
              {error}
            </p>
          )}
          <Button
            type="submit"
            className="h-[52px] w-full justify-center text-base"
            disabled={!email.trim() || !password}
          >
            Open Manage spaces
          </Button>
        </form>
      </section>
    </motion.main>
  )
}

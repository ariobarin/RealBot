import { motion } from 'framer-motion'
import { ArrowLeft } from 'lucide-react'
import { useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { PairingOrb } from '../components/onboarding/PairingOrb'
import { ArtExplore, ArtPair, ArtPlace, ArtPowerOn } from '../components/onboarding/StepArt'
import { StepCard } from '../components/onboarding/StepCard'
import { PageShell, Wordmark } from '../components/ui/PageShell'
import { Pill } from '../components/ui/Pill'
import { staggerList } from '../lib/motion'
import { PAIRING_MS, useOnboardingStore } from '../store/useOnboardingStore'

/** Until the Add flow creates real spaces, Start always opens the preset scan. */
const PRESET_MAP_ID = 'small-house'
const START_DELAY_MS = 600

const STEPS = [
  {
    title: 'Power on your bracketbot',
    body: 'Hold the top button until the ring light breathes red. It takes about ten seconds to boot.',
    art: <ArtPowerOn />,
  },
  {
    title: 'Place it at the entrance',
    body: 'Set the bot just inside the front door, facing in. This becomes the home position it returns to.',
    art: <ArtPlace />,
  },
  {
    title: 'Pair from this device',
    body: 'Stay on this page — we find the bot over the local network and pair automatically.',
    art: <ArtPair />,
  },
  {
    title: 'Let it explore',
    body: 'Press Start and the bot drives itself room to room, building the 3D SLAM map as it goes.',
    art: <ArtExplore />,
  },
]

export function OnboardingPage() {
  const navigate = useNavigate()
  const { phase, beginPairing, markPaired, start, reset } = useOnboardingStore()

  useEffect(() => {
    beginPairing()
    const t = window.setTimeout(markPaired, PAIRING_MS)
    return () => {
      window.clearTimeout(t)
      reset()
    }
  }, [beginPairing, markPaired, reset])

  useEffect(() => {
    if (phase !== 'starting') return
    const t = window.setTimeout(() => navigate(`/map/${PRESET_MAP_ID}`), START_DELAY_MS)
    return () => window.clearTimeout(t)
  }, [phase, navigate])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') navigate('/')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate])

  const activeStep = phase === 'paired' || phase === 'starting' ? 3 : 2

  return (
    <PageShell>
      <header className="flex items-center justify-between">
        <Wordmark />
        <Link
          to="/"
          className="inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium text-ink-2 no-underline transition-colors hover:bg-bg-soft hover:text-ink"
        >
          <ArrowLeft size={16} /> Your spaces
        </Link>
      </header>

      <div className="mt-12 text-center">
        <Pill tone="brand">New space</Pill>
        <h1 className="mt-4 text-3xl font-bold tracking-tight sm:text-4xl">
          Scan a space with your bracketbot
        </h1>
        <p className="mx-auto mt-3 max-w-md text-ink-2">
          Four quick steps. The bot does the walking; you get a SLAM map you can share.
        </p>
      </div>

      <motion.ol
        variants={staggerList(STEPS.length)}
        initial="hidden"
        animate="show"
        className="mx-auto mt-10 grid max-w-3xl gap-4 p-0 sm:grid-cols-2"
      >
        {STEPS.map((s, i) => (
          <StepCard
            key={s.title}
            index={i + 1}
            title={s.title}
            body={s.body}
            art={s.art}
            active={i === activeStep}
            done={i < activeStep}
          />
        ))}
      </motion.ol>

      <motion.div
        initial={{ opacity: 0, y: 12 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: 0.45, duration: 0.3 }}
        className="mt-10"
      >
        <PairingOrb phase={phase} onStart={start} />
      </motion.div>
    </PageShell>
  )
}

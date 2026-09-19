import { AnimatePresence, motion } from 'framer-motion'
import { Play } from 'lucide-react'
import type { OnboardingPhase } from '../../store/useOnboardingStore'

const ORB = 'pairing-orb'

interface PairingOrbProps {
  phase: OnboardingPhase
  onStart: () => void
}

/**
 * Radar orb while pairing; morphs (shared layoutId) into the Start button once paired.
 * The whiteboard frame: "pairing…" → [Start].
 */
export function PairingOrb({ phase, onStart }: PairingOrbProps) {
  const pairing = phase === 'idle' || phase === 'pairing'
  return (
    <div
      className="flex min-h-[152px] flex-col items-center justify-center gap-4"
      data-testid="pairing"
      data-phase={phase}
    >
      <AnimatePresence mode="wait" initial={false}>
        {pairing ? (
          <motion.div
            key="orb"
            layoutId={ORB}
            className="relative grid size-20 place-items-center rounded-full bg-brand shadow-md"
            exit={{ opacity: 0 }}
          >
            {[0, 1, 2].map((i) => (
              <motion.span
                key={i}
                className="absolute inset-0 rounded-full border-2 border-brand"
                initial={{ scale: 1, opacity: 0.7 }}
                animate={{ scale: 2.4, opacity: 0 }}
                transition={{ duration: 1.8, delay: i * 0.6, repeat: Infinity, ease: 'easeOut' }}
              />
            ))}
            <motion.span
              className="size-3 rounded-full bg-white"
              animate={{ scale: [1, 1.4, 1] }}
              transition={{ duration: 1.2, repeat: Infinity, ease: 'easeInOut' }}
            />
          </motion.div>
        ) : (
          <motion.button
            key="start"
            layoutId={ORB}
            type="button"
            data-testid="start-button"
            onClick={onStart}
            disabled={phase === 'starting'}
            whileHover={{ scale: 1.03 }}
            whileTap={{ scale: 0.97 }}
            transition={{ type: 'spring', stiffness: 320, damping: 26 }}
            className="inline-flex h-14 items-center gap-2 rounded-full bg-brand px-8 text-base font-semibold text-white shadow-md hover:bg-brand-2 disabled:opacity-70"
          >
            <Play size={18} fill="currentColor" />
            {phase === 'starting' ? 'Starting scan…' : 'Start'}
          </motion.button>
        )}
      </AnimatePresence>

      <AnimatePresence mode="wait" initial={false}>
        <motion.p
          key={pairing ? 'pairing' : 'paired'}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -6 }}
          transition={{ duration: 0.2 }}
          className="text-sm text-ink-2"
          aria-live="polite"
        >
          {pairing
            ? 'Pairing with your bracketbot…'
            : 'Paired. Press Start and the bot will map the space on its own.'}
        </motion.p>
      </AnimatePresence>
    </div>
  )
}

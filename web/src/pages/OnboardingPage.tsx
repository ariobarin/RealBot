import { AnimatePresence, motion } from 'framer-motion'
import { ArrowLeft, Play } from 'lucide-react'
import { useEffect, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { RobotIcon } from '../components/icons/RobotIcon'
import { ExploreScene, PairScene, PlaceScene, PowerScene } from '../components/onboarding/Scenes'
import { Button } from '../components/ui/Button'
import { Pill } from '../components/ui/Pill'
import { TopNav } from '../components/ui/TopNav'
import { EASE_OUT, fadeUp, pageVariants, staggerList } from '../lib/motion'
import { DEMO_BOT_NAME, PAIRING_MS, SCAN_MS, useOnboardingStore } from '../store/useOnboardingStore'

/** Until the Add flow creates real spaces, a finished scan opens the preset map. */
const PRESET_MAP_ID = 'small-house'

function Step({
  n,
  title,
  body,
  scene,
  status,
}: {
  n: number
  title: string
  body: string
  scene: ReactNode
  status?: ReactNode
}) {
  return (
    <motion.li
      variants={fadeUp}
      data-testid="step-card"
      className="flex list-none flex-col overflow-hidden rounded-3xl border border-line bg-white"
    >
      <div className="aspect-[376/220]">{scene}</div>
      <div className="flex flex-col gap-2 px-[22px] pb-[22px] pt-5">
        <div className="flex items-center gap-2.5">
          <span className="grid size-[26px] shrink-0 place-items-center rounded-full bg-ink text-xs font-bold text-white">
            {n}
          </span>
          <h2 className="text-[17px] font-bold tracking-tight">{title}</h2>
        </div>
        <p className="text-sm leading-relaxed text-ink-2">{body}</p>
        {status && <div className="mt-1">{status}</div>}
      </div>
    </motion.li>
  )
}

/** `/onboard` — Add a space: three steps on one screen, then the Scanning state. */
export function OnboardingPage() {
  const navigate = useNavigate()
  const { phase, beginPairing, markPaired, start, reset } = useOnboardingStore()
  const paired = phase === 'paired' || phase === 'scanning'

  useEffect(() => {
    beginPairing()
    const t = window.setTimeout(markPaired, PAIRING_MS)
    return () => {
      window.clearTimeout(t)
      reset()
    }
  }, [beginPairing, markPaired, reset])

  useEffect(() => {
    if (phase !== 'scanning') return
    const t = window.setTimeout(() => void navigate(`/map/${PRESET_MAP_ID}`), SCAN_MS)
    return () => window.clearTimeout(t)
  }, [phase, navigate])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') void navigate('/realtor')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navigate])

  return (
    <motion.main variants={pageVariants} initial="initial" animate="enter" exit="exit" className="min-h-dvh">
      <TopNav
        actions={
          <Button variant="ghost" onClick={() => void navigate('/realtor')}>
            <ArrowLeft size={16} /> Back to your spaces
          </Button>
        }
      />

      <AnimatePresence mode="wait" initial={false}>
        {phase === 'scanning' ? (
          <motion.div
            key="scanning"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.3, ease: EASE_OUT }}
            data-testid="scanning"
            className="mx-auto max-w-[1280px] px-6 pb-20 pt-8 sm:px-10"
          >
            <div className="mx-auto flex max-w-[760px] items-center gap-3">
              <span className="whitespace-nowrap text-sm font-bold">Scanning</span>
              <span className="h-2 flex-1 overflow-hidden rounded-full bg-line">
                <motion.span
                  className="block h-full rounded-full bg-brand"
                  initial={{ width: '8%' }}
                  animate={{ width: '96%' }}
                  transition={{ duration: SCAN_MS / 1000, ease: 'linear' }}
                />
              </span>
              <span className="whitespace-nowrap text-sm text-ink-2">a few minutes</span>
            </div>

            <div className="mt-9 flex flex-col items-center gap-10 lg:flex-row lg:gap-14">
              <ExploreScene className="rounded-3xl" />
              <div className="flex max-w-[560px] flex-col gap-4">
                <span className="text-[13px] font-bold uppercase tracking-wider text-ink-2">Scanning</span>
                <h1 className="text-[40px] font-extrabold leading-[1.05] tracking-[-0.03em]">
                  Mapping the space
                </h1>
                <p className="text-[17px] leading-relaxed text-ink-2">
                  The bot is walking the home and drawing the plan. You can leave this page — it will show up
                  under Your spaces when it's done.
                </p>
                <div className="flex items-center gap-2.5">
                  <Pill tone="brand">
                    <span className="size-1.5 rounded-full bg-current" />
                    Scanning
                  </Pill>
                  <span className="text-[13px] text-ink-2">{DEMO_BOT_NAME} · room by room</span>
                </div>
                <div className="mt-1 flex gap-2.5">
                  <Button
                    variant="secondary"
                    className="h-[52px] px-6 text-[15px]"
                    onClick={() => void navigate('/realtor')}
                  >
                    Back to your spaces
                  </Button>
                  <Button variant="ghost" className="h-[52px]" onClick={() => void navigate('/realtor')}>
                    Stop scan
                  </Button>
                </div>
                <p className="text-[13px] text-ink-3">Stopping keeps the rooms mapped so far.</p>
              </div>
            </div>
          </motion.div>
        ) : (
          <motion.div
            key="steps"
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.3, ease: EASE_OUT }}
            className="mx-auto max-w-[1280px] px-6 pb-20 pt-9 sm:px-10"
          >
            <div className="flex flex-col items-center gap-2 text-center">
              <h1 className="text-[36px] font-extrabold tracking-[-0.03em] sm:text-[40px]">Add a space</h1>
              <p className="text-[17px] text-ink-2">
                Three quick steps, then the bot maps the home on its own.
              </p>
            </div>

            <motion.ol
              variants={staggerList(3)}
              initial="hidden"
              animate="show"
              className="m-0 mt-8 grid gap-6 p-0 md:grid-cols-3"
            >
              <Step
                n={1}
                title="Power on your bracketbot"
                body="Hold the button on top until the ring light around the camera breathes red. About ten seconds."
                scene={<PowerScene className="h-full" />}
              />
              <Step
                n={2}
                title="Set it just inside the front door"
                body="Face it into the home. This spot is the home position: tours start here and the bot returns to it."
                scene={<PlaceScene className="h-full" />}
              />
              <Step
                n={3}
                title="Pair from this device"
                body="Stay on this page. We find the bot on the local network and pair on our own."
                scene={
                  <PairScene className="h-full" found={paired} name={paired ? DEMO_BOT_NAME : undefined} />
                }
                status={
                  <span data-testid="pairing" data-phase={phase}>
                    {paired ? (
                      <Pill tone="ok">
                        <span className="size-1.5 rounded-full bg-current" />
                        Found {DEMO_BOT_NAME} · 100%
                      </Pill>
                    ) : (
                      <Pill tone="brand">
                        <span className="size-1.5 animate-pulse rounded-full bg-current" />
                        Looking on this network…
                      </Pill>
                    )}
                  </span>
                }
              />
            </motion.ol>

            <div className="icon-hover mt-7 flex flex-col items-start gap-5 rounded-3xl bg-bg-soft px-7 py-[22px] sm:flex-row sm:items-center">
              <RobotIcon size={64} />
              <div className="flex flex-1 flex-col gap-1">
                <div className="flex flex-wrap items-center gap-2.5">
                  <span className="text-lg font-bold tracking-tight">
                    {paired ? 'Paired and at the front door' : 'Finding your bracketbot'}
                  </span>
                  {paired ? <Pill tone="ok">Ready</Pill> : <Pill tone="neutral">Waiting</Pill>}
                </div>
                <p className="text-sm text-ink-2">
                  {paired
                    ? "Press Start and it drives itself room to room, drawing the plan as it goes. Most homes take 8–15 minutes; you can leave once it's started."
                    : 'Make sure this device is on the same Wi‑Fi as the home. The bot shows up here as soon as it answers.'}
                </p>
              </div>
              <Button
                data-testid="start-button"
                className="h-14 px-[30px] text-base"
                disabled={!paired}
                onClick={start}
              >
                <Play size={18} fill="currentColor" /> Start scan
              </Button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </motion.main>
  )
}

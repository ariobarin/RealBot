import { motion, useReducedMotion } from 'framer-motion'
import { useEffect, useState } from 'react'
import { EASE_OUT } from '../../lib/motion'
import { PLACE_WORDS } from '../../lib/openTours'

const TINTS = ['var(--brand)', 'var(--arches)', 'var(--babu)', 'var(--plum)', 'var(--gold)', 'var(--ink)']

const HOLD_MS = 900
const MOVE_S = 0.32
/** Window height as a multiple of the row: 1 = only the live word, 3 = full picker wheel. */
const WINDOW = 1

/**
 * Word carousel: a one-row window that the next word slides up into while the
 * current one slides out; at rest only the live word shows. Loops seamlessly
 * (the first word is repeated at the end, then we snap back).
 */
export function WordCarousel({
  words = PLACE_WORDS,
  fontSize = 64,
  className = '',
}: {
  words?: string[]
  /** Pixels. The row is 1.25× this so ascenders and descenders stay inside their own row. */
  fontSize?: number
  className?: string
}) {
  const row = Math.round(fontSize * 1.25)
  const reduced = useReducedMotion()
  const [index, setIndex] = useState(0)
  const [snap, setSnap] = useState(false)
  const list = [...words, words[0]]

  useEffect(() => {
    if (reduced) return
    const id = window.setInterval(() => {
      setIndex((i) => {
        if (i + 1 < list.length) return i + 1
        // We're on the duplicated first word: jump to the real one without animating.
        setSnap(true)
        return 0
      })
    }, HOLD_MS)
    return () => window.clearInterval(id)
  }, [reduced, list.length])

  useEffect(() => {
    if (!snap) return
    const id = window.requestAnimationFrame(() => setSnap(false))
    return () => window.cancelAnimationFrame(id)
  }, [snap])

  const height = Math.round(row * WINDOW)
  const fade = Math.round((height - row) / 2)

  return (
    <span
      className={`relative inline-block overflow-hidden align-middle ${className}`}
      style={{ height, fontSize }}
      aria-live="off"
    >
      <motion.span
        className="block"
        style={{ paddingTop: (height - row) / 2 }}
        animate={{ y: -index * row }}
        transition={snap ? { duration: 0 } : { duration: MOVE_S, ease: EASE_OUT }}
      >
        {list.map((word, i) => (
          <span
            key={`${word}-${i}`}
            className="block whitespace-nowrap"
            style={{ height: row, lineHeight: `${row}px`, color: TINTS[i % TINTS.length] }}
            aria-hidden={i !== index}
          >
            {word}
          </span>
        ))}
      </motion.span>
      {fade > 0 && (
        <>
          <span
            aria-hidden
            className="pointer-events-none absolute inset-x-0 top-0"
            style={{ height: fade, background: 'linear-gradient(to bottom, var(--bg) 35%, transparent)' }}
          />
          <span
            aria-hidden
            className="pointer-events-none absolute inset-x-0 bottom-0"
            style={{ height: fade, background: 'linear-gradient(to top, var(--bg) 35%, transparent)' }}
          />
        </>
      )}
    </span>
  )
}

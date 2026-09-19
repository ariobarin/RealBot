import { motion } from 'framer-motion'

const loop = (duration: number, delay = 0) => ({
  duration,
  delay,
  repeat: Infinity,
  ease: 'easeInOut' as const,
})

/** Bot silhouette shared by the illustrations: two wheels, a body and a camera head. */
function Bot({ x = 0, y = 0, scale = 1 }: { x?: number; y?: number; scale?: number }) {
  return (
    <g transform={`translate(${x} ${y}) scale(${scale})`}>
      <rect x="-9" y="-12" width="18" height="18" rx="4" fill="#2b2b2b" />
      <circle cx="-7" cy="8" r="3.5" fill="#2b2b2b" />
      <circle cx="7" cy="8" r="3.5" fill="#2b2b2b" />
      <rect x="-4" y="-18" width="8" height="6" rx="2" fill="#2b2b2b" />
      <circle cx="0" cy="-15" r="1.6" fill="#ff385c" />
    </g>
  )
}

/** 1. Power on — bot with a pulsing status light. */
export function ArtPowerOn() {
  return (
    <svg viewBox="0 0 80 80" className="size-full" aria-hidden>
      <Bot x={40} y={44} scale={1.3} />
      <motion.circle
        cx="40"
        cy="24.5"
        r="3"
        fill="#ff385c"
        animate={{ opacity: [0.3, 1, 0.3], scale: [0.8, 1.15, 0.8] }}
        transition={loop(1.4)}
        style={{ transformOrigin: '40px 24.5px' }}
      />
      <motion.circle
        cx="40"
        cy="24.5"
        r="3"
        fill="none"
        stroke="#ff385c"
        strokeWidth="1.5"
        animate={{ scale: [1, 3.2], opacity: [0.6, 0] }}
        transition={loop(1.4)}
        style={{ transformOrigin: '40px 24.5px' }}
      />
    </svg>
  )
}

/** 2. Place at the entrance — a doorway with the bot rolling into position. */
export function ArtPlace() {
  return (
    <svg viewBox="0 0 80 80" className="size-full" aria-hidden>
      <rect x="14" y="10" width="52" height="60" rx="3" fill="none" stroke="#b0b0b0" strokeWidth="2" />
      <rect x="30" y="10" width="20" height="60" fill="#fff" />
      <line x1="30" y1="10" x2="30" y2="70" stroke="#b0b0b0" strokeWidth="2" />
      <line x1="50" y1="10" x2="50" y2="70" stroke="#b0b0b0" strokeWidth="2" />
      <motion.g animate={{ x: [-22, 0, 0, -22], opacity: [0, 1, 1, 0] }} transition={loop(3.2)}>
        <Bot x={40} y={56} scale={0.9} />
      </motion.g>
      <motion.ellipse
        cx="40"
        cy="64"
        rx="10"
        ry="2"
        fill="#ff385c"
        animate={{ opacity: [0, 0.35, 0.35, 0] }}
        transition={loop(3.2)}
      />
    </svg>
  )
}

/** 3. Pair — phone and bot exchanging a signal. */
export function ArtPair() {
  return (
    <svg viewBox="0 0 80 80" className="size-full" aria-hidden>
      <rect x="10" y="22" width="16" height="30" rx="3" fill="none" stroke="#2b2b2b" strokeWidth="2" />
      <circle cx="18" cy="47" r="1.4" fill="#2b2b2b" />
      <Bot x={60} y={44} scale={0.9} />
      {[0, 1, 2].map((i) => (
        <motion.circle
          key={i}
          cx="30"
          cy="37"
          r="4"
          fill="#ff385c"
          initial={{ opacity: 0 }}
          animate={{ x: [0, 20, 20], opacity: [0, 1, 0], scale: [0.6, 1, 0.6] }}
          transition={loop(1.8, i * 0.6)}
        />
      ))}
    </svg>
  )
}

/** 4. Explore — the bot sweeping a room while the SLAM grid fills in. */
export function ArtExplore() {
  const cells = Array.from({ length: 16 }, (_, i) => ({
    x: 14 + (i % 4) * 13,
    y: 14 + Math.floor(i / 4) * 13,
  }))
  return (
    <svg viewBox="0 0 80 80" className="size-full" aria-hidden>
      {cells.map((c, i) => (
        <motion.rect
          key={i}
          x={c.x}
          y={c.y}
          width="11"
          height="11"
          rx="2"
          fill="#f3efe8"
          stroke="#e6e2db"
          animate={{ opacity: [0.15, 1, 1, 0.15] }}
          transition={loop(4, i * 0.12)}
        />
      ))}
      <motion.g
        animate={{ x: [0, 26, 26, 0, 0], y: [0, 0, 26, 26, 0] }}
        transition={{ duration: 4, repeat: Infinity, ease: 'easeInOut' }}
      >
        <Bot x={26} y={26} scale={0.6} />
      </motion.g>
    </svg>
  )
}

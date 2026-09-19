import { Link } from 'react-router-dom'

type Tone = 'brand' | 'reversed' | 'ink'

const tones: Record<Tone, { fill: string; eye: string; text: string }> = {
  brand: { fill: 'var(--brand)', eye: '#ffffff', text: 'var(--brand)' },
  reversed: { fill: '#ffffff', eye: 'var(--brand)', text: '#ffffff' },
  ink: { fill: 'var(--ink)', eye: '#ffffff', text: 'var(--ink)' },
}

/** The mark: the robot's face — rounded body, two eyes, one antenna. Minimum 16 px. */
export function LogoMark({ size = 30, tone = 'brand' }: { size?: number; tone?: Tone }) {
  const t = tones[tone]
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden>
      <circle cx="16" cy="3.2" r="2.4" fill={t.fill} />
      <rect x="14.6" y="3" width="2.8" height="7" rx="1.4" fill={t.fill} />
      <rect x="3" y="9" width="26" height="20" rx="8" fill={t.fill} />
      <circle cx="11.6" cy="19" r="3.2" fill={t.eye} />
      <circle cx="20.4" cy="19" r="3.2" fill={t.eye} />
    </svg>
  )
}

/** Mark + wordmark. The wordmark is 0.86× the mark, weight 800, tracked −0.04em. */
export function Logo({ size = 30, tone = 'brand' }: { size?: number; tone?: Tone }) {
  const t = tones[tone]
  return (
    <span
      className="inline-flex items-center font-extrabold leading-none tracking-[-0.04em]"
      style={{ gap: Math.round(size * 0.28), fontSize: Math.round(size * 0.86), color: t.text }}
    >
      <LogoMark size={size} tone={tone} />
      realbot
    </span>
  )
}

/** The header lockup, linking home. */
export function Wordmark({ size = 30, tone = 'brand' }: { size?: number; tone?: Tone }) {
  return (
    <Link to="/" aria-label="RealBot home" className="no-underline">
      <Logo size={size} tone={tone} />
    </Link>
  )
}

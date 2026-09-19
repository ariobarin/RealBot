import './icons.css'

/** A dashed house with a coral plus that pops. Idle only. */
export function AddSpaceIcon({ size = 64 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" aria-hidden>
      <path
        d="M10 30 L32 12 L54 30 M14 27 V54 H50 V27"
        stroke="var(--ink-3)"
        strokeWidth="2.5"
        strokeDasharray="4 4"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <rect
        x="27"
        y="40"
        width="10"
        height="14"
        rx="1.5"
        fill="none"
        stroke="var(--ink-3)"
        strokeWidth="2"
        strokeDasharray="3 3"
      />
      <g className="ic-pop">
        <circle cx="50" cy="50" r="10" fill="var(--brand)" />
        <path d="M50 45v10M45 50h10" stroke="#fff" strokeWidth="2.6" strokeLinecap="round" />
      </g>
    </svg>
  )
}

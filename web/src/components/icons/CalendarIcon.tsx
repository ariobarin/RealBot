import './icons.css'

/** Your bookings. Idle: today's square pulses, the rings sway. Hover: today pops, the tick redraws. */
export function CalendarIcon({ size = 52 }: { size?: number }) {
  const cells = [
    [25, 47],
    [36, 47],
    [47, 47],
    [58, 47],
    [25, 57],
    [36, 57],
    [58, 57],
    [25, 67],
    [36, 67],
    [47, 67],
  ]
  return (
    <svg className="hero-icon" width={size} height={size} viewBox="0 0 96 96" aria-hidden>
      <ellipse cx="50" cy="88" rx="34" ry="4" fill="#000" opacity=".1" />
      <polygon points="78,26 86,21 86,74 78,80" fill="#CFCFCF" />
      <polygon points="18,22 26,17 86,21 78,26" fill="#FAFAFA" />
      <rect x="18" y="22" width="60" height="58" rx="7" fill="#F2F2F2" />
      <path d="M18 29a7 7 0 0 1 7-7h46a7 7 0 0 1 7 7v11H18z" fill="var(--brand)" />
      <rect x="18" y="40" width="60" height="1.5" fill="var(--brand-2)" opacity=".5" />
      <g className="ic-rings">
        <rect x="29" y="12" width="6" height="16" rx="3" fill="#3A3A3A" />
        <rect x="61" y="12" width="6" height="16" rx="3" fill="#3A3A3A" />
        <rect x="30" y="13" width="2" height="14" rx="1" fill="#6A6A6A" />
        <rect x="62" y="13" width="2" height="14" rx="1" fill="#6A6A6A" />
      </g>
      <g fill="#D6D6D6">
        {cells.map(([x, y]) => (
          <rect key={`${x}-${y}`} x={x} y={y} width="8" height="7" rx="1.5" />
        ))}
      </g>
      <g className="ic-today">
        <rect x="45" y="55" width="12" height="11" rx="2.5" fill="var(--brand)" />
        <path
          className="ic-tick"
          d="M48 60.5l2.2 2.2L54 58.5"
          stroke="#fff"
          strokeWidth="1.8"
          fill="none"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </g>
    </svg>
  )
}

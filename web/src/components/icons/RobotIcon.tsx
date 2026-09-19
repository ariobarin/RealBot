import './icons.css'

/** The bracketbot. Idle: lens blinks, antenna pulses. Hover: wheels spin, body leans forward. */
export function RobotIcon({ size = 52 }: { size?: number }) {
  const wheel = (cx: number) => (
    <g className="ic-wheel">
      <circle cx={cx} cy="76" r="10" fill="#2B2B2B" />
      <circle cx={cx} cy="76" r="6.5" fill="#3D3D3D" />
      <path d={`M${cx} 70v12M${cx - 6} 76h12`} stroke="#6A6A6A" strokeWidth="1.6" />
      <circle cx={cx} cy="76" r="2.4" fill="#BDBDBD" />
    </g>
  )
  return (
    <svg className="hero-icon" width={size} height={size} viewBox="0 0 96 96" aria-hidden>
      <ellipse cx="48" cy="88" rx="30" ry="4" fill="#000" opacity=".1" />
      <g className="ic-body">
        <line x1="48" y1="10" x2="48" y2="19" stroke="#444" strokeWidth="2" />
        <circle className="ic-bead" cx="48" cy="8" r="3.2" fill="var(--brand)" />
        <rect x="30" y="18" width="36" height="18" rx="7" fill="#2F2F2F" />
        <rect x="33" y="19.5" width="30" height="4" rx="2" fill="#4A4A4A" />
        <circle cx="48" cy="27" r="6.8" fill="var(--brand)" />
        <g className="ic-lens">
          <circle cx="48" cy="27" r="4.6" fill="#1C1C1C" />
          <circle cx="46.4" cy="25.4" r="1.5" fill="#fff" />
        </g>
        <rect x="44" y="36" width="8" height="4" fill="#9A9A9A" />
        <polygon points="32,40 38,36 66,36 60,40" fill="#FAFAFA" />
        <polygon points="60,40 66,36 66,66 60,70" fill="#CFCFCF" />
        <rect x="32" y="40" width="28" height="30" rx="5" fill="#F0F0F0" />
        <rect x="38" y="48" width="16" height="3" rx="1.5" fill="var(--brand)" />
        <circle cx="40" cy="57" r="1.5" fill="#BDBDBD" />
        <circle cx="45" cy="57" r="1.5" fill="#BDBDBD" />
        <rect x="28" y="70" width="40" height="4" fill="#555" />
      </g>
      {wheel(34)}
      {wheel(62)}
    </svg>
  )
}

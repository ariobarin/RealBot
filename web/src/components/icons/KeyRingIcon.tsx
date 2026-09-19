import './icons.css'

/** Realtor. Idle: hangs and swings. Hover: big swing, the tag jingles. */
export function KeyRingIcon({ size = 52 }: { size?: number }) {
  return (
    <svg className="hero-icon" width={size} height={size} viewBox="0 0 96 96" aria-hidden>
      <ellipse cx="50" cy="88" rx="26" ry="4" fill="#000" opacity=".1" />
      <circle cx="48" cy="18" r="8" fill="none" stroke="#BDBDBD" strokeWidth="3.2" />
      <path d="M42 13a8 8 0 0 1 9-2" stroke="#fff" strokeWidth="1.4" fill="none" strokeLinecap="round" />
      <g className="ic-hang">
        <path d="M48 26v4" stroke="#B8923A" strokeWidth="3" />
        <circle cx="48" cy="42" r="12" fill="#C79B3E" />
        <circle cx="46.5" cy="40.5" r="10.5" fill="#E8C25A" />
        <circle cx="48" cy="42" r="4.2" fill="#fff" />
        <rect x="44.5" y="52" width="7" height="30" rx="2" fill="#C79B3E" />
        <rect x="44.5" y="52" width="4" height="30" rx="2" fill="#E8C25A" />
        <rect x="51" y="68" width="7" height="4" rx="1" fill="#C79B3E" />
        <rect x="51" y="76" width="9" height="4" rx="1" fill="#C79B3E" />
      </g>
      <g className="ic-tag">
        <path d="M52 22 L64 30" stroke="#BDBDBD" strokeWidth="1.6" />
        <rect x="56" y="30" width="18" height="24" rx="4" fill="var(--brand)" />
        <rect x="70" y="30" width="4" height="24" rx="2" fill="var(--brand-2)" />
        <circle cx="64" cy="35" r="2" fill="#fff" />
        <path d="M59 46 L65 41 L71 46 V50 H59 Z" fill="#fff" />
        <rect x="63" y="46" width="4" height="4" fill="var(--brand)" />
      </g>
    </svg>
  )
}

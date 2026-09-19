import './icons.css'

/** Spaces / Manage spaces. Idle: tree sways. Hover: door swings open, chimney smokes. */
export function HouseIcon({ size = 52 }: { size?: number }) {
  return (
    <svg className="hero-icon" width={size} height={size} viewBox="0 0 96 96" aria-hidden>
      <ellipse cx="50" cy="88" rx="38" ry="4" fill="#000" opacity=".1" />
      <g className="ic-tree">
        <rect x="13" y="64" width="4" height="20" rx="1" fill="#8A5A2B" />
        <circle cx="15" cy="56" r="11" fill="#5FA34F" />
        <circle cx="7" cy="63" r="7" fill="#6FB35F" />
        <circle cx="23" cy="63" r="7" fill="#4F8F42" />
        <circle cx="12" cy="52" r="4.5" fill="#7CC26B" />
      </g>
      <polygon points="72,40 86,33 86,74 72,80" fill="#CFCFCF" />
      <rect x="24" y="40" width="48" height="40" fill="#EDEDED" />
      <g className="ic-smoke">
        <circle cx="64" cy="12" r="3" fill="#D9D9D9" />
        <circle cx="67" cy="8" r="2.2" fill="#E6E6E6" />
      </g>
      <rect x="60" y="16" width="7" height="16" fill="#B5B5B5" />
      <rect x="59" y="14" width="9" height="3" fill="#9A9A9A" />
      <polygon points="48,14 62,7 94,34 80,40" fill="#4A4A4A" />
      <polygon points="16,42 48,13 80,42" fill="#2F2F2F" />
      <polygon points="26,42 48,23 70,42" fill="#F6F6F6" />
      <circle cx="48" cy="34" r="3.2" fill="#CDE8FF" stroke="#fff" strokeWidth="1.2" />
      <rect x="28" y="48" width="10" height="10" rx="1" fill="#CDE8FF" stroke="#fff" strokeWidth="1.5" />
      <path d="M33 48v10M28 53h10" stroke="#fff" strokeWidth="1" />
      <rect x="58" y="48" width="10" height="10" rx="1" fill="#CDE8FF" stroke="#fff" strokeWidth="1.5" />
      <path d="M63 48v10M58 53h10" stroke="#fff" strokeWidth="1" />
      <rect x="41" y="55" width="14" height="25" rx="1.5" fill="#FFE2A8" />
      <g className="ic-door">
        <rect x="41" y="55" width="14" height="25" rx="1.5" fill="var(--brand)" />
        <rect x="52" y="55" width="3" height="25" fill="var(--brand-2)" />
        <circle cx="51" cy="68" r="1.3" fill="#F5D67A" />
      </g>
      <rect x="38" y="80" width="20" height="3" rx="1" fill="#D6D6D6" />
    </svg>
  )
}

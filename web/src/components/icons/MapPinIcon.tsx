import './icons.css'

/** Tours. Idle: pin hovers, route flows. Hover: pin jumps and lands with a squash. */
export function MapPinIcon({ size = 52 }: { size?: number }) {
  return (
    <svg className="hero-icon" width={size} height={size} viewBox="0 0 96 96" aria-hidden>
      <ellipse cx="48" cy="86" rx="38" ry="4" fill="#000" opacity=".1" />
      <polygon points="10,34 36,28 36,76 10,82" fill="#F4F1EA" />
      <polygon points="36,28 60,34 60,82 36,76" fill="#E9E4D8" />
      <polygon points="60,34 86,28 86,76 60,82" fill="#F4F1EA" />
      <path d="M36 28V76M60 34V82" stroke="#D8D2C4" strokeWidth="1" />
      <path
        d="M14 62 C 26 54 30 66 36 60 S 50 50 60 58 S 74 46 82 52"
        stroke="#CFCAB9"
        strokeWidth="2.5"
        fill="none"
        strokeLinecap="round"
      />
      <path
        d="M12 46 C 22 44 28 50 36 46"
        stroke="#BFD8EA"
        strokeWidth="2"
        fill="none"
        strokeLinecap="round"
      />
      <rect x="66" y="60" width="8" height="6" fill="#DAD3C2" />
      <rect x="20" y="66" width="6" height="6" fill="#DAD3C2" />
      <path
        className="ic-route"
        d="M20 70 C 34 58 44 70 66 44"
        stroke="var(--brand)"
        strokeWidth="2.2"
        fill="none"
        strokeLinecap="round"
      />
      <ellipse className="ic-pinshadow" cx="66" cy="46" rx="6" ry="2.2" fill="#000" opacity=".18" />
      <g className="ic-pin">
        <path
          d="M66 18c-8.3 0-13 6.2-13 12.6C53 40 66 46 66 46s13-6 13-15.4C79 24.2 74.3 18 66 18z"
          fill="var(--brand)"
        />
        <path
          d="M66 18c-8.3 0-13 6.2-13 12.6 0 2 .5 4 1.4 5.8C56 30 60 24 66 24c2 0 3.6.4 5 1-1.3-4-3-7-5-7z"
          fill="#fff"
          opacity=".25"
        />
        <circle cx="66" cy="31" r="4.6" fill="#fff" />
      </g>
    </svg>
  )
}

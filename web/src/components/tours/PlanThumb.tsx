/** A warm-paper floor plan with the robot's position. Four wall layouts to vary a grid. */
const WALLS = [
  'M30 28 H250 V168 H30 Z M30 98 H120 M120 28 V98 M170 98 H250 M170 98 V168',
  'M22 32 H258 V164 H22 Z M104 32 V112 M104 112 H22 M182 164 V92 M182 92 H258',
  'M42 22 H238 V174 H42 Z M42 82 H142 V22 M142 122 H238 M142 122 V174',
  'M26 42 H254 V154 H26 Z M112 42 V96 H190 V154 M190 96 H254',
]
const ROBOT: [number, number][] = [
  [200, 130],
  [140, 130],
  [90, 130],
  [150, 128],
]

export function PlanThumb({ variant, live }: { variant: 0 | 1 | 2 | 3; live: boolean }) {
  const [rx, ry] = ROBOT[variant]
  return (
    <svg className="size-full" viewBox="0 0 280 196" preserveAspectRatio="xMidYMid slice" aria-hidden>
      <rect width="280" height="196" fill="var(--map-floor)" />
      <g stroke="var(--map-grid)" strokeWidth="1">
        <path d="M20 0v196M60 0v196M100 0v196M140 0v196M180 0v196M220 0v196M260 0v196M0 20h280M0 60h280M0 100h280M0 140h280M0 180h280" />
      </g>
      <rect x="52" y="44" width="60" height="30" rx="4" fill="#e9e2d6" />
      <rect x="180" y="120" width="44" height="30" rx="4" fill="#e9e2d6" />
      <path d={WALLS[variant]} stroke="var(--map-wall)" strokeWidth="5" fill="none" strokeLinejoin="round" />
      {live ? (
        <>
          <circle
            className="orb-ring"
            cx={rx}
            cy={ry}
            r="8"
            fill="var(--brand)"
            opacity=".4"
            style={{ transformOrigin: `${rx}px ${ry}px` }}
          />
          <circle cx={rx} cy={ry} r="6.5" fill="var(--brand)" stroke="#fff" strokeWidth="2.5" />
        </>
      ) : (
        <circle cx={rx} cy={ry} r="6" fill="var(--ink-3)" stroke="#fff" strokeWidth="2.5" />
      )}
    </svg>
  )
}

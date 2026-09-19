import { RobotFigure } from '../icons/RobotIcon'
import './scenes.css'

const GRID =
  'M0 40h560M0 80h560M0 120h560M0 160h560M0 200h560M0 240h560M0 280h560M0 320h560M0 360h560M40 0v420M80 0v420M120 0v420M160 0v420M200 0v420M240 0v420M280 0v420M320 0v420M360 0v420M400 0v420M440 0v420M480 0v420M520 0v420'

function Stage({ className = '', children }: { className?: string; children: React.ReactNode }) {
  return <div className={`scene ${className}`}>{children}</div>
}

/** Step 1 — the bot boots: the top button holds, the ring light breathes, three dots tick. */
export function PowerScene({ className = '' }: { className?: string }) {
  return (
    <Stage className={className}>
      <svg viewBox="0 0 560 420" preserveAspectRatio="xMidYMid slice" aria-hidden>
        <ellipse cx="280" cy="332" rx="120" ry="12" fill="#000" opacity=".08" />
        <circle
          className="sc-ring"
          cx="280"
          cy="140"
          r="30"
          fill="none"
          stroke="var(--brand)"
          strokeWidth="6"
        />
        <circle
          className="sc-ring"
          cx="280"
          cy="140"
          r="46"
          fill="none"
          stroke="var(--brand)"
          strokeWidth="2"
          opacity=".4"
          style={{ animationDelay: '.3s' }}
        />
        <svg x="180" y="84" width="200" height="200" viewBox="0 0 96 96">
          <RobotFigure />
        </svg>
        <g className="sc-hold">
          <rect x="268" y="74" width="24" height="14" rx="6" fill="var(--brand)" />
          <rect x="272" y="70" width="16" height="6" rx="3" fill="var(--brand-2)" />
        </g>
        <g transform="translate(0,368)">
          <circle className="sc-dot" cx="262" cy="0" r="5" fill="var(--ink)" />
          <circle className="sc-dot" cx="280" cy="0" r="5" fill="var(--ink)" />
          <circle className="sc-dot" cx="298" cy="0" r="5" fill="var(--ink)" />
        </g>
      </svg>
    </Stage>
  )
}

const PLACE_PLAN = 'M120 60 H440 V330 H310 M250 330 H120 Z M120 200 H230 M230 60 V200 M320 200 H440'

/** Step 2 — the front door swings open, the bot rolls in and parks on the HOME ring. */
export function PlaceScene({ className = '' }: { className?: string }) {
  return (
    <Stage className={className}>
      <svg viewBox="0 0 560 420" preserveAspectRatio="xMidYMid slice" aria-hidden>
        <path d={GRID} stroke="var(--map-grid)" strokeWidth="1" />
        <rect x="120" y="60" width="320" height="270" fill="var(--map-floor)" />
        <path d={PLACE_PLAN} stroke="var(--map-wall)" strokeWidth="8" fill="none" strokeLinejoin="round" />
        <path
          className="sc-door"
          d="M250 330 V276"
          stroke="var(--brand)"
          strokeWidth="8"
          strokeLinecap="round"
        />
        <path
          d="M250 330 A54 54 0 0 1 304 276"
          stroke="var(--brand)"
          strokeWidth="1.5"
          strokeDasharray="3 4"
          fill="none"
        />
        <circle
          className="sc-home"
          cx="280"
          cy="262"
          r="26"
          fill="none"
          stroke="var(--brand)"
          strokeWidth="3"
        />
        <circle
          cx="280"
          cy="262"
          r="26"
          fill="none"
          stroke="var(--brand)"
          strokeWidth="2"
          strokeDasharray="5 5"
        />
        <text
          x="280"
          y="228"
          textAnchor="middle"
          fontSize="12"
          fontWeight="700"
          fill="var(--brand)"
          letterSpacing="1"
        >
          HOME
        </text>
        <g className="sc-roll">
          <svg x="250" y="344" width="60" height="60" viewBox="0 0 96 96">
            <RobotFigure />
          </svg>
        </g>
        <text x="280" y="404" textAnchor="middle" fontSize="13" fill="var(--ink-2)">
          front door
        </text>
      </svg>
    </Stage>
  )
}

/** Step 3 — the phone reaches the bot over radio; when found, a check and the bot's name land. */
export function PairScene({
  found,
  name,
  className = '',
}: {
  found: boolean
  name?: string
  className?: string
}) {
  return (
    <Stage className={`${found ? '' : 'sc-searching'} ${className}`}>
      <svg viewBox="0 0 560 420" preserveAspectRatio="xMidYMid slice" aria-hidden>
        <ellipse cx="150" cy="332" rx="60" ry="8" fill="#000" opacity=".08" />
        <ellipse cx="400" cy="332" rx="90" ry="10" fill="#000" opacity=".08" />
        <rect x="110" y="120" width="80" height="150" rx="14" fill="#2F2F2F" />
        <rect x="117" y="132" width="66" height="122" rx="6" fill="#fff" />
        <rect x="128" y="146" width="44" height="6" rx="3" fill="var(--line)" />
        <rect x="128" y="158" width="28" height="6" rx="3" fill="var(--line)" />
        <rect x="128" y="196" width="44" height="30" rx="8" fill="var(--brand-soft)" />
        <circle cx="150" cy="211" r="8" fill="var(--brand)" />
        <path
          className="sc-wave"
          d="M214 165a44 44 0 0 1 0 60"
          stroke="var(--brand)"
          strokeWidth="4"
          fill="none"
          strokeLinecap="round"
        />
        <path
          className="sc-wave"
          d="M236 150a66 66 0 0 1 0 90"
          stroke="var(--brand)"
          strokeWidth="4"
          fill="none"
          strokeLinecap="round"
        />
        <path
          className="sc-wave"
          d="M258 135a88 88 0 0 1 0 120"
          stroke="var(--brand)"
          strokeWidth="4"
          fill="none"
          strokeLinecap="round"
        />
        <svg x="320" y="110" width="170" height="170" viewBox="0 0 96 96">
          <RobotFigure />
        </svg>
        <g className="sc-found">
          <circle cx="470" cy="128" r="22" fill="var(--ok)" stroke="#fff" strokeWidth="4" />
          <path
            d="M460 128l7 7 13-14"
            stroke="#fff"
            strokeWidth="4"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </g>
        {name && (
          <g className="sc-found sc-found--late">
            <rect x="336" y="300" width="138" height="30" rx="15" fill="#fff" stroke="var(--line)" />
            <text
              x="405"
              y="320"
              textAnchor="middle"
              fontFamily="ui-monospace, monospace"
              fontSize="13"
              fontWeight="600"
              fill="var(--ink)"
            >
              {name}
            </text>
          </g>
        )}
      </svg>
    </Stage>
  )
}

const EXPLORE_PLAN = 'M100 80 H460 V360 H100 Z M100 250 H230 M230 80 V250 M350 250 H460 M350 250 V360'
const ROOMS = ['Entry', 'Living', 'Kitchen', 'Bedroom']

/**
 * Scanning — the bot walks the plan with its scan cone sweeping while the walls draw in
 * behind it. Fixed 560×420: the bot follows a pixel path.
 */
export function ExploreScene({ className = '' }: { className?: string }) {
  return (
    <Stage className={`h-[420px] w-[560px] ${className}`}>
      <svg viewBox="0 0 560 420" aria-hidden>
        <path d={GRID} stroke="var(--map-grid)" strokeWidth="1" />
        <rect x="100" y="80" width="360" height="280" fill="var(--map-floor)" />
        <path d={EXPLORE_PLAN} stroke="#DDD6C8" strokeWidth="8" fill="none" strokeLinejoin="round" />
        <path
          className="sc-walls"
          d={EXPLORE_PLAN}
          stroke="var(--map-wall)"
          strokeWidth="8"
          fill="none"
          strokeLinejoin="round"
        />
        <path
          className="sc-trail"
          d="M150 300 L150 130 L300 130 L300 210 L430 210 L430 320 L300 320 L300 250 L150 250 L150 300"
          stroke="var(--brand)"
          strokeWidth="2"
          fill="none"
          opacity=".6"
        />
        <circle
          cx="150"
          cy="300"
          r="10"
          fill="none"
          stroke="var(--brand)"
          strokeWidth="2"
          strokeDasharray="4 3"
        />
      </svg>
      <div className="sc-rover" aria-hidden>
        <svg width="56" height="56" viewBox="0 0 96 96">
          <g className="sc-cone">
            <path d="M48 48 L96 20 L96 76 Z" fill="var(--brand)" opacity=".18" />
          </g>
          <RobotFigure />
        </svg>
      </div>
      <div className="absolute left-4 top-4 flex flex-col gap-1.5" aria-hidden>
        {ROOMS.map((r) => (
          <span key={r} className="sc-chip rounded-full bg-white px-2.5 py-1 text-xs font-semibold shadow-sm">
            {r} · found
          </span>
        ))}
      </div>
    </Stage>
  )
}

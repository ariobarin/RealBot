import { RobotFigure } from '../icons/RobotIcon'
import './scenes.css'

const GRID =
  'M0 40h560M0 80h560M0 120h560M0 160h560M0 200h560M0 240h560M0 280h560M0 320h560M0 360h560M40 0v420M80 0v420M120 0v420M160 0v420M200 0v420M240 0v420M280 0v420M320 0v420M360 0v420M400 0v420M440 0v420M480 0v420M520 0v420'

function Stage({ className = '', children }: { className?: string; children: React.ReactNode }) {
  return <div className={`scene ${className}`}>{children}</div>
}

/** Step 1 — a finger holds the top button; the hold arc fills and the lens ring breathes on. */
export function PowerScene({ className = '' }: { className?: string }) {
  return (
    <Stage className={className}>
      <svg viewBox="0 0 560 420" preserveAspectRatio="xMidYMid slice" aria-hidden>
        <ellipse className="sc-ground" cx="280" cy="300" rx="170" ry="24" />
        <svg x="170" y="92" width="220" height="220" viewBox="0 0 96 96">
          <RobotFigure />
        </svg>
        <circle className="sc-glow" cx="280" cy="149" r="18" fill="none" stroke="#FF385C" strokeWidth="7" />
        <circle
          className="sc-glow"
          cx="280"
          cy="149"
          r="25"
          fill="none"
          stroke="#FF385C"
          strokeWidth="2"
          style={{ animationDelay: '.15s' }}
        />
        <circle className="sc-lit" cx="280" cy="149" r="12" fill="none" stroke="#FF385C" strokeWidth="5" />
        <g className="sc-finger">
          <g className="sc-finger-press">
            <path
              d="M262 -96 h60 a26 26 0 0 1 26 26 v40 a12 12 0 0 1 -12 12 h-8 v-6 h8 a6 6 0 0 0 6 -6 v-40 a20 20 0 0 0 -20 -20 h-60 z"
              fill="#E9B98F"
            />
            <rect x="266" y="-40" width="28" height="140" rx="14" fill="#F2C9A6" />
            <path d="M266 20 h28 M266 60 h28" stroke="#E4B48E" strokeWidth="2" strokeLinecap="round" />
            <rect x="271" y="72" width="18" height="24" rx="9" fill="#F9E3D3" />
          </g>
        </g>
        <circle
          className="sc-holdarc"
          cx="280"
          cy="104"
          r="20"
          fill="none"
          stroke="#FF385C"
          strokeWidth="4"
          strokeLinecap="round"
          transform="rotate(-90 280 104)"
        />
      </svg>
    </Stage>
  )
}

/** Step 2 — close-up of the doorway: the door swings in, the bot rolls off the mat onto the HOME ring. */
export function PlaceScene({ className = '' }: { className?: string }) {
  return (
    <Stage className={className}>
      <svg viewBox="0 0 560 420" preserveAspectRatio="xMidYMid slice" aria-hidden>
        <rect x="40" y="40" width="480" height="174" fill="#F3EFE8" />
        <g stroke="#E6E2DB" strokeWidth="1">
          <path d="M80 40v174M120 40v174M160 40v174M200 40v174M240 40v174M280 40v174M320 40v174M360 40v174M400 40v174M440 40v174M480 40v174M40 80h480M40 120h480M40 160h480M40 200h480" />
        </g>
        <rect x="40" y="214" width="480" height="180" fill="#E8E6E2" />
        <rect x="236" y="262" width="88" height="40" rx="6" fill="#D9D4CB" />
        <rect
          x="243"
          y="268"
          width="74"
          height="28"
          rx="4"
          fill="none"
          stroke="#C7C1B6"
          strokeWidth="2"
          strokeDasharray="3 3"
        />
        <rect x="40" y="206" width="188" height="16" fill="#2B2B2B" />
        <rect x="332" y="206" width="188" height="16" fill="#2B2B2B" />
        <rect x="228" y="200" width="8" height="28" rx="2" fill="#4A4A4A" />
        <rect x="324" y="200" width="8" height="28" rx="2" fill="#4A4A4A" />
        <path
          d="M332 214 A96 96 0 0 0 200 125"
          stroke="#FF385C"
          strokeWidth="1.5"
          strokeDasharray="3 4"
          fill="none"
          opacity=".6"
        />
        <g className="sc-door">
          <rect x="236" y="206" width="96" height="12" rx="3" fill="#FF385C" />
          <rect x="236" y="206" width="96" height="4" rx="2" fill="#FF6B86" opacity=".7" />
          <circle cx="322" cy="212" r="2.6" fill="#F5D67A" />
        </g>
        <circle className="sc-home" cx="280" cy="120" r="34" fill="none" stroke="#FF385C" strokeWidth="3" />
        <circle
          cx="280"
          cy="120"
          r="34"
          fill="none"
          stroke="#FF385C"
          strokeWidth="2.5"
          strokeDasharray="6 5"
        />
        <text
          x="280"
          y="70"
          textAnchor="middle"
          fontSize="12"
          fontWeight="700"
          fill="#FF385C"
          letterSpacing="1.5"
        >
          HOME
        </text>
        <g className="sc-roll">
          <svg x="232" y="246" width="96" height="96" viewBox="0 0 96 96">
            <RobotFigure />
          </svg>
        </g>
        <text
          x="280"
          y="336"
          textAnchor="middle"
          fontSize="12"
          fontWeight="600"
          fill="#9A9A9A"
          letterSpacing="1"
        >
          OUTSIDE
        </text>
      </svg>
    </Stage>
  )
}

/** Step 3 — the phone calls, the bot answers; when found, the screen says Paired and a check lands on the bot. */
export function PairScene({ found, className = '' }: { found: boolean; className?: string }) {
  return (
    <Stage className={`${found ? '' : 'sc-searching'} ${className}`}>
      <svg viewBox="0 0 560 420" preserveAspectRatio="xMidYMid slice" aria-hidden>
        <ellipse className="sc-ground" cx="280" cy="318" rx="230" ry="24" />
        <ellipse cx="146" cy="316" rx="54" ry="7" fill="#000000" opacity=".1" />
        <rect x="88" y="86" width="116" height="232" rx="24" fill="#1F1F1F" />
        <rect x="96" y="94" width="100" height="216" rx="18" fill="#ffffff" />
        <rect x="128" y="100" width="36" height="6" rx="3" fill="#1F1F1F" />
        <g transform="translate(131,134) scale(1.1)">
          <circle cx="16" cy="3.2" r="2.4" fill="#FF385C" />
          <rect x="14.6" y="3" width="2.8" height="7" rx="1.4" fill="#FF385C" />
          <rect x="3" y="9" width="26" height="20" rx="8" fill="#FF385C" />
          <circle cx="11.6" cy="19" r="3.2" fill="#ffffff" />
          <circle cx="20.4" cy="19" r="3.2" fill="#ffffff" />
        </g>
        <rect x="112" y="184" width="68" height="7" rx="3.5" fill="#DADADA" />
        <rect x="122" y="197" width="48" height="6" rx="3" fill="#EBEBEB" />
        <g className="sc-found-hide">
          <circle
            className="sc-scanring"
            cx="146"
            cy="250"
            r="14"
            fill="none"
            stroke="#FF385C"
            strokeWidth="3"
          />
          <circle cx="146" cy="250" r="7" fill="#FF385C" />
        </g>
        <g className="sc-found">
          <rect x="104" y="236" width="84" height="26" rx="13" fill="#E6F4E6" />
          <path
            d="M116 249l4 4 7-8"
            stroke="#008A05"
            strokeWidth="2.2"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
          <text x="156" y="253.5" textAnchor="middle" fontSize="11" fontWeight="700" fill="#1B5E20">
            Paired
          </text>
        </g>
        <path
          className="sc-pw"
          d="M222 196a40 40 0 0 1 0 56"
          stroke="#FF385C"
          strokeWidth="5"
          fill="none"
          strokeLinecap="round"
        />
        <path
          className="sc-pw sc-pw2"
          d="M242 180a62 62 0 0 1 0 88"
          stroke="#FF385C"
          strokeWidth="5"
          fill="none"
          strokeLinecap="round"
        />
        <path
          className="sc-pw sc-pw3"
          d="M262 164a84 84 0 0 1 0 120"
          stroke="#FF385C"
          strokeWidth="5"
          fill="none"
          strokeLinecap="round"
        />
        <path
          className="sc-bw"
          d="M338 196a40 40 0 0 0 0 56"
          stroke="#222222"
          strokeWidth="5"
          fill="none"
          strokeLinecap="round"
        />
        <path
          className="sc-bw sc-bw2"
          d="M318 180a62 62 0 0 0 0 88"
          stroke="#222222"
          strokeWidth="5"
          fill="none"
          strokeLinecap="round"
        />
        <path
          className="sc-bw sc-bw3"
          d="M298 164a84 84 0 0 0 0 120"
          stroke="#222222"
          strokeWidth="5"
          fill="none"
          strokeLinecap="round"
        />
        <svg x="322" y="152" width="180" height="180" viewBox="0 0 96 96">
          <RobotFigure />
        </svg>
        <g className="sc-found">
          <circle cx="482" cy="172" r="22" fill="#008A05" stroke="#ffffff" strokeWidth="4" />
          <path
            d="M472 172l7 7 13-14"
            stroke="#ffffff"
            strokeWidth="4"
            fill="none"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </g>
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

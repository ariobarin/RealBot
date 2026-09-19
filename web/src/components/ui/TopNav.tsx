import { motion } from 'framer-motion'
import { Link, NavLink } from 'react-router-dom'
import { useAuth } from '../../auth/useAuth'
import { spring } from '../../lib/motion'
import { HouseIcon } from '../icons/HouseIcon'
import { KeyRingIcon } from '../icons/KeyRingIcon'
import { MapPinIcon } from '../icons/MapPinIcon'
import { Wordmark } from '../brand/Logo'
import { AccountMenu } from './AccountMenu'

const tabs = [
  { to: '/', label: 'Tours', Icon: MapPinIcon, end: true },
  { to: '/realtor', label: 'Manage spaces', Icon: HouseIcon, end: false },
]

/** The public two-tab header: Tours (everyone) and Manage spaces (realtors). */
export function TopNav() {
  const { session } = useAuth()
  return (
    <header className="flex h-[84px] items-center justify-between gap-6 border-b border-line px-6 sm:px-10">
      <Wordmark />

      <nav aria-label="Primary" className="flex items-end gap-6 self-stretch sm:gap-11">
        {tabs.map(({ to, label, Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              `icon-hover relative flex items-center gap-2.5 self-stretch pt-3 text-[15px] no-underline transition-colors sm:text-base ${
                isActive ? 'font-semibold text-ink' : 'font-medium text-ink-2 hover:text-ink'
              }`
            }
          >
            {({ isActive }) => (
              <>
                <Icon size={52} />
                <span className="hidden sm:inline">{label}</span>
                {isActive && (
                  <motion.span
                    layoutId="nav-underline"
                    transition={spring}
                    className="absolute inset-x-0 bottom-0 h-0.5 rounded-full bg-ink"
                  />
                )}
              </>
            )}
          </NavLink>
        ))}
      </nav>

      <div className="flex items-center gap-3">
        {session ? (
          <AccountMenu />
        ) : (
          <Link
            to="/signin"
            className="icon-hover flex items-center gap-2 rounded-full py-1.5 pl-1.5 pr-4 text-[15px] font-semibold text-ink no-underline transition-colors hover:bg-bg-soft"
          >
            <KeyRingIcon size={36} />
            <span className="hidden sm:inline">Realtor sign in</span>
          </Link>
        )}
      </div>
    </header>
  )
}

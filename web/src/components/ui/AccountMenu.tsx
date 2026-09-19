import { AnimatePresence, motion } from 'framer-motion'
import { Menu } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../../auth/useAuth'
import { EASE_OUT } from '../../lib/motion'

type Item = { label: string; to?: string; onSelect?: () => void; strong?: boolean }

/** The header's account pill — hamburger + avatar — and the menu it opens. */
export function AccountMenu() {
  const { session, logout } = useAuth()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)
  const root = useRef<HTMLDivElement>(null)
  const menuId = useId()

  useEffect(() => {
    if (!open) return
    const onPointer = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  const signOut = () => {
    logout()
    void navigate('/')
  }

  const groups: Item[][] =
    session?.role === 'realtor'
      ? [
          [
            { label: 'Manage spaces', to: '/realtor', strong: true },
            { label: 'Add a space', to: '/onboard' },
          ],
          [{ label: 'Browse open tours', to: '/' }],
          [{ label: 'Sign out', onSelect: signOut }],
        ]
      : session?.role === 'visitor'
        ? [
            [{ label: 'Back to your tour', to: `/user/${encodeURIComponent(session.roomId)}`, strong: true }],
            [
              { label: 'Browse open tours', to: '/' },
              { label: 'Realtor sign in', to: '/signin' },
            ],
            [{ label: 'Leave tour', onSelect: signOut }],
          ]
        : [
            [
              { label: 'Realtor sign in', to: '/signin', strong: true },
              { label: 'Browse open tours', to: '/' },
            ],
            [{ label: 'How tours work', to: '/#open-tours' }],
          ]

  const initial = session
    ? (session.role === 'realtor' ? session.email : session.roomId).slice(0, 1).toUpperCase()
    : null

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        aria-label="Account menu"
        onClick={() => setOpen((o) => !o)}
        className="flex h-11 items-center gap-2.5 rounded-full border border-[#dddddd] bg-white pl-3 pr-2 shadow-sm transition-shadow hover:shadow-md"
      >
        <Menu size={16} strokeWidth={2.2} />
        <span
          className={`grid size-[30px] place-items-center rounded-full text-[13px] font-bold ${
            initial ? 'bg-brand text-white' : 'bg-ink-2 text-white'
          }`}
        >
          {initial ?? (
            <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden>
              <circle cx="12" cy="8" r="4" fill="currentColor" />
              <path d="M4 21c0-4 3.6-7 8-7s8 3 8 7" fill="currentColor" />
            </svg>
          )}
        </span>
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            id={menuId}
            role="menu"
            initial={{ opacity: 0, y: -6, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -6, scale: 0.98 }}
            transition={{ duration: 0.18, ease: EASE_OUT }}
            className="absolute right-0 top-[calc(100%+10px)] z-20 w-60 origin-top-right overflow-hidden rounded-2xl bg-white py-2 shadow-lg"
          >
            {groups.map((items, g) => (
              <div key={g} className={g > 0 ? 'mt-2 border-t border-line pt-2' : ''}>
                {items.map((item) =>
                  item.to ? (
                    <Link
                      key={item.label}
                      to={item.to}
                      role="menuitem"
                      onClick={() => setOpen(false)}
                      className={`block px-4 py-2.5 text-sm text-ink no-underline hover:bg-bg-soft ${
                        item.strong ? 'font-semibold' : ''
                      }`}
                    >
                      {item.label}
                    </Link>
                  ) : (
                    <button
                      key={item.label}
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setOpen(false)
                        item.onSelect?.()
                      }}
                      className="block w-full px-4 py-2.5 text-left text-sm text-ink hover:bg-bg-soft"
                    >
                      {item.label}
                    </button>
                  ),
                )}
              </div>
            ))}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

import { AnimatePresence } from 'framer-motion'
import { Suspense, lazy, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { LibraryPage } from './pages/LibraryPage'
import { OnboardingPage } from './pages/OnboardingPage'
import { PlaceholderPage } from './pages/PlaceholderPage'
import { RealtorSignInPage } from './pages/RealtorSignInPage'
import { ToursPage } from './pages/ToursPage'
import { useAuth } from './auth/useAuth'

/** three.js only ships when a map is opened. */
const MapPage = lazy(() => import('./pages/MapPage').then((m) => ({ default: m.MapPage })))
const ControlPage = lazy(() => import('./pages/ControlPage').then((m) => ({ default: m.ControlPage })))

function RequireAuth({ role, children }: { role: 'realtor' | 'visitor'; children: ReactNode }) {
  const { session } = useAuth()
  if (!session) return <Navigate to={role === 'realtor' ? '/signin' : '/'} replace />
  if (role === 'realtor' && session.role !== 'realtor') return <Navigate to="/signin" replace />
  return children
}

export default function App() {
  const location = useLocation()
  return (
    <AnimatePresence mode="wait" initial={false}>
      <Routes location={location} key={location.pathname}>
        <Route path="/" element={<ToursPage />} />
        <Route path="/signin" element={<RealtorSignInPage />} />
        <Route path="/connect" element={<Navigate to="/" replace />} />
        <Route path="/realtor/connect" element={<Navigate to="/signin" replace />} />
        <Route
          path="/realtor"
          element={
            <RequireAuth role="realtor">
              <LibraryPage />
            </RequireAuth>
          }
        />
        <Route
          path="/onboard"
          element={
            <RequireAuth role="realtor">
              <OnboardingPage />
            </RequireAuth>
          }
        />
        <Route
          path="/control/:roomId"
          element={
            <Suspense fallback={null}>
              <RequireAuth role="visitor">
                <ControlPage view="user" />
              </RequireAuth>
            </Suspense>
          }
        />
        <Route
          path="/user/:roomId"
          element={
            <Suspense fallback={null}>
              <RequireAuth role="visitor">
                <ControlPage view="user" />
              </RequireAuth>
            </Suspense>
          }
        />
        <Route
          path="/realtor/control/:roomId"
          element={
            <Suspense fallback={null}>
              <RequireAuth role="realtor">
                <ControlPage view="realtor" />
              </RequireAuth>
            </Suspense>
          }
        />
        <Route
          path="/map/:mapId"
          element={
            <Suspense fallback={null}>
              <RequireAuth role="realtor">
                <MapPage />
              </RequireAuth>
            </Suspense>
          }
        />
        <Route path="*" element={<PlaceholderPage title="Page not found" sprint={0} />} />
      </Routes>
    </AnimatePresence>
  )
}

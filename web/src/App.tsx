import { AnimatePresence } from 'framer-motion'
import { Suspense, lazy, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { LibraryPage } from './pages/LibraryPage'
import { OnboardingPage } from './pages/OnboardingPage'
import { PlaceholderPage } from './pages/PlaceholderPage'
import { RealtorSignInPage } from './pages/RealtorSignInPage'
import { ToursPage } from './pages/ToursPage'
import { BookingsPage } from './pages/BookingsPage'
import { useAuth } from './auth/useAuth'

/** three.js only ships when a map is opened. */
const MapPage = lazy(() => import('./pages/MapPage').then((m) => ({ default: m.MapPage })))
const ControlPage = lazy(() => import('./pages/ControlPage').then((m) => ({ default: m.ControlPage })))
const LiveTelemetryPage = lazy(() => import('./pages/LiveTelemetryPage').then((m) => ({ default: m.LiveTelemetryPage })))
const LiveDrivePage = lazy(() => import('./pages/LiveDrivePage').then((m) => ({ default: m.LiveDrivePage })))
const SshCameraPage = lazy(() => import('./pages/SshCameraPage').then((m) => ({ default: m.SshCameraPage })))
const sshCameraEnabled = import.meta.env.DEV && ['ssh', 'livekit'].includes(import.meta.env.VITE_ROBOT_TRANSPORT)

function VisitorControl() {
  if (sshCameraEnabled) return <SshCameraPage />
  return import.meta.env.VITE_ROBOT_TRANSPORT === 'relay' ? <ControlPage view="user" /> : <LiveDrivePage />
}

function RequireAuth({ role, children }: { role: 'realtor' | 'visitor'; children: ReactNode }) {
  const { session, isLoading } = useAuth()
  if (isLoading) {
    return <div className="grid min-h-dvh place-items-center text-sm text-ink-2">Checking your session…</div>
  }
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
        <Route path="/bookings" element={<BookingsPage />} />
        <Route path="/connect" element={<Navigate to="/" replace />} />
        <Route path="/realtor/live/:roomId" element={<Suspense fallback={null}><RequireAuth role="realtor"><LiveTelemetryPage /></RequireAuth></Suspense>} />
        <Route path="/user/:roomId/live" element={<Suspense fallback={null}><RequireAuth role="visitor">{sshCameraEnabled ? <SshCameraPage /> : <LiveDrivePage />}</RequireAuth></Suspense>} />
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
                <VisitorControl />
              </RequireAuth>
            </Suspense>
          }
        />
        <Route
          path="/user/:roomId"
          element={
            <Suspense fallback={null}>
              <RequireAuth role="visitor">
                <VisitorControl />
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

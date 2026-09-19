import { AnimatePresence } from 'framer-motion'
import { Suspense, lazy } from 'react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { LibraryPage } from './pages/LibraryPage'
import { OnboardingPage } from './pages/OnboardingPage'
import { PlaceholderPage } from './pages/PlaceholderPage'
import { ConnectPage } from './pages/ConnectPage'

/** three.js only ships when a map is opened. */
const MapPage = lazy(() => import('./pages/MapPage').then((m) => ({ default: m.MapPage })))
const ControlPage = lazy(() => import('./pages/ControlPage').then((m) => ({ default: m.ControlPage })))

export default function App() {
  const location = useLocation()
  return (
    <AnimatePresence mode="wait" initial={false}>
      <Routes location={location} key={location.pathname}>
        <Route path="/" element={<LibraryPage />} />
        <Route path="/onboard" element={<OnboardingPage />} />
        <Route path="/connect" element={<ConnectPage />} />
        <Route
          path="/control/:roomId"
          element={
            <Suspense fallback={null}>
              <ControlPage />
            </Suspense>
          }
        />
        <Route
          path="/map/:mapId"
          element={
            <Suspense fallback={null}>
              <MapPage />
            </Suspense>
          }
        />
        <Route path="*" element={<PlaceholderPage title="Page not found" sprint={0} />} />
      </Routes>
    </AnimatePresence>
  )
}

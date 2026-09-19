import { AnimatePresence } from 'framer-motion'
import { Suspense, lazy } from 'react'
import { Route, Routes, useLocation } from 'react-router-dom'
import { LibraryPage } from './pages/LibraryPage'
import { OnboardingPage } from './pages/OnboardingPage'
import { PlaceholderPage } from './pages/PlaceholderPage'

/** three.js only ships when a map is opened. */
const MapPage = lazy(() => import('./pages/MapPage').then((m) => ({ default: m.MapPage })))

export default function App() {
  const location = useLocation()
  return (
    <AnimatePresence mode="wait" initial={false}>
      <Routes location={location} key={location.pathname}>
        <Route path="/" element={<LibraryPage />} />
        <Route path="/onboard" element={<OnboardingPage />} />
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

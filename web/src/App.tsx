import { AnimatePresence } from 'framer-motion'
import { Route, Routes, useLocation } from 'react-router-dom'
import { LibraryPage } from './pages/LibraryPage'
import { PlaceholderPage } from './pages/PlaceholderPage'

export default function App() {
  const location = useLocation()
  return (
    <AnimatePresence mode="wait" initial={false}>
      <Routes location={location} key={location.pathname}>
        <Route path="/" element={<LibraryPage />} />
        <Route path="/onboard" element={<PlaceholderPage title="Onboarding & pairing" sprint={3} />} />
        <Route path="/map/:mapId" element={<PlaceholderPage title="Map view" sprint={4} />} />
        <Route path="*" element={<PlaceholderPage title="Page not found" sprint={0} />} />
      </Routes>
    </AnimatePresence>
  )
}

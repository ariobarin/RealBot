// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeAll, expect, it, vi } from 'vitest'
import type { ComponentType } from 'react'

const mocks = vi.hoisted(() => ({
  session: vi.fn(), snapshot: vi.fn(), fetch: vi.fn(),
}))
vi.mock('../lib/visitorSession', () => ({ requestVisitorSession: mocks.session, visitorAccessCode: () => '' }))
vi.mock('../lib/visitorLiveKit', () => ({ VisitorLiveKit: class {
  changed: (view: unknown) => void
  constructor(changed: (view: unknown) => void) { this.changed = changed }
  async connect() { this.changed({ connection: 'connected', robotOnline: true }) }
  disconnect() {}
  stopAction() {}
  readActionPoints = mocks.snapshot
} }))
vi.mock('../components/control/RemoteFreeCam', () => ({ RemoteFreeCam: () => null }))
vi.mock('../components/map/LiveSlamMap', () => ({ LiveSlamMap: () => null }))
let RobotCameraPanel: ComponentType<{ setup: boolean }>
beforeAll(async () => {
  vi.stubEnv('PROD', true)
  vi.stubEnv('VITE_ROBOT_TRANSPORT', 'livekit')
  RobotCameraPanel = (await import('./SshCameraPage')).RobotCameraPanel
})
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks() })

it('setup selects a robot by code and reads its recorder without treating the saved map as a robot', async () => {
  vi.stubGlobal('fetch', mocks.fetch)
  mocks.session.mockResolvedValue({ roomId: '0188', robotRole: 'act' })
  mocks.snapshot.mockResolvedValue({ recording: { state: 'idle', message: 'Recorder ready' }, landmarks: [] })
  render(<MemoryRouter initialEntries={['/setup/saved-room']}><Routes>
    <Route path="/setup/:roomId" element={<RobotCameraPanel setup />} />
  </Routes></MemoryRouter>)
  fireEvent.change(screen.getByLabelText('Robot access code'), { target: { value: '0188' } })
  fireEvent.click(screen.getByRole('button', { name: /^Connect$/ }))
  await waitFor(() => expect(mocks.session).toHaveBeenCalledWith('0188', undefined, expect.any(AbortSignal)))
  const summary = await screen.findByText('Action points')
  const details = summary.closest('details')!
  details.open = true
  fireEvent(details, new Event('toggle'))
  expect((await screen.findByText('Recorder ready')).textContent).toBe('Recorder ready')
  expect(screen.getByRole('link', { name: 'View saved room map' }).getAttribute('href')).toBe('/map/saved-room')
  expect(mocks.snapshot).toHaveBeenCalled()
  expect(mocks.fetch).not.toHaveBeenCalled()
})

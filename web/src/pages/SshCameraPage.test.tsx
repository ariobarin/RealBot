// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeAll, expect, it, vi } from 'vitest'
import type { ComponentType } from 'react'

const mocks = vi.hoisted(() => ({
  session: vi.fn(), snapshot: vi.fn(), fetch: vi.fn(), startAction: vi.fn(), stopAction: vi.fn(),
}))
vi.mock('../lib/visitorSession', () => ({ requestVisitorSession: mocks.session, visitorAccessCode: () => '' }))
vi.mock('../lib/visitorLiveKit', () => ({ VisitorLiveKit: class {
  changed: (view: unknown) => void
  actions: (view: unknown) => void
  constructor(changed: (view: unknown) => void, _map: unknown, _freecam: unknown, actions: (view: unknown) => void) {
    this.changed = changed
    this.actions = actions
  }
  async connect() {
    this.changed({ connection: 'connected', robotOnline: true })
    this.actions({ points: [], state: { available: true, owned: false, phase: 'idle', attempt: '' } })
  }
  disconnect() {}
  startAction = mocks.startAction
  stopAction = mocks.stopAction
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

it('runs ACT from the button without a waypoint and keeps Stop available during startup', async () => {
  mocks.session.mockResolvedValue({ roomId: '0188', robotRole: 'act' })
  let finish!: () => void
  mocks.startAction.mockImplementation(() => new Promise<void>(resolve => { finish = resolve }))
  render(<MemoryRouter><RobotCameraPanel setup /></MemoryRouter>)
  fireEvent.change(screen.getByLabelText('Robot access code'), { target: { value: '0188' } })
  fireEvent.click(screen.getByRole('button', { name: /^Connect$/ }))
  const run = await screen.findByRole('button', { name: 'Run ACT' })
  expect(mocks.startAction).not.toHaveBeenCalled()
  fireEvent.click(run)
  fireEvent.click(run)
  expect(mocks.startAction).toHaveBeenCalledExactlyOnceWith('policy:electric_box')
  expect(screen.getByRole('dialog', { name: 'Right-arm camera' })).toBeTruthy()
  fireEvent.click(screen.getAllByRole('button', { name: 'Stop / hold' })[0])
  expect(mocks.stopAction).toHaveBeenCalledOnce()
  finish()
  await waitFor(() => expect((run as HTMLButtonElement).disabled).toBe(false))
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

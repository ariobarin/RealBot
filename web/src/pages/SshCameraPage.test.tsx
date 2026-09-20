// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, beforeAll, expect, it, vi } from 'vitest'
import type { ComponentType } from 'react'

const mocks = vi.hoisted(() => ({
  changed: (_view: unknown) => {}, session: vi.fn(), snapshot: vi.fn(), fetch: vi.fn(), disconnect: vi.fn(), startAction: vi.fn(), stopAction: vi.fn(), runScript: vi.fn(),
}))
vi.mock('../lib/visitorSession', () => ({ requestVisitorSession: mocks.session, visitorAccessCode: () => '' }))
vi.mock('../lib/visitorLiveKit', () => ({ VisitorLiveKit: class {
  changed: (view: unknown) => void
  actions: (view: unknown) => void
  constructor(changed: (view: unknown) => void, _map: unknown, _freecam: unknown, actions: (view: unknown) => void) {
    this.changed = changed
    mocks.changed = changed
    this.actions = actions
  }
  async connect() {
    this.changed({ connection: 'connected', robotOnline: true })
    this.actions({ points: [], state: { available: true, owned: false, phase: 'idle', attempt: '' } })
  }
  disconnect() { mocks.disconnect(); this.changed({ connection: 'disconnected', robotOnline: false }) }
  startAction = mocks.startAction
  stopAction = mocks.stopAction
  runScript = mocks.runScript
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

it('runs the three demo scripts from the panel and keeps Stop available during a run', async () => {
  mocks.session.mockResolvedValue({ roomId: '0188', robotRole: 'act' })
  let finish!: (value: { reason: string }) => void
  mocks.runScript.mockImplementation(() => new Promise<{ reason: string }>(resolve => { finish = resolve }))
  render(<MemoryRouter><RobotCameraPanel setup /></MemoryRouter>)
  fireEvent.change(screen.getByLabelText('Robot access code'), { target: { value: '0188' } })
  fireEvent.click(screen.getByRole('button', { name: /^Connect$/ }))
  const go = await screen.findByRole('button', { name: 'Go' })
  const init = screen.getByRole('button', { name: 'Initialize (guided)' })
  expect(mocks.runScript).not.toHaveBeenCalled()
  fireEvent.click(init)
  expect(mocks.runScript).toHaveBeenCalledExactlyOnceWith('init')
  expect(screen.getByText(/Wait for READY before Go/)).toBeTruthy()
  finish({ reason: 'READY: teleop homed, policy loaded' })
  await waitFor(() => expect((init as HTMLButtonElement).disabled).toBe(false))

  // Go opens the wrist camera, and a double click cannot start two attempts.
  fireEvent.click(go)
  fireEvent.click(go)
  expect(mocks.runScript).toHaveBeenLastCalledWith('go')
  expect(mocks.runScript).toHaveBeenCalledTimes(2)
  expect(screen.getByRole('dialog', { name: 'Right-arm camera' })).toBeTruthy()

  // Stop stays clickable while Go is still in flight.
  fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
  expect(mocks.runScript).toHaveBeenLastCalledWith('stop')
  finish({ reason: 'stop finished' })
  await waitFor(() => expect((go as HTMLButtonElement).disabled).toBe(false))
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
  fireEvent.click(screen.getByRole('button', { name: 'Stop action points' }))
  await waitFor(() => expect(mocks.disconnect).toHaveBeenCalledOnce())
  expect(screen.queryByText('Action points')).toBeNull()
  expect(screen.getByText('Action points stopped. Robot connection released.')).toBeTruthy()
  expect(screen.getByLabelText('Robot access code')).toBeTruthy()
  expect(mocks.session).toHaveBeenCalledTimes(1)
})

it('can reconnect when the robot disappears without a connection error', async () => {
  mocks.session.mockResolvedValue({ roomId: '0188', robotRole: 'act' })
  render(<MemoryRouter><RobotCameraPanel setup={false} /></MemoryRouter>)
  fireEvent.change(screen.getByLabelText('Robot access code'), { target: { value: '0188' } })
  fireEvent.click(screen.getByRole('button', { name: /^Connect$/ }))
  await screen.findByRole('button', { name: 'Go' })
  act(() => mocks.changed({ connection: 'connected', robotOnline: false }))
  const reconnect = screen.getByRole('button', { name: 'Reconnect' })
  expect(screen.getByText('Robot offline. Turn it on, then reconnect.')).toBeTruthy()
  fireEvent.click(reconnect)
  await waitFor(() => expect(mocks.session).toHaveBeenCalledTimes(2))
  expect(mocks.disconnect).toHaveBeenCalledOnce()
  expect(mocks.runScript).not.toHaveBeenCalled()
})

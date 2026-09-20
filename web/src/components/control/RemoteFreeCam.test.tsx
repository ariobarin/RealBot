// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { RemoteFreeCam } from './RemoteFreeCam'
import type { VisitorLiveKit } from '../../lib/visitorLiveKit'

afterEach(() => { cleanup(); vi.useRealTimers() })

it('continuously refreshes held keys, stops on release or blur, and closes on unmount', () => {
  vi.useFakeTimers()
  const client = { freeCamPulse: vi.fn(), freeCamCommand: vi.fn().mockResolvedValue(undefined) }
  const result = render(<RemoteFreeCam client={client as unknown as VisitorLiveKit}
    state={{ available: true, viewing: true, phase: 'active', ready: true }} onViewing={() => {}} />)
  fireEvent.keyDown(window, { key: 'ArrowRight' })
  act(() => { vi.advanceTimersByTime(350) })
  expect(client.freeCamPulse.mock.calls).toEqual([[1, 0], [1, 0], [1, 0]])
  fireEvent.keyUp(window, { key: 'ArrowRight' })
  act(() => { vi.advanceTimersByTime(100) })
  expect(client.freeCamPulse).toHaveBeenLastCalledWith(0, 0)
  fireEvent.keyDown(window, { key: 'w' })
  fireEvent.blur(window)
  expect(client.freeCamCommand).toHaveBeenCalledWith('stop')
  act(() => { vi.advanceTimersByTime(100) })
  expect(client.freeCamPulse).toHaveBeenLastCalledWith(0, 0)
  expect((screen.getByText('Release torque') as HTMLButtonElement).disabled).toBe(true)
  result.unmount()
  expect(client.freeCamCommand).toHaveBeenLastCalledWith('close')
})

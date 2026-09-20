// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ActionLocations } from './ActionLocations'
import type { ActionView, VisitorLiveKit } from '../../lib/visitorLiveKit'

afterEach(cleanup)
const view: ActionView = { points: [
  { id: 'saved-box', action_id: 'electric_box', label: 'Electric box', x: .25, y: .5 },
], state: { available: true, owned: false, phase: 'idle', attempt: '' } }

it('shows locations without starting a policy on click and hides them in hand view', () => {
  const client = { startAction: vi.fn(), stopAction: vi.fn() }
  const props = { client: client as unknown as VisitorLiveKit, view }
  const result = render(<ActionLocations {...props} disabled={false} />)
  expect(screen.queryByRole('button', { name: 'Run Electric box' })).toBeNull()
  const marker = screen.getByLabelText('Electric box')
  expect(marker.style.left).toBe('25%')
  fireEvent.click(marker)
  expect(client.startAction).not.toHaveBeenCalled()
  result.rerender(<ActionLocations {...props} disabled />)
  expect(screen.queryByLabelText('Electric box')).toBeNull()
})

it('stops on blur, Escape, hidden tab, and unmount', () => {
  const client = { stopAction: vi.fn() }
  const result = render(<ActionLocations client={client as unknown as VisitorLiveKit} view={view} disabled={false} />)
  fireEvent.blur(window)
  fireEvent.keyDown(window, { key: 'Escape' })
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(true)
  fireEvent(document, new Event('visibilitychange'))
  result.unmount()
  expect(client.stopAction).toHaveBeenCalledTimes(4)
  vi.restoreAllMocks()
})

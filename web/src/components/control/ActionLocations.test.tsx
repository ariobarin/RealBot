// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ActionLocations } from './ActionLocations'
import type { ActionView, VisitorLiveKit } from '../../lib/visitorLiveKit'

afterEach(cleanup)
const view: ActionView = { points: [
  { id: 'saved-box', action_id: 'electric_box', label: 'Electric box', x: .25, y: .5 },
  { id: 'saved-switch', action_id: 'light_switch', label: 'Light switch', x: .5, y: .5 },
], state: { available: true, owned: false, phase: 'idle', attempt: '' } }

it('clicks the saved ID once, leaves unsupported actions disabled, and stops on blur', async () => {
  let finish!: () => void
  const client = { startAction: vi.fn(() => new Promise<void>(resolve => { finish = resolve })), stopAction: vi.fn() }
  const onStart = vi.fn()
  const result = render(<ActionLocations client={client as unknown as VisitorLiveKit} view={view} disabled={false} onStart={onStart} />)
  const button = screen.getByRole('button', { name: 'Run Electric box' })
  expect(button.style.left).toBe('25%')
  fireEvent.click(button)
  fireEvent.click(button)
  expect(client.startAction).toHaveBeenCalledExactlyOnceWith('saved-box')
  expect(onStart).toHaveBeenCalledOnce()
  expect((screen.getByRole('button', { name: 'Run Light switch' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.blur(window)
  expect(client.stopAction).toHaveBeenCalledOnce()
  finish()
  await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false))
  result.unmount()
  expect(client.stopAction).toHaveBeenCalledTimes(2)
})

it('hides clickable markers when viewing the hand and exposes policy rejection', async () => {
  const client = { startAction: vi.fn().mockRejectedValue(new Error('Arms already in use')), stopAction: vi.fn() }
  const props = { client: client as unknown as VisitorLiveKit, view, onStart: vi.fn() }
  const result = render(<ActionLocations {...props} disabled />)
  expect(screen.queryByRole('button', { name: 'Run Electric box' })).toBeNull()
  result.rerender(<ActionLocations {...props} disabled={false} />)
  fireEvent.click(screen.getByRole('button', { name: 'Run Electric box' }))
  expect((await screen.findByRole('alert')).textContent).toContain('Arms already in use')
})

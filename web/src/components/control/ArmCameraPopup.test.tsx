// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import type { RemoteVideoTrack } from 'livekit-client'
import { ArmCameraPopup } from './ArmCameraPopup'

afterEach(cleanup)

it('attaches the right track, hides stale video, and detaches on close', () => {
  const track = { attach: vi.fn(), detach: vi.fn() }
  const props = { track: track as unknown as RemoteVideoTrack, onClose: vi.fn(), onStop: vi.fn() }
  const result = render(<ArmCameraPopup {...props} fresh />)
  const video = screen.getByLabelText('Live right-arm camera')
  expect(track.attach).toHaveBeenCalledWith(video)
  result.rerender(<ArmCameraPopup {...props} fresh={false} />)
  expect(track.detach).toHaveBeenCalledWith(video)
  expect(screen.queryByLabelText('Live right-arm camera')).toBeNull()
  expect(screen.getByRole('status').textContent).toContain('unavailable')
  result.rerender(<ArmCameraPopup {...props} fresh />)
  result.unmount()
  expect(track.detach).toHaveBeenCalledTimes(2)
})

it('keeps closing the view separate from stopping ACT, even without video', () => {
  const onClose = vi.fn(), onStop = vi.fn()
  render(<ArmCameraPopup fresh={false} onClose={onClose} onStop={onStop} />)
  fireEvent.click(screen.getByRole('button', { name: 'Close right-arm camera' }))
  expect(onClose).toHaveBeenCalledOnce()
  expect(onStop).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: 'Stop / hold' }))
  expect(onStop).toHaveBeenCalledOnce()
})

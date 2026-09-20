// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { LiveSlamMap } from './LiveSlamMap'

vi.mock('@react-three/fiber', () => ({ Canvas: () => <canvas aria-label="SLAM point cloud" /> }))
vi.mock('@react-three/drei', () => ({ Bounds: () => null, OrbitControls: () => null }))
vi.mock('./ScanPointCloud', () => ({ ColoredPointCloud: () => null }))
vi.mock('./RobotMarker', () => ({ RobotMarker: () => null }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('keeps the minimap visible while waiting, empty, receiving, and losing SLAM data', () => {
  const fetch = vi.fn()
  vi.stubGlobal('fetch', fetch)
  const view = render(<LiveSlamMap snapshot={null} />)
  expect(screen.getByRole('status').textContent).toBe('Waiting for SLAM data')
  const empty = { positions: [], colors: [], pointSize: 0.02, totalPoints: 0, robot: { x: 0, y: 0, heading: 0 } }
  view.rerender(<LiveSlamMap snapshot={empty} />)
  expect(screen.getByRole('status').textContent).toBe('No map points yet')
  expect(screen.queryByLabelText('SLAM point cloud')).toBeNull()
  view.rerender(<LiveSlamMap snapshot={{ ...empty, positions: [0, 0, 0], colors: [255, 255, 255], totalPoints: 1 }} />)
  expect(screen.getByLabelText('SLAM point cloud')).toBeTruthy()
  expect(screen.queryByRole('status')).toBeNull()
  view.rerender(<LiveSlamMap snapshot={null} />)
  expect(screen.getByRole('status').textContent).toBe('Waiting for SLAM data')
  expect(screen.queryByLabelText('SLAM point cloud')).toBeNull()
  expect(fetch).not.toHaveBeenCalled()
})

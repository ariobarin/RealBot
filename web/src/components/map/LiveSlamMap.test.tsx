// @vitest-environment jsdom
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { LiveSlamMap } from './LiveSlamMap'
import { mapCloud } from '../../lib/liveSlamMap'
import { Box3, BufferGeometry, Float32BufferAttribute, Vector3 } from 'three'

vi.mock('@react-three/fiber', () => ({ Canvas: () => <canvas aria-label="SLAM point cloud" /> }))
vi.mock('@react-three/drei', () => ({ Bounds: () => null, OrbitControls: () => null }))
vi.mock('./ScanPointCloud', () => ({ ColoredPointCloud: () => null }))
vi.mock('./RobotMarker', () => ({ RobotMarker: () => null }))
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

it('keeps invalid mapping coordinates out of camera bounds and preserves matching colors', () => {
  const cloud = mapCloud({
    positions: [1, 0, 2, 64424508, 0.045, -64424508, NaN, 0, 0, 3, 1, 4, -43, 1, 98],
    colors: [10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 110, 120, 130, 140, 150],
    pointSize: 0.03, totalPoints: 5, robot: { x: 0, y: 0, heading: 0 },
  })
  const geometry = new BufferGeometry().setAttribute('position', new Float32BufferAttribute(cloud.positions, 3))
  geometry.computeBoundingBox()
  expect((geometry.boundingBox as Box3).getSize(new Vector3()).toArray()).toEqual([2, 1, 2])
  expect([...cloud.colors]).toEqual([10, 20, 30, 100, 110, 120])
  geometry.dispose()
  const moved = mapCloud({ positions: [100, 0, -100], colors: [1, 2, 3], pointSize: 0.03,
    totalPoints: 1, robot: { x: 100, y: 100, heading: 0 } })
  expect([...moved.positions]).toEqual([100, 0, -100])
})

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

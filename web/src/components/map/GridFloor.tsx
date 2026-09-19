import { useEffect, useMemo } from 'react'
import { DataTexture, NearestFilter, RGBAFormat, SRGBColorSpace } from 'three'
import type { ThreeEvent } from '@react-three/fiber'
import type { GridMap } from '../../lib/grid'
import { floorTexels, frameGrid } from '../../lib/scene'

const PALETTE = { floor: '#f3efe8', wall: '#2b2b2b', unknown: '#ffffff' }

interface GridFloorProps {
  grid: GridMap
  onPick: (e: ThreeEvent<MouseEvent>) => void
}

/** The occupancy grid painted onto a single plane; one texel per SLAM cell. */
export function GridFloor({ grid, onPick }: GridFloorProps) {
  const texture = useMemo(() => {
    const { data, width, height } = floorTexels(grid, PALETTE)
    const t = new DataTexture(data, width, height, RGBAFormat)
    t.magFilter = NearestFilter
    t.minFilter = NearestFilter
    t.colorSpace = SRGBColorSpace
    t.needsUpdate = true
    return t
  }, [grid])
  useEffect(() => () => texture.dispose(), [texture])

  const { center, width, depth } = useMemo(() => frameGrid(grid), [grid])

  return (
    <mesh
      position={[center[0], 0, center[2]]}
      rotation={[-Math.PI / 2, 0, 0]}
      receiveShadow
      onClick={onPick}
      name="floor"
    >
      <planeGeometry args={[width, depth]} />
      <meshStandardMaterial map={texture} transparent roughness={0.95} metalness={0} />
    </mesh>
  )
}

import { useFrame } from '@react-three/fiber'
import { useMemo, useRef } from 'react'
import { InstancedMesh, Object3D } from 'three'
import type { GridMap } from '../../lib/grid'
import { WALL_HEIGHT, frameGrid, wallInstances } from '../../lib/scene'

const RISE_S = 0.9
const RADIAL_S = 0.7
const scratch = new Object3D()

/** Every obstacle cell extruded into a small column; rises radially from the map centre on mount. Re-key on grid change. */
export function GridWalls({ grid }: { grid: GridMap }) {
  const ref = useRef<InstancedMesh>(null)
  const positions = useMemo(() => wallInstances(grid), [grid])
  const count = positions.length / 3
  const { center, radius } = useMemo(() => frameGrid(grid), [grid])
  const t0 = useRef<number | null>(null)
  const done = useRef(false)

  const setMatrices = (progress: (i: number) => number) => {
    const mesh = ref.current
    if (!mesh) return
    for (let k = 0; k < count; k++) {
      const x = positions[k * 3]
      const y = positions[k * 3 + 1]
      const z = positions[k * 3 + 2]
      const s = progress(k)
      scratch.position.set(x, y * s, z)
      scratch.scale.set(1, Math.max(s, 0.0001), 1)
      scratch.updateMatrix()
      mesh.setMatrixAt(k, scratch.matrix)
    }
    mesh.instanceMatrix.needsUpdate = true
  }

  useFrame(({ clock }) => {
    if (done.current) return
    if (t0.current === null) t0.current = clock.elapsedTime
    const t = clock.elapsedTime - t0.current
    let allDone = true
    setMatrices((k) => {
      const dx = positions[k * 3] - center[0]
      const dz = positions[k * 3 + 2] - center[2]
      const delay = (Math.hypot(dx, dz) / Math.max(radius, 0.001)) * RADIAL_S
      const u = Math.min(Math.max((t - delay) / RISE_S, 0), 1)
      if (u < 1) allDone = false
      return 1 - Math.pow(1 - u, 3)
    })
    if (allDone) done.current = true
  })

  const r = grid.resolution

  return (
    <instancedMesh ref={ref} args={[undefined, undefined, count]} castShadow receiveShadow>
      <boxGeometry args={[r, WALL_HEIGHT, r]} />
      <meshStandardMaterial color="#3a3a3a" roughness={0.7} metalness={0.05} />
    </instancedMesh>
  )
}

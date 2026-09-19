import { useFrame } from '@react-three/fiber'
import { useRef } from 'react'
import { Group, Mesh, MeshBasicMaterial } from 'three'
import { worldToScene } from '../../lib/grid'
import type { Waypoint } from '../../store/useViewStore'

const RIPPLE_S = 0.8

function WaypointPin({ wp }: { wp: Waypoint }) {
  const ripple = useRef<Mesh>(null)
  const pin = useRef<Group>(null)
  const born = useRef<number | null>(null)
  const [sx, , sz] = worldToScene(wp.x, wp.y)

  useFrame(({ clock }) => {
    if (born.current === null) born.current = clock.elapsedTime
    const u = Math.min((clock.elapsedTime - born.current) / RIPPLE_S, 1)
    if (ripple.current) {
      ripple.current.scale.setScalar(0.2 + u * 2.2)
      ;(ripple.current.material as MeshBasicMaterial).opacity = (1 - u) * 0.6
    }
    if (pin.current) {
      const s = 1 - Math.pow(1 - Math.min(u * 1.6, 1), 3)
      pin.current.scale.setScalar(s)
      pin.current.position.y = 0.05 + Math.sin(clock.elapsedTime * 3 + wp.id) * 0.015
    }
  })

  return (
    <group position={[sx, 0, sz]}>
      <mesh ref={ripple} rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.006, 0]}>
        <ringGeometry args={[0.28, 0.32, 48]} />
        <meshBasicMaterial color="#ff385c" transparent opacity={0.6} depthWrite={false} />
      </mesh>
      <group ref={pin} scale={0}>
        <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.004, 0]}>
          <circleGeometry args={[0.12, 32]} />
          <meshBasicMaterial color="#ff385c" />
        </mesh>
        <mesh rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.008, 0]}>
          <circleGeometry args={[0.05, 24]} />
          <meshBasicMaterial color="#ffffff" />
        </mesh>
      </group>
    </group>
  )
}

export function Waypoints({ waypoints }: { waypoints: Waypoint[] }) {
  return (
    <>
      {waypoints.map((wp) => (
        <WaypointPin key={wp.id} wp={wp} />
      ))}
    </>
  )
}

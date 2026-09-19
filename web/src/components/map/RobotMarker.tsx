import { useFrame } from '@react-three/fiber'
import { useRef } from 'react'
import { Group, Mesh } from 'three'
import { worldToScene } from '../../lib/grid'

interface RobotMarkerProps {
  /** World pose (metres, radians). */
  x: number
  y: number
  heading: number
}

/** Stub bracketbot pose. Bobs gently and pulses a ring so it reads as "live". */
export function RobotMarker({ x, y, heading }: RobotMarkerProps) {
  const body = useRef<Group>(null)
  const ring = useRef<Mesh>(null)
  const [sx, , sz] = worldToScene(x, y)

  useFrame(({ clock }) => {
    const t = clock.elapsedTime
    if (body.current) body.current.position.y = 0.02 + Math.sin(t * 2) * 0.02
    if (ring.current) {
      const u = (t % 1.8) / 1.8
      ring.current.scale.setScalar(0.6 + u * 1.6)
      ;(ring.current.material as { opacity: number }).opacity = (1 - u) * 0.55
    }
  })

  return (
    <group position={[sx, 0, sz]} rotation={[0, heading, 0]}>
      <mesh ref={ring} rotation={[-Math.PI / 2, 0, 0]} position={[0, 0.005, 0]}>
        <ringGeometry args={[0.3, 0.36, 48]} />
        <meshBasicMaterial color="#ff385c" transparent opacity={0.5} depthWrite={false} />
      </mesh>
      <group ref={body}>
        <mesh position={[0, 0.16, 0]} castShadow>
          <cylinderGeometry args={[0.18, 0.2, 0.3, 32]} />
          <meshStandardMaterial color="#ff385c" roughness={0.45} />
        </mesh>
        <mesh position={[0, 0.35, 0]} castShadow>
          <sphereGeometry args={[0.1, 24, 16]} />
          <meshStandardMaterial color="#2b2b2b" roughness={0.4} />
        </mesh>
        <mesh position={[0.24, 0.16, 0]} rotation={[0, 0, -Math.PI / 2]}>
          <coneGeometry args={[0.07, 0.14, 16]} />
          <meshStandardMaterial color="#ffffff" roughness={0.5} />
        </mesh>
      </group>
    </group>
  )
}

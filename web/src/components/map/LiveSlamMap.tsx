import { Bounds, OrbitControls } from '@react-three/drei'
import { Canvas } from '@react-three/fiber'
import { useEffect, useMemo, useState } from 'react'
import { ColoredPointCloud } from './ScanPointCloud'
import { RobotMarker } from './RobotMarker'

export interface MapSnapshot {
  positions: number[]
  colors: number[]
  pointSize: number
  totalPoints: number
  robot: { x: number; y: number; heading: number }
}

type MapCloud = Omit<MapSnapshot, 'positions' | 'colors'> & {
  positions: Float32Array
  colors: Uint8Array
}

export function LiveSlamMap({ snapshot }: { snapshot?: MapSnapshot | null }) {
  const [polled, setCloud] = useState<MapCloud | null>(null)
  const [error, setError] = useState('')
  const external = snapshot !== undefined
  const cloud = useMemo(() => external ? snapshot ? {
    ...snapshot, positions: new Float32Array(snapshot.positions), colors: new Uint8Array(snapshot.colors),
  } : null : polled, [external, snapshot, polled])
  useEffect(() => {
    if (external) return
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function refresh() {
      try {
        const response = await fetch('/robot-camera/slam-map', {
          signal: AbortSignal.any([abort.signal, AbortSignal.timeout(5000)]), cache: 'no-store',
        })
        const data = await response.json()
        if (!response.ok) throw new Error(data.detail || 'Map unavailable')
        const snapshot = data as MapSnapshot
        if (!abort.signal.aborted) {
          setCloud({ ...snapshot, positions: new Float32Array(snapshot.positions), colors: new Uint8Array(snapshot.colors) })
          setError('')
        }
      } catch (failure) {
        if (!abort.signal.aborted) setError(failure instanceof Error ? failure.message : 'Map unavailable')
      } finally {
        if (!abort.signal.aborted) timer = setTimeout(refresh, 1000)
      }
    }
    void refresh()
    return () => { abort.abort(); clearTimeout(timer) }
  }, [external])

  return (
    <section aria-label="Live SLAM map" className="relative h-full w-full overflow-hidden">
      {cloud && !error && (
        <Canvas camera={{ position: [5, 5, 5], fov: 50 }} dpr={[1, 1.5]} gl={{ alpha: true }}>
          <ambientLight intensity={2} />
          <Bounds fit clip margin={1.3}>
            <ColoredPointCloud positions={cloud.positions} colors={cloud.colors} pointSize={cloud.pointSize} />
          </Bounds>
          <RobotMarker {...cloud.robot} heading={cloud.robot.heading + Math.PI / 2} />
          <OrbitControls makeDefault minDistance={0.2} />
        </Canvas>
      )}
    </section>
  )
}

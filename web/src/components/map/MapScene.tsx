import { Grid, MapControls } from '@react-three/drei'
import { Canvas, useFrame, useThree, type ThreeEvent } from '@react-three/fiber'
import { useEffect, useMemo, useRef, type ComponentRef, type RefObject } from 'react'
import { MathUtils, Vector3 } from 'three'
import { botClient } from '../../lib/botClient'
import { FLOOR, cellToWorld, getCell, type GridMap } from '../../lib/grid'
import { frameGrid, sceneToCell } from '../../lib/scene'
import { useViewStore } from '../../store/useViewStore'
import { GridFloor } from './GridFloor'
import { GridWalls } from './GridWalls'
import { RobotMarker } from './RobotMarker'
import { Waypoints } from './Waypoints'

const BG = '#f7f7f7'

type MapControlsImpl = ComponentRef<typeof MapControls>

/** Flies the camera between the 3D (tilted) and 2D (top-down) framings; also handles "reset view". */
function CameraRig({ grid, controls }: { grid: GridMap; controls: RefObject<MapControlsImpl | null> }) {
  const { camera } = useThree()
  const mode = useViewStore((s) => s.mode)
  const resetSeq = useViewStore((s) => s.resetSeq)
  const frame = useMemo(() => frameGrid(grid), [grid])
  const goal = useRef<{ pos: Vector3; target: Vector3 } | null>(null)

  useEffect(() => {
    const c = new Vector3(...frame.center)
    const r = frame.radius
    const pos =
      mode === '3d'
        ? c.clone().add(new Vector3(0, r * 1.5, r * 1.5))
        : c.clone().add(new Vector3(0, r * 2.3, 0.0001))
    goal.current = { pos, target: c }
  }, [mode, resetSeq, frame])

  useFrame((_, dt) => {
    const g = goal.current
    const ctl = controls.current
    if (!g || !ctl) return
    const k = 1 - Math.exp(-dt * 7)
    camera.position.lerp(g.pos, k)
    ctl.target.lerp(g.target, k)
    ctl.update()
    if (camera.position.distanceTo(g.pos) < 0.01 && ctl.target.distanceTo(g.target) < 0.01) {
      camera.position.copy(g.pos)
      ctl.target.copy(g.target)
      ctl.update()
      goal.current = null
    }
  })

  return null
}

/** Pick a floor cell for the robot's stub home pose: nearest FLOOR cell to the bbox centre. */
const homePose = (m: GridMap) => {
  const ci = Math.round((m.bbox.iMin + m.bbox.iMax) / 2)
  const cj = Math.round((m.bbox.jMin + m.bbox.jMax) / 2)
  for (let r = 0; r < Math.max(m.width, m.height); r++) {
    for (let dj = -r; dj <= r; dj++) {
      for (let di = -r; di <= r; di++) {
        if (Math.max(Math.abs(di), Math.abs(dj)) !== r) continue
        if (getCell(m, ci + di, cj + dj) === FLOOR) {
          const [x, y] = cellToWorld(m, ci + di, cj + dj)
          return { x, y, heading: 0 }
        }
      }
    }
  }
  const [x, y] = cellToWorld(m, ci, cj)
  return { x, y, heading: 0 }
}

function Scene({ grid }: { grid: GridMap }) {
  const controls = useRef<MapControlsImpl>(null)
  const mode = useViewStore((s) => s.mode)
  const waypoints = useViewStore((s) => s.waypoints)
  const addWaypoint = useViewStore((s) => s.addWaypoint)
  const frame = useMemo(() => frameGrid(grid), [grid])
  const home = useMemo(() => homePose(grid), [grid])

  const onPick = (e: ThreeEvent<MouseEvent>) => {
    if (e.delta > 4) return // it was a drag
    const cell = sceneToCell(grid, e.point.x, e.point.z)
    if (!cell || getCell(grid, cell[0], cell[1]) !== FLOOR) return
    const [x, y] = cellToWorld(grid, cell[0], cell[1])
    addWaypoint(x, y)
    botClient.sendCommand({ type: 'add_wp', x: +x.toFixed(3), y: +y.toFixed(3) })
  }

  return (
    <>
      <color attach="background" args={[BG]} />
      <fog attach="fog" args={[BG, frame.radius * 2.5, frame.radius * 6]} />
      <hemisphereLight args={['#ffffff', '#e8e2d8', 1.85]} />
      <directionalLight
        position={[frame.center[0] + 8, 14, frame.center[2] + 6]}
        intensity={1.0}
        castShadow
        shadow-mapSize={[2048, 2048]}
        shadow-camera-left={-frame.radius}
        shadow-camera-right={frame.radius}
        shadow-camera-top={frame.radius}
        shadow-camera-bottom={-frame.radius}
        shadow-bias={-0.0005}
      />

      <GridFloor grid={grid} onPick={onPick} />
      <GridWalls key={grid.id} grid={grid} />
      <Grid
        position={[frame.center[0], 0.002, frame.center[2]]}
        args={[frame.width, frame.depth]}
        cellSize={1}
        cellThickness={0.6}
        cellColor="#e6e2db"
        sectionSize={5}
        sectionThickness={1}
        sectionColor="#d6d1c8"
        fadeDistance={frame.radius * 4}
        fadeStrength={1}
        followCamera={false}
      />
      <RobotMarker x={home.x} y={home.y} heading={home.heading} />
      <Waypoints waypoints={waypoints} />

      <MapControls
        ref={controls}
        makeDefault
        enableDamping
        dampingFactor={0.12}
        enableRotate={mode === '3d'}
        minDistance={1.5}
        maxDistance={frame.radius * 4}
        maxPolarAngle={MathUtils.degToRad(80)}
        screenSpacePanning={false}
      />
      <CameraRig grid={grid} controls={controls} />
    </>
  )
}

export function MapScene({ grid }: { grid: GridMap }) {
  const frame = useMemo(() => frameGrid(grid), [grid])
  const start: [number, number, number] = [
    frame.center[0],
    frame.radius * 1.5,
    frame.center[2] + frame.radius * 1.5,
  ]
  return (
    <Canvas
      shadows
      flat
      dpr={[1, 2]}
      camera={{ position: start, fov: 42, near: 0.1, far: frame.radius * 12 }}
      gl={{ antialias: true, powerPreference: 'high-performance' }}
      data-testid="map-canvas"
    >
      <Scene grid={grid} />
    </Canvas>
  )
}

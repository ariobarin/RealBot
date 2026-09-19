import { useEffect, useRef } from 'react'
import { FLOOR, OBSTACLE, type GridMap } from '../../lib/grid'
import type { NavigationTelemetry, RobotState } from '../../lib/remoteBotClient'

interface VisitorMiniMapProps {
  grid?: GridMap
  pose?: RobotState['pose']
  navigation?: NavigationTelemetry
}

function mapPoint(grid: GridMap, x: number, y: number) {
  const { iMin, iMax, jMin, jMax } = grid.bbox
  return {
    x: (x - grid.origin[0]) / grid.resolution - iMin,
    y: jMax - ((y - grid.origin[1]) / grid.resolution - jMin),
    inBounds:
      x >= grid.origin[0] + iMin * grid.resolution &&
      x <= grid.origin[0] + (iMax + 1) * grid.resolution &&
      y >= grid.origin[1] + jMin * grid.resolution &&
      y <= grid.origin[1] + (jMax + 1) * grid.resolution,
  }
}

export function VisitorMiniMap({ grid, pose, navigation }: VisitorMiniMapProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas || !grid) return
    const context = canvas.getContext('2d')
    if (!context) return

    const { iMin, iMax, jMin, jMax } = grid.bbox
    const width = iMax - iMin + 1
    const height = jMax - jMin + 1
    canvas.width = width
    canvas.height = height

    const image = context.createImageData(width, height)
    for (let j = jMin; j <= jMax; j += 1) {
      for (let i = iMin; i <= iMax; i += 1) {
        const cell = grid.cells[j * grid.width + i]
        const pixel = ((jMax - j) * width + (i - iMin)) * 4
        const shade = cell === OBSTACLE ? 37 : cell === FLOOR ? 231 : 103
        image.data[pixel] = shade
        image.data[pixel + 1] = cell === FLOOR ? 229 : shade
        image.data[pixel + 2] = cell === FLOOR ? 224 : shade
        image.data[pixel + 3] = cell === 0 ? 80 : 255
      }
    }
    context.putImageData(image, 0, 0)

    const route = navigation?.path ?? []
    if (route.length > 1) {
      context.beginPath()
      route.forEach((point, index) => {
        const mapped = mapPoint(grid, point.x, point.y)
        if (index === 0) context.moveTo(mapped.x, mapped.y)
        else context.lineTo(mapped.x, mapped.y)
      })
      context.strokeStyle = '#ff385c'
      context.lineWidth = 5
      context.lineCap = 'round'
      context.lineJoin = 'round'
      context.stroke()
    }

    if (navigation?.goal) {
      const goal = mapPoint(grid, navigation.goal.x, navigation.goal.y)
      if (goal.inBounds) {
        context.beginPath()
        context.arc(goal.x, goal.y, 7, 0, Math.PI * 2)
        context.fillStyle = '#ffffff'
        context.fill()
        context.lineWidth = 4
        context.strokeStyle = '#ff385c'
        context.stroke()
      }
    }

    if (pose) {
      const robot = mapPoint(grid, pose.x, pose.y)
      if (robot.inBounds) {
        context.save()
        context.translate(robot.x, robot.y)
        context.rotate(-pose.heading)
        context.beginPath()
        context.moveTo(11, 0)
        context.lineTo(-7, -7)
        context.lineTo(-4, 0)
        context.lineTo(-7, 7)
        context.closePath()
        context.fillStyle = '#ff385c'
        context.fill()
        context.lineWidth = 3
        context.strokeStyle = '#ffffff'
        context.stroke()
        context.restore()
      }
    }
  }, [grid, navigation, pose])

  const routeStatus = navigation
    ? navigation.status === 'replanning'
      ? 'Replanning…'
      : navigation.path.length > 1
        ? 'Active route'
        : 'Planning…'
    : 'Robot position'

  return (
    <aside
      data-testid="visitor-minimap"
      aria-label="Robot location and planned route"
      className="pointer-events-none absolute bottom-4 left-4 z-10 w-44 overflow-hidden rounded-2xl border border-white/20 bg-[#20242b]/90 shadow-lg backdrop-blur sm:w-56"
    >
      <div className="flex items-center justify-between px-3 py-2 text-white">
        <span className="text-xs font-semibold">Floor map</span>
        <span className="flex items-center gap-1.5 text-[10px] text-white/65">
          <span className={`size-1.5 rounded-full ${navigation ? 'bg-brand' : 'bg-white/45'}`} />
          {routeStatus}
        </span>
      </div>
      <div className="relative grid min-h-24 place-items-center overflow-hidden bg-[#676767]/40">
        {grid ? (
          <canvas ref={canvasRef} className="block h-auto max-h-36 w-full" />
        ) : (
          <span className="text-xs text-white/60">Loading map…</span>
        )}
      </div>
    </aside>
  )
}

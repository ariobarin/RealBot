import {
  FLOOR,
  OBSTACLE,
  UNKNOWN,
  bboxSizeMetres,
  cellToWorld,
  getCell,
  sceneToWorld,
  worldToCell,
  worldToScene,
  type GridMap,
} from './grid'

/** Wall extrusion height in metres (scene y). */
export const WALL_HEIGHT = 0.6

/** Camera framing: centre of the known region in scene space, and its diagonal in metres. */
export const frameGrid = (m: GridMap) => {
  const [x0, y0] = cellToWorld(m, m.bbox.iMin, m.bbox.jMin)
  const [x1, y1] = cellToWorld(m, m.bbox.iMax, m.bbox.jMax)
  const [w, h] = bboxSizeMetres(m)
  const center = worldToScene((x0 + x1) / 2, (y0 + y1) / 2, 0)
  return { center, width: w, depth: h, radius: Math.hypot(w, h) / 2 }
}

/** Cell under a scene-space point, or null when off-grid. */
export const sceneToCell = (m: GridMap, sx: number, sz: number): [number, number] | null => {
  const { x, y } = sceneToWorld(sx, 0, sz)
  const [i, j] = worldToCell(m, x, y)
  if (i < 0 || j < 0 || i >= m.width || j >= m.height) return null
  return [i, j]
}

/** Scene-space position for each obstacle cell (its centre, resting on the floor). */
export const wallInstances = (m: GridMap): Float32Array => {
  const out: number[] = []
  for (let j = m.bbox.jMin; j <= m.bbox.jMax; j++) {
    for (let i = m.bbox.iMin; i <= m.bbox.iMax; i++) {
      if (getCell(m, i, j) !== OBSTACLE) continue
      const [x, y] = cellToWorld(m, i, j)
      out.push(...worldToScene(x, y, WALL_HEIGHT / 2))
    }
  }
  return new Float32Array(out)
}

const hex = (css: string): [number, number, number] => {
  const n = parseInt(css.replace('#', ''), 16)
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255]
}

export interface FloorPalette {
  floor: string
  wall: string
  unknown: string
}

/**
 * RGBA texels for the known bbox of the grid, row 0 = jMin. Consumers flip via texture.flipY so
 * +j (world +y) points toward scene -z.
 */
export const floorTexels = (m: GridMap, palette: FloorPalette) => {
  const w = m.bbox.iMax - m.bbox.iMin + 1
  const h = m.bbox.jMax - m.bbox.jMin + 1
  const data = new Uint8Array(w * h * 4)
  const cF = hex(palette.floor)
  const cW = hex(palette.wall)
  const cU = hex(palette.unknown)
  let k = 0
  for (let j = m.bbox.jMin; j <= m.bbox.jMax; j++) {
    for (let i = m.bbox.iMin; i <= m.bbox.iMax; i++) {
      const c = getCell(m, i, j)
      const rgb = c === FLOOR ? cF : c === OBSTACLE ? cW : cU
      data[k++] = rgb[0]
      data[k++] = rgb[1]
      data[k++] = rgb[2]
      data[k++] = c === UNKNOWN ? 0 : 255
    }
  }
  return { data, width: w, height: h }
}

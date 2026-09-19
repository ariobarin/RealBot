/** Cell encoding matches the robot's `mapping.grid2d` topic (nav/main.py): 0 unknown, 1 floor, 2 obstacle. */
export const UNKNOWN = 0
export const FLOOR = 1
export const OBSTACLE = 2
export type Cell = typeof UNKNOWN | typeof FLOOR | typeof OBSTACLE

export interface GridBBox {
  iMin: number
  iMax: number
  jMin: number
  jMax: number
}

export interface GridMap {
  id: string
  name: string
  /** cells along x (i) */
  width: number
  /** cells along y (j) */
  height: number
  /** metres per cell (== mapping voxel_size_m) */
  resolution: number
  /** world (x, y) of the corner of cell (0, 0), metres */
  origin: [number, number]
  /** row-major by j: idx = j * width + i */
  cells: Uint8Array
  bbox: GridBBox
}

/** On-disk form of a GridMap (`map.grid.json`). */
export interface GridMapJson {
  id: string
  name: string
  width: number
  height: number
  resolution: number
  origin: [number, number]
  cellsBase64: string
  bbox: GridBBox
  credit?: string
}

export const cellIndex = (m: Pick<GridMap, 'width'>, i: number, j: number): number => j * m.width + i

export const getCell = (m: GridMap, i: number, j: number): Cell => {
  if (i < 0 || j < 0 || i >= m.width || j >= m.height) return UNKNOWN
  return m.cells[cellIndex(m, i, j)] as Cell
}

/** Centre of cell (i, j) in world metres. Same formula as pack_floor() in nav/main.py. */
export const cellToWorld = (
  m: Pick<GridMap, 'origin' | 'resolution'>,
  i: number,
  j: number,
): [number, number] => [m.origin[0] + (i + 0.5) * m.resolution, m.origin[1] + (j + 0.5) * m.resolution]

export const worldToCell = (
  m: Pick<GridMap, 'origin' | 'resolution'>,
  x: number,
  y: number,
): [number, number] => [
  Math.floor((x - m.origin[0]) / m.resolution),
  Math.floor((y - m.origin[1]) / m.resolution),
]

/** World (x, y, z-up) -> three.js scene (x, y-up, z). Same mapping as the existing nav UI (`x: pt.x, y: -pt.z`). */
export const worldToScene = (x: number, y: number, z = 0): [number, number, number] => [x, z, -y]
export const sceneToWorld = (sx: number, sy: number, sz: number) => ({ x: sx, y: -sz, z: sy })

export const computeBBox = (width: number, height: number, cells: Uint8Array): GridBBox => {
  let iMin = width,
    iMax = -1,
    jMin = height,
    jMax = -1
  for (let j = 0; j < height; j++) {
    for (let i = 0; i < width; i++) {
      if (cells[j * width + i] !== UNKNOWN) {
        if (i < iMin) iMin = i
        if (i > iMax) iMax = i
        if (j < jMin) jMin = j
        if (j > jMax) jMax = j
      }
    }
  }
  if (iMax < 0) return { iMin: 0, iMax: 0, jMin: 0, jMax: 0 }
  return { iMin, iMax, jMin, jMax }
}

/** Metres covered by the known (non-unknown) region. */
export const bboxSizeMetres = (m: Pick<GridMap, 'bbox' | 'resolution'>): [number, number] => [
  (m.bbox.iMax - m.bbox.iMin + 1) * m.resolution,
  (m.bbox.jMax - m.bbox.jMin + 1) * m.resolution,
]

export const countCells = (m: GridMap): Record<Cell, number> => {
  const out: Record<Cell, number> = { 0: 0, 1: 0, 2: 0 }
  for (const c of m.cells) out[c as Cell]++
  return out
}

const b64ToBytes = (b64: string): Uint8Array => {
  if (typeof Buffer !== 'undefined') return new Uint8Array(Buffer.from(b64, 'base64'))
  const bin = atob(b64)
  const out = new Uint8Array(bin.length)
  for (let k = 0; k < bin.length; k++) out[k] = bin.charCodeAt(k)
  return out
}

const bytesToB64 = (bytes: Uint8Array): string => {
  if (typeof Buffer !== 'undefined') return Buffer.from(bytes).toString('base64')
  let bin = ''
  for (const b of bytes) bin += String.fromCharCode(b)
  return btoa(bin)
}

export const gridFromJson = (j: GridMapJson): GridMap => {
  const cells = b64ToBytes(j.cellsBase64)
  if (cells.length !== j.width * j.height) {
    throw new Error(`grid ${j.id}: expected ${j.width * j.height} cells, got ${cells.length}`)
  }
  return {
    id: j.id,
    name: j.name,
    width: j.width,
    height: j.height,
    resolution: j.resolution,
    origin: j.origin,
    cells,
    bbox: j.bbox,
  }
}

export const gridToJson = (m: GridMap, credit?: string): GridMapJson => ({
  id: m.id,
  name: m.name,
  width: m.width,
  height: m.height,
  resolution: m.resolution,
  origin: m.origin,
  cellsBase64: bytesToB64(m.cells),
  bbox: m.bbox,
  ...(credit ? { credit } : {}),
})

import { describe, expect, it } from 'vitest'
import {
  FLOOR,
  OBSTACLE,
  UNKNOWN,
  cellToWorld,
  computeBBox,
  gridFromJson,
  gridToJson,
  sceneToWorld,
  worldToCell,
  worldToScene,
  type GridMap,
} from './grid'
import { classifyPixel, parsePgm, pgmToGrid, type RosMapYaml } from './pgm'

const meta: RosMapYaml = {
  image: 'map.pgm',
  resolution: 0.05,
  origin: [-12.5, -12.5, 0],
  negate: 0,
  occupied_thresh: 0.65,
  free_thresh: 0.196,
}

const encode = (s: string) => new TextEncoder().encode(s)

/** 3x2 P5 with a comment line. Row 0 (top): free, unknown, occupied; row 1: free free free */
const pgmBytes = (): Uint8Array => {
  const header = encode('P5\n# CREATOR: test\n3 2\n255\n')
  const body = new Uint8Array([254, 205, 0, 254, 254, 254])
  const out = new Uint8Array(header.length + body.length)
  out.set(header)
  out.set(body, header.length)
  return out
}

describe('classifyPixel (ROS map_server thresholds)', () => {
  it('maps 254/205/0 to floor/unknown/obstacle', () => {
    expect(classifyPixel(254, meta)).toBe(FLOOR)
    expect(classifyPixel(205, meta)).toBe(UNKNOWN)
    expect(classifyPixel(0, meta)).toBe(OBSTACLE)
  })
  it('honours negate', () => {
    expect(classifyPixel(0, { ...meta, negate: 1 })).toBe(FLOOR)
    expect(classifyPixel(255, { ...meta, negate: 1 })).toBe(OBSTACLE)
  })
})

describe('parsePgm / pgmToGrid', () => {
  it('parses header with comments and flips y so row 0 becomes max j', () => {
    const pgm = parsePgm(pgmBytes())
    expect([pgm.width, pgm.height, pgm.maxval]).toEqual([3, 2, 255])
    const g = pgmToGrid(pgm, meta, 't', 'T')
    // top image row -> j = 1
    expect([g.cells[1 * 3 + 0], g.cells[1 * 3 + 1], g.cells[1 * 3 + 2]]).toEqual([FLOOR, UNKNOWN, OBSTACLE])
    expect([g.cells[0], g.cells[1], g.cells[2]]).toEqual([FLOOR, FLOOR, FLOOR])
    expect(g.origin).toEqual([-12.5, -12.5])
    expect(g.bbox).toEqual({ iMin: 0, iMax: 2, jMin: 0, jMax: 1 })
  })
  it('rejects non-P5', () => {
    expect(() => parsePgm(encode('P2\n1 1\n255\n0'))).toThrow()
  })
})

describe('coordinate helpers (match nav/main.py)', () => {
  const m = { origin: [-12.5, -12.5] as [number, number], resolution: 0.05 }
  it('cellToWorld uses cell centre', () => {
    expect(cellToWorld(m, 0, 0)).toEqual([-12.475, -12.475])
    expect(cellToWorld(m, 250, 250)[0]).toBeCloseTo(0.025)
  })
  it('round-trips world -> cell -> world within one cell', () => {
    for (const [x, y] of [
      [0, 0],
      [3.14, -2.7],
      [-12.4, 12.4],
    ]) {
      const [i, j] = worldToCell(m, x, y)
      const [wx, wy] = cellToWorld(m, i, j)
      expect(Math.abs(wx - x)).toBeLessThanOrEqual(m.resolution / 2 + 1e-9)
      expect(Math.abs(wy - y)).toBeLessThanOrEqual(m.resolution / 2 + 1e-9)
    }
  })
  it('scene mapping matches nav UI (x, z, -y)', () => {
    expect(worldToScene(1, 2, 3)).toEqual([1, 3, -2])
    expect(sceneToWorld(1, 3, -2)).toEqual({ x: 1, y: 2, z: 3 })
  })
})

describe('json round trip', () => {
  it('preserves cells and metadata', () => {
    const cells = new Uint8Array([0, 1, 2, 1])
    const g: GridMap = {
      id: 'x',
      name: 'X',
      width: 2,
      height: 2,
      resolution: 0.1,
      origin: [1, 2],
      cells,
      bbox: computeBBox(2, 2, cells),
    }
    const back = gridFromJson(gridToJson(g, 'c'))
    expect(Array.from(back.cells)).toEqual([0, 1, 2, 1])
    expect(back.bbox).toEqual({ iMin: 0, iMax: 1, jMin: 0, jMax: 1 })
    expect(back.origin).toEqual([1, 2])
  })
  it('detects size mismatch', () => {
    expect(() =>
      gridFromJson({
        id: 'x',
        name: 'X',
        width: 3,
        height: 3,
        resolution: 1,
        origin: [0, 0],
        cellsBase64: Buffer.from([1, 2]).toString('base64'),
        bbox: { iMin: 0, iMax: 0, jMin: 0, jMax: 0 },
      }),
    ).toThrow()
  })
})

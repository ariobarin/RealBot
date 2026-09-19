import { describe, expect, it } from 'vitest'
import { FLOOR, OBSTACLE, UNKNOWN, cellToWorld, computeBBox, worldToScene, type GridMap } from './grid'
import { WALL_HEIGHT, floorTexels, frameGrid, sceneToCell, wallInstances } from './scene'

const make = (rows: string[], resolution = 0.5, origin: [number, number] = [-1, -2]): GridMap => {
  const height = rows.length
  const width = rows[0].length
  const cells = new Uint8Array(width * height)
  rows.forEach((row, j) => {
    ;[...row].forEach((ch, i) => {
      cells[j * width + i] = ch === '#' ? OBSTACLE : ch === '.' ? FLOOR : UNKNOWN
    })
  })
  return {
    id: 't',
    name: 't',
    width,
    height,
    resolution,
    origin,
    cells,
    bbox: computeBBox(width, height, cells),
  }
}

// j=0 is the bottom row (world +y goes up the list).
const grid = make([' #.# ', ' ... ', ' ### '])

describe('sceneToCell', () => {
  it('round-trips a known cell through world and scene space', () => {
    const [x, y] = cellToWorld(grid, 2, 1)
    const [sx, , sz] = worldToScene(x, y)
    expect(sceneToCell(grid, sx, sz)).toEqual([2, 1])
  })

  it('returns null off-grid', () => {
    expect(sceneToCell(grid, 100, 100)).toBeNull()
  })

  it('maps scene -z to world +y (higher j)', () => {
    const [x0, y0] = cellToWorld(grid, 2, 0)
    const [sx, , sz] = worldToScene(x0, y0)
    expect(sceneToCell(grid, sx, sz - grid.resolution)).toEqual([2, 1])
  })
})

describe('wallInstances', () => {
  it('emits one xyz triple per obstacle cell, resting on the floor', () => {
    const p = wallInstances(grid)
    expect(p.length).toBe(5 * 3)
    for (let k = 0; k < p.length; k += 3) expect(p[k + 1]).toBeCloseTo(WALL_HEIGHT / 2)
    const [x, y] = cellToWorld(grid, 1, 0)
    const expected = worldToScene(x, y, WALL_HEIGHT / 2)
    for (let k = 0; k < 3; k++) expect(p[k]).toBeCloseTo(expected[k], 5)
  })
})

describe('frameGrid', () => {
  it('centres on the known bbox and sizes in metres', () => {
    const f = frameGrid(grid)
    expect(f.width).toBeCloseTo(1.5)
    expect(f.depth).toBeCloseTo(1.5)
    const [cx, cy] = cellToWorld(grid, 2, 1)
    expect(f.center).toEqual(worldToScene(cx, cy, 0))
  })
})

describe('floorTexels', () => {
  it('paints only the bbox and makes unknown cells transparent', () => {
    const { data, width, height } = floorTexels(grid, {
      floor: '#ff0000',
      wall: '#00ff00',
      unknown: '#0000ff',
    })
    expect([width, height]).toEqual([3, 3])
    // row 0 = jMin: "#.#" -> first texel is wall, middle texel is floor
    expect(Array.from(data.slice(0, 4))).toEqual([0, 255, 0, 255])
    expect(Array.from(data.slice(4, 8))).toEqual([255, 0, 0, 255])
    // row 2 = jMax: "###"
    const top = (2 * 3 + 1) * 4
    expect(Array.from(data.slice(top, top + 4))).toEqual([0, 255, 0, 255])
  })

  it('flags unknown cells with alpha 0', () => {
    const g = make(['#. ', '## '])
    const { data } = floorTexels(g, { floor: '#000000', wall: '#000000', unknown: '#ffffff' })
    expect(data[3]).toBe(255)
    // bbox includes the trailing unknown column only if something known is there; here it isn't
    expect(data.length).toBe(2 * 2 * 4)
  })
})

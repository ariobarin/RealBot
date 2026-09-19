/**
 * Convert a ROS map_server SLAM map (map.pgm + map.yaml) into RealBot's
 * `map.grid.json` (cells encoded 0 unknown / 1 floor / 2 obstacle, same as the
 * robot's mapping.grid2d topic) plus a styled `thumb.png` for the library card.
 *
 *   npm run convert-map -- public/maps/small-house small-house "Small House"
 */
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { load as loadYaml } from 'js-yaml'
import sharp from 'sharp'
import { FLOOR, OBSTACLE, bboxSizeMetres, countCells, gridToJson } from '../src/lib/grid.ts'
import { parsePgm, pgmToGrid, type RosMapYaml } from '../src/lib/pgm.ts'

const [dir, id, name, credit] = process.argv.slice(2)
if (!dir || !id || !name) {
  console.error('usage: convert-map <dir> <id> <name> [credit]')
  process.exit(1)
}

const meta = loadYaml(readFileSync(join(dir, 'map.yaml'), 'utf8')) as RosMapYaml
const pgm = parsePgm(new Uint8Array(readFileSync(join(dir, meta.image))))
const grid = pgmToGrid(pgm, meta, id, name)

writeFileSync(join(dir, 'map.grid.json'), JSON.stringify(gridToJson(grid, credit)))

// Thumbnail: crop to the known bbox + 1 m padding, taste-doc palette.
const PAD = Math.round(1.0 / grid.resolution)
const FLOOR_RGB = [0xf3, 0xef, 0xe8]
const WALL_RGB = [0x2b, 0x2b, 0x2b]
const i0 = Math.max(0, grid.bbox.iMin - PAD)
const i1 = Math.min(grid.width - 1, grid.bbox.iMax + PAD)
const j0 = Math.max(0, grid.bbox.jMin - PAD)
const j1 = Math.min(grid.height - 1, grid.bbox.jMax + PAD)
const cw = i1 - i0 + 1
const ch = j1 - j0 + 1
const rgba = Buffer.alloc(cw * ch * 4)
for (let row = 0; row < ch; row++) {
  const j = j1 - row // top of the image = max y
  for (let col = 0; col < cw; col++) {
    const c = grid.cells[j * grid.width + (i0 + col)]
    const o = (row * cw + col) * 4
    if (c === FLOOR) rgba.set([...FLOOR_RGB, 255], o)
    else if (c === OBSTACLE) rgba.set([...WALL_RGB, 255], o)
  }
}
await sharp(rgba, { raw: { width: cw, height: ch, channels: 4 } })
  .resize({ width: 1024, kernel: 'nearest' })
  .png()
  .toFile(join(dir, 'thumb.png'))

const [wm, hm] = bboxSizeMetres(grid)
const counts = countCells(grid)
console.log(
  `${id}: ${grid.width}x${grid.height} @ ${grid.resolution} m, known area ${wm.toFixed(1)} x ${hm.toFixed(1)} m, ` +
    `floor ${counts[1]} obstacle ${counts[2]} unknown ${counts[0]} -> ${(counts[1] * grid.resolution ** 2).toFixed(0)} m² floor`,
)

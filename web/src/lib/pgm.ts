import { FLOOR, OBSTACLE, UNKNOWN, computeBBox, type GridMap } from './grid'

export interface Pgm {
  width: number
  height: number
  maxval: number
  /** row-major, row 0 = top of image */
  pixels: Uint8Array
}

/** ROS map_server YAML metadata. */
export interface RosMapYaml {
  image: string
  resolution: number
  origin: [number, number, number]
  negate: 0 | 1
  occupied_thresh: number
  free_thresh: number
}

const isSpace = (b: number) => b === 0x20 || b === 0x0a || b === 0x0d || b === 0x09

/** Parse a binary (P5) PGM. Handles `#` comments in the header. */
export function parsePgm(buf: Uint8Array): Pgm {
  let pos = 0
  const tokens: string[] = []
  while (tokens.length < 4) {
    while (pos < buf.length && isSpace(buf[pos])) pos++
    if (buf[pos] === 0x23 /* # */) {
      while (pos < buf.length && buf[pos] !== 0x0a) pos++
      continue
    }
    const start = pos
    while (pos < buf.length && !isSpace(buf[pos])) pos++
    tokens.push(String.fromCharCode(...buf.subarray(start, pos)))
  }
  pos++ // single whitespace after maxval
  const [magic, w, h, mv] = tokens
  if (magic !== 'P5') throw new Error(`unsupported PGM magic ${magic} (only binary P5)`)
  const width = Number(w)
  const height = Number(h)
  const maxval = Number(mv)
  if (maxval > 255) throw new Error('16-bit PGM not supported')
  const pixels = buf.subarray(pos, pos + width * height)
  if (pixels.length !== width * height) throw new Error('PGM truncated')
  return { width, height, maxval, pixels: new Uint8Array(pixels) }
}

/**
 * ROS map_server trinary semantics -> RealBot cell.
 *   p = (maxval - pixel) / maxval   (or pixel / maxval when negate)
 *   p > occupied_thresh -> OBSTACLE, p < free_thresh -> FLOOR, else UNKNOWN
 */
export function classifyPixel(pixel: number, meta: RosMapYaml, maxval = 255): 0 | 1 | 2 {
  const p = meta.negate ? pixel / maxval : (maxval - pixel) / maxval
  if (p > meta.occupied_thresh) return OBSTACLE
  if (p < meta.free_thresh) return FLOOR
  return UNKNOWN
}

/**
 * Convert PGM + YAML into a GridMap. PGM row 0 is the top of the image, which is
 * max-y in the ROS map frame, so j = height - 1 - row.
 */
export function pgmToGrid(pgm: Pgm, meta: RosMapYaml, id: string, name: string): GridMap {
  const { width, height } = pgm
  const cells = new Uint8Array(width * height)
  for (let row = 0; row < height; row++) {
    const j = height - 1 - row
    for (let i = 0; i < width; i++) {
      cells[j * width + i] = classifyPixel(pgm.pixels[row * width + i], meta, pgm.maxval)
    }
  }
  return {
    id,
    name,
    width,
    height,
    resolution: meta.resolution,
    origin: [meta.origin[0], meta.origin[1]],
    cells,
    bbox: computeBBox(width, height, cells),
  }
}

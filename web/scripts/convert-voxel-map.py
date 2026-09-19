"""Convert a bracketbot ``mapping.voxels`` NPZ export into web map assets.

The output keeps the full coloured voxel cloud for the 3D view and derives the
occupancy grid used by the existing 2D renderer and waypoint picker.

Usage:
    python scripts/convert-voxel-map.py INPUT.npz public/maps/ID ID "Display name"
"""

from __future__ import annotations

import argparse
import base64
import json
import struct
import zlib
from pathlib import Path

import numpy as np


FLOOR = 1
OBSTACLE = 2


def write_png(path: Path, rgba: np.ndarray) -> None:
    """Write an 8-bit RGBA PNG without requiring Pillow."""

    height, width, channels = rgba.shape
    if channels != 4 or rgba.dtype != np.uint8:
        raise ValueError("expected an HxWx4 uint8 image")

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    scanlines = b"".join(b"\0" + rgba[row].tobytes() for row in range(height))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(scanlines, 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("id")
    parser.add_argument("name")
    args = parser.parse_args()

    with np.load(args.input, allow_pickle=False) as archive:
        points = np.asarray(archive["points"], dtype="<f4")
        colors = np.asarray(archive["colors"], dtype=np.uint8)
        labels = np.asarray(archive["labels"])
        resolution = float(archive["voxel_size_m"])

    if points.ndim != 2 or points.shape[1] != 3:
        raise ValueError("points must have shape (N, 3)")
    if colors.shape != points.shape or labels.shape != (len(points),):
        raise ValueError("colors/labels must align with points")

    args.output.mkdir(parents=True, exist_ok=True)

    # The exported points lie at voxel centres. Anchor the grid at the minimum
    # centre so float noise cannot move adjacent 3 cm voxels into one cell.
    min_xy = points[:, :2].min(axis=0).astype(np.float64)
    ij = np.rint((points[:, :2].astype(np.float64) - min_xy) / resolution).astype(np.int32)
    width = int(ij[:, 0].max()) + 1
    height = int(ij[:, 1].max()) + 1
    cells = np.zeros((height, width), dtype=np.uint8)

    floor = labels < 0
    cells[ij[floor, 1], ij[floor, 0]] = FLOOR

    # Positive labels are occupied voxels. Ignore the two lowest layers when
    # projecting them so floor-edge noise does not erase traversable cells.
    floor_z = float(points[floor, 2].min()) if np.any(floor) else float(points[:, 2].min())
    occupied = (labels >= 0) & (points[:, 2] >= floor_z + 1.5 * resolution)
    cells[ij[occupied, 1], ij[occupied, 0]] = OBSTACLE

    known_j, known_i = np.nonzero(cells)
    bbox = {
        "iMin": int(known_i.min()),
        "iMax": int(known_i.max()),
        "jMin": int(known_j.min()),
        "jMax": int(known_j.max()),
    }
    origin = [float(min_xy[0] - resolution / 2), float(min_xy[1] - resolution / 2)]
    grid = {
        "id": args.id,
        "name": args.name,
        "width": width,
        "height": height,
        "resolution": resolution,
        "origin": origin,
        "cellsBase64": base64.b64encode(cells.tobytes()).decode("ascii"),
        "bbox": bbox,
        "credit": "Bracketbot mapping.voxels scan",
    }
    (args.output / "map.grid.json").write_text(json.dumps(grid, separators=(",", ":")))

    # Interleaved binary: N little-endian float32 scene positions, followed by
    # N RGB bytes. Convert z-up SLAM coordinates to three.js [x, z, -y].
    scene_points = np.column_stack((points[:, 0], points[:, 2], -points[:, 1])).astype("<f4")
    (args.output / "cloud.bin").write_bytes(scene_points.tobytes() + colors.tobytes())

    cloud = {
        "count": int(len(points)),
        "pointSize": resolution * 1.45,
        "bounds": {
            "min": scene_points.min(axis=0).tolist(),
            "max": scene_points.max(axis=0).tolist(),
        },
    }
    (args.output / "cloud.json").write_text(json.dumps(cloud, separators=(",", ":")))

    # Warm floor-plan thumbnail matching the rest of the space library.
    crop = cells[bbox["jMin"] : bbox["jMax"] + 1, bbox["iMin"] : bbox["iMax"] + 1]
    rgba = np.zeros((*crop.shape, 4), dtype=np.uint8)
    rgba[crop == FLOOR] = (0xF3, 0xEF, 0xE8, 255)
    rgba[crop == OBSTACLE] = (0x2B, 0x2B, 0x2B, 255)
    rgba = np.flipud(rgba)
    scale = max(1, 1024 // max(rgba.shape[:2]))
    rgba = np.repeat(np.repeat(rgba, scale, axis=0), scale, axis=1)
    write_png(args.output / "thumb.png", rgba)

    floor_area = int(np.count_nonzero(cells == FLOOR)) * resolution**2
    print(
        f"{args.id}: {len(points):,} voxels, {width}x{height} @ {resolution:.3f} m, "
        f"{floor_area:.1f} m² traversable"
    )


if __name__ == "__main__":
    main()

"""Rectified live-video click -> floor goal. No IPC or hardware writes.

The stream and fresh robot-side capture use the same rectified pixel geometry.
Clicks are accepted only while the robot is stationary; depth remains local at
its calibrated native resolution and is never inferred from a compressed frame.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import time

import numpy as np


@dataclass
class FloorCapture:
    capture_id: str
    created: float  # robot monotonic seconds
    image: bytes  # JPEG of the rectified left image represented by this depth
    depth: np.ndarray  # optical-axis millimetres
    intrinsics: tuple[float, float, float, float]
    camera_to_base: np.ndarray  # 3x4, same contract as bbOS mapping
    pose: tuple[float, float, float]  # map x, y, yaw, planar bbOS mapping convention
    revision: str


def floor_goal(capture: FloorCapture, u: float, v: float, *,
               current_pose: tuple[float, float, float], revision: str,
               grid: np.ndarray, origin: tuple[float, float], resolution: float,
               clearance_m: float, now: float | None = None) -> dict[str, float]:
    """Require fresh stationary evidence, floor height and a clear known footprint.

    bbOS navigation still owns path planning; this validates only the goal.
    Coordinates use grid[x_index, y_index], matching the deployed nav daemon.
    """
    now = time.monotonic() if now is None else now
    if not 0 <= now - capture.created <= 10:
        raise ValueError("View expired. Choose a fresh view.")
    if revision != capture.revision:
        raise ValueError("Map changed. Choose a fresh view.")
    if any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n) for n in (u, v)) or not (0 <= u <= 1 and 0 <= v <= 1):
        raise ValueError("Click is outside the image.")
    if not all(math.isfinite(n) for n in (*current_pose, *capture.pose, *origin, resolution, clearance_m)) or resolution <= 0 or clearance_m <= 0:
        raise ValueError("Invalid map geometry.")
    dx, dy = current_pose[0] - capture.pose[0], current_pose[1] - capture.pose[1]
    dyaw = math.atan2(math.sin(current_pose[2] - capture.pose[2]), math.cos(current_pose[2] - capture.pose[2]))
    if math.hypot(dx, dy) > .03 or abs(dyaw) > math.radians(2):
        raise ValueError("Robot moved. Choose a fresh view.")
    depth = capture.depth
    if depth.ndim != 2 or min(depth.shape) < 3:
        raise ValueError("Invalid depth image.")
    height, width = depth.shape
    px, py = u * (width - 1), v * (height - 1)
    col, row = round(px), round(py)
    # A click on missing depth must not borrow a neighboring surface.
    center = float(depth[row, col]) * .001
    samples = depth[max(0, row-1):row+2, max(0, col-1):col+2].astype(float) * .001
    valid = samples[np.isfinite(samples) & (samples >= .15) & (samples <= 4.)]
    if not math.isfinite(center) or not .15 <= center <= 4 or len(valid) < 5:
        raise ValueError("No reliable depth here. Click visible floor.")
    z = float(np.median(valid))
    if np.ptp(valid) > .15 or abs(center - z) > .10:
        raise ValueError("Depth edge is uncertain. Click clear floor.")
    fx, fy, cx, cy = capture.intrinsics
    if not all(math.isfinite(n) for n in (fx, fy, cx, cy)) or min(fx, fy) <= 0:
        raise ValueError("Camera calibration unavailable.")
    transform = np.asarray(capture.camera_to_base)
    if transform.shape != (3, 4) or not np.isfinite(transform).all():
        raise ValueError("Camera transform unavailable.")
    base = transform @ np.array([(px-cx)*z/fx, (py-cy)*z/fy, z, 1.])
    if abs(float(base[2])) > .12:
        raise ValueError("That point is not on the floor.")
    if not .25 <= math.hypot(base[0], base[1]) <= 2.:
        raise ValueError("Choose floor between 0.25 and 2 metres away.")
    x0, y0, yaw = capture.pose
    x = x0 + math.cos(yaw)*base[0] - math.sin(yaw)*base[1]
    y = y0 + math.sin(yaw)*base[0] + math.cos(yaw)*base[1]
    i, j = math.floor((x-origin[0])/resolution), math.floor((y-origin[1])/resolution)
    radius = math.ceil(clearance_m/resolution)
    if grid.ndim != 2 or i-radius < 0 or j-radius < 0 or i+radius >= grid.shape[0] or j+radius >= grid.shape[1]:
        raise ValueError("Target is outside the mapped floor.")
    # Conservative square footprint, at least the nav robot inflation radius.
    if not np.all(grid[i-radius:i+radius+1, j-radius:j+radius+1] == 1):
        raise ValueError("Target is too close to an obstacle or unknown floor.")
    return {"x": float(x), "y": float(y)}

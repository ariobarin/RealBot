"""Navigation planner — pure math, no bbos dependency.

Extracted from map_nav_simple.py. Dijkstra + pure pursuit.
Functions that used CFG_M.grid_res now take grid_res as a parameter.
"""
import io
import json
import math
import struct
import time
from enum import IntEnum

import numpy as np
from numba import njit
from PIL import Image
from scipy.ndimage import uniform_filter

# Navigation
V = 0.15
PLAN_RADIUS = 5.0
GOAL_TOLERANCE = 0.25
LOOKAHEAD = 0.5  # pure pursuit lookahead (meters)
MIN_V_FRAC = 0.3   # minimum forward speed fraction (never scale below this)
MAX_OMEGA = 1.5     # clamp angular velocity (rad/s)

# Stuck detection
STUCK_POS_THRESH = 0.03
STUCK_ANG_THRESH = 0.15
STUCK_TIME = 12.0
VEL_CMD_MIN = 0.03
VEL_MISMATCH_FACTOR = 0.2  # stuck if actual_v < commanded_v * factor
VEL_MISMATCH_TIME = 8.0
GOAL_PROGRESS_TIME = 45.0
GOAL_PROGRESS_DIST = 0.1

# Recovery
BACKUP_SPEED = -0.06
BACKUP_DIST = 0.15
BACKUP_TIME = 2.0
FORWARD_PUSH_SPEED = 0.15
FORWARD_PUSH_DIST = 0.3
FORWARD_PUSH_TIME = 3.0
ROTATE_SPEED = 0.2
ROTATE_ANGLE = 1.57
ROTATE_TIME = 5.0
MAX_CONSECUTIVE_RECOVERIES = 3


class Recovery(IntEnum):
    NONE = 0
    BACKUP = 1
    REPLAN = 2
    ROTATE = 3
    CLEAR_AND_RETRY = 4
    FORWARD_PUSH = 5


# Velocity smoothing
SMOOTH_V = 0.5
SMOOTH_W = 0.6

# Timing
GRID_INTERVAL = 0.2
STATE_INTERVAL = 0.125

# Dijkstra
PROX_WEIGHT = 120.0
_INF = 1e18
_DI = np.array([1, -1, 0, 0, 1, -1, 1, -1], dtype=np.int32)
_DJ = np.array([0, 0, 1, -1, 1, 1, -1, -1], dtype=np.int32)
_DC = np.array([1.0, 1.0, 1.0, 1.0, 1.4142135, 1.4142135, 1.4142135, 1.4142135], dtype=np.float64)
_HEAP_CAP = 8_000_000

PALETTE = [0, 0, 0, 16, 185, 129, 239, 68, 68] + [0] * (256 - 3) * 3


def quat_yaw(q):
    return 2.0 * math.atan2(q[2], q[3])


def w2g(x, y, origin, inv_res):
    return int(math.floor((x - origin[0]) * inv_res)), int(math.floor((y - origin[1]) * inv_res))


def g2w(gi, gj, origin, res):
    return origin[0] + (gi + 0.5) * res, origin[1] + (gj + 0.5) * res


# --- Grid processing (2D BEV grid now comes ready from the mapping daemon: mapping.grid2d) ---


def forward_obstacles(grid, origin, pos, yaw, radius, grid_res, blacklist=None):
    inv = 1.0 / grid_res
    GS = grid.shape[0]
    ci, cj = w2g(pos[0], pos[1], origin, inv)
    rc = int(math.ceil(radius * inv))
    i0, i1 = max(0, ci - rc), min(GS, ci + rc + 1)
    j0, j1 = max(0, cj - rc), min(GS, cj + rc + 1)
    if i0 >= i1 or j0 >= j1:
        return []
    sub = grid[i0:i1, j0:j1]
    oi, oj = np.where(sub == 2)
    if len(oi) == 0:
        return []
    di, dj = oi - (ci - i0), oj - (cj - j0)
    dist_sq = di * di + dj * dj
    hx, hy = -math.sin(yaw), math.cos(yaw)
    dot = di * hx + dj * hy
    mask = (dist_sq <= rc * rc) & (dot > 0)
    now = time.time()
    result = []
    for k in range(len(oi)):
        if not mask[k]:
            pass
        else:
            cell = (int(oi[k]) + i0, int(oj[k]) + j0)
            if blacklist and cell in blacklist and blacklist[cell] > now:
                pass
            else:
                result.append(cell)
    return result


def compute_crop(grid):
    nz = np.nonzero(grid)
    if len(nz[0]) == 0:
        return None
    i0, i1 = int(nz[0].min()), int(nz[0].max()) + 1
    j0, j1 = int(nz[1].min()), int(nz[1].max()) + 1
    pad = 5
    GS = grid.shape[0]
    return max(0, i0 - pad), max(0, j0 - pad), min(GS, i1 + pad), min(GS, j1 + pad)


def grid_to_png(grid, crop):
    i0, j0, i1, j1 = crop
    sub = grid[i0:i1, j0:j1]
    img_data = np.flipud(sub.T)
    img = Image.fromarray(img_data, mode='P')
    img.putpalette(PALETTE)
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=False)
    return buf.getvalue()


# --- Backward Dijkstra ---

@njit(cache=True)
def _dijkstra_backward(g, passable, prox, goal_i, goal_j, GS, hc, hn):
    for i in range(GS):
        for j in range(GS):
            g[i, j] = _INF
    if goal_i < 0 or goal_i >= GS or goal_j < 0 or goal_j >= GS:
        return
    if not passable[goal_i, goal_j]:
        return
    g[goal_i, goal_j] = 0.0
    hc[0] = 0.0
    hn[0] = goal_i * GS + goal_j
    hs = 1
    while hs > 0:
        c = hc[0]; u = hn[0]
        hs -= 1
        if hs > 0:
            hc[0] = hc[hs]; hn[0] = hn[hs]
            idx = 0
            while True:
                left = 2 * idx + 1; right = left + 1; sm = idx
                if left < hs and hc[left] < hc[sm]: sm = left
                if right < hs and hc[right] < hc[sm]: sm = right
                if sm != idx:
                    hc[idx], hc[sm] = hc[sm], hc[idx]
                    hn[idx], hn[sm] = hn[sm], hn[idx]
                    idx = sm
                else:
                    break
        ui = u // GS; uj = u - ui * GS
        if c > g[ui, uj]:
            pass
        else:
            for d in range(8):
                ni = ui + _DI[d]; nj = uj + _DJ[d]
                if 0 <= ni < GS and 0 <= nj < GS and passable[ni, nj]:
                    nc = c + _DC[d] + prox[ni, nj]
                    if nc < g[ni, nj]:
                        g[ni, nj] = nc
                        pos = hs
                        hc[pos] = nc; hn[pos] = ni * GS + nj; hs += 1
                        while pos > 0:
                            par = (pos - 1) >> 1
                            if hc[pos] < hc[par]:
                                hc[pos], hc[par] = hc[par], hc[pos]
                                hn[pos], hn[par] = hn[par], hn[pos]
                                pos = par
                            else:
                                break


@njit(cache=True)
def _follow_gradient(g, ri, rj, GS, steps):
    if ri < 0 or ri >= GS or rj < 0 or rj >= GS or g[ri, rj] >= _INF:
        return -1, -1
    ci, cj = ri, rj
    for _ in range(steps):
        best = g[ci, cj]; bi, bj = ci, cj
        for d in range(8):
            ni = ci + _DI[d]; nj = cj + _DJ[d]
            if 0 <= ni < GS and 0 <= nj < GS:
                v = g[ni, nj]
                if v < best: best = v; bi, bj = ni, nj
        if bi == ci and bj == cj: break
        ci, cj = bi, bj
    return ci, cj


@njit(cache=True)
def _extract_path_jit(g, ri, rj, GS, out_i, out_j, max_steps):
    if ri < 0 or ri >= GS or rj < 0 or rj >= GS or g[ri, rj] >= _INF:
        return 0
    out_i[0] = ri; out_j[0] = rj; n = 1
    ci, cj = ri, rj
    for _ in range(max_steps - 1):
        best = g[ci, cj]; bi, bj = ci, cj
        for d in range(8):
            ni = ci + _DI[d]; nj = cj + _DJ[d]
            if 0 <= ni < GS and 0 <= nj < GS:
                v = g[ni, nj]
                if v < best: best = v; bi, bj = ni, nj
        if bi == ci and bj == cj: break
        ci, cj = bi, bj
        out_i[n] = ci; out_j[n] = cj; n += 1
    return n


# --- JIT warmup ---

def _warmup_jit():
    gs = 10
    g = np.full((gs, gs), _INF, dtype=np.float64)
    p = np.ones((gs, gs), dtype=np.bool_)
    pr = np.zeros((gs, gs), dtype=np.float32)
    hc = np.empty(_HEAP_CAP, dtype=np.float64)
    hn = np.empty(_HEAP_CAP, dtype=np.int32)
    _dijkstra_backward(g, p, pr, 5, 5, gs, hc, hn)
    _follow_gradient(g, 0, 0, gs, 3)
    oi = np.empty(50, dtype=np.int32); oj = np.empty(50, dtype=np.int32)
    _extract_path_jit(g, 0, 0, gs, oi, oj, 50)


# --- Exploration ---

def random_frontier_goal(pos, grid, origin, plan_radius, grid_res, bounds=None):
    GS = grid.shape[0]
    inv = 1.0 / grid_res
    ri, rj = w2g(pos[0], pos[1], origin, inv)
    rc = int(plan_radius * inv)
    i0, i1 = max(0, ri - rc), min(GS, ri + rc + 1)
    j0, j1 = max(0, rj - rc), min(GS, rj + rc + 1)
    sub = grid[i0:i1, j0:j1]
    fi, fj = np.where(sub == 1)
    if len(fi) == 0:
        return None
    di, dj = fi - (ri - i0), fj - (rj - j0)
    dist_sq = di * di + dj * dj
    # Only pick cells within plan radius but at least 1m away
    min_r = int(1.0 * inv)
    mask = (dist_sq <= rc * rc) & (dist_sq >= min_r * min_r)
    fi, fj = fi[mask], fj[mask]
    if len(fi) == 0:
        return None
    # Filter by rotated bounds polygon if provided
    if bounds is not None:
        # Convert candidate grid cells to world coords
        wx = origin[0] + (fi + i0 + 0.5) * grid_res
        wy = origin[1] + (fj + j0 + 0.5) * grid_res
        inside = np.ones(len(fi), dtype=np.bool_)
        for k in range(4):
            ax, ay = bounds[k]
            bx, by = bounds[(k + 1) % 4]
            ex, ey = bx - ax, by - ay
            # Normal points inward for CCW winding
            inside &= (ey * (wx - ax) - ex * (wy - ay)) >= 0
        fi, fj = fi[inside], fj[inside]
        if len(fi) == 0:
            return None
    idx = np.random.randint(len(fi))
    return g2w(fi[idx] + i0, fj[idx] + j0, origin, grid_res)


# --- Message helpers ---

def make_state_msg(pos, yaw, goal, path, explore, recovery_state=Recovery.NONE):
    msg = {
        "t": "state",
        "rx": round(float(pos[0]), 3), "ry": round(float(pos[1]), 3),
        "rh": round(float(yaw), 4),
        "explore": explore,
        "recovery": recovery_state.name if recovery_state != Recovery.NONE else None,
    }
    if goal is not None:
        msg["gx"] = round(float(goal[0]), 3)
        msg["gy"] = round(float(goal[1]), 3)
    if path:
        step = max(1, len(path) // 200)
        sampled = path[::step]
        msg["path"] = [[round(float(p[0]), 3) for p in sampled], [round(float(p[1]), 3) for p in sampled]]
    return json.dumps(msg)

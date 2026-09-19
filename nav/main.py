# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "bbos",
#   "numba",
#   "numpy",
#   "pillow",
#   "fastapi",
#   "uvicorn",
#   "websockets",
#   "scipy",
#   "opencv-python-headless",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""reloc_nav — waypoint navigation on a given (relocalized) map.

The robot localizes into a pre-built map via QR portals (p_slam daemon) and navigates it:
waypoint patrols (with optional final heading), frontier mode (plan anywhere on the given map
with live obstacles overlaid), manual teleop, and a 3D web UI on :8010.

Inputs:  slam.pose (map-frame pose), mapping.grid2d / mapping.voxels (live traversability)
Output:  drive.ctrl (twist)
Map artifacts (per map, in ~/bbapps/nav/maps/): map_cloud.npz, <name>_cloud_nav.npz,
auki_nav_grid_<name>.npz; portal registry lives with the p_slam daemon.
"""
import asyncio
import ctypes
import json
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import zlib
from pathlib import Path
from queue import Queue, Empty

try:                        # glibc's default arena retains freed memory instead of returning it
    _libc = ctypes.CDLL("libc.so.6")    # to the OS — voxel_loop's periodic large-array frees left
    _libc.mallopt(-3, 131072)           # RSS permanently elevated. Route allocations >=128KB
    _libc.mallopt(-1, 131072)           # through mmap (M_MMAP_THRESHOLD) and lower the trim
except Exception:                       # threshold (M_TRIM_THRESHOLD) so they're actually freed.
    pass

import numpy as np
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from numba import njit
from scipy.ndimage import uniform_filter, distance_transform_edt, label

from bbos import Writer, Reader, Config, Type
from reloc_planner import (
    MIN_V_FRAC, STATE_INTERVAL, _INF, _HEAP_CAP,
    quat_yaw, w2g, g2w,
    _dijkstra_backward, _extract_path_jit, _warmup_jit,
)

CFG_M = Config('mapping')

import reloc_geom as G            # COLMAP/GL map -> z-up nav frame + floor fit

# --- Live depth-camera point cloud (raw depth frames, unprojected client-side) ---
# The depth daemon already publishes a pre-projected camera.points, but sending raw depth and
# unprojecting on the client is deliberately different: depth images compress far better than
# XYZ float triples (spatially smooth vs effectively-random bit patterns), and doing the
# unprojection on the client's GPU costs the robot's CPU nothing — the opposite of computing
# points here and shipping them.
depth_calib = None   # {'fx','fy','cx','cy','w','h','T_base_cam'} — computed once in main()
_depth_imu = {'ref': None, 'idx': 0, 'sign': 1.0, 'lim': 0.0, 'drive_sign': -1.0}   # live-pitch tracking (see depth_loop)


def compute_depth_calib():
    """Depth-camera intrinsics + camera->base extrinsic, computed the same way the depth
    daemon computes them (bbos/daemons/depth/daemon.py) so the client can unproject raw
    camera.depth frames itself. Values verified empirically against a live depth frame before
    this was written (fx=161.5 fy=137.0 cx=354.5 cy=197.4 for the 640x384 rectified output;
    depth values are uint16 millimeters)."""
    CFG_C = Config("cam_head")
    CFG_D = Config("depth")
    (mtx_l, dist_l, mtx_r, dist_r, R1, R2, P1_cam, P2_cam,
     Q, baseline_m, fx_ds, R, t) = CFG_D.camera_cal()
    src_w, src_h = CFG_C.width // 2, CFG_C.height
    dst_w, dst_h = CFG_D.width_D, CFG_D.height_D
    scale_x, scale_y = dst_w / src_w, dst_h / src_h
    P1_ds = P1_cam.copy(); P1_ds[0, :] *= scale_x; P1_ds[1, :] *= scale_y
    crop = getattr(CFG_D, 'rect_crop', 0)   # folded into the rectify maps on robots that use it (023); absent on e.g. 091 -> the crop math degenerates to a no-op
    cx_ds = P1_ds[0, 2]; fy_ds = P1_ds[1, 1]; cy_ds = P1_ds[1, 2]
    uncropped_h = dst_h - 2 * crop
    fy_final = fy_ds * dst_h / uncropped_h
    cy_final = (cy_ds - crop) * dst_h / uncropped_h
    # Extrinsic comes from whichever daemon actually OWNS camera.depth on this robot: on
    # bb-023 that's depth_b (l_narrow), whose height/pitch/roll differ from the stock depth
    # daemon's baked-in matrix (1.20m/60deg/1deg roll vs 1.50m/35deg/none) — using the stock
    # one projected the cloud at a visibly wrong heading. Intrinsics deliberately stay on the
    # stock camera_cal() derivation above: depth_b's 640x384 output keeps the stock rectified
    # contract, and mapping_v1 (consuming this same topic) derives intrinsics the same way.
    # Extrinsic + range gates come from whichever config family the daemon that OWNS
    # camera.depth actually uses: on 023 depth_b registers its own; on 091 the depth_b daemon
    # borrows depth_custom's configs (its constants.py registers nothing); stock depth is the
    # last resort. First config that exists wins. The gates ship to the client so the viz
    # drops the same pixels the daemon's own point pipeline does (confidence/validity zeros
    # are already baked into the image; max_depth and the horizontal base-frame max_radius
    # are points-stage cuts the client replicates).
    cands = []
    for _name in ("depth_b", "depth_custom", "depth"):
        try:
            _c = Config(_name)
            cands.append((_name, _c.T_base_cam.mat(),
                          float(getattr(_c, 'max_depth_m', 5.0)),
                          float(getattr(_c, 'max_radius_m', 0.0) or 0.0)))
        except Exception:
            continue
    if not cands:
        raise RuntimeError("no depth config with T_base_cam found")
    name, T_bc, max_d, max_r = cands[0]
    fx, fy, cx, cy = fx_ds, fy_final, cx_ds, cy_final

    # --- Data-driven self-calibration against the daemon's OWN output ---
    # The config-derived intrinsics above assume the daemon rectifies the way the stock
    # pipeline does — which broke the moment a reflash changed the daemon's internals
    # (measured live on 091: 0.78m mean disagreement, the whole cloud sat ~0.5m too far
    # forward). camera.points carries the ground truth: mask indexes the depth image, so
    # (pixel, depth) -> base-frame point pairs let us fit fx/fy/cx/cy per axis linearly and
    # pick whichever candidate extrinsic minimizes pixel residual. Whatever the daemon
    # actually does, this matches it by construction. Falls back to config-derived values
    # when the depth daemon isn't publishing (e.g. viz-only sessions).
    try:
        # Accumulate pairs across SEVERAL distinct frames: a single frame can be degenerate
        # (facing one flat wall -> the per-axis fit goes ill-conditioned; seen live as a 9.7px
        # fit that had to be rejected). Points are BASE-frame (robot-relative), so the
        # pixel->point mapping is identical every frame and frames pool cleanly even while
        # the robot is driving. SAME-FRAME pairing per sample is still load-bearing: both
        # topics publish from one internal frame with an identical timestamp.
        _packs = []
        _last_ts = None
        with Reader("camera.depth", keeptime=False) as _rd, \
             Reader("camera.points", keeptime=False) as _rp:
            _t0 = time.time()
            while time.time() - _t0 < 6.0 and len(_packs) < 6:
                if _rd.ready() and _rp.ready():
                    _ts = _rd.data['timestamp']
                    if _ts == _rp.data['timestamp'] and _ts != _last_ts:
                        _n = int(_rp.data['num_points'])
                        if _n > 500:
                            _last_ts = _ts
                            _packs.append((_rd.data['depth'].copy(),
                                           _rp.data['points'][:_n].astype(np.float64),
                                           _rp.data['mask'][:_n].copy()))
                time.sleep(0.05)
        depth_img = _packs[0][0] if _packs else None
        if depth_img is not None:
            H, W = depth_img.shape
            dst_w, dst_h = W, H          # trust the LIVE image shape over config math
            _rng = np.random.default_rng(0)
            _us = []; _vs = []; _zs = []; _Ps = []
            for _dimg, _Pp, _Mm in _packs:
                _idx = _rng.choice(len(_Pp), min(3000, len(_Pp)), replace=False)
                _m = _Mm[_idx]
                _uu = (_m % W).astype(np.float64); _vv = (_m // W).astype(np.float64)
                _zz = _dimg[(_m // W), (_m % W)].astype(np.float64) / 1000.0
                _ok = _zz > 0.15
                _us.append(_uu[_ok]); _vs.append(_vv[_ok]); _zs.append(_zz[_ok]); _Ps.append(_Pp[_idx][_ok])
            _u = np.concatenate(_us); _v = np.concatenate(_vs)
            _z = np.concatenate(_zs); _P = np.concatenate(_Ps)
            best = None
            for _nm, _T, _md, _mr in cands:
                _c = (_P - _T[:3, 3]) @ _T[:3, :3]        # R^T (P - t), row-wise
                _zz = _c[:, 2]
                _g = _zz > 0.15
                if _g.sum() < 200:
                    continue
                _a = _c[_g, 0] / _zz[_g]; _b = _c[_g, 1] / _zz[_g]
                _A = np.stack([_a, np.ones_like(_a)], 1)
                _fx, _cx = np.linalg.lstsq(_A, _u[_g], rcond=None)[0]
                _B = np.stack([_b, np.ones_like(_b)], 1)
                _fy, _cy = np.linalg.lstsq(_B, _v[_g], rcond=None)[0]
                _res = float(np.hypot(_A @ [_fx, _cx] - _u[_g], _B @ [_fy, _cy] - _v[_g]).mean())
                if best is None or _res < best[0]:
                    best = (_res, _nm, _fx, _fy, _cx, _cy, _T, _md, _mr)
            if best is not None and best[0] < 3.0:
                _res, name, fx, fy, cx, cy, T_bc, max_d, max_r = best
                # Rigid residual refinement: a per-axis pinhole fit can't absorb a leftover
                # rotation/translation between the assumed extrinsic and the daemon's real one
                # (seen live on 091 as a persistent +0.2m lateral bias — the wheel-mask blob
                # rendered off-center). Kabsch-align our reprojection onto the daemon's points
                # (outlier-trimmed so the edge-pixel range tail can't steer it), fold the
                # correction into T_base_cam, then refit K once in the corrected frame.
                _Rc = np.eye(3); _tc = np.zeros(3)
                for _it in range(2):
                    _xc = (_u - cx) * _z / fx; _yc = (_v - cy) * _z / fy
                    _cam = np.stack([_xc, _yc, _z], 1)
                    _est = _cam @ T_bc[:3, :3].T + T_bc[:3, 3]
                    _e = np.linalg.norm(_est - _P, axis=1)
                    _k = _e < max(0.3, 3 * float(np.median(_e)))
                    if _k.sum() < 200:
                        break
                    _ma = _est[_k].mean(0); _mb = _P[_k].mean(0)
                    _Hm = (_est[_k] - _ma).T @ (_P[_k] - _mb)
                    _U, _S, _Vt = np.linalg.svd(_Hm)
                    _Rc = _Vt.T @ _U.T
                    if np.linalg.det(_Rc) < 0:
                        _Vt[-1] *= -1; _Rc = _Vt.T @ _U.T
                    _tc = _mb - _Rc @ _ma
                    _Tc = np.eye(4); _Tc[:3, :3] = _Rc; _Tc[:3, 3] = _tc
                    T_bc = _Tc @ T_bc
                    _c2 = (_P - T_bc[:3, 3]) @ T_bc[:3, :3]
                    _zz2 = _c2[:, 2]; _g2 = _zz2 > 0.15
                    if _g2.sum() < 200:
                        break
                    _a2 = _c2[_g2, 0] / _zz2[_g2]; _b2 = _c2[_g2, 1] / _zz2[_g2]
                    _A2 = np.stack([_a2, np.ones_like(_a2)], 1)
                    fx, cx = np.linalg.lstsq(_A2, _u[_g2], rcond=None)[0]
                    _B2 = np.stack([_b2, np.ones_like(_b2)], 1)
                    fy, cy = np.linalg.lstsq(_B2, _v[_g2], rcond=None)[0]
                _ang = math.degrees(math.acos(max(-1.0, min(1.0, (float(np.trace(_Rc)) - 1) / 2))))
                print(f"[depth] self-calibrated+refined: fx={fx:.1f} fy={fy:.1f} cx={cx:.1f} cy={cy:.1f} "
                      f"rigid dR={_ang:.2f}deg dt=({_tc[0]:+.3f},{_tc[1]:+.3f},{_tc[2]:+.3f})m "
                      f"(extrinsic: {name})", flush=True)
            else:
                print(f"[depth] self-calibration fit poor ({best[0]:.1f}px) — keeping config-derived intrinsics"
                      if best else "[depth] self-calibration: not enough valid pairs — keeping config-derived",
                      flush=True)
        else:
            print("[depth] camera.points not publishing — using config-derived intrinsics", flush=True)
    except Exception as _e:
        print(f"[depth] self-calibration skipped ({_e}) — using config-derived intrinsics", flush=True)

    # Arm live IMU-pitch tracking: the daemon post-multiplies its camera_to_base by
    # rot(-X, lean-delta-from-ITS-startup) every frame (balancing robot). We can't know its
    # reference lean, but the rigid alignment above already captured the TOTAL offset at this
    # instant — so tracking our own delta from the lean AT THIS SAME INSTANT composes to the
    # daemon's live extrinsic exactly (same-axis rotations add). depth_loop reads the IMU and
    # ships the delta per frame; the client applies the identical composition.
    try:
        _ic = None
        for _nm in ("depth_b", "depth_custom"):
            try:
                _cc = Config(_nm)
                if getattr(_cc, "imu_pitch", 0):
                    _ic = _cc
                    break
            except Exception:
                continue
        if _ic is not None:
            try:
                _ds = float(Config("drive").sign_pitch)
            except Exception:
                _ds = -1.0
            _depth_imu['idx'] = int(_ic.imu_pitch_idx)
            _depth_imu['sign'] = float(_ic.imu_pitch_sign)
            _depth_imu['lim'] = float(_ic.imu_pitch_max_deg)
            _depth_imu['drive_sign'] = _ds
            with Reader("imu.orientation", keeptime=False) as _ri:
                _t0 = time.time()
                while time.time() - _t0 < 2.0:
                    if _ri.ready():
                        _depth_imu['ref'] = _depth_imu['drive_sign'] * float(
                            np.asarray(_ri.data['rpy'])[_depth_imu['idx']])
                        break
                    time.sleep(0.05)
            if _depth_imu['ref'] is not None:
                print(f"[depth] live imu-pitch tracking armed (ref={_depth_imu['ref']:+.3f}deg, "
                      f"idx={_depth_imu['idx']}, lim={_depth_imu['lim']}deg)", flush=True)
            else:
                print("[depth] imu.orientation not publishing — pitch tracking off", flush=True)
    except Exception as _e:
        print(f"[depth] imu-pitch arm skipped ({_e})", flush=True)

    return {
        'fx': float(fx), 'fy': float(fy), 'cx': float(cx), 'cy': float(cy),
        'w': int(dst_w), 'h': int(dst_h),
        'T_base_cam': [float(x) for x in T_bc.flatten()],   # row-major 4x4
        'max_d': max_d,                                     # (m) match camera.points' range gate
        'max_r': max_r,                                     # (m) horizontal base-frame radius cut, 0=off
    }


# --- p_slam (Auki reloc) integration ---
# p_slam publishes slam.pose as the BASE pose in the GL frame (COLMAP->GL via MW), plus the raw
# odom in vo_pos/vo_quat. We localize/plan in a z-up NAV frame built from the COLMAP
# floor (T_nav_gl, fit at startup). mapping.voxels are in the odom frame (mapping reads vo_*),
# so we fold them into the nav frame via T_nav_odom = T_nav_gl @ T_gl_base @ inv(T_odom_base).
PSLAM_MAP_DIR = Path(__file__).resolve().parent / "maps"   # per-map artifacts live with the app
FLOOR_BAND = 0.08                 # (m) |z| band around the fitted floor (display-cloud clip)

T_nav_gl = np.eye(4)              # GL -> nav (z-up, floor at z=0); set in main() by the floor fit
map_clouds = {}                  # single display cloud "map" (nav-frame xyz f32 + rgb u8)
cloud_source = "map"             # Map button toggles "off" <-> "map"
cloud_names = []                 # built in main()

# Two planning states: Auki Path Mode ON -> given map's grid fused with live obstacles;
# OFF -> live mapping.grid2d only. (plan_source stays "floor"; the fuse sits on top.)
AUKI_GRID_DIR = PSLAM_MAP_DIR
plan_source = "floor"
auki_maps = {}                   # name -> (grid uint8 0/1/2, origin float32[2]); nav frame, res = voxel_size_m
plan_order = ["floor"]           # cycle order, built in main() from the loaded maps
portals_json = b'[]'             # /portals payload: nav-frame portal markers
show_nav_map = False             # toggle: overlay the live mapping voxels (the "waypoint nav map")

OBS_INFLATE = 9
MAX_BROWSER_POINTS = 600000      # hard cap on points sent to the browser (v11-proven)

# --- v23 voxel delta-streaming params ---
VOX_KR = float(CFG_M.voxel_size_m) / 2.0   # cell-key resolution (true voxel grid is res/2)
VOX_OFF = 1 << 20                       # signed-cell offset so keys are non-negative
VOX_COLOR_EPS = 8                       # suppress per-channel color jitter <= this (0..255)
VOX_INTERVAL = 0.5                      # min seconds between voxel delta pushes — dedup+diff
                                        # over the full live cloud (~450k points on bb-023)
                                        # dominates voxel_loop's CPU even after the algorithmic
                                        # speedups below; this only throttles the browser
                                        # point-cloud viz refresh, not navigation
VOX_KF_CHUNK = 40000                    # points per progressive keyframe chunk
VOX_QMAX = 8                            # shallow: a slow link resyncs after ~2s instead of
                                        # dragging a 64-packet tail (the lagging clear-circle)
DEPTH_INTERVAL = 0.3                    # min seconds between live depth-frame pushes — this is
                                        # a single-frame live view (replaced, not accumulated),
                                        # so it doesn't need the map's refresh rate to be fast

# --- Manual WASD teleop (ported from teleop.py) ---
CFG_DRIVE = Config('drive')
# (left_vel, right_vel) m/s per held-key combo; diagonals curve while moving.
WHEEL_VEL_COMBOS = {   # ~1.75x the original teleop.py values (shift still doubles vs no-shift)
    'w': (0.35, 0.35), 's': (-0.35, -0.35), 'a': (-0.25, 0.25), 'd': (0.25, -0.25),
    'wa': (0.09, 0.49), 'wd': (0.49, 0.09), 'sa': (-0.09, -0.49), 'sd': (-0.49, -0.09),
    '': (0.0, 0.0),
}
TELEOP_TIMEOUT = 0.4   # s without a teleop update -> stop (covers a dropped key-up packet)

def teleop_twist(keys, shift, gain):
    """held-key combo -> (v, w) twist. Shift = full speed, else half; gain scales both."""
    vl, vr = WHEEL_VEL_COMBOS.get(keys, (0.0, 0.0))
    mult = (1.0 if shift else 0.5) * float(gain)
    vl *= mult; vr *= mult
    R = CFG_DRIVE.robot_width * 0.5
    return (vl + vr) / 2.0, (vr - vl) / (2.0 * R)

# --- Live-tunable controller/planner params ---
# Edited via UI sliders (applied live). Written/read ONLY when you click Save/Load;
# nothing touches params.txt automatically. Read fresh every loop iteration.
PARAMS = {
    'SPEED':              0.08,     # base forward speed (m/s)
    'MAX_OMEGA':          0.15,     # angular velocity clamp (rad/s)
    'LOOKAHEAD':          0.5,     # pure-pursuit lookahead distance (m)
    'K_CTE':              1.2,     # cross-track error P-gain
    'K_CTE_D':            0.3,     # cross-track error D-gain (damping)
    'CTE_SPEED_K':        80.0,    # off-path speed reduction (higher = slower off path)
    'TURN_SLOW_K':        3.0,     # turning speed reduction (higher = slower when turning)
    'CLEAR_FULL':         0.6,     # wall clearance (m) at/above which full speed
    'CLEAR_MIN':          0.3,    # wall clearance (m) at/below which slowest
    'V_TIGHT_FRAC':       0.4,     # speed floor fraction in tight spaces
    'ROBOT_RADIUS_CELLS': 10,      # hard inflation radius (cells) — planner
    'PROX_WEIGHT':        20000.0, # soft wall-repulsion cost weight — planner
    'REPLAN_INTERVAL':    0.5,     # seconds between periodic replans
    'SMOOTH_V':           0.5,     # forward velocity low-pass (0..1, higher = snappier)
    'SMOOTH_W':           0.6,     # angular velocity low-pass (0..1, higher = snappier)
    'GOAL_TOLERANCE':     0.25,    # arrival radius (m)
    # --- final heading alignment (turn-in-place at the waypoint, slam_reloc style) ---
    'HEADING_OMEGA':      0.15,     # constant turn speed for the heading phase (rad/s) — = slam_reloc max_omega
    'HEADING_TOL':        0.07,    # heading aligned when |error| < this (rad, ~6deg)
    # --- recovery state machine (all delays tunable) ---
    'STUCK_TIME':         15.0,    # seconds of no progress before rotating to rescan
    'ROTATE_TIME':        3.0,     # seconds spent rotating in place per rescan
    'ROTATE_FRAC':        0.5,     # rescan rotate speed as fraction of MAX_OMEGA
    'PROGRESS_EPS':       0.1,     # goal-distance drop (m) that counts as progress
    'N_ROTATIONS':        8,       # rescans before skipping the waypoint (patrol only)
}
INT_PARAMS = {'ROBOT_RADIUS_CELLS', 'N_ROTATIONS'}  # stored as int, not float
PLANNER_PARAMS = {'ROBOT_RADIUS_CELLS', 'PROX_WEIGHT'}  # force replan on change
PARAMS_FILE = Path(__file__).resolve().parent / "params.txt"


def save_params_file():
    with open(PARAMS_FILE, 'w') as f:
        for k, v in PARAMS.items():
            f.write(f"{k}={v}\n")
    print(f"[params] saved -> {PARAMS_FILE}", flush=True)


def load_params_file():
    if not PARAMS_FILE.exists():
        print(f"[params] no file at {PARAMS_FILE}", flush=True)
        return False
    for line in PARAMS_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        k = k.strip()
        if k in PARAMS:
            try:
                PARAMS[k] = int(round(float(v))) if k in INT_PARAMS else float(v)
            except ValueError:
                pass
    print(f"[params] loaded <- {PARAMS_FILE}", flush=True)
    return True


# --- Double-buffered planner output ---

class PlannerOutput:
    """Double-buffered g_cost for lock-free planner->control handoff.
    Control reads active, planner writes work, swap() flips them.
    GIL-safe: int assignment is atomic."""
    __slots__ = ('_bufs', '_idx', 'goal_gi', 'goal_gj', 'passable_count',
                 'replan_ms', 'dist_field', 'grid', 'origin', 'grid_ready')

    def __init__(self):
        self._bufs = [None, None]
        self._idx = 0
        self.goal_gi = 0
        self.goal_gj = 0
        self.passable_count = 0
        self.replan_ms = 0.0
        self.dist_field = None   # EDT result for wall-distance diagnostic
        self.grid = None         # 3-state uint8 grid
        self.origin = np.zeros(2, dtype=np.float32)
        self.grid_ready = False

    def ensure_init(self, GS):
        if self._bufs[0] is None or self._bufs[0].shape[0] != GS:
            self._bufs[0] = np.full((GS, GS), _INF, dtype=np.float64)
            self._bufs[1] = np.full((GS, GS), _INF, dtype=np.float64)

    @property
    def active(self):
        return self._bufs[self._idx]

    @property
    def work(self):
        return self._bufs[1 - self._idx]

    def swap(self):
        self._idx = 1 - self._idx


pout = PlannerOutput()


# --- Shared state ---

waypoints = []
wp_idx = 0
patrol_running = False
patrol_loop = False
global_mode = False    # go to a single goal over mapped floor; stop at closest reachable cell
frontier_active = True    # frontier MODE (DEFAULT ON): plan on the given map + live obstacles
frontier_map = None       # base Auki map of the fused grid (derived from plan_source each tick)
nav_bounds_corners = None
show_floor = False
show_gradient = False
show_slam_path = False
DEPTH_MODES = ("off", "normal", "raw")   # RAW = normal + what the filter removed, in red
show_depth = 0           # live single-frame depth-camera point cloud (replaced every frame,
                          # not accumulated — distinct from the accumulated BBMap/voxel cloud)
cmd_queue = Queue()
goal = None          # control writes, planner reads (GIL-safe ref swap)
robot_status = "idle"
manual_drive = False     # WASD teleop mode (mutually exclusive with autonomous patrol)
_teleop_keys = ''
_teleop_shift = False
_teleop_gain = 100.0
_teleop_t = 0.0          # time of last teleop command (for dead-man timeout)
_replan_needed = False
_vis_dirty = False
_slam_ready = False
_shared_pos = np.zeros(3, dtype=np.float32)  # control writes, planner reads
_map_gen = 0             # bumped on wipe so the browser drops the old cloud + SLAM trail
_wiping = False          # one wipe at a time
_rebuild = {'count': 0, 'frame': 0, 'moved': 0, 'emptied': 0, 'filled': 0, 't': 0.0, 'in_progress': False}

# --- Freshness watchdog: empirical logging to tell "SLAM/mapping stalled" apart from
# "wifi/client fell behind" apart from "resync is just slow" — set from each reader's own
# .ready() so this reflects the RAW publish rate, not whatever reloc_nav's own throttling
# decided to act on. See freshness_watchdog() for the STALL START/END log lines, and
# heavy_ep/broadcast_heavy for the /heavy connect/disconnect/fell-behind log lines.
_last_slam_t = 0.0
_last_grid_t = 0.0
_last_vox_t = 0.0
_FRESH_THRESH = {'slam': 0.5, 'grid': 1.5, 'vox': 2.5}   # seconds; ~3x each source's normal period

# --- /ws: per-client fanout. One shared queue raced by every client meant a second tab STOLE
# half the first tab's state stream (each message consumed by exactly one client) — connections
# looked like they dropped whenever someone else connected. Now every client has its own 1-deep
# latest-state queue and every message goes to ALL clients.
ws_lock = threading.Lock()
ws_clients = []                       # list of Queue(maxsize=2), one per connected /ws client


def broadcast_ws(msg):
    with ws_lock:
        for q in ws_clients:
            put_latest(q, msg)

# --- /heavy: per-client broadcast (voxel keyframe/delta + floor/gradient overlays) ---
# Each connected browser gets its own packet queue. The voxel_loop computes ONE delta and
# broadcasts it to every client; a freshly connected client is sent a full keyframe first
# (built from vox_state) so the map always loads. If a client falls behind, its queue is
# dropped and it is flagged for a fresh keyframe resync — deltas are never applied to a
# stale cloud.
heavy_lock = threading.Lock()
heavy_clients = []                     # list of {'q': Queue, 'resync': [bool]}
vox_state = {'cell': None, 'col': None}  # authoritative deduped cloud for keyframe builds
# Keyframe chunks (compressed bytes) built ONCE per voxel_loop cycle and reused for every
# client resync — building+compressing a multi-hundred-thousand-point keyframe is expensive,
# and on a flaky wifi link a client's /heavy socket can reconnect far more often than the map
# actually changes. Rebuilding per-reconnect (instead of per-update) turned reconnect storms
# into a CPU spiral; caching makes a resync just replay already-compressed bytes.
vox_keyframe_cache = [None]
floor_latest = [None]                  # last floor packet (re-sent to new clients)
heat_latest = [None]                   # last gradient packet
depth_latest = [0, None]               # (seq, pkt) — live depth frames are LATEST-ONLY: they
                                       # never enter the per-client queue. Found live: on a weak
                                       # wifi link, queued ~500KB depth frames arrived seconds
                                       # late (queue depth + TCP buffering) without ever tripping
                                       # the fell-behind overflow — the cloud visibly trailed
                                       # reality. A stale depth frame is worthless; skip to newest.


def put_latest(q, val):
    try: q.put_nowait(val)
    except:
        try: q.get_nowait()
        except: pass
        try: q.put_nowait(val)
        except: pass


def mask_outside_bounds(passable, corners, origin, inv, GS):
    if corners is None:
        return
    gc = np.array([w2g(c[0], c[1], origin, inv) for c in corners], dtype=np.float64)
    before = int(passable.sum())
    cx, cy = gc.mean(axis=0)
    center_in = True
    for k in range(4):
        ax, ay = gc[k]; bx, by = gc[(k + 1) % 4]
        ex, ey = bx - ax, by - ay
        if (-ey * (cx - ax) + ex * (cy - ay)) < 0:
            center_in = False; break
    sign = 1.0 if center_in else -1.0
    ii, jj = np.mgrid[:GS, :GS]
    inside = np.ones((GS, GS), dtype=np.bool_)
    for k in range(4):
        ax, ay = gc[k]; bx, by = gc[(k + 1) % 4]
        ex, ey = bx - ax, by - ay
        inside &= (sign * (-ey * (ii - ax) + ex * (jj - ay))) >= 0
    passable[~inside] = False
    after = int(passable.sum())
    print(f"[bounds] sign={sign} inside={int(inside.sum())} passable: {before}->{after} masked={before-after} gc={gc.tolist()}", flush=True)


def dedup_voxels(coords, colors):
    """Filter ceiling + collapse to unique cells. Returns (ucell int32 (m,3), ucol u8 (m,3),
    ukey int64 (m,) sorted-ascending). Cell = round(coord/VOX_KR); ukey is the stable
    per-cell identity used for delta diffing.

    Uses a stable argsort + a manual first-occurrence mask instead of
    np.unique(return_index=True) — np.unique does the sort AND a second index-recovery pass;
    skipping that second pass measured ~1.3-1.5x faster on real-sized (~450k) clouds while
    staying bit-identical (kind='stable' is required: an unstable sort picks an arbitrary
    duplicate for cells hit by >1 raw point in the same frame, unlike np.unique which always
    keeps the first-in-original-order one — verified against np.unique on synthetic data with
    injected same-cell duplicates before adopting this)."""
    m = coords[:, 2] < 1.5
    coords, colors = coords[m], colors[m]
    if len(coords) == 0:
        return (np.empty((0, 3), np.int32), np.empty((0, 3), np.uint8), np.empty(0, np.int64))
    cell = np.round(coords / VOX_KR).astype(np.int64)
    key = (((cell[:, 0] + VOX_OFF) << 42) | ((cell[:, 1] + VOX_OFF) << 21) | (cell[:, 2] + VOX_OFF))
    order = np.argsort(key, kind='stable')
    skey = key[order]
    first = np.empty(len(skey), dtype=bool)
    first[0] = True
    np.not_equal(skey[1:], skey[:-1], out=first[1:])
    uidx = order[first]
    return cell[uidx].astype(np.int32), colors[uidx], skey[first]


@njit(cache=True)
def _vox_merge_diff(prev_key, prev_col, key, col, color_eps):
    """O(n+m) linear merge-diff of two already-sorted, deduped key arrays — replaces two
    O(n log n) np.searchsorted passes (measured ~6x faster on real-sized clouds, verified
    against the searchsorted-based reference on synthetic add/remove/recolor data).
    Returns (up_idx into key/col, up_n, rm_idx into prev_key, rm_n, new_col)."""
    n, m = key.shape[0], prev_key.shape[0]
    up_idx = np.empty(n, dtype=np.int64); up_n = 0
    rm_idx = np.empty(m, dtype=np.int64); rm_n = 0
    new_col = col.copy()
    a = b = 0
    while a < n and b < m:
        ka, kb = key[a], prev_key[b]
        if ka == kb:
            dr = abs(np.int16(col[a, 0]) - np.int16(prev_col[b, 0]))
            dg = abs(np.int16(col[a, 1]) - np.int16(prev_col[b, 1]))
            db = abs(np.int16(col[a, 2]) - np.int16(prev_col[b, 2]))
            if dr > color_eps or dg > color_eps or db > color_eps:
                up_idx[up_n] = a; up_n += 1
            else:
                new_col[a] = prev_col[b]          # suppress jitter, keep old color (bounded drift)
            a += 1; b += 1
        elif ka < kb:
            up_idx[up_n] = a; up_n += 1            # new cell, wasn't in prev
            a += 1
        else:
            rm_idx[rm_n] = b; rm_n += 1             # prev cell gone from current
            b += 1
    while a < n:
        up_idx[up_n] = a; up_n += 1; a += 1
    while b < m:
        rm_idx[rm_n] = b; rm_n += 1; b += 1
    return up_idx, up_n, rm_idx, rm_n, new_col


def _pack_cells(cell, base):
    return (cell - base).astype(np.uint16).tobytes()


def pack_vox_keyframe_chunks(cell, col):
    """Split the full cloud into self-contained progressive chunks (type 4). The first chunk
    carries first=1 (client resets its cloud); the rest append. Sending many small messages
    instead of one monolithic blob is what makes the map load incrementally and reliably."""
    n = len(cell)
    pkts = []
    if n == 0:
        # empty keyframe = a single reset so a reconnecting client clears a stale cloud
        payload = struct.pack('<iiifI', 0, 0, 0, VOX_KR, 1)
        return [struct.pack('<II', 4, 0) + zlib.compress(payload, 1)]
    total = (n + VOX_KF_CHUNK - 1) // VOX_KF_CHUNK
    for ci in range(total):
        s = ci * VOX_KF_CHUNK; e = min(s + VOX_KF_CHUNK, n)
        c = cell[s:e]; k = col[s:e]
        base = c.min(axis=0)
        payload = (struct.pack('<iiifI', int(base[0]), int(base[1]), int(base[2]), VOX_KR, 1 if ci == 0 else 0)
                   + _pack_cells(c, base) + k.tobytes())
        pkts.append(struct.pack('<II', 4, e - s) + zlib.compress(payload, 1))
    return pkts


def pack_vox_delta(up_cell, up_col, rm_cell):
    """One delta packet (type 5): upsert cells+colors and removal cells, relative to a shared
    base. Tiny — only the cells that actually changed since the client's last state."""
    nu, nr = len(up_cell), len(rm_cell)
    if nu == 0 and nr == 0:
        return None
    if nu and nr:   allc = np.vstack([up_cell, rm_cell])
    elif nu:        allc = up_cell
    else:           allc = rm_cell
    base = allc.min(axis=0)
    payload = (struct.pack('<iiifII', int(base[0]), int(base[1]), int(base[2]), VOX_KR, nu, nr)
               + _pack_cells(up_cell, base) + _pack_cells(rm_cell, base) + up_col.tobytes())
    return struct.pack('<II', 5, nu + nr) + zlib.compress(payload, 1)


def broadcast_heavy(pkt):
    """Push a packet to every connected /heavy client. On overflow, drop the client's queue
    and flag it for a keyframe resync (so deltas are never applied to a stale cloud)."""
    if pkt is None:
        return
    with heavy_lock:
        for c in heavy_clients:
            try:
                c['q'].put_nowait(pkt)
            except Exception:
                if not c['resync'][0]:   # log the transition only, not every dropped packet
                    print(f"[heavy] client fell behind (queue full, still connected) — flagging resync", flush=True)
                c['resync'][0] = True
                while not c['q'].empty():
                    try: c['q'].get_nowait()
                    except Exception: break


def _clear_live_cloud():
    """Drop the in-app voxel/floor overlays and push an empty keyframe so browsers go blank."""
    global _map_gen
    _map_gen += 1
    with heavy_lock:
        vox_state['cell'] = None
        vox_state['col'] = None
        vox_keyframe_cache[0] = None
        floor_latest[0] = None
        heat_latest[0] = None
        for c in heavy_clients:
            c['resync'][0] = True
    empty = pack_vox_keyframe_chunks(np.zeros((0, 3), np.int32), np.zeros((0, 3), np.uint8))
    for pkt in empty:
        broadcast_heavy(pkt)
    z = zlib.compress(b'', 1)
    broadcast_heavy(struct.pack('<II', 2, 0) + z)   # empty floor overlay
    broadcast_heavy(struct.pack('<II', 3, 0) + z)   # empty gradient overlay


def _daemon_running(name):
    return subprocess.run(
        ["pgrep", "-f", f"[d]aemon.py {name}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _stop_daemons(names):
    root = Path(Config('slam').map_path).resolve().parent.parent.parent
    for name in names:
        (root / name / ".stopped").touch()
        subprocess.run(["pkill", "-f", f"python daemon.py {name}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    deadline = time.time() + 8.0
    while time.time() < deadline:
        if not any(_daemon_running(n) for n in names):
            return
        time.sleep(0.2)
    for name in names:
        subprocess.run(["pkill", "-9", "-f", f"python daemon.py {name}"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    time.sleep(0.3)


def _start_daemons(names):
    root = Path(Config('slam').map_path).resolve().parent.parent.parent
    shm = Path("/dev/shm")
    for name in names:
        for leftover in shm.glob(f"{name}.*"):
            leftover.unlink(missing_ok=True)
        (shm / f"{name}_lock").unlink(missing_ok=True)
        (root / name / ".stopped").unlink(missing_ok=True)
    # manager's check_alive() respawns them within ~1s once .stopped is gone


def wipe_slam_and_map():
    """Stop slam+mapping, delete their live maps, restart them into a fresh frame."""
    global robot_status, _slam_ready, _wiping
    stopped = False
    try:
        robot_status = "wiping SLAM + map…"
        _clear_live_cloud()
        print("[wipe] stopping slam + mapping", flush=True)
        _stop_daemons(("slam", "mapping"))
        stopped = True

        # slam's cache = every file next to map_path (slam.bbmap + slam.history + slam.poses and
        # any *.failed-* leftovers); subdirs are left alone. mapping's map = its whole map_dir
        # (frames.bin + tiles/), which the daemon recreates on boot exactly as it does after a
        # fresh-slam boot.
        slam_maps = Path(Config('slam').map_path).parent
        for p in slam_maps.iterdir():
            if p.is_file():
                p.unlink()
        shutil.rmtree(Config('mapping').map_dir, ignore_errors=True)

        print("[wipe] restarting slam + mapping", flush=True)
        _slam_ready = False
        robot_status = "wiped — waiting for SLAM"
        print("[wipe] files gone — slam/mapping will boot into a fresh map", flush=True)
    except Exception as e:
        robot_status = f"wipe failed: {e}"
        print(f"[wipe] FAILED: {e}", flush=True)
        import traceback
        traceback.print_exc()
    finally:
        if stopped:
            _start_daemons(("slam", "mapping"))
        _wiping = False


def rebuild_loop():
    # mapping.reproject flips while a PGO rebuild is in flight; mapping.rebuild is written once
    # per landed rebuild (cumulative count, the frame it landed at, floor cells moved/emptied/filled)
    with Reader("mapping.reproject", keeptime=False) as r_prog, \
         Reader("mapping.rebuild", keeptime=False) as r_done:
        while True:
            try:
                if r_prog.ready():
                    _rebuild['in_progress'] = bool(r_prog.data['reprojecting'])
                if r_done.ready():
                    d = r_done.data
                    _rebuild.update(count=int(d['count']), frame=int(d['frame']), moved=int(d['num_moved']),
                                    emptied=int(d['num_emptied']), filled=int(d['num_filled']),
                                    t=int(d['timestamp'].view('i8')) / 1e9)   # when mapping wrote it, not when we read it
                    print(f"[rebuild] #{_rebuild['count']} landed at frame {_rebuild['frame']}: floor moved={_rebuild['moved']} "
                          f"emptied={_rebuild['emptied']} filled={_rebuild['filled']}", flush=True)
            except Exception as e:
                print(f"[rebuild] ERROR: {e}", flush=True)
            time.sleep(0.5)


def pack_floor(grid, origin, voxel_size_m):
    fi, fj = np.where(grid == 1)
    if len(fi) == 0: return None
    if len(fi) > 100000:
        idx = np.random.choice(len(fi), 100000, replace=False)
        fi, fj = fi[idx], fj[idx]
    n = len(fi)
    coords = np.zeros((n, 3), dtype=np.float32)
    coords[:, 0] = origin[0] + (fi + 0.5) * voxel_size_m
    coords[:, 1] = origin[1] + (fj + 0.5) * voxel_size_m
    coords[:, 2] = 0.02
    return struct.pack('<II', 2, n) + zlib.compress(coords.tobytes(), 1)


def pack_heatmap(g_cost, origin, voxel_size_m):
    finite = g_cost < _INF
    fi, fj = np.where(finite)
    if len(fi) == 0: return None
    costs = g_cost[fi, fj]
    cmin, cmax = costs.min(), costs.max()
    norm = np.zeros(len(fi), dtype=np.float32) if cmax - cmin < 1e-6 else ((costs - cmin) / (cmax - cmin)).astype(np.float32)
    if len(fi) > 100000:
        idx = np.random.choice(len(fi), 100000, replace=False)
        fi, fj, norm = fi[idx], fj[idx], norm[idx]
    n = len(fi)
    coords = np.zeros((n, 3), dtype=np.float32)
    coords[:, 0] = origin[0] + (fi + 0.5) * voxel_size_m
    coords[:, 1] = origin[1] + (fj + 0.5) * voxel_size_m
    coords[:, 2] = 0.03
    r = np.clip(1.5 - np.abs(4 * norm - 3), 0, 1)
    g = np.clip(1.5 - np.abs(4 * norm - 2), 0, 1)
    b = np.clip(1.5 - np.abs(4 * norm - 1), 0, 1)
    colors = np.zeros((n, 3), dtype=np.uint8)
    colors[:, 0] = (r * 255).astype(np.uint8)
    colors[:, 1] = (g * 255).astype(np.uint8)
    colors[:, 2] = (b * 255).astype(np.uint8)
    return struct.pack('<II', 3, n) + zlib.compress(coords.tobytes() + colors.tobytes(), 1)


def make_state(pos, yaw, goal_pos, path, pred, diag=None, ready=True):
    msg = {"t": "state",
           "ready": bool(ready),
           "rx": round(float(pos[0]), 3), "ry": round(float(pos[1]), 3),
           "rh": round(float(yaw), 4),
           "status": robot_status,
           "wp": wp_idx, "running": patrol_running, "loop": patrol_loop,
           "global": global_mode, "manual": manual_drive,
           "frontier": frontier_active, "frontier_map": frontier_map,
           "floor": show_floor, "gradient": show_gradient, "slam_path": show_slam_path,
           "depth": show_depth,
           "nav_map": show_nav_map, "cloud_source": cloud_source, "cloud_names": cloud_names,
           "plan_source": plan_source, "plan_order": plan_order,
           "map_gen": _map_gen,
           "rebuild": {**_rebuild, "age": round(time.time() - _rebuild["t"], 1) if _rebuild["t"] else None},
           "waypoints": [[round(float(w[0]), 3), round(float(w[1]), 3),
                          (None if (len(w) < 3 or w[2] is None) else round(float(w[2]), 4))] for w in waypoints]}
    if goal_pos is not None:
        msg["gx"] = round(float(goal_pos[0]), 3)
        msg["gy"] = round(float(goal_pos[1]), 3)
    if path:
        step = max(1, len(path) // 200); sampled = path[::step]
        msg["path"] = [[round(float(p[0]), 3) for p in sampled],
                       [round(float(p[1]), 3) for p in sampled]]
    if pred:
        msg["pred"] = [[round(float(p[0]), 3) for p in pred],
                       [round(float(p[1]), 3) for p in pred]]
    if global_mode and path and len(path) > 1:
        # breadcrumbs every ~0.2m along the mapped-floor route to the (closest reachable) goal
        stepm = max(1, int(round(0.2 / CFG_M.voxel_size_m)))
        msg["route_wps"] = [[round(float(p[0]), 3), round(float(p[1]), 3)] for p in path[::stepm]]
    if diag:
        msg["diag"] = diag
    return json.dumps(msg)


def control_loop():
    """Crash guard: if the control body ever raises, log it and restart the loop instead of
    silently killing the thread — a dead control thread zombifies the whole app (no state
    broadcasts -> every client stuck on 'connecting', no command handling, no drive output)."""
    while True:
        try:
            _control_loop_impl()
        except Exception:
            import traceback
            print("[ctrl] CRASHED — restarting control loop", flush=True)
            traceback.print_exc()
            time.sleep(1.0)


def _control_loop_impl():
    global wp_idx, patrol_running, patrol_loop, waypoints, robot_status, nav_bounds_corners
    global frontier_active, frontier_map
    global show_floor, show_gradient, show_slam_path, show_depth, goal, _replan_needed, _vis_dirty
    global _slam_ready, _shared_pos, global_mode, show_nav_map, cloud_source, plan_source
    global manual_drive, _teleop_keys, _teleop_shift, _teleop_gain, _teleop_t, _last_slam_t
    global _wiping

    with Writer("drive.ctrl", Type("drive_ctrl")) as w_drive, \
         Reader("slam.pose") as r_slam:

        pos = np.zeros(3, dtype=np.float32)
        path = []
        _path_i = np.empty(500, dtype=np.int32)
        _path_j = np.empty(500, dtype=np.int32)
        smooth_v = smooth_w = 0.0
        slam_ready = False
        last_state_t = last_path_t = 0.0
        # Stuck recovery
        last_goal_dist = float('inf')
        last_progress_t = 0.0
        last_move_pos = np.zeros(2, dtype=np.float32)   # robot pos at last detected movement
        stuck_rotating = False
        stuck_rotate_start = 0.0
        stuck_rotations = 0
        aligning = False                                # final turn-in-place to the waypoint heading
        prev_cte = 0.0
        # Diagnostics
        diag = {}

        while True:
            now = time.time()

            # Read SLAM pose (p_slam GL base pose) -> nav frame (same frame mapping builds in)
            if r_slam.ready():
                _last_slam_t = now
                T_nav_base = T_nav_gl @ G.quat_to_mat(r_slam.data['pos'], r_slam.data['quat'])
                new_x, new_y = float(T_nav_base[0, 3]), float(T_nav_base[1, 3])
                new_yaw = G.yaw_from_R(T_nav_base[:3, :3])
                if not slam_ready:
                    slam_ready = True
                    _slam_ready = True
                    if robot_status.startswith('wiped'):
                        robot_status = 'idle'
                    print(f"[ctrl] SLAM ready pos=({new_x:.2f}, {new_y:.2f})", flush=True)
                # RAW slam pose — no smoothing of any kind (deliberate): loop-closure and
                # relock jumps snap immediately, downstream sees exactly what SLAM publishes.
                pos[0], pos[1], pos[2] = new_x, new_y, new_yaw
                _shared_pos[:] = pos

            # Drain commands
            while not cmd_queue.empty():
                try:
                    cmd = cmd_queue.get_nowait()
                    if cmd['type'] == 'add_wp':
                        h = cmd.get('h')                                  # optional final heading (rad)
                        waypoints.append((cmd['x'], cmd['y'], None if h is None else float(h)))
                        print(f"[wp] added #{len(waypoints)-1}: ({cmd['x']:.2f}, {cmd['y']:.2f}) "
                              f"h={'%.0fdeg' % math.degrees(h) if h is not None else 'none'}", flush=True)
                    elif cmd['type'] == 'start':
                        if waypoints:
                            manual_drive = False   # autonomous and manual are mutually exclusive
                            patrol_running = True; wp_idx = 0
                            goal = np.array(waypoints[0][:2], dtype=np.float32)
                            path = []; _replan_needed = True
                            last_goal_dist = float('inf'); last_progress_t = now
                            stuck_rotations = 0; stuck_rotating = False; aligning = False
                            print(f"[wp] patrol started, {len(waypoints)} waypoints", flush=True)
                    elif cmd['type'] == 'stop':
                        patrol_running = False; goal = None; path = []
                        smooth_v = smooth_w = 0.0
                        print("[wp] patrol stopped", flush=True)
                    elif cmd['type'] == 'loop':
                        patrol_loop = cmd['enabled']
                        print(f"[wp] loop={'ON' if patrol_loop else 'OFF'}", flush=True)
                    elif cmd['type'] == 'clear':
                        waypoints = []; wp_idx = 0
                        patrol_running = False; goal = None; path = []
                        smooth_v = smooth_w = 0.0
                        print("[wp] cleared all waypoints", flush=True)
                    elif cmd['type'] == 'nav_bounds':
                        c = cmd.get('corners')
                        if c is None:
                            nav_bounds_corners = None
                            print("[wp] bounds cleared", flush=True)
                        else:
                            nav_bounds_corners = [(float(p[0]), float(p[1])) for p in c]
                            print(f"[wp] bounds set", flush=True)
                        _replan_needed = True; _vis_dirty = True
                    elif cmd['type'] == 'toggle':
                        key = cmd['key']
                        if key == 'floor':
                            show_floor = not show_floor; _vis_dirty = True
                        elif key == 'gradient':
                            show_gradient = not show_gradient; _vis_dirty = True
                        elif key == 'slam_path':
                            show_slam_path = not show_slam_path
                        elif key == 'depth':
                            show_depth = (show_depth + 1) % 3
                            print(f"[wp] show_depth={DEPTH_MODES[show_depth]}", flush=True)
                        elif key == 'nav_map':
                            show_nav_map = not show_nav_map; _replan_needed = True
                            print(f"[wp] nav_map(live)={'ON' if show_nav_map else 'OFF'}", flush=True)
                        elif key == 'map':
                            order = ['off'] + cloud_names
                            i = order.index(cloud_source) if cloud_source in order else 0
                            cloud_source = order[(i + 1) % len(order)]
                            print(f"[wp] cloud={cloud_source}", flush=True)
                        elif key == 'global':
                            global_mode = not global_mode; _replan_needed = True
                            print(f"[wp] global_mode={'ON' if global_mode else 'OFF'}", flush=True)
                        elif key == 'frontier':
                            # Auki Path Mode: while on, ALL planning runs on the fused grid =
                            # given map + live obstacles (see planner_loop). Needs a map installed.
                            if not auki_maps:
                                robot_status = "no map installed — Auki Path Mode unavailable"
                                print("[wp] frontier toggle refused: no map", flush=True)
                            else:
                                frontier_active = not frontier_active; _replan_needed = True
                                print(f"[wp] frontier={'ON' if frontier_active else 'OFF'}", flush=True)
                        elif key == 'manual':
                            manual_drive = not manual_drive
                            if manual_drive:        # taking manual control stops the patrol
                                patrol_running = False; goal = None; path = []
                                smooth_v = smooth_w = 0.0
                            _teleop_keys = ''       # start from a stopped state
                            print(f"[wp] manual_drive={'ON' if manual_drive else 'OFF'}", flush=True)
                    elif cmd['type'] == 'teleop':
                        _teleop_keys = cmd.get('keys', '')
                        _teleop_shift = bool(cmd.get('shift', False))
                        _teleop_gain = float(cmd.get('gain', 1.0))
                        _teleop_t = now
                    elif cmd['type'] == 'remove_last':
                        if waypoints:
                            waypoints.pop()
                            if wp_idx >= len(waypoints):
                                wp_idx = 0
                                if patrol_running and waypoints:
                                    goal = np.array(waypoints[0][:2], dtype=np.float32)
                                    _replan_needed = True
                                elif not waypoints:
                                    patrol_running = False; goal = None; path = []
                    elif cmd['type'] == 'set_param':
                        k = cmd.get('key'); val = cmd.get('value')
                        if k in PARAMS and val is not None:
                            PARAMS[k] = int(round(float(val))) if k in INT_PARAMS else float(val)
                            if k in PLANNER_PARAMS:
                                _replan_needed = True
                    elif cmd['type'] == 'save_params':
                        save_params_file()
                    elif cmd['type'] == 'load_params':
                        ok = load_params_file()
                        _replan_needed = True
                        broadcast_ws(json.dumps(
                            {"t": "params", "params": dict(PARAMS), "loaded": bool(ok)}))
                    elif cmd['type'] == 'wipe_map':
                        if not _wiping:
                            _wiping = True
                            patrol_running = False
                            manual_drive = False
                            waypoints = []; wp_idx = 0
                            goal = None; path = []
                            smooth_v = smooth_w = 0.0
                            slam_ready = False
                            _slam_ready = False
                            pos[:] = 0
                            _shared_pos[:] = pos
                            robot_status = "wiping SLAM + map…"
                            print("[wipe] requested from UI", flush=True)
                            threading.Thread(target=wipe_slam_and_map, daemon=True).start()
                except: pass

            # Grab planner state (local refs, safe even if planner swaps)
            g_cost = pout.active
            grid = pout.grid
            origin = pout.origin.copy()
            GS = grid.shape[0] if grid is not None else 0
            # Plan-source switches (auki <-> floor) RESIZE the grid, and these reads are not
            # atomic vs the planner thread — across a swap g_cost can still be sized for the
            # old grid while grid/origin are the new ones, so ri/rj bounds-checked against GS
            # index out of g_cost (this killed the control thread once). If the cost buffer
            # doesn't match the grid, treat planner output as not-ready this tick; a
            # consistent replan lands within REPLAN_INTERVAL.
            if g_cost is not None and g_cost.shape[0] != GS:
                g_cost = None

            # Check arrival — same sequential waypoint advance in both modes. (Global mode
            # only changes HOW the planner targets each waypoint: greedy creep over mapped
            # floor toward the closest reachable cell, never skipping the waypoint.)
            wp_h = (waypoints[wp_idx][2] if (patrol_running and wp_idx < len(waypoints)
                    and len(waypoints[wp_idx]) > 2) else None)
            do_advance = False
            if patrol_running and goal is not None and not aligning and \
                    np.linalg.norm(goal - pos[:2]) < PARAMS['GOAL_TOLERANCE']:
                if wp_h is not None:                       # reached position -> turn in place to heading
                    aligning = True
                    robot_status = f"WP {wp_idx} reached — turning to heading"
                else:
                    do_advance = True
            if patrol_running and aligning:
                if wp_h is None:
                    aligning = False; do_advance = True
                else:
                    herr = math.atan2(math.sin(wp_h - pos[2]), math.cos(wp_h - pos[2]))
                    if abs(herr) < PARAMS['HEADING_TOL']:
                        aligning = False; do_advance = True
            if do_advance:
                robot_status = f"arrived at WP {wp_idx}"
                print(f"[wp] arrived at waypoint {wp_idx}", flush=True)
                wp_idx += 1
                if wp_idx >= len(waypoints):
                    if patrol_loop:
                        wp_idx = 0
                        goal = np.array(waypoints[0][:2], dtype=np.float32)
                        _replan_needed = True; path = []
                        robot_status = "looping back to WP 0"
                        last_goal_dist = float('inf'); last_progress_t = now
                        stuck_rotations = 0; stuck_rotating = False
                    else:
                        patrol_running = False; goal = None; path = []
                        smooth_v = smooth_w = 0.0
                        robot_status = "patrol complete"
                else:
                    goal = np.array(waypoints[wp_idx][:2], dtype=np.float32)
                    _replan_needed = True; path = []
                    robot_status = f"heading to WP {wp_idx}"
                    last_goal_dist = float('inf'); last_progress_t = now
                    stuck_rotations = 0; stuck_rotating = False

            # Extract path for viz (every 0.2s)
            if g_cost is not None and goal is not None and GS > 0 and now - last_path_t >= 0.2:
                last_path_t = now
                inv = 1.0 / CFG_M.voxel_size_m
                ri, rj = w2g(pos[0], pos[1], origin, inv)
                n = _extract_path_jit(g_cost, ri, rj, GS, _path_i, _path_j, 500)
                path = [g2w(_path_i[k], _path_j[k], origin, CFG_M.voxel_size_m) for k in range(n)]

            # --- Stuck detection + rotate recovery ---
            v, omega = 0.0, 0.0
            cte = 0.0
            he_deg = 0.0
            la_dist = 0.0
            robot_cost = _INF
            goal_dist = 0.0
            n_path = 0
            # controller term breakdown (for the tuning graphs)
            t_pp = t_p = t_d = 0.0          # ω contributions: pursuit, cross-track P, D
            f_op = f_tn = f_al = f_cl = 1.0  # v multipliers: off-path, turn, align, clearance

            if patrol_running and aligning and wp_h is not None:
                # final heading phase: spin in place at constant speed toward the target heading
                # (slam_reloc commands a constant omega=max_omega; we close the loop with a tol stop)
                herr = math.atan2(math.sin(wp_h - pos[2]), math.cos(wp_h - pos[2]))
                v = 0.0
                omega = PARAMS['HEADING_OMEGA'] * (1.0 if herr >= 0 else -1.0)
                robot_status = f"turning to heading WP {wp_idx} ({math.degrees(herr):+.0f}deg)"
            elif patrol_running and goal is not None:
                goal_dist = float(np.linalg.norm(goal - pos[:2]))
                # Progress = the robot actually MOVED. Distance-to-goal is misleading in
                # global mode: the true goal may be unreachable, yet creeping Euclidean-
                # closer to it (even straight into a dead end) reads as "progress" forever,
                # so the stuck timer never fires and it just keeps nosing forward. Position
                # movement is honest — a robot wedged against an obstacle isn't moving.
                if math.hypot(pos[0] - last_move_pos[0], pos[1] - last_move_pos[1]) > PARAMS['PROGRESS_EPS']:
                    last_move_pos[0], last_move_pos[1] = pos[0], pos[1]; last_progress_t = now
                n_rot = int(PARAMS['N_ROTATIONS'])
                if stuck_rotating:
                    v = 0.0; omega = PARAMS['MAX_OMEGA'] * PARAMS['ROTATE_FRAC']
                    robot_status = f"stuck — rotating to rescan ({stuck_rotations}/{n_rot})"
                    if now - stuck_rotate_start > PARAMS['ROTATE_TIME']:
                        stuck_rotating = False
                        _replan_needed = True
                        last_progress_t = now
                        print(f"[ctrl] rotation done, replanning", flush=True)
                elif (now - last_progress_t > PARAMS['STUCK_TIME']) and not stuck_rotating:
                    stuck_rotations += 1
                    if stuck_rotations > n_rot and not global_mode:
                        # global mode has one goal and never skips it
                        print(f"[ctrl] skipping unreachable WP {wp_idx}", flush=True)
                        wp_idx += 1
                        if wp_idx >= len(waypoints):
                            if patrol_loop: wp_idx = 0
                            else:
                                patrol_running = False; goal = None; path = []
                                robot_status = "patrol complete"
                        if patrol_running and wp_idx < len(waypoints):
                            goal = np.array(waypoints[wp_idx][:2], dtype=np.float32)
                            _replan_needed = True; path = []
                            last_goal_dist = float('inf'); last_progress_t = now
                            stuck_rotations = 0
                    else:
                        stuck_rotating = True; stuck_rotate_start = now
                        print(f"[ctrl] no progress {PARAMS['STUCK_TIME']:.0f}s, rotating ({stuck_rotations}/{n_rot})", flush=True)

            # --- Pure pursuit ---
            if patrol_running and goal is not None and g_cost is not None and grid is not None and not stuck_rotating and not aligning:
                inv = 1.0 / CFG_M.voxel_size_m
                ri, rj = w2g(pos[0], pos[1], origin, inv)
                robot_cost = g_cost[ri, rj] if 0 <= ri < GS and 0 <= rj < GS else _INF
                if robot_cost >= _INF:
                    robot_status = "stuck — no path to goal"
                else:
                    robot_status = f"navigating WP {wp_idx} — {goal_dist:.1f}m away"
                n_path = _extract_path_jit(g_cost, ri, rj, GS, _path_i, _path_j, 500)
                if n_path > 2:
                    wx_arr = origin[0] + (_path_i[:n_path] + 0.5) * CFG_M.voxel_size_m
                    wy_arr = origin[1] + (_path_j[:n_path] + 0.5) * CFG_M.voxel_size_m

                    # Pure pursuit: lookahead point
                    cum_dist = np.concatenate([[0], np.cumsum(np.sqrt(np.diff(wx_arr)**2 + np.diff(wy_arr)**2))])
                    la_idx = min(np.searchsorted(cum_dist, PARAMS['LOOKAHEAD']), n_path - 1)
                    dx, dy = float(wx_arr[la_idx]) - pos[0], float(wy_arr[la_idx]) - pos[1]
                    L = math.hypot(dx, dy)
                    la_dist = L
                    if L > 0.01:
                        hx, hy = -math.sin(pos[2]), math.cos(pos[2])
                        fwd = hx * dx + hy * dy; lat = -hy * dx + hx * dy
                        kappa = 2.0 * lat / (L * L)

                        # Cross-track error: signed distance from robot to nearest path segment
                        dx_all = wx_arr - pos[0]; dy_all = wy_arr - pos[1]
                        nearest = int(np.argmin(dx_all**2 + dy_all**2))
                        fwd_i = min(nearest + 2, n_path - 1)
                        seg_dx = float(wx_arr[fwd_i] - wx_arr[max(nearest-1, 0)])
                        seg_dy = float(wy_arr[fwd_i] - wy_arr[max(nearest-1, 0)])
                        seg_len = math.hypot(seg_dx, seg_dy)
                        if seg_len > 0.001:
                            ex = pos[0] - float(wx_arr[nearest])
                            ey = pos[1] - float(wy_arr[nearest])
                            cte = (ex * seg_dy - ey * seg_dx) / seg_len
                        else:
                            cte = 0.0
                        cte_d = (cte - prev_cte) / max(STATE_INTERVAL, 0.01)
                        prev_cte = cte

                        # Heading error (degrees)
                        if seg_len > 0.001:
                            he_deg = math.degrees(math.atan2(
                                hx * seg_dy - hy * seg_dx,
                                hx * seg_dx + hy * seg_dy))

                        # Combined: pure pursuit + cross-track PD (each term captured for graphs)
                        v = PARAMS['SPEED']
                        t_pp = v * kappa                       # pursuit steering
                        t_p = PARAMS['K_CTE'] * cte            # cross-track P
                        t_d = PARAMS['K_CTE_D'] * cte_d        # cross-track D
                        omega = t_pp + t_p + t_d
                        f_op = 1.0 / (1.0 + PARAMS['CTE_SPEED_K'] * cte * cte)   # off-path slow
                        v *= f_op
                        f_tn = 1.0 / (1.0 + PARAMS['TURN_SLOW_K'] * abs(omega))  # turn slow
                        v *= f_tn
                        mo = PARAMS['MAX_OMEGA']
                        omega = max(-mo, min(mo, omega))
                        if L > 0.05:
                            f_al = max(0.0, fwd / L)           # heading-alignment gate
                            v *= f_al
                        # Slow down in tight spaces — ramp v with wall clearance (EDT)
                        df = pout.dist_field
                        if df is not None and 0 <= ri < df.shape[0] and 0 <= rj < df.shape[1]:
                            dw = float(df[ri, rj]) * CFG_M.voxel_size_m   # m to nearest wall
                            span = max(1e-3, PARAMS['CLEAR_FULL'] - PARAMS['CLEAR_MIN'])
                            cf = max(0.0, min(1.0, (dw - PARAMS['CLEAR_MIN']) / span))
                            f_cl = PARAMS['V_TIGHT_FRAC'] + (1.0 - PARAMS['V_TIGHT_FRAC']) * cf  # clearance slow
                            v *= f_cl
                elif n_path > 0:
                    wx = float(origin[0] + (_path_i[n_path-1] + 0.5) * CFG_M.voxel_size_m)
                    wy = float(origin[1] + (_path_j[n_path-1] + 0.5) * CFG_M.voxel_size_m)
                    dx, dy = wx - pos[0], wy - pos[1]
                    L = math.hypot(dx, dy)
                    la_dist = L
                    if L > 0.01:
                        hx, hy = -math.sin(pos[2]), math.cos(pos[2])
                        kappa = 2.0 * (-hy * dx + hx * dy) / (L * L)
                        v = PARAMS['SPEED']; omega = v * kappa
                        mo = PARAMS['MAX_OMEGA']
                        omega = max(-mo, min(mo, omega))

            # Velocity smoothing + drive
            if patrol_running:
                smooth_v += PARAMS['SMOOTH_V'] * (v - smooth_v)
                smooth_w += PARAMS['SMOOTH_W'] * (omega - smooth_w)
            elif manual_drive:
                # WASD teleop: drive straight from the held keys (snappy, like teleop.py).
                # Dead-man timeout stops the robot if key updates stop arriving.
                keys = _teleop_keys if (now - _teleop_t) < TELEOP_TIMEOUT else ''
                smooth_v, smooth_w = teleop_twist(keys, _teleop_shift, _teleop_gain)
                if keys: robot_status = f"manual drive [{keys}]"
                elif not _teleop_keys: robot_status = "manual drive — ready"
            else:
                smooth_v = smooth_w = 0.0

            # Bounds safety: stop forward motion if robot is near/outside bounds edge
            # (autonomous only — manual teleop is an explicit operator override)
            if not manual_drive and nav_bounds_corners is not None and smooth_v > 0:
                corners = nav_bounds_corners
                rx, ry = float(pos[0]), float(pos[1])
                # Check if robot is inside bounds polygon (same winding logic)
                cx = sum(c[0] for c in corners) / 4
                cy = sum(c[1] for c in corners) / 4
                center_in = True
                for k in range(4):
                    ax, ay = corners[k]; bx, by = corners[(k+1) % 4]
                    ex, ey = bx - ax, by - ay
                    if (-ey * (cx - ax) + ex * (cy - ay)) < 0:
                        center_in = False; break
                sign = 1.0 if center_in else -1.0
                # Signed distance to nearest edge (negative = outside)
                min_dist = float('inf')
                for k in range(4):
                    ax, ay = corners[k]; bx, by = corners[(k+1) % 4]
                    ex, ey = bx - ax, by - ay
                    d = sign * (-ey * (rx - ax) + ex * (ry - ay)) / math.hypot(ex, ey)
                    if d < min_dist:
                        min_dist = d
                if min_dist < 0.15:  # within 15cm of edge or outside
                    smooth_v = 0.0
                    robot_status = "stopped — at bounds edge"

            if not os.path.exists("/dev/shm/drive.ctrl"):   # a full `restart` rm's /dev/shm/*.ctrl: our segment is an orphan inode
                w_drive.__exit__(None, None, None)
                w_drive = Writer("drive.ctrl", Type("drive_ctrl"))
            w_drive['twist'] = np.array([smooth_v, smooth_w], dtype=np.float32)

            # Build diagnostics
            diag = {}
            if patrol_running and goal is not None:
                diag['v'] = round(float(smooth_v), 3)
                diag['w'] = round(float(smooth_w), 3)
                diag['cte'] = round(float(cte) * 100, 1)   # cm
                diag['he'] = round(float(he_deg), 1)        # degrees
                diag['la'] = round(float(la_dist), 3)       # m
                # controller term breakdown (ω = pursuit + cte_P + cte_D; v *= factors)
                diag['t_pp'] = round(float(t_pp), 3)
                diag['t_p'] = round(float(t_p), 3)
                diag['t_d'] = round(float(t_d), 3)
                diag['f_op'] = round(float(f_op), 2)
                diag['f_tn'] = round(float(f_tn), 2)
                diag['f_al'] = round(float(f_al), 2)
                diag['f_cl'] = round(float(f_cl), 2)
                diag['rc'] = round(float(robot_cost), 1) if robot_cost < _INF else None
                ggi, ggj = pout.goal_gi, pout.goal_gj
                gc_val = float(g_cost[ggi, ggj]) if g_cost is not None and 0 <= ggi < GS and 0 <= ggj < GS else _INF
                diag['gc'] = round(gc_val, 1) if gc_val < _INF else None
                diag['pc'] = int(pout.passable_count)
                diag['rpt'] = round(float(pout.replan_ms), 1)
                # Wall distance from EDT
                df = pout.dist_field
                if df is not None and GS > 0:
                    inv = 1.0 / CFG_M.voxel_size_m
                    ri, rj = w2g(pos[0], pos[1], origin, inv)
                    dw_cells = float(df[ri, rj]) if 0 <= ri < df.shape[0] and 0 <= rj < df.shape[1] else 0.0
                    diag['dw'] = round(float(dw_cells * CFG_M.voxel_size_m), 2)
                diag['dg'] = round(float(goal_dist), 2)
                # Dist to nearest path point
                if n_path > 0:
                    px = origin[0] + (_path_i[:n_path] + 0.5) * CFG_M.voxel_size_m
                    py = origin[1] + (_path_j[:n_path] + 0.5) * CFG_M.voxel_size_m
                    dp = float(np.sqrt(np.min((px - pos[0])**2 + (py - pos[1])**2)))
                    diag['dp'] = round(dp, 2)
                # --- recovery state machine (what it's doing and the timers) ---
                n_rot = int(PARAMS['N_ROTATIONS'])
                if stuck_rotating:
                    diag['st'] = 'ROTATING'
                    diag['tr'] = round(now - stuck_rotate_start, 1)     # s into this rotation
                elif robot_cost >= _INF:
                    diag['st'] = 'NO PATH'
                else:
                    diag['st'] = 'NAV'
                diag['tp'] = round(now - last_progress_t, 1)           # s since last progress
                diag['stk'] = round(float(PARAMS['STUCK_TIME']), 1)    # rotates when tp exceeds this
                diag['rot'] = f"{stuck_rotations}/{n_rot}"             # rescans used / max

            # State broadcast — ALWAYS streams, even before the first QR lock. Gating on
            # slam_ready meant zero messages until the robot saw a wall QR, so every tab sat
            # on "Connecting..." with a perfectly healthy socket. Pre-lock states carry
            # ready=false and the UI says what's actually happening.
            if now - last_state_t >= STATE_INTERVAL:
                last_state_t = now
                # Predicted controller trajectory: roll out current (v, ω) as a
                # constant-curvature arc — shows where the commanded twist actually leads.
                pred = []
                if patrol_running and smooth_v > 0.01:
                    px, py, pth = float(pos[0]), float(pos[1]), float(pos[2])
                    dt = 0.12
                    for _ in range(25):  # ~3s horizon
                        px += smooth_v * (-math.sin(pth)) * dt
                        py += smooth_v * (math.cos(pth)) * dt
                        pth += smooth_w * dt
                        pred.append((px, py))
                try:
                    broadcast_ws(make_state(pos[:2], pos[2], goal, path, pred, diag,
                                            ready=slam_ready))
                except Exception as e:
                    print(f"[ctrl] state error: {e}", flush=True)

            # No explicit sleep here: r_slam.ready() and w_drive[...] above both already pace
            # this loop via bbos's Loop.keeptime() (gcd of slam.pose's and drive.ctrl's declared
            # periods, ~4-5ms). An extra fixed sleep on top of that only pushed every iteration
            # over its budget, which is why this loop was constantly logging "Loop lagging".


# --- Planner loop (~3Hz) ---

def planner_loop():
    global _replan_needed, _vis_dirty, robot_status, frontier_map, _last_grid_t

    with Reader("mapping.grid2d", keeptime=False) as r_grid:   # nav-frame traversability (mapping uses T_nav_gl)
        _hc = np.empty(_HEAP_CAP, dtype=np.float64)
        _hn = np.empty(_HEAP_CAP, dtype=np.int32)
        last_replan_t = 0.0
        last_floor_vis_t = 0.0
        auki_active = None

        while True:
         try:
            now = time.time()
            pos = _shared_pos.copy()

            # Planning grid source:
            #  - FRONTIER MODE (toggle): every waypoint plans on the GIVEN Auki map (robot
            #    relocalized in it, no pre-mapping needed) with LIVE mapping obstacles OVERLAID
            #    -> dynamic avoidance while traversing the global map. Base = the Plan:Auki
            #    selection (else the first loaded map); live only ADDS obstacles.
            #  - "auki:<name>": inspect that static Auki grid (raw).
            #  - "floor": live mapping grid only (its own obstacle detection).
            fmap = next(iter(auki_maps)) if auki_maps else None
            if frontier_active and fmap is not None:
                frontier_map = fmap                       # published in state for the button label
                ag, ao = auki_maps[fmap]
                new_live = r_grid.ready()
                if new_live:
                    _last_grid_t = now
                fa_key = 'frontier:' + fmap
                if auki_active != fa_key or new_live:
                    map_switched = auki_active != fa_key or not pout.grid_ready
                    auki_active = fa_key
                    fused = ag.copy()
                    try:                                  # stamp live obstacle cells into the Auki grid
                        mg = r_grid.data['grid']; mo = r_grid.data['origin']
                        mi, mj = np.nonzero(mg == 2)
                        if len(mi):
                            res = CFG_M.voxel_size_m
                            ai = np.round((mo[0] + (mi + 0.5) * res - ao[0]) / res).astype(np.int64)
                            aj = np.round((mo[1] + (mj + 0.5) * res - ao[1]) / res).astype(np.int64)
                            v = (ai >= 0) & (ai < fused.shape[0]) & (aj >= 0) & (aj < fused.shape[1])
                            fused[ai[v], aj[v]] = 2
                    except Exception:
                        pass
                    pout.origin[:] = ao
                    pout.grid = fused
                    pout.grid_ready = True
                    pout.ensure_init(fused.shape[0])
                    # Live obstacle refresh alone shouldn't force the expensive EDT+Dijkstra
                    # replan (that's what REPLAN_INTERVAL below is for) — only a real map
                    # switch (or the very first grid) needs an immediate replan.
                    if map_switched:
                        _replan_needed = True
                    # distance_transform_edt inside self_do_floor_vis is as expensive as the
                    # replan EDT — don't run it at the live grid tick rate (~5Hz), only as
                    # often as the plan itself actually changes (measured: this alone was
                    # ~60% of total CPU once the Floor overlay got toggled on).
                    if show_floor and (map_switched or now - last_floor_vis_t >= 5.0):  # floor changes slowly; ~72KB/push was saturating weak wifi at replan cadence
                        last_floor_vis_t = now
                        self_do_floor_vis(fused, inflate=True)
            else:
                auki_active = None
                grid_ready = r_grid.ready()
                if grid_ready:
                    _last_grid_t = now
                if grid_ready and _slam_ready:
                    grid = np.array(r_grid.data['grid'], copy=True)   # 0 unknown, 1 floor, 2 obstacle
                    origin = r_grid.data['origin'].copy()
                    first_grid = not pout.grid_ready
                    pout.origin[:] = origin
                    pout.grid = grid
                    pout.grid_ready = True
                    pout.ensure_init(grid.shape[0])
                    if first_grid:
                        _replan_needed = True
                    if show_floor and (first_grid or now - last_floor_vis_t >= 5.0):  # floor changes slowly; ~72KB/push was saturating weak wifi at replan cadence
                        last_floor_vis_t = now
                        self_do_floor_vis(grid, inflate=True)  # live nav: show inflated passable

            # voxels: now streamed (keyframe + deltas) by the dedicated voxel_loop thread.

            # Floor/gradient vis on toggle/bounds change
            if _vis_dirty and pout.grid is not None:
                _vis_dirty = False
                print(f"[plan] vis_dirty: floor={show_floor} gradient={show_gradient}", flush=True)
                if show_floor:
                    last_floor_vis_t = now
                    self_do_floor_vis(pout.grid)
                if show_gradient and pout.active is not None:
                    hm = pack_heatmap(pout.active, pout.origin, CFG_M.voxel_size_m)
                    if hm:
                        heat_latest[0] = hm; broadcast_heavy(hm)
                        print(f"[plan] gradient vis pushed ({len(hm)} bytes)", flush=True)

            # Periodic replan check
            if goal is not None and pout.grid is not None and not _replan_needed:
                if now - last_replan_t >= PARAMS['REPLAN_INTERVAL']:
                    _replan_needed = True

            # --- Dijkstra ---
            if _replan_needed and goal is not None and pout.grid_ready:
                _replan_needed = False
                t0 = time.time()
                grid = pout.grid
                GS = grid.shape[0]
                inv = 1.0 / CFG_M.voxel_size_m
                origin = pout.origin.copy()

                # Passable = floor cells directly (no dilation needed, mapping is clean)
                passable = (grid == 1)
                mask_outside_bounds(passable, nav_bounds_corners, origin, inv, GS)

                # EDT for wall distance diagnostic + robot radius inflation
                dist = distance_transform_edt(passable)
                pout.dist_field = dist.copy()
                passable[dist < PARAMS['ROBOT_RADIUS_CELLS']] = False
                # Force robot cell passable so it can route back if outside bounds
                ri, rj = w2g(pos[0], pos[1], origin, inv)
                if 0 <= ri < GS and 0 <= rj < GS and not passable[ri, rj]:
                    if grid[ri, rj] != 2:  # don't clear actual obstacles
                        passable[ri, rj] = True

                pout.passable_count = int(passable.sum())

                # Proximity cost — soft repulsion from walls on top of hard inflation
                dist_safe = np.maximum(dist, 1.0)
                prox = (PARAMS['PROX_WEIGHT'] / (dist_safe * dist_safe)).astype(np.float32)

                # Goal cell
                cur_goal = goal
                if cur_goal is None:
                    time.sleep(0.01)
                    continue
                gi, gj = w2g(cur_goal[0], cur_goal[1], origin, inv)
                gi, gj = max(0, min(GS-1, gi)), max(0, min(GS-1, gj))
                if global_mode:
                    # Plan over MAPPED FLOOR ONLY — never route through unmapped space.
                    # If the goal isn't on the robot's reachable floor, retarget to the
                    # reachable floor cell closest to it; the robot drives there and stops.
                    lbl, _ = label(passable, structure=np.ones((3, 3), dtype=np.int32))
                    rc = int(lbl[ri, rj]) if 0 <= ri < GS and 0 <= rj < GS else 0
                    if rc != 0 and (not passable[gi, gj] or int(lbl[gi, gj]) != rc):
                        ci, cj = np.where(lbl == rc)
                        idx = np.argmin((ci - gi)**2 + (cj - gj)**2)
                        gi, gj = int(ci[idx]), int(cj[idx])
                elif not passable[gi, gj]:
                    fi, fj = np.where(passable)
                    if len(fi) > 0:
                        idx = np.argmin((fi-gi)**2 + (fj-gj)**2)
                        gi, gj = int(fi[idx]), int(fj[idx])

                if 0 <= gi < GS and 0 <= gj < GS and passable[gi, gj]:
                    pout.goal_gi, pout.goal_gj = gi, gj
                    _dijkstra_backward(pout.work, passable, prox, gi, gj, GS, _hc, _hn)
                    pout.swap()
                else:
                    pout.work[:, :] = _INF; pout.swap()
                    robot_status = "no path — goal unreachable"

                pout.replan_ms = (time.time() - t0) * 1000
                last_replan_t = time.time()
                print(f"[plan] replan {pout.replan_ms:.0f}ms passable={pout.passable_count}", flush=True)

                # Gradient viz
                if show_gradient:
                    hm = pack_heatmap(pout.active, origin, CFG_M.voxel_size_m)
                    if hm: heat_latest[0] = hm; broadcast_heavy(hm)

            # mapping.grid2d publishes ~5Hz — polling at 100Hz just burns CPU on redundant
            # full-grid IPC copies (each .ready() call copies the whole grid regardless of
            # whether new data arrived).
            time.sleep(0.05)
         except Exception as e:
            print(f"[plan] ERROR: {e}", flush=True)
            import traceback; traceback.print_exc()
            time.sleep(1.0)


def self_do_floor_vis(grid, inflate=True):
    """Push floor visualization. inflate=True shows the actual passable area after robot-radius
    inflation — matches what the planner uses."""
    GS = grid.shape[0]
    inv = 1.0 / CFG_M.voxel_size_m
    passable_vis = (grid == 1).copy()
    if nav_bounds_corners is not None:
        mask_outside_bounds(passable_vis, nav_bounds_corners, pout.origin, inv, GS)
    if inflate:
        # Reuse the replan's own EDT instead of recomputing it — this overlay is meant to
        # show exactly what the planner used (see docstring), so this isn't just an
        # optimization, it keeps the display honest. Only falls back to a fresh EDT before
        # the first replan has run against this grid size (e.g. right after a map switch).
        dist = pout.dist_field
        if dist is None or dist.shape != passable_vis.shape:
            dist = distance_transform_edt(passable_vis)
        passable_vis[dist < PARAMS['ROBOT_RADIUS_CELLS']] = False
    vis_grid = np.zeros_like(grid)
    vis_grid[passable_vis] = 1
    floor_data = pack_floor(vis_grid, pout.origin, CFG_M.voxel_size_m)
    if floor_data:
        floor_latest[0] = floor_data; broadcast_heavy(floor_data)
        print(f"[plan] floor vis pushed ({len(floor_data)} bytes)", flush=True)


# --- Freshness watchdog: EMPIRICAL evidence for "what's actually lagging" ---
# Logs one line per stall EPISODE (start + end with duration), not a continuous stream, so a
# `grep '\[fresh\]' /dev/shm/app-reloc_nav*.log` gives a clean timeline you can correlate
# against `[heavy]` connect/disconnect lines below. If SLAM stalls, that's the slam_v1 daemon.
# If SLAM keeps ticking but GRID/VOX stall, that's the mapping daemon falling behind SLAM. If
# neither stalls but the browser still looks stuck, check the [heavy] lines — that's the
# network/client, not the robot's own pipeline.
def freshness_watchdog():
    stalled = {'slam': False, 'grid': False, 'vox': False}
    stall_t = {'slam': 0.0, 'grid': 0.0, 'vox': 0.0}
    while True:
        time.sleep(0.5)
        now = time.time()
        for name, last_t in (('slam', _last_slam_t), ('grid', _last_grid_t), ('vox', _last_vox_t)):
            if last_t == 0.0:
                continue   # hasn't ticked even once yet — not a stall, just not started
            gap = now - last_t
            is_stalled = gap > _FRESH_THRESH[name]
            if is_stalled and not stalled[name]:
                stalled[name] = True
                stall_t[name] = now
                print(f"[fresh] {name.upper()} STALL START gap={gap:.2f}s (threshold={_FRESH_THRESH[name]}s)", flush=True)
            elif not is_stalled and stalled[name]:
                stalled[name] = False
                print(f"[fresh] {name.upper()} STALL END duration={now - stall_t[name]:.2f}s", flush=True)


# --- Live depth-frame streaming loop (own thread) ---

def depth_loop():
    """Stream raw camera.depth frames (+ robot pose at capture) for client-side unprojection.
    Gated by show_depth: skips the .ready() call (and its ~480KB memcpy) entirely when no
    client has the toggle on, same reasoning as everywhere else in this file about not paying
    IPC copy costs for data nobody's using. This is a live, single-frame view (each push
    replaces the last, unlike the accumulated BBMap/voxel cloud) so it doesn't need — and
    deliberately doesn't get — a fast refresh rate."""
    last_push = 0.0
    pitch_d = 0.0
    with Reader("camera.depth", keeptime=False) as r_depth, \
         Reader("imu.orientation", keeptime=False) as r_imu:
        while True:
            try:
                # Same live pitch correction the daemon applies to its own extrinsic — delta
                # from the reference captured at self-calibration time (see compute_depth_calib).
                if _depth_imu['ref'] is not None and r_imu.ready():
                    _cur = _depth_imu['drive_sign'] * float(np.asarray(r_imu.data['rpy'])[_depth_imu['idx']])
                    _dd = _depth_imu['sign'] * (_cur - _depth_imu['ref'])
                    _lim = _depth_imu['lim']
                    pitch_d = math.radians(max(-_lim, min(_lim, _dd)))
                if show_depth and r_depth.ready():
                    now = time.time()
                    if now - last_push >= DEPTH_INTERVAL:
                        last_push = now
                        d = r_depth.data['depth']              # uint16 (h, w), millimeters
                        frames = d.tobytes()
                        if show_depth == 2:                     # raw = the unfiltered twin of d, same shape
                            frames += r_depth.data['depth_raw'].tobytes()
                        pos = _shared_pos                       # [x, y, yaw], nav frame
                        header = struct.pack('<ffff', float(pos[0]), float(pos[1]), float(pos[2]),
                                             float(pitch_d))
                        pkt = struct.pack('<II', 6, d.size) + zlib.compress(header + frames, 1)
                        with heavy_lock:            # latest-only slot, NOT the queue (see depth_latest)
                            depth_latest[0] += 1
                            depth_latest[1] = pkt
                time.sleep(0.1)
            except Exception as e:
                print(f"[depth] ERROR: {e}", flush=True)
                import traceback; traceback.print_exc()
                time.sleep(1.0)


# --- Voxel delta-streaming loop (~3Hz, own thread) ---

def voxel_loop():
    """Read mapping.voxels, dedup to unique cells, and broadcast only what changed since the
    last frame (added/removed/recolored), suppressing sub-COLOR_EPS color jitter. Keeps the
    authoritative deduped cloud in vox_state for keyframe builds on new connections."""
    global _last_vox_t
    prev_key = prev_col = prev_cell = None     # client-known baseline (sorted by key)
    last_push = 0.0
    seen_gen = _map_gen
    with Reader("mapping.voxels", keeptime=False) as r_vox:
        while True:
            try:
                if _map_gen != seen_gen:
                    seen_gen = _map_gen
                    prev_key = prev_col = prev_cell = None
                vox_ready = r_vox.ready()
                if vox_ready:
                    _last_vox_t = time.time()
                if vox_ready and _slam_ready:
                    now = time.time()
                    if now - last_push >= VOX_INTERVAL:
                        last_push = now
                        nv = int(r_vox.data['num_voxels'])
                        coords = r_vox.data['coords'][:nv].copy()    # already nav frame (mapping uses T_nav_gl)
                        colors = r_vox.data['colors'][:nv].copy()
                        ucell, ucol, ukey = dedup_voxels(coords, colors)
                        # publish authoritative cloud for keyframe builds (exact colors), and
                        # drop the cached keyframe — it's for the previous cloud, a client
                        # resyncing now needs one built from this one.
                        with heavy_lock:
                            vox_state['cell'] = ucell
                            vox_state['col'] = ucol
                            vox_keyframe_cache[0] = None
                        if prev_key is None or len(prev_key) == 0:
                            # first frame: nothing to diff against; connecting clients get a
                            # keyframe. Seed the baseline.
                            prev_key, prev_col, prev_cell = ukey, ucol.copy(), ucell
                        else:
                            # O(n+m) linear merge of two sorted key arrays, instead of two
                            # O(n log n) searchsorted passes — both prev_key and ukey are
                            # already sorted, so a merge is the natural (and ~6x faster,
                            # measured) way to diff them.
                            up_idx, up_n, rm_idx, rm_n, new_col = _vox_merge_diff(
                                prev_key, prev_col, ukey, ucol, VOX_COLOR_EPS)
                            pkt = pack_vox_delta(ucell[up_idx[:up_n]], ucol[up_idx[:up_n]],
                                                  prev_cell[rm_idx[:rm_n]])
                            if pkt is not None:
                                broadcast_heavy(pkt)
                            prev_key, prev_col, prev_cell = ukey, new_col, ucell
                # mapping.voxels publishes ~5Hz and VOX_INTERVAL only acts every 0.5s — polling
                # faster than that just burns CPU/memory on redundant full-record IPC copies
                # (each .ready() call copies the whole fixed-size ~80MB shm record regardless
                # of whether new data arrived).
                time.sleep(0.15)
            except Exception as e:
                print(f"[vox] ERROR: {e}", flush=True)
                import traceback; traceback.print_exc()
                time.sleep(1.0)


# --- Web UI ---

app = FastAPI()
robot_mesh_bytes = b''

HTML = r'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>reloc_nav</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{overflow:hidden;font-family:system-ui;background:#111;color:#fff}
#ui{position:absolute;top:10px;left:10px;z-index:10;display:flex;flex-direction:column;gap:8px;max-width:300px}
.row{display:flex;gap:6px;flex-wrap:wrap}
.btn{padding:6px 14px;border:none;border-radius:5px;font:500 13px system-ui;cursor:pointer;background:#333;color:#fff}
.btn.active{background:#10b981;color:#000}
.btn.danger{background:#ef4444}
.btn:hover{opacity:0.85}
.btn:disabled{opacity:0.45;cursor:not-allowed}
#status{font:13px monospace;color:#10b981;padding:4px}
#rebuild{font:12px monospace;color:#666;padding:0 4px;min-height:14px}
#rebuild.fresh{color:#ef4444;font-weight:bold}
#rebuild.busy{color:#f59e0b}
#wplist{font:11px monospace;color:#888;max-height:200px;overflow-y:auto;padding:4px}
#wplist .active{color:#0ea5e9;font-weight:bold}
#wplist .done{color:#333;text-decoration:line-through}
#info{font:11px monospace;color:#555;position:absolute;bottom:50px;left:10px;z-index:10}
#bounds-overlay{position:absolute;border:2px dashed #0ea5e9;background:rgba(14,165,233,0.1);pointer-events:none;display:none;z-index:15}
#diag{position:absolute;bottom:50px;right:10px;z-index:10;background:rgba(0,0,0,0.85);
  border:1px solid #333;border-radius:6px;padding:8px 10px;font:11px/1.6 monospace;color:#888;
  min-width:220px;display:none}
#diag .w{color:#f59e0b}
#diag .d{color:#ef4444}
#graphs{position:absolute;bottom:46px;left:50%;transform:translateX(-50%);z-index:12;
  background:rgba(0,0,0,0.85);border:1px solid #333;border-radius:6px;padding:6px 8px}
#graphs .ghdr{display:flex;justify-content:space-between;align-items:center;gap:10px;
  font:11px monospace;color:#888;margin-bottom:4px}
#graphs .gbtn{padding:2px 8px;border:none;border-radius:4px;font:600 10px monospace;
  cursor:pointer;background:#333;color:#aaa}
#graphs .gbtn.active{background:#10b981;color:#000}
#gcanvas{display:block;background:#0a0a0a;border-radius:4px}
#glegend{font:10px monospace;color:#888;margin-top:4px}
#params{position:absolute;top:10px;right:10px;z-index:10;background:rgba(0,0,0,0.85);
  border:1px solid #333;border-radius:6px;padding:8px 10px;width:248px;max-height:78vh;overflow-y:auto;
  font:11px monospace;color:#aaa}
#params .phdr{display:flex;justify-content:space-between;align-items:center;
  font:600 12px monospace;color:#10b981;margin-bottom:6px}
#params .phdr .row{gap:5px}
#params #ptip{font:10px monospace;color:#0ea5e9;min-height:26px;margin:2px 0 6px;
  padding:4px 6px;background:rgba(14,165,233,0.08);border-radius:4px}
#params .prow{margin:6px 0}
#params .prow:hover label{color:#fff}
#params .prow label{display:flex;justify-content:space-between;margin-bottom:1px}
#params .prow .pval{color:#0ea5e9}
#params .prow input[type=range]{width:100%;accent-color:#0ea5e9;height:3px}
#params .btn{padding:3px 9px;font:600 10px monospace}
#timeline{position:absolute;bottom:0;left:0;right:0;height:36px;background:rgba(0,0,0,0.9);
  z-index:20;display:flex;align-items:center;padding:0 10px;gap:8px;border-top:1px solid #222}
#timeline input[type=range]{flex:1;accent-color:#0ea5e9;height:3px}
#live-btn{padding:3px 10px;border:none;border-radius:4px;font:600 11px monospace;cursor:pointer;
  background:#10b981;color:#000}
#live-btn.off{background:#333;color:#888}
#scrub-time{font:11px monospace;color:#888;width:60px;text-align:right}
</style>
</head><body>
<div id="ui">
  <div id="status">connecting to robot…</div>
  <div id="rebuild"></div>
  <div class="row">
    <button class="btn" id="startbtn" onclick="doStart()">Start</button>
    <button class="btn" id="stopbtn" onclick="doStop()">Stop</button>
    <button class="btn" id="loopbtn" onclick="doLoop()">Loop: OFF</button>
    <button class="btn" id="globalbtn" onclick="doToggle('global')">Global Goal: OFF</button>
  </div>
  <div class="row">
    <button class="btn" id="manualbtn" onclick="doToggle('manual')">Manual Drive: OFF</button>
    <button class="btn" onclick="doUndo()">Undo</button>
    <button class="btn danger" onclick="doClear()">Clear All</button>
  </div>
  <div class="row">
    <button class="btn" id="drawbtn" onclick="toggleDrawBounds()">Draw Bounds</button>
    <button class="btn" id="clearbtn" onclick="clearBounds()" style="display:none">Clear Bounds</button>
  </div>
  <div class="row">
    <button class="btn active" id="mapbtn" onclick="doToggle('map')">Auki Map: ON</button>
    <button class="btn" id="navmapbtn" onclick="doToggle('nav_map')">BBMap: OFF</button>
  </div>
  <div class="row">
    <button class="btn active" id="frontierbtn" onclick="doToggle('frontier')">Auki Path Mode: ON</button>
  </div>
  <div class="row">
    <button class="btn" id="floorbtn" onclick="doToggle('floor')">Floor</button>
    <button class="btn" id="gradientbtn" onclick="doToggle('gradient')">Gradient</button>
    <button class="btn" id="slambtn" onclick="doToggle('slam_path')">SLAM Path</button>
    <button class="btn" id="depthbtn" onclick="doToggle('depth')">Depth</button>
    <button class="btn" id="chasebtn" onclick="toggleChase()">Chase</button>
    <button class="btn" id="graphbtn" onclick="toggleGraphs()">Graphs</button>
  </div>
  <div class="row">
    <button class="btn danger" id="wipebtn" onclick="doWipeMap()">Wipe SLAM + Map</button>
  </div>
  <div id="wplist"></div>
</div>
<div id="bounds-overlay"></div>
<div id="params">
  <div class="phdr">Tuning
    <div class="row">
      <button class="btn" id="savep" onclick="saveParams()">Save</button>
      <button class="btn" id="loadp" onclick="loadParams()">Load</button>
    </div>
  </div>
  <div id="ptip">hover a parameter for details</div>
  <div id="sliders"></div>
</div>
<div id="diag"></div>
<div id="graphs" style="display:none">
  <div class="ghdr">
    <span>controller</span>
    <span class="grow">
      <button class="gbtn active" onclick="setGraphGroup('steer',this)">steering ω</button>
      <button class="gbtn" onclick="setGraphGroup('vfac',this)">speed factors</button>
      <button class="gbtn" onclick="setGraphGroup('err',this)">tracking error</button>
    </span>
  </div>
  <canvas id="gcanvas" width="560" height="150"></canvas>
  <div id="glegend"></div>
</div>
<div id="info">Double-click to add waypoint | WASD <span id="wasdmode">fly cam</span> (Q/E up/down) | Shift = full speed<br><span style="color:#0ea5e9">plan</span> · <span style="color:#ff3df0">predicted</span> · <span style="color:#ffc800">traveled</span></div>
<div id="timeline">
  <button id="live-btn" onclick="goLive()">LIVE</button>
  <input type="range" id="scrub" min="0" max="0" value="0">
  <span id="scrub-time"></span>
</div>
<script src="https://cdn.jsdelivr.net/npm/pako@2.1.0/dist/pako.min.js"></script>
<script type="importmap">{"imports":{"three":"https://cdn.jsdelivr.net/npm/three@0.160/build/three.module.js","three/addons/":"https://cdn.jsdelivr.net/npm/three@0.160/examples/jsm/"}}</script>
<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';

const scene=new THREE.Scene();
const cam=new THREE.PerspectiveCamera(60,innerWidth/innerHeight,0.1,500);
cam.position.set(0,8,6);
const renderer=new THREE.WebGLRenderer({antialias:true});
renderer.setSize(innerWidth,innerHeight);renderer.setClearColor(0x111111);renderer.outputColorSpace=THREE.LinearSRGBColorSpace;
document.body.appendChild(renderer.domElement);
const ctrl=new OrbitControls(cam,renderer.domElement);
ctrl.target.set(0,0,0);
ctrl.minDistance=0.5;ctrl.maxDistance=120;   // wheel-dolly bounds: past far-plane everything culls to background = "black screen"
scene.add(new THREE.GridHelper(50,50,0x333333,0x222222));
scene.add(new THREE.AmbientLight(0xffffff,0.3));
scene.add(new THREE.DirectionalLight(0xffffff,0.6));

let voxGeo=new THREE.BufferGeometry();
const voxMat=new THREE.PointsMaterial({size:0.04,vertexColors:true,sizeAttenuation:true});
const voxPts=new THREE.Points(voxGeo,voxMat);voxPts.frustumCulled=false;scene.add(voxPts);
voxPts.visible=false;   // the live mapping "Nav Map" overlay is off by default (toggle)

// --- Named COLMAP / slam_viz clouds (LG, LoMa, ...); the Map button cycles which is shown. ---
// nav frame (z-up) -> three (X=x, Y=z, Z=-y), floor at y=0. Fetched once per cloud from /cloud/<name>.
const cloudPts={};   // name -> THREE.Points (only the selected one is visible)
function loadCloud(name){
  fetch('/cloud/'+name).then(r=>r.arrayBuffer()).then(buf=>{
    const dv=new DataView(buf);const n=dv.getUint32(0,true);if(n===0)return;
    const xyz=new Float32Array(buf,4,n*3);const rgb=new Uint8Array(buf,4+n*12,n*3);
    const pos=new Float32Array(n*3),col=new Float32Array(n*3);
    for(let i=0;i<n;i++){const o=i*3;
      pos[o]=xyz[o];pos[o+1]=xyz[o+2];pos[o+2]=-xyz[o+1];
      col[o]=rgb[o]/255;col[o+1]=rgb[o+1]/255;col[o+2]=rgb[o+2]/255;}
    const g=new THREE.BufferGeometry();
    g.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
    g.setAttribute('color',new THREE.Float32BufferAttribute(col,3));
    const p=new THREE.Points(g,new THREE.PointsMaterial({size:0.025,vertexColors:true,sizeAttenuation:true}));
    p.frustumCulled=false;p.visible=false;scene.add(p);cloudPts[name]=p;
  }).catch(e=>console.warn('cloud '+name+' load failed',e));
}
fetch('/clouds').then(r=>r.json()).then(ns=>ns.forEach(loadCloud)).catch(e=>console.warn('clouds list failed',e));

// --- Portal markers (orange cubes at the Auki QR portals) ---
const portalGrp=new THREE.Group();scene.add(portalGrp);
fetch('/portals').then(r=>r.json()).then(ps=>{
  ps.forEach(p=>{
    const s=Math.max(p.size||0.1,0.08);
    const m=new THREE.Mesh(new THREE.BoxGeometry(s,s,s),
      new THREE.MeshBasicMaterial({color:0xffbe00,transparent:true,opacity:0.8}));
    m.position.set(p.pos[0],p.pos[2],-p.pos[1]);portalGrp.add(m);
  });
}).catch(e=>console.warn('No portals:',e));
// --- Persistent voxel cloud: keyframe + deltas applied in place (slot map + swap-remove) ---
// The cloud lives in growable typed arrays; each cell occupies a slot, vSlot maps cellkey->
// slot, vKeyArr is the reverse. Deltas upsert/remove individual cells without rebuilding the
// whole buffer. Server sends only what changed, so this touches ~thousands of points/frame.
const VK_OX=1<<20, VK_OY=1<<20, VK_OZ=512;   // client-internal cell-key packing (own scheme)
function vkey(cx,cy,cz){ return ((cx+VK_OX)*2097152+(cy+VK_OY))*1024+(cz+VK_OZ); } // <2^53, exact
let vCap=0,vCount=0,vPos=null,vCol=null,vKeyArr=null;
const vSlot=new Map();
function vEnsure(need){
  if(need<=vCap)return;
  let nc=Math.max(need, vCap?vCap*2:(1<<19));
  const np_=new Float32Array(nc*3), nco=new Float32Array(nc*3), nk=new Float64Array(nc);
  if(vPos){np_.set(vPos);nco.set(vCol);nk.set(vKeyArr);}
  vPos=np_;vCol=nco;vKeyArr=nk;vCap=nc;
  voxGeo.setAttribute('position',new THREE.BufferAttribute(vPos,3).setUsage(THREE.DynamicDrawUsage));
  voxGeo.setAttribute('color',new THREE.BufferAttribute(vCol,3).setUsage(THREE.DynamicDrawUsage));
}
function vReset(hint){ vCount=0; vSlot.clear(); vEnsure(Math.max(hint||0,1<<17)); }
function vUpsert(key,x,y,z,r,g,b){
  let s=vSlot.get(key);
  if(s===undefined){ s=vCount++; if(s>=vCap)vEnsure(s+1); vSlot.set(key,s); vKeyArr[s]=key; }
  const o=s*3; vPos[o]=x;vPos[o+1]=z;vPos[o+2]=-y; vCol[o]=r;vCol[o+1]=g;vCol[o+2]=b;
}
function vRemove(key){
  const s=vSlot.get(key); if(s===undefined)return;
  const last=vCount-1;
  if(s!==last){ const lo=last*3, so=s*3;
    vPos[so]=vPos[lo];vPos[so+1]=vPos[lo+1];vPos[so+2]=vPos[lo+2];
    vCol[so]=vCol[lo];vCol[so+1]=vCol[lo+1];vCol[so+2]=vCol[lo+2];
    const lk=vKeyArr[last]; vKeyArr[s]=lk; vSlot.set(lk,s);
  }
  vSlot.delete(key); vCount--;
}
function vCommit(){
  voxGeo.setDrawRange(0,vCount);
  if(voxGeo.attributes.position){voxGeo.attributes.position.needsUpdate=true;voxGeo.attributes.color.needsUpdate=true;}
}

let floorGeo=new THREE.BufferGeometry();
const floorMat=new THREE.PointsMaterial({size:0.03,color:0x10b981,transparent:true,opacity:0.5,sizeAttenuation:true,depthTest:false,depthWrite:false});
const floorPts=new THREE.Points(floorGeo,floorMat);floorPts.renderOrder=-1;floorPts.visible=false;scene.add(floorPts);

let heatGeo=new THREE.BufferGeometry();
const heatMat=new THREE.PointsMaterial({size:0.03,vertexColors:true,transparent:true,opacity:0.5,sizeAttenuation:true,depthTest:false,depthWrite:false});
const heatPts=new THREE.Points(heatGeo,heatMat);heatPts.renderOrder=-1;heatPts.visible=false;scene.add(heatPts);

// --- Live depth-camera point cloud: server sends raw depth frames, we unproject here. Each
// frame REPLACES the last (single live view), unlike the accumulated voxel/BBMap cloud.
let depthGeo=new THREE.BufferGeometry();
const depthMat=new THREE.PointsMaterial({size:0.02,vertexColors:true,sizeAttenuation:true});
const depthPts=new THREE.Points(depthGeo,depthMat);depthPts.frustumCulled=false;depthPts.visible=false;scene.add(depthPts);
let depthCalib=null;
const DEPTH_DIFF_MM=20;   // raw vs normal disagreement (mm) that counts as 'the filter changed it'
// Re-fetched on every /heavy (re)connect, not just page load: the server reads the extrinsic
// from constants at ITS startup, so after an app restart the auto-reconnecting tab would
// otherwise keep unprojecting with the stale calib forever — edits to height/pitch/roll
// looked like they "did nothing" unless you remembered to hard-refresh the page.
function fetchDepthCalib(){
  fetch('/depth_calib').then(r=>r.json()).then(c=>{
    if(c && c.fx){depthCalib=c;console.log('depth calib',c);}
    else console.warn('depth calib unavailable');
  }).catch(e=>console.warn('depth calib fetch failed:',e));
}
fetchDepthCalib();

const robotGrp=new THREE.Group();
robotGrp.add(new THREE.Mesh(new THREE.SphereGeometry(0.12),new THREE.MeshBasicMaterial({color:0xffffff})));
robotGrp.add(new THREE.ArrowHelper(new THREE.Vector3(0,0,-1),new THREE.Vector3(0,0,0),0.5,0x10b981,0.15,0.08));
scene.add(robotGrp);

fetch('/robot_mesh').then(r=>{if(!r.ok)throw new Error(r.status);return r.arrayBuffer();}).then(buf=>{
  if(buf.byteLength<8)return;
  const dv=new DataView(buf);const nv=dv.getUint32(0,true);const nf=dv.getUint32(4,true);
  const pos=new Float32Array(nv*3);
  const vOff=8,fOff=8+nv*12;
  for(let i=0;i<nv;i++){
    const j=i*3;const bv=vOff+j*4;
    pos[j]=dv.getFloat32(bv,true);pos[j+1]=dv.getFloat32(bv+8,true);pos[j+2]=-dv.getFloat32(bv+4,true);
  }
  const idx=new Uint32Array(nf*3);
  for(let i=0;i<nf*3;i++)idx[i]=dv.getUint32(fOff+i*4,true);
  const geo=new THREE.BufferGeometry();
  geo.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
  geo.setIndex(new THREE.BufferAttribute(idx,1));geo.computeVertexNormals();
  const mat=new THREE.MeshPhongMaterial({color:0xcccccc,flatShading:true,transparent:true,opacity:0.85,side:THREE.DoubleSide});
  const mesh=new THREE.Mesh(geo,mat);mesh.scale.set(1.2,1.2,1.2);
  robotGrp.add(mesh);
}).catch(e=>console.warn('No robot mesh:',e));

let pathLine=null,goalMk=null,slamLine=null,predLine=null;
const routeGrp=new THREE.Group();scene.add(routeGrp);   // global-mode 0.2m breadcrumbs
let slamTrail=[];   // accumulated live robot positions (client-side, monotonic)
let lastRebuildCount=-1,lastRebuildAt=0;   // rebuild banner: flash red for 8s when mapping.rebuild.count changes
let lastMapGen=0;  // last map_gen we rendered; wipe bumps this and we drop the trail
const wpGrp=new THREE.Group();scene.add(wpGrp);
const wpLineGrp=new THREE.Group();scene.add(wpLineGrp);

let ws, wsHeavy;

// --- Timeline ring buffer ---
const TL_SIZE=9000;   // ~19min of state history at the 8Hz state rate (states are small dicts)
const tlStates=new Array(TL_SIZE);
let tlHead=0,tlCount=0;
const tlVoxSnaps=[];
let lastVoxSnapT=0;
let isLive=true;

function tlRecord(s){
  s._ts=Date.now();
  tlStates[tlHead]=s;
  tlHead=(tlHead+1)%TL_SIZE;
  if(tlCount<TL_SIZE)tlCount++;
  // rerun-style: the timeline keeps EXTENDING while you're scrubbed back — recording never
  // stops, the right end is always "now". Only the thumb position is pinned while scrubbed.
  const sb=document.getElementById('scrub');
  sb.max=Math.max(0,tlCount-1);
  if(isLive)sb.value=tlCount-1;
}
// Snapshot the LIVE cloud (deltas are applied continuously, so the live buffers are always
// current) — lets the scrubber show the map roughly as it was, without server resends.
let voxScrubGeo=null;
let tlVoxBytes=0;
const TL_VOX_BUDGET=600e6;   // retained-snapshot cap; a 1M-pt cloud is ~26MB per snapshot
function tlRecordVox(){
  // 60 snaps x 5s = ~5min of map history, FULL fidelity — scrubbing back must show the map
  // exactly as it was, not a decimated stand-in. Memory is bounded by a byte budget that
  // evicts the OLDEST snapshots (shorter history on huge maps), never by degrading them.
  const now=Date.now();
  if(now-lastVoxSnapT<5000||vCount<=0)return;
  lastVoxSnapT=now;
  const pos=vPos.slice(0,vCount*3),col=vCol.slice(0,vCount*3);
  const bytes=pos.byteLength+col.byteLength;
  tlVoxSnaps.push({ts:now,pos,col,count:vCount,bytes});
  tlVoxBytes+=bytes;
  while(tlVoxSnaps.length>1&&(tlVoxSnaps.length>60||tlVoxBytes>TL_VOX_BUDGET)){
    tlVoxBytes-=tlVoxSnaps.shift().bytes;
  }
}
function showVoxSnapshot(snap){
  if(voxScrubGeo)voxScrubGeo.dispose();
  voxScrubGeo=new THREE.BufferGeometry();
  voxScrubGeo.setAttribute('position',new THREE.Float32BufferAttribute(snap.pos,3));
  voxScrubGeo.setAttribute('color',new THREE.Float32BufferAttribute(snap.col,3));
  voxPts.geometry=voxScrubGeo;
}
// Depth history: snapshot the unprojected live depth cloud every few seconds so scrubbing
// shows the depth view as it was. 100 snaps x 3s = ~5min, ~1MB each at typical valid counts.
const tlDepthSnaps=[];
let lastDepthSnapT=0;
let depthScrubGeo=null;
function tlRecordDepth(pos,col,n){
  const now=Date.now();
  if(now-lastDepthSnapT<3000||n<=0)return;
  lastDepthSnapT=now;
  if(tlDepthSnaps.length>=100)tlDepthSnaps.shift();
  tlDepthSnaps.push({ts:now,pos:pos.slice(0,n*3),col:col.slice(0,n*3)});
}
function showDepthSnapshot(snap){
  if(depthScrubGeo)depthScrubGeo.dispose();
  depthScrubGeo=new THREE.BufferGeometry();
  depthScrubGeo.setAttribute('position',new THREE.Float32BufferAttribute(snap.pos,3));
  depthScrubGeo.setAttribute('color',new THREE.Float32BufferAttribute(snap.col,3));
  depthPts.geometry=depthScrubGeo;depthPts.visible=true;
}
function tlGet(idx){
  if(idx<0||idx>=tlCount)return null;
  return tlStates[(tlHead-tlCount+idx+TL_SIZE)%TL_SIZE];
}
window.goLive=()=>{
  isLive=true;
  voxPts.geometry=voxGeo;   // restore the live (delta-updated) cloud
  depthPts.geometry=depthGeo;depthPts.visible=false;   // next live frame (<0.3s) re-shows if toggled
  for(const k of Object.keys(scrubOverride))delete scrubOverride[k];   // back to server truth
  document.getElementById('live-btn').className='';
  document.getElementById('scrub-time').textContent='';
};
// --- Scrub-time view overrides: toggling a display layer while scrubbed shouldn't poke the
// server (that flips LIVE state the scrubbed view doesn't render — clicks looked dead).
// Instead the toggle is applied locally on top of the historical frame, and cleared on LIVE.
const scrubOverride={};
const SCRUB_VIEW_KEYS=new Set(['floor','gradient','slam_path','nav_map','depth','map']);
let scrubIdx=-1;
function renderScrub(idx){
  const s=tlGet(idx);
  if(!s)return;
  scrubIdx=idx;
  const sv=Object.assign({},s);
  for(const k in scrubOverride){
    if(k==='map')sv.cloud_source=scrubOverride[k];
    else sv[k]=scrubOverride[k];
  }
  const offset=(Date.now()-s._ts)/1000;
  document.getElementById('scrub-time').textContent='-'+offset.toFixed(1)+'s';
  updateState(sv);
  updateDiag(s.diag);
  // Nearest voxel snapshot (gate ~= snapshot cadence + slack); visibility follows sv.nav_map
  if(sv.nav_map&&tlVoxSnaps.length>0){
    let best=tlVoxSnaps[0];
    for(const snap of tlVoxSnaps){
      if(Math.abs(snap.ts-s._ts)<Math.abs(best.ts-s._ts))best=snap;
    }
    if(best.count>0&&Math.abs(best.ts-s._ts)<7000)showVoxSnapshot(best);
  }
  // Floor/gradient only have live-latest geometry (no history) — toggling them on while
  // scrubbed shows the most recent overlay, which beats showing nothing.
  if(sv.floor&&floorGeo.attributes&&floorGeo.attributes.position)floorPts.visible=true;
  if(sv.gradient&&heatGeo.attributes&&heatGeo.attributes.position)heatPts.visible=true;
  // Depth snapshot; hidden when the layer is off or no snapshot is close enough — a frame
  // from "now" shown against a scrubbed-back pose would be silently misleading.
  if(sv.depth&&tlDepthSnaps.length>0){
    let bd=tlDepthSnaps[0];
    for(const snap of tlDepthSnaps){
      if(Math.abs(snap.ts-s._ts)<Math.abs(bd.ts-s._ts))bd=snap;
    }
    if(Math.abs(bd.ts-s._ts)<4500)showDepthSnapshot(bd);
    else depthPts.visible=false;
  }else depthPts.visible=false;
}
document.getElementById('scrub').addEventListener('input',function(){
  const idx=parseInt(this.value);
  // Dragging the thumb all the way to the right end = "catch up to now, keep following" —
  // same as rerun's timeline; the LIVE button remains as an explicit shortcut.
  if(idx>=parseInt(this.max)){goLive();return;}
  isLive=false;
  document.getElementById('live-btn').className='off';
  renderScrub(idx);
});

// --- Diagnostics overlay ---
function updateDiag(d){
  const el=document.getElementById('diag');
  if(!d||Object.keys(d).length===0){el.style.display='none';return;}
  el.style.display='';
  function cv(v,wt,dt,inv){
    if(v==null||v===undefined)return'<span style="color:#555">--</span>';
    let c='';
    if(inv){if(v<dt)c='d';else if(v<wt)c='w';}
    else{if(Math.abs(v)>dt)c='d';else if(Math.abs(v)>wt)c='w';}
    return c?'<span class="'+c+'">'+v+'</span>':''+v;
  }
  const stc=d.st==='NO PATH'?'d':d.st==='ROTATING'?'w':'';
  const stk=(d.stk!=null?d.stk:15);
  el.innerHTML=[
    'state <b><span class="'+stc+'">'+(d.st||'--')+'</span></b>'+(d.tr!=null?' '+d.tr+'s rotating':'')+' rescans '+(d.rot||'--'),
    'no-progress '+cv(d.tp,stk*0.6,stk)+'s / '+(d.stk!=null?d.stk:'--')+'s \u2192 rotate',
    '\u2500\u2500\u2500',
    'v='+cv(d.v,99,99)+' w='+cv(d.w,99,99)+' la='+cv(d.la,99,99),
    'cte='+cv(d.cte,10,20)+'cm he='+cv(d.he,17,46)+'\u00b0',
    'rpt='+cv(d.rpt,150,250)+'ms pc='+cv(d.pc,99,99),
    'dw='+cv(d.dw,0.33,0.20,true)+'m dg='+cv(d.dg,99,99)+'m',
    'dp='+cv(d.dp,99,99)+'m rc='+cv(d.rc,99,99)+' gc='+cv(d.gc,99,99),
    '───',
    'ω: pp='+cv(d.t_pp,99,99)+' P='+cv(d.t_p,99,99)+' D='+cv(d.t_d,99,99),
    'v×: op='+cv(d.f_op,9,9)+' tn='+cv(d.f_tn,9,9)+' al='+cv(d.f_al,9,9)+' cl='+cv(d.f_cl,9,9),
  ].join('<br>');
}

// --- Controller time-series graphs ---
const GRAPH_GROUPS={
  steer:{mode:'center',series:[
    {k:'w',c:'#ffffff',l:'ω'},{k:'t_pp',c:'#22d3ee',l:'pursuit'},
    {k:'t_p',c:'#f59e0b',l:'cte·P'},{k:'t_d',c:'#ff3df0',l:'cte·D'}]},
  vfac:{mode:'unit',series:[
    {k:'f_op',c:'#f59e0b',l:'off-path'},{k:'f_tn',c:'#ff3df0',l:'turn'},
    {k:'f_al',c:'#22d3ee',l:'align'},{k:'f_cl',c:'#a78bfa',l:'clear'}]},
  err:{mode:'center',series:[
    {k:'cte',c:'#ef4444',l:'cte(cm)'},{k:'he',c:'#0ea5e9',l:'he(°)'}]},
};
const GALL=['w','t_pp','t_p','t_d','f_op','f_tn','f_al','f_cl','cte','he'];
const GN=1500; const gbuf={}; GALL.forEach(k=>gbuf[k]=[]);   // ~3min of controller history at 8Hz
let graphGroup='steer', showGraph=false;
function gpush(d){
  if(!d)return;
  GALL.forEach(k=>{const a=gbuf[k];a.push(d[k]!=null?d[k]:0);if(a.length>GN)a.shift();});
  if(showGraph)drawGraph();
}
function drawGraph(){
  const cv=document.getElementById('gcanvas'),ctx=cv.getContext('2d'),W=cv.width,H=cv.height;
  ctx.clearRect(0,0,W,H);
  const g=GRAPH_GROUPS[graphGroup], unit=g.mode==='unit';
  let m=1e-6; if(unit){m=1;}else{g.series.forEach(s=>gbuf[s.k].forEach(v=>m=Math.max(m,Math.abs(v))));}
  // reference lines
  ctx.strokeStyle='#222';ctx.lineWidth=1;ctx.beginPath();
  const y0=unit?H-2:H/2; ctx.moveTo(0,y0);ctx.lineTo(W,y0);
  if(unit){ctx.moveTo(0,2);ctx.lineTo(W,2);} ctx.stroke();
  g.series.forEach(s=>{
    const a=gbuf[s.k];ctx.strokeStyle=s.c;ctx.lineWidth=1.5;ctx.beginPath();
    a.forEach((v,i)=>{const x=W*i/(GN-1);
      const y=unit?(H-2-(v/m)*(H-4)):(H/2-(v/m)*(H/2-6));
      i?ctx.lineTo(x,y):ctx.moveTo(x,y);});
    ctx.stroke();
  });
  document.getElementById('glegend').innerHTML=g.series.map(s=>{
    const a=gbuf[s.k],cur=a.length?a[a.length-1]:0;
    return '<span style="color:'+s.c+'">'+s.l+'='+(+cur).toFixed(2)+'</span>';
  }).join('   ')+'   <span style="color:#555">scale ±'+m.toFixed(2)+(unit?' (0..1)':'')+'</span>';
}
window.toggleGraphs=()=>{showGraph=!showGraph;
  document.getElementById('graphs').style.display=showGraph?'':'none';
  document.getElementById('graphbtn').className=showGraph?'btn active':'btn';
  if(showGraph)drawGraph();};
window.setGraphGroup=(grp,btn)=>{graphGroup=grp;
  document.querySelectorAll('#graphs .gbtn').forEach(b=>b.className='gbtn');
  if(btn)btn.className='gbtn active';drawGraph();};

// Double-click to add waypoint (but not during draw mode)
let drawMode=false;
// double-click = quick waypoint, NO heading (skips the final turn)
renderer.domElement.addEventListener('dblclick',e=>{
  if(drawMode)return;
  if(aimWp)cancelAim();
  const pt=groundAt(e.clientX,e.clientY);
  if(pt&&ws&&ws.readyState===1)ws.send(JSON.stringify({type:'add_wp',x:pt.x,y:-pt.z}));
});

// ctrl/cmd-click = heading waypoint: 1st click drops the point + enters aim mode, move the mouse
// to pivot the heading arrow, 2nd ctrl-click locks the heading. (forward = (-sin h, cos h))
function groundAt(sx,sy){
  const m=new THREE.Vector2((sx/innerWidth)*2-1,-(sy/innerHeight)*2+1);
  const rc=new THREE.Raycaster();rc.setFromCamera(m,cam);
  const pt=new THREE.Vector3();
  return rc.ray.intersectPlane(new THREE.Plane(new THREE.Vector3(0,1,0),0),pt)?pt:null;
}
let aimWp=null;   // {x,y nav, px,pz three} while aiming the heading
const aimArrow=new THREE.ArrowHelper(new THREE.Vector3(0,0,-1),new THREE.Vector3(),0.6,0xffa500,0.18,0.1);
aimArrow.visible=false;scene.add(aimArrow);
function cancelAim(){ aimWp=null; aimArrow.visible=false; ctrl.enabled=true; }
// macOS treats ctrl+click as a secondary click -> suppress the OS context menu on the canvas.
renderer.domElement.addEventListener('contextmenu',e=>e.preventDefault());
// CAPTURE-phase pointerdown: runs before OrbitControls (which pointer-captures on mousedown and
// would otherwise swallow the click). stopPropagation keeps OrbitControls from rotating.
// The ctrl/cmd modifier is only required to START aiming; the 2nd click (lock) is a plain click.
renderer.domElement.addEventListener('pointerdown',e=>{
  if(drawMode||e.button!==0)return;
  if(!aimWp){
    if(!(e.ctrlKey||e.metaKey))return;               // start needs the modifier
    e.stopPropagation();e.preventDefault();
    const pt=groundAt(e.clientX,e.clientY); if(!pt)return;
    aimWp={x:pt.x,y:-pt.z,px:pt.x,pz:pt.z};
    aimArrow.position.set(pt.x,0.06,pt.z);aimArrow.setDirection(new THREE.Vector3(0,0,-1));
    aimArrow.visible=true; ctrl.enabled=false;        // freeze camera while aiming
  }else{
    e.stopPropagation();e.preventDefault();           // lock heading on a plain click
    const pt=groundAt(e.clientX,e.clientY); if(!pt)return;
    const h=Math.atan2(-(pt.x-aimWp.x),(-pt.z)-aimWp.y);
    if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'add_wp',x:aimWp.x,y:aimWp.y,h}));
    cancelAim();
  }
},true);
renderer.domElement.addEventListener('pointermove',e=>{
  if(!aimWp)return;
  const pt=groundAt(e.clientX,e.clientY); if(!pt)return;
  const d=new THREE.Vector3(pt.x-aimWp.px,0,pt.z-aimWp.pz);
  if(d.lengthSq()>1e-6){d.normalize();aimArrow.setDirection(d);}
});
window.addEventListener('keydown',e=>{ if(e.key==='Escape')cancelAim(); });

window.doStart=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'start'}));};
window.doStop=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'stop'}));};
window.doLoop=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'loop'}));};
window.doUndo=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'remove_last'}));};
window.doClear=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'clear'}));};
window.doWipeMap=()=>{
  if(!confirm('Wipe the live SLAM map and occupancy map? This cannot be undone.\n\nSLAM and mapping will restart from scratch at the current pose.'))return;
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'wipe_map'}));
};
window.doToggle=(key)=>{
  // While scrubbed, display-layer toggles are view-local overrides on the historical frame
  // (behavioral toggles — manual/global/frontier — still go to the server as usual).
  if(!isLive&&SCRUB_VIEW_KEYS.has(key)){
    const s=tlGet(scrubIdx);
    if(!s)return;
    if(key==='map'){
      const order=['off'].concat(s.cloud_names||[]);
      const cur=('map' in scrubOverride)?scrubOverride['map']:(s.cloud_source||'off');
      scrubOverride['map']=order[(Math.max(0,order.indexOf(cur))+1)%order.length];
    }else{
      const cur=(key in scrubOverride)?scrubOverride[key]:!!s[key];
      scrubOverride[key]=!cur;
    }
    renderScrub(scrubIdx);
    return;
  }
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'toggle',key}));
};

// --- Tuning sliders ---
// [key, min, max, step, explanation]   label is the real param name; hover for the explanation
const PARAM_META=[
  ['SPEED',0,0.5,0.01,'Base forward cruise speed (m/s)'],
  ['MAX_OMEGA',0,1.5,0.05,'Max angular velocity / turn-rate clamp (rad/s)'],
  ['LOOKAHEAD',0.1,1.5,0.05,'Pure-pursuit look-ahead distance along the path (m)'],
  ['K_CTE',0,5,0.1,'Cross-track P-gain: how hard it steers back onto the path'],
  ['K_CTE_D',0,2,0.05,'Cross-track D-gain: damps the steer-back so it stops weaving'],
  ['CTE_SPEED_K',0,200,5,'Slow-down when off the path (higher = slower off path)'],
  ['TURN_SLOW_K',0,10,0.5,'Slow-down while turning (higher = slower in turns)'],
  ['CLEAR_FULL',0.1,1.5,0.05,'Wall clearance at/above which it runs full speed (m)'],
  ['CLEAR_MIN',0,1.0,0.05,'Wall clearance at/below which it crawls (m)'],
  ['V_TIGHT_FRAC',0,1,0.05,'Crawl speed in tight spaces, as a fraction of cruise'],
  ['ROBOT_RADIUS_CELLS',3,25,1,'Obstacle inflation radius in grid cells (robot half-width)'],
  ['PROX_WEIGHT',0,50000,1000,'Soft wall-repulsion weight (higher = hug corridor center)'],
  ['REPLAN_INTERVAL',0.1,2,0.1,'Seconds between planner replans'],
  ['SMOOTH_V',0.05,1,0.05,'Forward-speed low-pass (higher = snappier, less smoothing)'],
  ['SMOOTH_W',0.05,1,0.05,'Turn-rate low-pass (higher = snappier, less smoothing)'],
  ['GOAL_TOLERANCE',0,0.3,0.01,'Arrival radius — how close counts as reaching the goal (m)'],
  ['HEADING_OMEGA',0,1.0,0.05,'Final heading turn speed (rad/s) — constant, slam_reloc style'],
  ['HEADING_TOL',0.02,0.5,0.01,'Final heading tolerance — aligned when |error| below this (rad)'],
  ['STUCK_TIME',1,60,1,'Seconds of no progress before it rotates in place to rescan'],
  ['ROTATE_TIME',0.5,10,0.5,'Seconds spent rotating per rescan before replanning'],
  ['ROTATE_FRAC',0.1,1,0.05,'Rescan rotate speed as a fraction of MAX_OMEGA'],
  ['PROGRESS_EPS',0.02,0.5,0.01,'Goal-distance drop (m) that counts as progress (resets the stuck timer)'],
  ['N_ROTATIONS',1,20,1,'Rescans before it skips the waypoint (patrol mode only)'],
];
function buildSliders(){
  document.getElementById('sliders').innerHTML=PARAM_META.map(([k,mn,mx,st,expl],i)=>
    `<div class="prow" title="${expl}" onmouseover="showTip(${i})" onmouseout="clearTip()">`+
    `<label>${k}<span class="pval" id="pv_${k}">--</span></label>`+
    `<input type="range" id="ps_${k}" min="${mn}" max="${mx}" step="${st}" oninput="onSlider('${k}')"></div>`
  ).join('');
}
window.showTip=(i)=>{document.getElementById('ptip').textContent=PARAM_META[i][0]+': '+PARAM_META[i][4];};
window.clearTip=()=>{document.getElementById('ptip').textContent='hover a parameter for details';};
window.onSlider=(k)=>{
  const v=parseFloat(document.getElementById('ps_'+k).value);
  document.getElementById('pv_'+k).textContent=v;
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'set_param',key:k,value:v}));
};
function applyParams(p){
  for(const [k] of PARAM_META){
    if(p[k]===undefined)continue;
    const el=document.getElementById('ps_'+k);
    if(el){el.value=p[k];document.getElementById('pv_'+k).textContent=p[k];}
  }
}
window.saveParams=()=>{
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'save_params'}));
  const b=document.getElementById('savep');b.textContent='Saved';setTimeout(()=>b.textContent='Save',1000);
};
window.loadParams=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'load_params'}));};
buildSliders();

// --- Draw bounds ---
let drawStart=null,boundsRect=null;
const bov=document.getElementById('bounds-overlay');
function groundHit(sx,sy){
  const m=new THREE.Vector2((sx/innerWidth)*2-1,-(sy/innerHeight)*2+1);
  const rc=new THREE.Raycaster();rc.setFromCamera(m,cam);
  const pl=new THREE.Plane(new THREE.Vector3(0,1,0),0);
  const pt=new THREE.Vector3();
  return rc.ray.intersectPlane(pl,pt)?pt:null;
}
window.toggleDrawBounds=()=>{
  drawMode=!drawMode;
  const b=document.getElementById('drawbtn');
  b.className=drawMode?'btn active':'btn';
  b.textContent=drawMode?'Drawing...':'Draw Bounds';
  renderer.domElement.style.cursor=drawMode?'crosshair':'';
  ctrl.enabled=!drawMode;
};
window.clearBounds=()=>{
  if(boundsRect){scene.remove(boundsRect);boundsRect.geometry.dispose();boundsRect=null;}
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'nav_bounds',corners:null}));
  document.getElementById('clearbtn').style.display='none';
};
renderer.domElement.addEventListener('mousedown',e=>{
  if(!drawMode||e.button!==0)return;
  drawStart={x:e.clientX,y:e.clientY};
  bov.style.display='block';bov.style.left=e.clientX+'px';bov.style.top=e.clientY+'px';
  bov.style.width='0px';bov.style.height='0px';
});
renderer.domElement.addEventListener('mousemove',e=>{
  if(!drawMode||!drawStart)return;
  const x0=Math.min(drawStart.x,e.clientX),y0=Math.min(drawStart.y,e.clientY);
  bov.style.left=x0+'px';bov.style.top=y0+'px';
  bov.style.width=Math.abs(e.clientX-drawStart.x)+'px';
  bov.style.height=Math.abs(e.clientY-drawStart.y)+'px';
});
renderer.domElement.addEventListener('mouseup',e=>{
  if(!drawMode||!drawStart)return;
  bov.style.display='none';
  const sx0=drawStart.x,sy0=drawStart.y,sx1=e.clientX,sy1=e.clientY;
  drawStart=null;
  const c0=groundHit(sx0,sy0),c1=groundHit(sx1,sy0),c2=groundHit(sx1,sy1),c3=groundHit(sx0,sy1);
  if(!c0||!c1||!c2||!c3)return;
  if(ws&&ws.readyState===1){
    ws.send(JSON.stringify({type:'nav_bounds',corners:[[c0.x,-c0.z],[c1.x,-c1.z],[c2.x,-c2.z],[c3.x,-c3.z]]}));
  }
  if(boundsRect){scene.remove(boundsRect);boundsRect.geometry.dispose();}
  const corners=[
    new THREE.Vector3(c0.x,0.02,c0.z),new THREE.Vector3(c1.x,0.02,c1.z),
    new THREE.Vector3(c2.x,0.02,c2.z),new THREE.Vector3(c3.x,0.02,c3.z),
  ];
  boundsRect=new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(corners),
    new THREE.LineBasicMaterial({color:0x0ea5e9}));
  scene.add(boundsRect);
  drawMode=false;
  document.getElementById('drawbtn').className='btn';
  document.getElementById('drawbtn').textContent='Draw Bounds';
  document.getElementById('clearbtn').style.display='';
  renderer.domElement.style.cursor='';
  ctrl.enabled=true;
});

// Keyframe chunk (type 4): header [bx by bz int32, res f32, first u32], then u16x3 cells,
// then u8x3 colors. first=1 resets the cloud (start of a fresh keyframe); chunks append.
function applyVoxChunk(raw,n){
  const dv=new DataView(raw.buffer,raw.byteOffset,20);
  const bx=dv.getInt32(0,true),by=dv.getInt32(4,true),bz=dv.getInt32(8,true);
  const res=dv.getFloat32(12,true),first=dv.getUint32(16,true);
  if(first)vReset(n);
  if(n>0){
    const d=new Uint16Array(raw.buffer,raw.byteOffset+20,n*3);
    const c=new Uint8Array(raw.buffer,raw.byteOffset+20+n*6,n*3);
    vEnsure(vCount+n);
    for(let i=0;i<n;i++){
      const cx=bx+d[i*3],cy=by+d[i*3+1],cz=bz+d[i*3+2];
      vUpsert(vkey(cx,cy,cz),cx*res,cy*res,cz*res,c[i*3]/255,c[i*3+1]/255,c[i*3+2]/255);
    }
  }
  vCommit();
}
// Delta (type 5): header [bx by bz i32, res f32, nu u32, nr u32], then u16x3 upsert cells,
// u16x3 removal cells, u8x3 upsert colors.
function applyVoxDelta(raw){
  const dv=new DataView(raw.buffer,raw.byteOffset,24);
  const bx=dv.getInt32(0,true),by=dv.getInt32(4,true),bz=dv.getInt32(8,true);
  const res=dv.getFloat32(12,true),nu=dv.getUint32(16,true),nr=dv.getUint32(20,true);
  let p=raw.byteOffset+24;
  const ud=new Uint16Array(raw.buffer,p,nu*3); p+=nu*6;
  const rd=new Uint16Array(raw.buffer,p,nr*3); p+=nr*6;
  const uc=new Uint8Array(raw.buffer,p,nu*3);
  vEnsure(vCount+nu);
  for(let i=0;i<nu;i++){
    const cx=bx+ud[i*3],cy=by+ud[i*3+1],cz=bz+ud[i*3+2];
    vUpsert(vkey(cx,cy,cz),cx*res,cy*res,cz*res,uc[i*3]/255,uc[i*3+1]/255,uc[i*3+2]/255);
  }
  for(let i=0;i<nr;i++){
    vRemove(vkey(bx+rd[i*3],by+rd[i*3+1],bz+rd[i*3+2]));
  }
  vCommit();
}
function updateFloor(raw,n){
  if(n<=0){floorPts.visible=false;return;}
  const cf=new Float32Array(raw.buffer,raw.byteOffset,n*3);
  const pos=new Float32Array(n*3);
  for(let i=0;i<n;i++){pos[i*3]=cf[i*3];pos[i*3+1]=cf[i*3+2];pos[i*3+2]=-cf[i*3+1];}
  floorGeo.dispose();floorGeo=new THREE.BufferGeometry();
  floorGeo.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
  floorPts.geometry=floorGeo;floorPts.visible=true;
}
function updateHeatmap(raw,n){
  if(n<=0){heatPts.visible=false;return;}
  const buf=raw.buffer;
  const pos=new Float32Array(n*3);const col=new Float32Array(n*3);
  const cf=new Float32Array(buf,raw.byteOffset,n*3);
  const cu=new Uint8Array(buf,raw.byteOffset+n*12,n*3);
  for(let i=0;i<n;i++){
    pos[i*3]=cf[i*3];pos[i*3+1]=cf[i*3+2];pos[i*3+2]=-cf[i*3+1];
    col[i*3]=cu[i*3]/255;col[i*3+1]=cu[i*3+1]/255;col[i*3+2]=cu[i*3+2]/255;
  }
  heatGeo.dispose();heatGeo=new THREE.BufferGeometry();
  heatGeo.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
  heatGeo.setAttribute('color',new THREE.Float32BufferAttribute(col,3));
  heatPts.geometry=heatGeo;heatPts.visible=true;
}
// Depth packet: [pos_x f32][pos_y f32][yaw f32][pitch_delta f32][depth u16 x w*h, row-major, mm].
// Unprojects camera-frame pinhole rays -> base frame (T_base_cam) -> nav frame (robot pose at
// capture) -> three.js axes (same X=navX,Y=navZ,Z=-navY convention as everywhere else here).
// forward(h)=(-sin h,cos h) matches robotGrp.rotation.y and the server's own control-loop
// velocity integration — verified against both before writing this transform.
function updateDepth(raw){
  if(!depthCalib)return;
  const{fx,fy,cx,cy,w,h,T_base_cam:T}=depthCalib;
  const maxd=depthCalib.max_d||5, maxmm=maxd*1000;   // depth_b's points-stage range gate
  const maxr=depthCalib.max_r||0, maxr2=maxr*maxr;   // horizontal base-frame radius cut (0=off)
  const dv=new DataView(raw.buffer,raw.byteOffset,16);
  const px=dv.getFloat32(0,true),py=dv.getFloat32(4,true),ph=dv.getFloat32(8,true),pd=dv.getFloat32(12,true);
  const depth=new Uint16Array(raw.buffer,raw.byteOffset+16,w*h);
  const hasRaw=raw.byteLength>=16+w*h*4;   // RAW mode: the unfiltered frame rides behind the normal one
  const rawd=hasRaw?new Uint16Array(raw.buffer,raw.byteOffset+16+w*h*2,w*h):null;
  // Live IMU pitch: T3' = T3 @ [[1,0,0],[0,c,s],[0,-s,c]] — byte-identical composition to the
  // daemon's own las2_depth_set_camera_to_base update, using the delta shipped per frame.
  const cpd=Math.cos(pd),spd=Math.sin(pd);
  const R01=cpd*T[1]-spd*T[2],  R02=spd*T[1]+cpd*T[2];
  const R11=cpd*T[5]-spd*T[6],  R12=spd*T[5]+cpd*T[6];
  const R21=cpd*T[9]-spd*T[10], R22=spd*T[9]+cpd*T[10];
  const sh=Math.sin(ph),ch=Math.cos(ph);
  // Match the daemon's own sampling: camera.points strides the FLAT pixel index by 2, which
  // on a row-major image = every other column of every row (u+=2, v+=1) — not both axes.
  // With the daemon now zeroing density-culled pixels in the image too, this makes the
  // projected cloud pixel-equivalent to camera.points (modulo the 512->640 nearest resize).
  const maxPts=Math.ceil(w/2)*h*(hasRaw?2:1);
  const pos=new Float32Array(maxPts*3),col=new Float32Array(maxPts*3);
  let n=0;
  const put=(u,v,dmm,red)=>{
    const z=dmm/1000;                                 // camera frame (OpenCV: X=right,Y=down,Z=fwd)
    const xc=(u-cx)*z/fx,yc=(v-cy)*z/fy;
    const bx=T[0]*xc+R01*yc+R02*z+T[3];               // camera -> base (pitch-corrected)
    const by=T[4]*xc+R11*yc+R12*z+T[7];
    const bz=T[8]*xc+R21*yc+R22*z+T[11];
    if(maxr2>0&&bx*bx+by*by>maxr2)return;   // same horizontal cut the daemon applies for mapping
    // base -> nav: base Y is forward, base X is lateral (verified numerically against
    // T_base_cam — a center-pixel ray comes out almost entirely on the Y axis, not X).
    // forward(h)=(-sin h,cos h) (matches robotGrp.rotation.y / control loop); right(h) is
    // forward rotated -90 deg = (cos h,sin h).
    const navX=px+(-sh)*by+(ch)*bx;
    const navY=py+(ch)*by+(sh)*bx;
    const o=n*3;
    pos[o]=navX;pos[o+1]=bz;pos[o+2]=-navY;            // nav -> three
    if(red){col[o]=1;col[o+1]=0.1;col[o+2]=0.1;}
    else{const g=Math.max(0,Math.min(1,1-z/maxd));col[o]=g;col[o+1]=g;col[o+2]=g;}   // near=bright .. far=dim (0-max_d)
    n++;
  };
  for(let v=0;v<h;v++){
    for(let u=0;u<w;u+=2){
      const i=v*w+u,dn=depth[i];
      if(dn!==0&&dn<=maxmm)put(u,v,dn,false);   // 0 = daemon's baked-in confidence mask; >max = its points range gate
      if(!hasRaw)continue;
      // raw is the unfiltered twin of normal, so where they agree it is redundant: only what the
      // filter removed (normal=0) or moved by more than DEPTH_DIFF_MM is drawn, in red
      const dr=rawd[i];
      if(dr!==0&&dr<=maxmm&&(dn===0||Math.abs(dr-dn)>DEPTH_DIFF_MM))put(u,v,dr,true);
    }
  }
  tlRecordDepth(pos,col,n);
  depthGeo.dispose();depthGeo=new THREE.BufferGeometry();
  depthGeo.setAttribute('position',new THREE.Float32BufferAttribute(pos.subarray(0,n*3),3));
  depthGeo.setAttribute('color',new THREE.Float32BufferAttribute(col.subarray(0,n*3),3));
  // While scrubbing, keep updating the live geometry+history but don't clobber the
  // snapshot the scrubber is showing (same pattern as the voxel cloud's scrub geo).
  if(isLive){depthPts.geometry=depthGeo;depthPts.visible=true;}
}

function updateState(s){
  robotGrp.position.set(s.rx,0,-s.ry);
  robotGrp.rotation.y=s.rh;
  if(s.gx!==undefined){
    if(!goalMk){goalMk=new THREE.Mesh(new THREE.SphereGeometry(0.15),new THREE.MeshBasicMaterial({color:0xef4444,depthTest:false}));scene.add(goalMk);}
    goalMk.position.set(s.gx,0.15,-s.gy);goalMk.visible=true;
  }else if(goalMk)goalMk.visible=false;
  if(s.path&&s.path[0].length>1){
    if(pathLine){scene.remove(pathLine);pathLine.geometry.dispose();}
    const pts=s.path[0].map((x,i)=>new THREE.Vector3(x,0.4,-s.path[1][i]));
    const curve=new THREE.CatmullRomCurve3(pts);
    pathLine=new THREE.Mesh(new THREE.TubeGeometry(curve,pts.length,0.02,6,false),new THREE.MeshBasicMaterial({color:0x0ea5e9,depthTest:false}));
    scene.add(pathLine);
  }
  // Predicted controller trajectory (magenta) — where the current twist leads
  if(s.pred&&s.pred[0].length>1){
    if(predLine){scene.remove(predLine);predLine.geometry.dispose();}
    const pts=s.pred[0].map((x,i)=>new THREE.Vector3(x,0.25,-s.pred[1][i]));
    const curve=new THREE.CatmullRomCurve3(pts);
    predLine=new THREE.Mesh(new THREE.TubeGeometry(curve,pts.length,0.018,6,false),new THREE.MeshBasicMaterial({color:0xff3df0,depthTest:false}));
    scene.add(predLine);
  }else if(predLine){scene.remove(predLine);predLine.geometry.dispose();predLine=null;}
  // Global-mode breadcrumbs every 0.2m along the mapped-floor route to the goal
  routeGrp.clear();
  if(s.route_wps){
    s.route_wps.forEach(p=>{
      const m=new THREE.Mesh(new THREE.SphereGeometry(0.045),new THREE.MeshBasicMaterial({color:0x22d3ee,depthTest:false}));
      m.position.set(p[0],0.12,-p[1]);routeGrp.add(m);
    });
  }
  // SLAM trail (yellow) — accumulate live positions client-side; only grows, no resample churn
  if(s.slam_path){
    let changed=false;
    if(isLive){
      const last=slamTrail.length?slamTrail[slamTrail.length-1]:null;
      const d=last?Math.hypot(s.rx-last[0],s.ry-last[1]):Infinity;
      if(!last||(d>0.02&&d<0.3)){slamTrail.push([s.rx,s.ry]);if(slamTrail.length>8000)slamTrail.shift();changed=true;}
    }
    if(slamTrail.length>1&&(changed||!slamLine)){
      if(slamLine){scene.remove(slamLine);slamLine.geometry.dispose();}
      // thick tube, same component as the planned/predicted paths (downsampled for perf)
      const step=Math.max(1,Math.floor(slamTrail.length/400));
      const sp=slamTrail.filter((_,i)=>i%step===0||i===slamTrail.length-1);
      const pts=sp.map(p=>new THREE.Vector3(p[0],0.06,-p[1]));
      const curve=new THREE.CatmullRomCurve3(pts);
      slamLine=new THREE.Mesh(new THREE.TubeGeometry(curve,Math.max(2,pts.length*2),0.035,6,false),
        new THREE.MeshBasicMaterial({color:0xffc800,depthTest:false}));
      scene.add(slamLine);
    }
  }else{slamTrail=[];if(slamLine){scene.remove(slamLine);slamLine.geometry.dispose();slamLine=null;}}

  // Waypoint markers
  wpGrp.clear();wpLineGrp.clear();
  if(s.waypoints&&s.waypoints.length>0){
    const wpPts=[];
    s.waypoints.forEach((w,i)=>{
      const done=s.running&&i<s.wp;const active=s.running&&i===s.wp;
      const color=active?0x0ea5e9:done?0x333333:0xf59e0b;
      const size=active?0.14:0.09;
      const sp=new THREE.Mesh(new THREE.SphereGeometry(size),new THREE.MeshBasicMaterial({color,depthTest:false}));
      sp.position.set(w[0],0.5,-w[1]);wpGrp.add(sp);
      if(w.length>2&&w[2]!=null){                       // target heading arrow (forward=(-sin h,cos h))
        const h=w[2],dir=new THREE.Vector3(-Math.sin(h),0,-Math.cos(h));
        wpGrp.add(new THREE.ArrowHelper(dir,new THREE.Vector3(w[0],0.5,-w[1]),0.5,color,0.16,0.09));
      }
      wpPts.push(new THREE.Vector3(w[0],0.05,-w[1]));
    });
    if(wpPts.length>1){
      const lg=new THREE.BufferGeometry().setFromPoints(wpPts);
      const ln=new THREE.Line(lg,new THREE.LineDashedMaterial({color:0x555555,dashSize:0.1,gapSize:0.1}));
      ln.computeLineDistances();wpLineGrp.add(ln);
    }
  }
  // Toggle buttons
  const fb=document.getElementById('floorbtn');fb.className=s.floor?'btn active':'btn';fb.textContent=s.floor?'Floor: ON':'Floor';
  const gb=document.getElementById('gradientbtn');gb.className=s.gradient?'btn active':'btn';gb.textContent=s.gradient?'Gradient: ON':'Gradient';
  const db=document.getElementById('depthbtn');db.className=s.depth?'btn active':'btn';db.textContent=['Depth','Depth: NORMAL','Depth: RAW'][s.depth|0]||'Depth';
  const sb2=document.getElementById('slambtn');sb2.className=s.slam_path?'btn active':'btn';sb2.textContent=s.slam_path?'SLAM: ON':'SLAM Path';
  const mpb=document.getElementById('mapbtn');const cs=s.cloud_source||'off';
  mpb.className=cs==='off'?'btn':'btn active';mpb.textContent='Auki Map: '+(cs==='off'?'OFF':'ON');
  for(const nm in cloudPts){cloudPts[nm].visible=(nm===cs);}
  const nmb=document.getElementById('navmapbtn');nmb.className=s.nav_map?'btn active':'btn';nmb.textContent=s.nav_map?'BBMap: ON':'BBMap: OFF';
  voxPts.visible=!!s.nav_map;
  if(!s.floor)floorPts.visible=false;
  if(!s.gradient)heatPts.visible=false;
  if(!s.depth)depthPts.visible=false;
  // Loop/start
  const lb=document.getElementById('loopbtn');lb.textContent=s.loop?'Loop: ON':'Loop: OFF';lb.className=s.loop?'btn active':'btn';
  const gbtn=document.getElementById('globalbtn');gbtn.textContent=s.global?'Global Goal: ON':'Global Goal: OFF';gbtn.className=s.global?'btn active':'btn';
  const fbtn=document.getElementById('frontierbtn');
  if(fbtn){fbtn.className=s.frontier?'btn active':'btn';fbtn.textContent=s.frontier?'Auki Path Mode: ON':'Auki Path Mode: OFF';}
  const mbtn=document.getElementById('manualbtn');mbtn.textContent=s.manual?'Manual Drive: ON':'Manual Drive: OFF';mbtn.className=s.manual?'btn active':'btn';
  manualDrive=!!s.manual;
  const wm=document.getElementById('wasdmode');if(wm)wm.textContent=s.manual?'DRIVE robot':'fly cam';
  const stb=document.getElementById('startbtn');stb.className=s.running?'btn active':'btn';stb.textContent=s.running?'Running':'Start';
  const rb=document.getElementById('rebuild'),r=s.rebuild;
  if(rb&&r){
    if(lastRebuildCount<0)lastRebuildCount=r.count;   // first state after (re)connect: no flash for an old rebuild
    else if(r.count!==lastRebuildCount){lastRebuildCount=r.count;lastRebuildAt=Date.now();}
    const fresh=r.count>0&&Date.now()-lastRebuildAt<8000;
    if(r.in_progress){rb.className='busy';rb.textContent='map rebuild in progress (PGO)…';}
    else if(r.count>0){rb.className=fresh?'fresh':'';rb.textContent=(fresh?'MAP REBUILD #':'last rebuild #')+r.count+' merged at frame '+r.frame+': floor moved '+r.moved+', emptied '+r.emptied+', filled '+r.filled+(fresh||r.age===null?'':' ('+Math.round(r.age)+'s ago)');}
    else{rb.className='';rb.textContent='';}
  }
  const wb=document.getElementById('wipebtn');
  if(wb){
    const wiping=!!(s.status&&String(s.status).startsWith('wiping'));
    wb.disabled=wiping;
    wb.textContent=wiping?'Wiping…':'Wipe SLAM + Map';
  }
  if(s.map_gen!==undefined && s.map_gen!==lastMapGen){
    lastMapGen=s.map_gen;
    slamTrail=[];
    if(slamLine){scene.remove(slamLine);slamLine.geometry.dispose();slamLine=null;}
  }
  // Status
  const el=document.getElementById('status');
  const st=s.status||'';
  if(st.startsWith('wiping')||st.startsWith('wipe failed')){
    el.textContent=st;el.style.color=st.startsWith('wipe failed')?'#ef4444':'#f59e0b';
  }else if(s.ready===false){el.textContent=st.startsWith('wiped')?st:'connected — waiting for SLAM (show the robot a wall QR)';el.style.color='#f59e0b';}
  else if(s.status&&s.running){el.textContent=s.status;el.style.color=s.status.includes('stuck')||s.status.includes('no path')?'#ef4444':'#10b981';}
  else if(!s.running&&s.waypoints&&s.waypoints.length>0){el.textContent=s.status==='patrol complete'?'Patrol complete!':`${s.waypoints.length} waypoints set`;el.style.color=s.status==='patrol complete'?'#f59e0b':'#10b981';}
  else{el.textContent='Double-click map to add waypoints';el.style.color='#888';}
  if(s.waypoints){
    document.getElementById('wplist').innerHTML=s.waypoints.map((w,i)=>{
      const cls=s.running&&i===s.wp?'active':s.running&&i<s.wp?'done':'';
      return `<div class="${cls}">${i+1}. (${w[0]}, ${w[1]})</div>`;
    }).join('');
  }
}

// Render-latest-only: state messages BUFFER behind a busy JS thread (500k-pt cloud renders, DOM
// updates). Rendering every message in onmessage replays the backlog — the UI shows seconds-old
// state, toggles look dead, the map trails the pose. Instead onmessage only stores the newest
// state and one rAF loop renders it: the backlog collapses and the UI is always current.
let latestState=null;
function stateRenderLoop(){
  if(latestState){
    const m=latestState; latestState=null;
    tlRecord(m);
    if(isLive){updateState(m);updateDiag(m.diag);gpush(m.diag);}
  }
  requestAnimationFrame(stateRenderLoop);
}
requestAnimationFrame(stateRenderLoop);
function connect(){   // REALTIME socket: state + params (text), commands out. Never blocked.
  const p=location.protocol==='https:'?'wss:':'ws:';
  ws=new WebSocket(`${p}//${location.host}/ws`);
  ws.onmessage=e=>{
    try{
      const m=JSON.parse(e.data);
      if(m.t==='state'){
        latestState=m;                       // rendered by the rAF loop; backlog is dropped
      }else if(m.t==='params'){
        applyParams(m.params);
        if(m.loaded!==undefined){
          const b=document.getElementById('loadp');
          b.textContent=m.loaded?'Loaded':'No file';setTimeout(()=>b.textContent='Load',1200);
        }
      }
    }catch(err){console.error(err);}
  };
  ws.onclose=()=>{                              // fast, visible retry — no silent dead pages
    const el=document.getElementById('status');
    if(el){el.textContent='link lost — reconnecting…';el.style.color='#ef4444';}
    setTimeout(connect,500);
  };
}
function connectHeavy(){   // HEAVY socket: voxel cloud + floor/gradient (binary), separate.
  const p=location.protocol==='https:'?'wss:':'ws:';
  wsHeavy=new WebSocket(`${p}//${location.host}/heavy`);
  wsHeavy.binaryType='arraybuffer';
  wsHeavy.onopen=fetchDepthCalib;   // server restart -> reconnect -> pick up new extrinsic, no page refresh needed
  wsHeavy.onmessage=e=>{
    try{
      const dv0=new DataView(e.data);const t=dv0.getUint32(0,true);const n=dv0.getUint32(4,true);
      const raw=pako.inflate(new Uint8Array(e.data,8));
      // Voxel keyframe/deltas MUST always be applied (skipping one corrupts the cloud), even
      // while scrubbing — the scrubber shows a snapshot but the live buffers stay current.
      if(t===4){applyVoxChunk(raw,n);tlRecordVox();}
      else if(t===5){applyVoxDelta(raw);tlRecordVox();}
      else if(t===2){updateFloor(raw,n);}
      else if(t===3){updateHeatmap(raw,n);}
      else if(t===6){updateDepth(raw);}
    }catch(err){console.error(err);}
  };
  wsHeavy.onclose=()=>setTimeout(connectHeavy,1000);
}
connect();connectHeavy();
addEventListener('resize',()=>{cam.aspect=innerWidth/innerHeight;cam.updateProjectionMatrix();renderer.setSize(innerWidth,innerHeight);});
const keys={};
let manualDrive=false, shiftHeld=false;
// In manual mode WASD drives the robot (teleop, like teleop.py); otherwise it flies the cam.
function teleopCombo(){let c='';if(keys['w'])c+='w';if(keys['s'])c+='s';if(keys['a'])c+='a';if(keys['d'])c+='d';return c;}
function sendTeleop(){if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'teleop',keys:teleopCombo(),shift:shiftHeld,gain:1.0}));}
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  if(e.key==='Shift'){shiftHeld=true;if(manualDrive)sendTeleop();return;}
  if(e.repeat)return;
  keys[e.key.toLowerCase()]=true;
  if(manualDrive&&'wasd'.includes(e.key.toLowerCase())){e.preventDefault();sendTeleop();}
});
document.addEventListener('keyup',e=>{
  if(e.key==='Shift'){shiftHeld=false;if(manualDrive)sendTeleop();return;}
  keys[e.key.toLowerCase()]=false;
  if(manualDrive&&'wasd'.includes(e.key.toLowerCase()))sendTeleop();
});
// Safety: losing focus releases all keys (and stops the robot if driving).
addEventListener('blur',()=>{for(const k in keys)keys[k]=false;shiftHeld=false;if(manualDrive)sendTeleop();});
// Dead-man heartbeat: keep re-asserting the held keys so a dropped key-up can't run away.
setInterval(()=>{if(manualDrive)sendTeleop();},150);
const MOVE_SPEED=0.15;
// --- GTA-style third-person chase cam (client-only toggle) ---
// Sits behind the robot along its heading; position eases (lerp) so turns swing the camera
// around with a bit of lag, the look-at tracks hard. Wheel zooms the follow distance.
// OrbitControls are disabled while chasing (they fight lookAt); handed back on exit with the
// target parked on the robot so orbiting resumes from where you were looking.
let chaseCam=false, chaseDist=2.8;
let chaseYaw=0;                      // camera's own eased yaw — NOT the raw slam heading:
                                     // pose smoothing was removed, so the robot yaw steps at
                                     // the 8Hz state rate and snaps on jumps; tracking it
                                     // directly made the camera judder left-right. Easing here
                                     // keeps the pose raw and the camera calm.
let chaseOrbit=0;                    // user mouse-orbit offset; eases back behind the robot
let chaseLook=null;                  // eased look-at point (same jitter story as yaw)
let chaseDragT=0,chaseDragging=false,chaseLastX=0;
window.toggleChase=()=>{
  chaseCam=!chaseCam;
  const b=document.getElementById('chasebtn');
  b.className=chaseCam?'btn active':'btn';
  b.textContent=chaseCam?'Chase: ON':'Chase';
  ctrl.enabled=!chaseCam;
  if(chaseCam){chaseYaw=robotGrp.rotation.y;chaseOrbit=0;chaseLook=robotGrp.position.clone();}
  else ctrl.target.copy(robotGrp.position);
};
addEventListener('wheel',e=>{if(chaseCam)chaseDist=Math.max(1.5,Math.min(8,chaseDist+e.deltaY*0.003));},{passive:true});
// GTA-style manual orbit: drag to swing the camera around the robot; it eases back behind
// the heading ~0.7s after you let go.
renderer.domElement.addEventListener('mousedown',e=>{if(chaseCam){chaseDragging=true;chaseLastX=e.clientX;}});
addEventListener('mousemove',e=>{
  if(chaseCam&&chaseDragging){chaseOrbit-=(e.clientX-chaseLastX)*0.008;chaseLastX=e.clientX;chaseDragT=performance.now();}
});
addEventListener('mouseup',()=>{chaseDragging=false;});
(function anim(){
  requestAnimationFrame(anim);
  // Camera watchdog: one NaN anywhere (bad pose sample x raw passthrough, degenerate math)
  // poisons cam.position through lerp PERMANENTLY — classic sticky black screen. Runaway
  // distance gets the same treatment. Reset to a sane view instead of dying dark.
  if(!isFinite(cam.position.x)||!isFinite(cam.position.y)||!isFinite(cam.position.z)
     ||cam.position.length()>400){
    cam.position.set(robotGrp.position.x,8,robotGrp.position.z+6);
    ctrl.target.copy(robotGrp.position);
    chaseYaw=robotGrp.rotation.y||0;chaseOrbit=0;
    if(chaseLook)chaseLook.copy(robotGrp.position);
    console.warn('camera watchdog: reset from invalid state');
  }
  if(chaseCam){
    const h=robotGrp.rotation.y;
    if(!isFinite(h)||!isFinite(robotGrp.position.x)){renderer.render(scene,cam);return;}  // skip bad-pose frames
    chaseYaw+=Math.atan2(Math.sin(h-chaseYaw),Math.cos(h-chaseYaw))*0.08;  // wrap-aware ease
    if(!chaseDragging&&performance.now()-chaseDragT>700)chaseOrbit*=0.93;   // swing back behind
    const yaw=chaseYaw+chaseOrbit;
    const fwd3=new THREE.Vector3(-Math.sin(yaw),0,-Math.cos(yaw));
    const tgt=robotGrp.position.clone().addScaledVector(fwd3,-chaseDist);
    tgt.y=0.85*chaseDist;                 // steeper than eye-level so map clutter can't occlude
    cam.position.lerp(tgt,0.12);
    const lookTgt=robotGrp.position.clone().addScaledVector(fwd3,1.0);lookTgt.y=0.5;
    chaseLook.lerp(lookTgt,0.2);
    cam.lookAt(chaseLook);
    renderer.render(scene,cam);
    return;
  }
  const fwd=new THREE.Vector3();cam.getWorldDirection(fwd);fwd.y=0;fwd.normalize();
  const right=new THREE.Vector3().crossVectors(fwd,new THREE.Vector3(0,1,0)).normalize();
  const d=new THREE.Vector3();
  if(!manualDrive){   // WASD flies the camera only when NOT driving the robot
    if(keys['w'])d.add(fwd);if(keys['s'])d.sub(fwd);
    if(keys['a'])d.sub(right);if(keys['d'])d.add(right);
  }
  if(keys['q'])d.y-=1;if(keys['e'])d.y+=1;
  if(d.lengthSq()>0){d.normalize().multiplyScalar(MOVE_SPEED);cam.position.add(d);ctrl.target.add(d);}
  ctrl.update();renderer.render(scene,cam);
})();

// --- Improved scrubber: keyboard + scroll ---
document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT'&&e.target.id!=='scrub')return;
  const sb=document.getElementById('scrub');
  const step=e.shiftKey?30:1;
  if(e.key==='ArrowLeft'){
    e.preventDefault();isLive=false;document.getElementById('live-btn').className='off';
    sb.value=Math.max(0,parseInt(sb.value)-step);sb.dispatchEvent(new Event('input'));
  }else if(e.key==='ArrowRight'){
    e.preventDefault();
    const nv=Math.min(parseInt(sb.max),parseInt(sb.value)+step);
    if(nv>=parseInt(sb.max)){goLive();}
    else{isLive=false;document.getElementById('live-btn').className='off';sb.value=nv;sb.dispatchEvent(new Event('input'));}
  }else if(e.key===' '&&e.target.id==='scrub'){
    e.preventDefault();goLive();
  }
});
</script></body></html>'''


@app.get("/", response_class=HTMLResponse)
async def index():
    # no-store so a reload always fetches fresh inline JS (avoids stale cached-tab bugs)
    return HTMLResponse(content=HTML, headers={"Cache-Control": "no-store, must-revalidate"})

@app.get("/robot_mesh")
async def serve_robot_mesh():
    from fastapi.responses import Response
    return Response(content=robot_mesh_bytes, media_type="application/octet-stream")

@app.get("/clouds")
async def serve_cloud_names():
    return list(map_clouds)

@app.get("/cloud/{name}")
async def serve_cloud(name: str):
    # Named nav-frame cloud: [nv u32][xyz f32*3][rgb u8*3]. Sent once per cloud.
    from fastapi.responses import Response
    return Response(content=map_clouds.get(name, b'\x00\x00\x00\x00'), media_type="application/octet-stream")

@app.get("/portals")
async def serve_portals():
    from fastapi.responses import Response
    return Response(content=portals_json, media_type="application/json")

@app.get("/depth_calib")
async def serve_depth_calib():
    return depth_calib or {}

@app.websocket("/ws")
async def ws_ep(ws: WebSocket):
    # REALTIME channel: state (pose/path/diag) + params out, commands in. Text only, all
    # tiny — so this socket is never head-of-line-blocked by the heavy voxel cloud, which
    # lives on /heavy. Pose/path stay ~8 Hz regardless of map size or WiFi.
    await ws.accept()
    myq = Queue(maxsize=2)                     # this client's own latest-state queue (fanout)
    with ws_lock:
        ws_clients.append(myq)
    try:
        async def tx():
            try:
                await ws.send_text(json.dumps({"t": "params", "params": dict(PARAMS)}))
            except Exception:
                pass
            while True:
                try:
                    await ws.send_text(myq.get_nowait())
                except Empty:
                    await asyncio.sleep(0.01)

        async def rx():
            global show_floor, show_gradient, show_slam_path
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                t = msg.get('type')
                if t in ('add_wp','start','stop','loop','clear','remove_last','nav_bounds','toggle',
                         'set_param','save_params','load_params','teleop','wipe_map'):
                    cmd_queue.put_nowait(msg if t != 'loop' else {'type':'loop','enabled': not patrol_loop})

        await asyncio.gather(tx(), rx(), return_exceptions=True)
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        with ws_lock:
            if myq in ws_clients:
                ws_clients.remove(myq)


@app.websocket("/heavy")
async def heavy_ep(ws: WebSocket):
    # HEAVY channel: voxel keyframe/deltas (+ floor/gradient). Per-client. On connect (or
    # after falling behind) the client is sent a fresh PROGRESSIVE keyframe — many small
    # chunks instead of one monolithic blob — so the map loads incrementally and reliably;
    # thereafter only tiny deltas flow. Binary, off /ws so it never stalls the realtime state.
    await ws.accept()
    conn_t = time.time()
    resync_count = 0
    print(f"[heavy] client connected", flush=True)
    client = {'q': Queue(maxsize=VOX_QMAX), 'resync': [True]}
    with heavy_lock:
        heavy_clients.append(client)
    try:
        while True:
            if client['resync'][0]:
                resync_count += 1
                resync_t0 = time.time()
                with heavy_lock:
                    # Build+compress once per voxel_loop cycle, not once per reconnect — on a
                    # flaky link the /heavy socket can reconnect far more often than the map
                    # actually changes, and re-encoding a 500k+ point cloud per reconnect was
                    # turning reconnect storms into a CPU spiral (found live: ~80% of total
                    # CPU going into repeated pack_vox_keyframe_chunks calls for the same data).
                    chunks = vox_keyframe_cache[0]
                    if chunks is None and vox_state['cell'] is not None:
                        # NOT decimated to MAX_BROWSER_POINTS here: voxel_loop's delta diffing
                        # runs against the full (undecimated) cloud, so a client that received
                        # a decimated keyframe would get deltas referencing cells it was never
                        # sent — that's a real correctness bug, not just a perf question, so
                        # it needs the diffing baseline changed too, not a quick subsample here.
                        chunks = pack_vox_keyframe_chunks(vox_state['cell'], vox_state['col'])
                        vox_keyframe_cache[0] = chunks
                    floor_pkt = floor_latest[0]; heat_pkt = heat_latest[0]
                    while not client['q'].empty():        # drop stale deltas before keyframe
                        try: client['q'].get_nowait()
                        except Exception: break
                if chunks is None:
                    await asyncio.sleep(0.1); continue     # no map yet — wait, stay flagged
                for pkt in chunks:
                    await ws.send_bytes(pkt)
                    await asyncio.sleep(0)                 # yield: let the browser render each chunk
                if floor_pkt is not None: await ws.send_bytes(floor_pkt)
                if heat_pkt is not None: await ws.send_bytes(heat_pkt)
                client['resync'][0] = False
                print(f"[heavy] resync #{resync_count} sent ({len(chunks) if chunks else 0} chunks) "
                      f"in {time.time() - resync_t0:.2f}s", flush=True)
            # Latest-only depth: never queued — each client tracks the last seq it sent and
            # jumps straight to the newest frame (see depth_latest above for the why).
            with heavy_lock:
                dseq, dpkt = depth_latest[0], depth_latest[1]
            if dpkt is not None and client.get('dseq') != dseq:
                client['dseq'] = dseq
                await ws.send_bytes(dpkt)
            try:
                await ws.send_bytes(client['q'].get_nowait())
            except Empty:
                await asyncio.sleep(0.02)
    except (WebSocketDisconnect, Exception) as e:
        print(f"[heavy] client disconnected after {time.time() - conn_t:.2f}s, "
              f"{resync_count} resync(s) ({type(e).__name__})", flush=True)
    finally:
        with heavy_lock:
            if client in heavy_clients:
                heavy_clients.remove(client)


def load_pslam_map():
    """Load the COLMAP/slam_viz map, fit the floor -> T_nav_gl, write it for the mapping daemon,
    and build the /map_cloud + /portals display payloads (z-up nav frame). The planner uses the
    mapping daemon's grid2d for navigation — this map is display-only."""
    global T_nav_gl, map_clouds, portals_json
    # follow the daemons' mode knob (slam.reloc_mode): "off" forces mapless even with a map
    # installed; "on"/"auto" -> map mode iff T_nav_gl.npy (from tools/make_map.py) is present.
    try:
        _mode = str(getattr(Config('slam'), 'reloc_mode', 'auto')).strip().lower()
    except Exception:
        _mode = 'auto'
    tng = PSLAM_MAP_DIR / "T_nav_gl.npy"
    if _mode == 'off':
        print("[map] slam.reloc_mode=off — MAPLESS mode (live floor planning only)", flush=True)
        return
    if not tng.exists():
        # MAPLESS MODE: no map installed. T_nav_gl stays identity; run mapless (live floor only).
        # Install = run tools/make_map.py on the recon and copy its output into maps/.
        print("[map] no T_nav_gl.npy in maps/ — MAPLESS mode (live floor planning only)", flush=True)
        return
    T_nav_gl = np.load(tng)                             # produced by tools/make_map.py
    print(f"[map] T_nav_gl loaded from {tng.name}", flush=True)

    # Single display cloud "map": prefer the supplied dense nav-frame cloud; else fall back to
    # the sparse map cloud (clipped to the floor-band footprint).
    cd = cc = None
    dense = sorted(PSLAM_MAP_DIR.glob("*_cloud_nav.npz"))
    if dense:
        L = np.load(dense[0])
        cd = L['xyz'].astype(np.float32); cc = L['rgb'].astype(np.uint8)
        print(f"[map] display cloud '{dense[0].name}' {len(cd)} pts", flush=True)
    if cd is not None:
        map_clouds['map'] = struct.pack('<I', len(cd)) + cd.astype(np.float32).tobytes() + cc.astype(np.uint8).tobytes()

    plist = []
    ppath = PSLAM_MAP_DIR / "portals_colmap.json"
    if ppath.exists():
        for pid, v in json.load(open(ppath)).items():
            p_nav = G.apply(T_nav_gl, (G.MW @ np.asarray(v["position"], float)).reshape(1, 3))[0]
            plist.append({"id": pid, "pos": [float(p_nav[0]), float(p_nav[1]), float(p_nav[2])],
                          "size": float(v["size"])})
    portals_json = json.dumps(plist).encode()
    print(f"[map] portals: {len(plist)}  cloud pts: {len(cd)}", flush=True)


def main():
    global robot_mesh_bytes, depth_calib
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        depth_calib = compute_depth_calib()
        print(f"[depth] calib ready: fx={depth_calib['fx']:.1f} fy={depth_calib['fy']:.1f} "
              f"cx={depth_calib['cx']:.1f} cy={depth_calib['cy']:.1f}", flush=True)
    except Exception as e:
        print(f"[depth] calib unavailable ({e}) — depth toggle will show nothing", flush=True)

    load_pslam_map()

    global auki_maps, plan_order
    for p in sorted(AUKI_GRID_DIR.glob("auki_nav_grid_*.npz"))[:1]:   # one map per robot
        name = p.stem.replace("auki_nav_grid_", "")
        a = np.load(p)
        auki_maps[name] = (np.ascontiguousarray(a['grid'], np.uint8), a['origin'].astype(np.float32))
        g = auki_maps[name][0]
        print(f"[map] auki '{name}' {g.shape[0]}x{g.shape[0]} free={(g==1).sum()} obs={(g==2).sum()}", flush=True)
    plan_order = ["floor"]                      # path mode fuses the auki grid on top
    global frontier_active
    frontier_active = bool(auki_maps)           # mapless -> live floor only
    print(f"[map] plan sources: {plan_order} path_mode={'ON' if frontier_active else 'MAPLESS'}", flush=True)

    global cloud_names, cloud_source
    cloud_names = list(map_clouds)
    cloud_source = cloud_names[0] if cloud_names else "off"
    print(f"[map] clouds: {cloud_names}", flush=True)

    mesh_path = Path.home() / "bb-models/bb1/robot_mesh.npz"
    if mesh_path.exists():
        d = np.load(mesh_path)
        mv, mf = d['vertices'].astype(np.float32), d['faces'].astype(np.uint32)
        robot_mesh_bytes = struct.pack('<II', len(mv), len(mf)) + mv.tobytes() + mf.tobytes()

    print("[+] JIT warmup...", flush=True)
    _warmup_jit()
    _k = np.zeros(2, dtype=np.int64); _c = np.zeros((2, 3), dtype=np.uint8)
    _vox_merge_diff(_k, _c, _k, _c, VOX_COLOR_EPS)   # compile ahead of the first real voxel frame
    print("[+] JIT ready", flush=True)

    # The bbos TimeLog registry is lazily initialized by the FIRST Reader/Writer built in
    # the process; our loop threads below all build theirs at the same instant, and two
    # threads interleaving inside _ensure_registry() close each other's fd (cls._shm is
    # shared class state) -> EBADF that kills one thread at startup (seen live: voxel_loop
    # died, BBMap silently never streamed that session). Initialize once on the main
    # thread so every later call takes the already-initialized fast path.
    from bbos.time import TimeLog
    TimeLog._ensure_registry()

    threading.Thread(target=control_loop, daemon=True).start()
    threading.Thread(target=planner_loop, daemon=True).start()
    threading.Thread(target=voxel_loop, daemon=True).start()
    threading.Thread(target=rebuild_loop, daemon=True).start()
    threading.Thread(target=depth_loop, daemon=True).start()
    threading.Thread(target=freshness_watchdog, daemon=True).start()

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]; s.close()
    except: ip = "localhost"
    print(f"[reloc_nav] http://{ip}:8010", flush=True)
    # /heavy already sends hand-zlib-compressed binary packets — permessage-deflate would just
    # recompress already-compressed bytes for no gain.
    # ws ping detects wifi-vanished peers (asyncio silently no-ops writes to a dead transport,
    # so our cleanup handlers never fire without it). But the timeout must tolerate BULK
    # TRANSFERS: a 5s/5s setting culled healthy connections whose pong sat behind a multi-MB
    # keyframe on a saturated link -> reconnect -> keyframe re-blast -> permanent churn loop
    # (diagnosed live: /ws churned with it, waypoint clicks died). 30s pong grace fixes the
    # churn; a truly dead peer now lingers ~40s as harmless no-op writes before cleanup.
    uvicorn.run(app, host="0.0.0.0", port=8010, log_level="warning", ws_per_message_deflate=False,
                ws_ping_interval=10.0, ws_ping_timeout=30.0)


if __name__ == "__main__":
    main()

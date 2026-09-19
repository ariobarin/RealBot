# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "bbos",
#   "numba",
#   "numpy",
#   "scipy",
#   "pillow",
#   "fastapi",
#   "uvicorn",
#   "websockets",
#   "pyyaml",
#   "opencv-python-headless",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Navigation UI: submit routes to nav.command and display BBOS telemetry."""
import asyncio
import ctypes
import hashlib
import io
import json
import math
import signal
import socket
import struct
import sys
import threading
import time
import zlib
from pathlib import Path
from queue import Queue, Empty
# I don't get why this is needed. A/B needed.
try:                        # glibc's default arena retains freed memory instead of returning it
    _libc = ctypes.CDLL("libc.so.6")    # to the OS — voxel_loop's periodic large-array frees left
    _libc.mallopt(-3, 131072)           # RSS permanently elevated. Route allocations >=128KB
    _libc.mallopt(-1, 131072)           # through mmap (M_MMAP_THRESHOLD) and lower the trim
except Exception:                       # threshold (M_TRIM_THRESHOLD) so they're actually freed.
    pass

import numpy as np
import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from numba import njit
from scipy.ndimage import distance_transform_edt

from bbos import Writer, Reader, Config, Type
from bbos.daemons.nav import constants as nav_constants

CFG_M = Config("mapping")
CFG_BASE = Config("base")
STATE_INTERVAL = Type("nav_state")()[1] / 1000.0


def  compute_depth_calib():
    """Parse depth calibration matrix.
    
   [ fx 0 cx Tx] 
   [ 0 fy cy 0 ]
   [ 0 0  1  0 ]
    """
    
    CFG_C = Config("cam_head")
    CFG_D = Config("depth")
    
    (mtx_l, dist_l, mtx_r, dist_r, R1, R2, P1_cam, P2_cam, Q, baseline_m, fx_ds, R, t) = CFG_D.camera_cal()
    base_from_cam = CFG_D.T_base_cam.mat()
    
    src_w, src_h = CFG_C.width // 2, CFG_C.height
    dst_w, dst_h = CFG_D.width_D, CFG_D.height_D
    scale_x, scale_y = dst_w / src_w, dst_h / src_h
    
    P1_ds = P1_cam.copy()
    P1_ds[0, :] *= scale_x
    P1_ds[1, :] *= scale_y
    
    cx_ds = P1_ds[0, 2]
    cy_ds = P1_ds[1, 2]
    fx_ds = P1_ds[0, 0]
    fy_ds = P1_ds[1, 1]
    
    
    fx, fy, cx, cy = fx_ds, fy_ds, cx_ds, cy_ds

    return {
        'fx': float(fx), 'fy': float(fy), 'cx': float(cx), 'cy': float(cy),
        'w': int(dst_w), 'h': int(dst_h),
        'base_from_cam': [float(x) for x in base_from_cam.flatten()],
        'max_d': float(CFG_D.max_depth_m),
    }


show_nav_map = False

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

# The daemon watches this same file for tuning changes.
PARAMS_FILE = Path(nav_constants.__file__).with_name("params.yaml")
PARAMS = {}
INT_PARAMS = {'ROBOT_RADIUS_CELLS', 'N_ROTATIONS'}


def save_params_file():
    # Keep the existing comments; replace atomically so the daemon never reads half a file.
    lines = []
    for line in PARAMS_FILE.read_text().splitlines():
        key = line.partition(':')[0].strip()
        if key in PARAMS:
            comment = line.partition('#')[2]
            line = f"{key}: {PARAMS[key]}" + (f"  # {comment.strip()}" if comment else "")
        lines.append(line)
    pending = PARAMS_FILE.with_suffix(".yaml.tmp")
    pending.write_text("\n".join(lines) + "\n")
    pending.replace(PARAMS_FILE)


def load_params_file():
    global PARAMS
    PARAMS = yaml.safe_load(PARAMS_FILE.read_text())
    return True


# --- Shared state ---

waypoints = []
wp_idx = -1
patrol_running = False
patrol_loop = False
global_mode = False
floor_mode = 0  # off, raw, inflated, both
show_gradient = False
show_slam_path = False
DEPTH_MODES = ("off", "normal", "raw")   # RAW = normal + what the filter removed, in red
show_depth = 0           # live single-frame depth-camera point cloud (replaced every frame,
                          # not accumulated — distinct from the accumulated BBMap/voxel cloud)
cmd_queue = Queue()
robot_status = "waiting for nav daemon"
command_error = ""
nav_path = []
resetting = False
stopping = threading.Event()
manual_drive = False     # WASD teleop mode (mutually exclusive with autonomous patrol)
_slam_ready = False
_shared_pos = np.zeros(3, dtype=np.float32)  # latest SLAM x, y, yaw for the viewer
_map_gen = 0             # bumped when the mapping grid origin changes
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
inflated_floor_latest = [None]
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
        inflated_floor_latest[0] = None
        heat_latest[0] = None
        for c in heavy_clients:
            c['resync'][0] = True
    empty = pack_vox_keyframe_chunks(np.zeros((0, 3), np.int32), np.zeros((0, 3), np.uint8))
    for pkt in empty:
        broadcast_heavy(pkt)
    z = zlib.compress(b'', 1)
    broadcast_heavy(struct.pack('<II', 2, 0) + z)   # empty floor overlay
    broadcast_heavy(struct.pack('<II', 7, 0) + z)   # empty inflated floor overlay
    broadcast_heavy(struct.pack('<II', 3, 0) + z)   # empty gradient overlay


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


def pack_floor(grid, origin, voxel_size_m, packet_type=2):
    fi, fj = np.where(grid == 1)
    if len(fi) > 100000:
        idx = np.random.choice(len(fi), 100000, replace=False)
        fi, fj = fi[idx], fj[idx]
    n = len(fi)
    coords = np.zeros((n, 3), dtype=np.float32)
    coords[:, 0] = origin[0] + (fi + 0.5) * voxel_size_m
    coords[:, 1] = origin[1] + (fj + 0.5) * voxel_size_m
    coords[:, 2] = 0.02
    return struct.pack('<II', packet_type, n) + zlib.compress(coords.tobytes(), 1)


def pack_heatmap(g_cost, origin, voxel_size_m):
    finite = g_cost < 1e18
    fi, fj = np.where(finite)
    if len(fi) == 0:
        return struct.pack("<II", 3, 0) + zlib.compress(b"", 1)
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
           "status": command_error or robot_status,
           "resetting": resetting,
           "wp": wp_idx, "running": patrol_running, "loop": patrol_loop,
           "global": global_mode,
           "manual": manual_drive,
           "floor": floor_mode, "gradient": show_gradient, "slam_path": show_slam_path,
           "depth": show_depth,
           "nav_map": show_nav_map,
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
    if diag:
        msg["diag"] = diag
    return json.dumps(msg)


def write_route(writer, enabled):
    if writer is None:
        writer = Writer("nav.command", Type("nav_command"), keeptime=False)
    with writer.buf() as command:
        command["enabled"] = enabled
        command["num_waypoints"] = len(waypoints)
        command["waypoints"].fill(np.nan)
        for index, (x, y, yaw) in enumerate(waypoints):
            command["waypoints"][index] = (x, y, np.nan if yaw is None else yaw)
        command["loop"] = patrol_loop
        command["global_goal"] = global_mode
    return writer


def nav_client_loop():
    global waypoints, wp_idx, patrol_running, patrol_loop, global_mode, robot_status, command_error
    global manual_drive, floor_mode, show_gradient, show_slam_path, show_depth, show_nav_map
    global _slam_ready, _shared_pos, _last_slam_t
    global resetting, nav_path
    command_writer = drive_writer = mode_writer = None
    reset_writer = None
    reset_frames = reset_after = 0
    command_time = 0
    teleop_keys, teleop_shift, teleop_gain, teleop_time = '', False, 1.0, 0.0
    next_state = 0.0
    params_mtime = PARAMS_FILE.stat().st_mtime_ns
    control_interval = 1.0 / Config("base").command_hz
    with Reader("nav.command", keeptime=False) as commands, \
            Reader("nav.state", keeptime=False) as states, \
            Reader("slam.pose", keeptime=False) as poses, \
            Reader("slam.health", keeptime=False) as health, \
            Reader("drive.ctrl", keeptime=False) as drive:
        try:
            while not stopping.is_set():
                now = time.monotonic()
                if now >= next_state:
                    if (reset_writer is not None and health.ready()
                            and int(health.data['timestamp']) > reset_after):
                        # Two publications cover a frame already in flight when we wrote the flag.
                        reset_frames -= 1
                        if reset_frames == 2:
                            reset_writer['reset_map'] = True
                            reset_after = time.time_ns()
                        elif reset_frames == 0:
                            reset_writer['reset_map'] = False
                            reset_writer.__exit__(None, None, None)
                            reset_writer = None
                            resetting = False
                            command_error = ""
                            _clear_live_cloud()
                    # Read-only until Start: an external BBOS client can own navigation.
                    if commands.ready() and command_writer is None:
                        data = commands.data
                        waypoints = [(float(x), float(y), None if np.isnan(yaw) else float(yaw))
                                     for x, y, yaw in data["waypoints"][:int(data["num_waypoints"])]]
                        patrol_loop = bool(data["loop"])
                        global_mode = bool(data["global_goal"])
                    if states.ready():
                        data = states.data
                        status = data["state"].decode()
                        reason = data["reason"].decode()
                        wp_idx = int(data["waypoint_index"])
                        patrol_running = status in ("navigating", "waiting_for_drive")
                        robot_status = status + (f": {reason}" if reason else "")
                        if (command_writer is not None and status in ("reached", "failed")
                                and int(data["timestamp"]) > command_time):
                            command_writer.__exit__(None, None, None)
                            command_writer = None
                    if (not states.readable or states.data is None
                            or (time.time_ns() - int(states.data["timestamp"])) * 1e-9 > 0.5):
                        robot_status = "waiting for nav daemon"
                        patrol_running = False
                        wp_idx = -1
                    if poses.ready():
                        pose = poses.data
                        _shared_pos = (float(pose['pos'][0]), float(pose['pos'][1]),
                                       2.0 * math.atan2(float(pose['quat'][2]), float(pose['quat'][3])))
                        _last_slam_t = time.time()
                    _slam_ready = (poses.readable and poses.data is not None
                                   and (time.time_ns() - int(poses.data['timestamp'])) * 1e-9 < 0.3)
                    mtime = PARAMS_FILE.stat().st_mtime_ns
                    if mtime != params_mtime:
                        load_params_file()
                        params_mtime = mtime
                        broadcast_ws(json.dumps({"t": "params", "params": PARAMS}))

                while not cmd_queue.empty():
                    cmd = cmd_queue.get_nowait()
                    kind = cmd['type']
                    if kind != 'teleop':
                        command_error = ""
                    try:
                        send_route = False
                        if resetting and (kind in ('reset_map', 'start', 'add_wp', 'remove_last', 'clear', 'loop')
                                          or (kind == 'toggle' and cmd['key'] in ('manual', 'global'))):
                            raise RuntimeError("Map reset in progress")
                        if kind == 'reset_map':
                            if commands.readable and command_writer is None:
                                raise RuntimeError("Stop navigation from the app that owns nav.command first")
                            reset_writer = Writer("slam.trigger", Type("slam_trigger"), keeptime=False)
                            reset_writer['reset_map'] = False
                            if command_writer is not None:
                                write_route(command_writer, False)
                                command_writer.__exit__(None, None, None)
                                command_writer = None
                            manual_drive = False
                            if drive_writer is not None:
                                drive_writer['twist'] = (0.0, 0.0)
                                drive_writer.__exit__(None, None, None)
                                drive_writer = None
                            waypoints, nav_path = [], []
                            resetting = True
                            reset_frames = 4  # Observe False, then True, before releasing the writer.
                            reset_after = time.time_ns()
                        elif kind in ('add_wp', 'remove_last', 'clear', 'loop'):
                            if commands.readable and command_writer is None:
                                raise RuntimeError("nav.command is owned by another app")
                            if kind == 'add_wp':
                                if len(waypoints) == nav_constants.MAX_WAYPOINTS:
                                    raise ValueError("Maximum 128 waypoints")
                                waypoint = (float(cmd['x']), float(cmd['y']),
                                            None if cmd.get('h') is None else float(cmd['h']))
                                if not all(math.isfinite(v) for v in waypoint if v is not None):
                                    raise ValueError("Invalid waypoint coordinates")
                                waypoints.append(waypoint)
                            elif kind == 'remove_last' and waypoints:
                                waypoints.pop()
                            elif kind == 'clear':
                                waypoints = []
                            elif kind == 'loop':
                                patrol_loop = not patrol_loop
                            send_route = command_writer is not None
                        elif kind == 'start' and waypoints:
                            manual_drive = False
                            if drive_writer is not None:
                                drive_writer['twist'] = (0.0, 0.0)
                                drive_writer.__exit__(None, None, None)
                                drive_writer = None
                            send_route = True
                        elif kind == 'stop' or (kind == 'toggle' and cmd['key'] == 'manual'):
                            if kind == 'stop':
                                manual_drive = False
                            else:
                                manual_drive = not manual_drive
                                teleop_keys = ''
                            if command_writer is not None:
                                write_route(command_writer, False)
                                command_writer.__exit__(None, None, None)
                                command_writer = None
                            elif kind == 'stop' and commands.readable:
                                raise RuntimeError("Stop navigation from the app that owns nav.command")
                        elif kind == 'toggle':
                            key = cmd['key']
                            if key == 'floor': floor_mode = (floor_mode + 1) % 4
                            elif key == 'gradient': show_gradient = not show_gradient
                            elif key == 'slam_path': show_slam_path = not show_slam_path
                            elif key == 'nav_map': show_nav_map = not show_nav_map
                            elif key == 'depth': show_depth = (show_depth + 1) % len(DEPTH_MODES)
                            elif key == 'global':
                                if commands.readable and command_writer is None:
                                    raise RuntimeError("nav.command is owned by another app")
                                global_mode = not global_mode
                                send_route = command_writer is not None
                        elif kind == 'teleop' and manual_drive:
                            teleop_keys = cmd.get('keys', '')
                            teleop_shift = bool(cmd.get('shift', False))
                            teleop_gain = float(cmd.get('gain', 1.0))
                            teleop_time = now
                        elif kind == 'set_param':
                            key = cmd['key']
                            if key in PARAMS:
                                value = float(cmd['value'])
                                if not math.isfinite(value):
                                    raise ValueError("Invalid parameter value")
                                PARAMS[key] = int(round(value)) if key in INT_PARAMS else value
                                save_params_file()
                        elif kind == 'save_params':
                            save_params_file()
                            broadcast_ws(json.dumps({"t": "params", "params": PARAMS, "saved": True}))
                        elif kind == 'load_params':
                            load_params_file()
                            broadcast_ws(json.dumps({"t": "params", "params": PARAMS, "loaded": True}))
                        if send_route:
                            command_time = time.time_ns()
                            command_writer = write_route(command_writer, bool(waypoints))
                            if not waypoints:
                                command_writer.__exit__(None, None, None)
                                command_writer = None
                    except (RuntimeError, ValueError, KeyError, OSError) as error:
                        command_error = str(error)

                if manual_drive:
                    if drive_writer is None:
                        if mode_writer is not None:
                            mode_writer.__exit__(None, None, None)
                            mode_writer = None
                        try:
                            drive_writer = Writer("drive.ctrl", Type("drive_ctrl"), keeptime=False)
                            mode_writer = Writer("base.mode", Type("base_mode"), keeptime=False)
                        except RuntimeError as error:
                            if drive_writer is not None:
                                drive_writer.__exit__(None, None, None)
                                drive_writer = None
                            if not str(error).startswith(("Writer for drive.ctrl already exists (pid=", "Writer for base.mode already exists (pid=")):
                                raise
                    if drive_writer is not None:
                        with mode_writer.buf() as mode:
                            mode['mode'] = CFG_BASE.MODE_BALANCE
                            mode['lean_angle_deg'] = CFG_BASE.lean_angle_deg
                        keys = teleop_keys if now - teleop_time < TELEOP_TIMEOUT else ''
                        drive_writer['twist'] = teleop_twist(keys, teleop_shift, teleop_gain)
                        robot_status = "manual"
                    else:
                        robot_status = "manual: waiting for base control"
                elif drive_writer is not None:
                    drive_writer['twist'] = (0.0, 0.0)
                    drive_writer.__exit__(None, None, None)
                    drive_writer = None
                if not manual_drive and mode_writer is not None:
                    mode_writer.__exit__(None, None, None)
                    mode_writer = None

                if now >= next_state:
                    drive.ready()
                    speed = turn_rate = 0.0
                    if (drive.readable and drive.data is not None
                            and (time.time_ns() - int(drive.data['timestamp'])) * 1e-9 < 0.1):
                        speed, turn_rate = map(float, drive.data['twist'])
                    x, y, yaw = _shared_pos
                    # Display the current command's three-second trajectory; no control calculation.
                    prediction = []
                    px, py, heading = x, y, yaw
                    if speed or turn_rate:
                        for _ in range(30):
                            px -= math.sin(heading) * speed * 0.1
                            py += math.cos(heading) * speed * 0.1
                            heading += turn_rate * 0.1
                            prediction.append((px, py))
                    goal = waypoints[wp_idx] if patrol_running and 0 <= wp_idx < len(waypoints) else None
                    if resetting:
                        robot_status = "resetting map"
                    diag = {"v": round(speed, 3), "w": round(turn_rate, 3)}
                    broadcast_ws(make_state((x, y), yaw, goal, nav_path if patrol_running else [],
                                            prediction, diag, ready=_slam_ready))
                    next_state = now + STATE_INTERVAL
                stopping.wait(control_interval if manual_drive else STATE_INTERVAL)
        finally:
            if mode_writer is not None:
                mode_writer.__exit__(None, None, None)
            if reset_writer is not None:
                reset_writer['reset_map'] = False
                reset_writer.__exit__(None, None, None)
            if drive_writer is not None:
                drive_writer['twist'] = (0.0, 0.0)
                drive_writer.__exit__(None, None, None)
            if command_writer is not None:
                write_route(command_writer, False)
                command_writer.__exit__(None, None, None)


def map_view_loop():
    global _last_grid_t, nav_path
    grid = plan = None
    floor_shown = inflated_floor_shown = gradient_shown = False
    inflated_radius = None
    next_plan = 0.0
    with Reader("mapping.grid2d", keeptime=False) as grids, \
            Reader("nav.plan", keeptime=False) as plans:
        while not stopping.is_set():
            grid_changed = grids.ready()
            origin_changed = False
            if grid_changed:
                data = grids.data
                origin_changed = grid is None or not np.array_equal(grid['origin'], data['origin'])
                if grid is not None and origin_changed:
                    _clear_live_cloud()
                grid = data
                _last_grid_t = time.time()
            now = time.monotonic()
            plan_changed = False
            if now >= next_plan:
                plan_changed = plans.ready()
                if plan_changed:
                    plan = plans.data
                elif not plans.readable and plan is not None:
                    plan = None
                    plan_changed = True
                if plan_changed:
                    nav_path = (plan['path'][:int(plan['num_path_points'])].tolist()
                                if plan is not None and plan['waypoint_index'] >= 0 else [])
                next_plan = now + 0.5
            show_floor = floor_mode in (1, 3)
            show_inflated_floor = floor_mode in (2, 3)
            if show_floor and grid is not None and (grid_changed or not floor_shown):
                packet = pack_floor(grid['grid'], grid['origin'], CFG_M.voxel_size_m)
                with heavy_lock:
                    floor_latest[0] = packet
                broadcast_heavy(packet)
            radius = PARAMS['ROBOT_RADIUS_CELLS']
            if show_inflated_floor and grid is not None and (grid_changed or not inflated_floor_shown or radius != inflated_radius):
                # Same inflation rule as nav.compute_plan; preview it even with no active route.
                passable = grid['grid'] == 1
                passable[distance_transform_edt(passable) < radius] = False
                packet = pack_floor(passable, grid['origin'], CFG_M.voxel_size_m, packet_type=7)
                with heavy_lock:
                    inflated_floor_latest[0] = packet
                broadcast_heavy(packet)
                inflated_radius = radius
            if show_gradient and (plan_changed or origin_changed or not gradient_shown):
                packet = (pack_heatmap(plan['cost'], grid['origin'], CFG_M.voxel_size_m)
                          if plan is not None and grid is not None and plan['waypoint_index'] >= 0
                          else struct.pack('<II', 3, 0) + zlib.compress(b'', 1))
                with heavy_lock:
                    heat_latest[0] = packet
                broadcast_heavy(packet)
            floor_shown, gradient_shown = show_floor, show_gradient
            inflated_floor_shown = show_inflated_floor
            stopping.wait(0.2)


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
    pitch_rad = 0.0
    with Reader("camera.depth", keeptime=False) as r_depth, \
         Reader("imu.orientation", keeptime=False) as r_imu:
        while True:
            try:
                # imu.orientation publishes [roll, pitch, yaw] in degrees.
                if r_imu.ready():
                    pitch_rad = math.radians(float(r_imu.data['rpy'][1]))
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
                                             float(pitch_rad))
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
                        coords = r_vox.data['coords'][:nv].copy()    # already in SLAM world coordinates
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
    <button class="btn" id="globalbtn" onclick="doToggle('global')" title="Target the mapped floor connected to the robot nearest to the requested goal.">Global Goal: OFF</button>
  </div>
  <div class="row">
    <button class="btn" id="manualbtn" onclick="doToggle('manual')" title="Enables balance mode and keyboard driving. Stop returns to the configured default base mode.">Manual Drive: OFF</button>
    <button class="btn" onclick="doUndo()">Undo</button>
    <button class="btn danger" onclick="doClear()">Clear All</button>
  </div>
  <div class="row">
    <button class="btn" id="navmapbtn" onclick="doToggle('nav_map')">BBMap: OFF</button>
  </div>
  <div class="row">
    <button class="btn" id="floorbtn" onclick="doToggle('floor')" title="Cycle Off / Raw / Inflated / Both. Green: raw floor. Cyan: inflation preview using the current map and radius, even while idle.">Floor: OFF</button>
    <button class="btn" id="gradientbtn" onclick="doToggle('gradient')">Gradient</button>
    <button class="btn" id="slambtn" onclick="doToggle('slam_path')">SLAM Path</button>
    <button class="btn" id="depthbtn" onclick="doToggle('depth')">Depth</button>
    <button class="btn" id="chasebtn" onclick="toggleChase()">Chase</button>
    <button class="btn" id="graphbtn" onclick="toggleGraphs()">Graphs</button>
    <button class="btn" id="exportbtn" onclick="exportMap()">Export Map (.npz)</button>
  </div>
  <div class="row">
    <button class="btn danger" id="resetbtn" onclick="doResetMap()">Reset Map</button>
  </div>
  <div id="wplist"></div>
</div>
<div id="params">
  <div class="phdr">Tuning
    <div class="row">
      <button class="btn" id="savep" onclick="saveParams()">Save</button>
      <button class="btn" id="loadp" onclick="loadParams()">Load</button>
    </div>
  </div>
  <div id="ptip">Changes save and apply live; hover for details</div>
  <div id="sliders"></div>
</div>
<div id="diag"></div>
<div id="graphs" style="display:none">
  <div class="ghdr">
    <span>drive command</span>
    <span class="grow">
      <button class="gbtn active" onclick="setGraphGroup('speed',this)">speed v</button>
      <button class="gbtn" onclick="setGraphGroup('steer',this)">steering ω</button>
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

const floorGeo=new THREE.BufferGeometry();
const floorMat=new THREE.PointsMaterial({size:0.03,color:0x10b981,transparent:true,opacity:0.5,sizeAttenuation:true,depthTest:false,depthWrite:false});
const floorPts=new THREE.Points(floorGeo,floorMat);floorPts.renderOrder=-2;floorPts.visible=false;scene.add(floorPts);
const inflatedFloorMat=new THREE.PointsMaterial({size:0.03,color:0x38bdf8,transparent:true,opacity:0.85,sizeAttenuation:true,depthTest:false,depthWrite:false});
const inflatedFloorPts=new THREE.Points(new THREE.BufferGeometry(),inflatedFloorMat);inflatedFloorPts.renderOrder=-1;inflatedFloorPts.visible=false;scene.add(inflatedFloorPts);

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
const SCRUB_VIEW_KEYS=new Set(['floor','gradient','slam_path','nav_map','depth']);
let scrubIdx=-1;
function renderScrub(idx){
  const s=tlGet(idx);
  if(!s)return;
  scrubIdx=idx;
  const sv=Object.assign({},s);
  for(const k in scrubOverride){
    sv[k]=scrubOverride[k];
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
  if((sv.floor===1||sv.floor===3)&&floorPts.geometry.attributes.position)floorPts.visible=true;
  if((sv.floor===2||sv.floor===3)&&inflatedFloorPts.geometry.attributes.position)inflatedFloorPts.visible=true;
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
  if(!d){el.style.display='none';return;}
  el.style.display='';
  el.textContent='command v='+d.v+' m/s  ω='+d.w+' rad/s';
}

// Commanded velocities from drive.ctrl.
const GRAPH_GROUPS={
  speed:{mode:'center',series:[{k:'v',c:'#22d3ee',l:'v (m/s)'}]},
  steer:{mode:'center',series:[{k:'w',c:'#ffffff',l:'ω (rad/s)'}]},
};
const GALL=['v','w'];
const GN=1500; const gbuf={}; GALL.forEach(k=>gbuf[k]=[]);
let graphGroup='speed', showGraph=false;
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

// double-click = quick waypoint, NO heading (skips the final turn)
renderer.domElement.addEventListener('dblclick',e=>{
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
  if(e.button!==0)return;
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

window.exportMap=()=>{window.location.href='/export_map';};
window.doStart=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'start'}));};
window.doStop=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'stop'}));};
window.doLoop=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'loop'}));};
window.doUndo=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'remove_last'}));};
window.doClear=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'clear'}));};
window.doResetMap=()=>{
  if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'reset_map'}));
};
window.doToggle=(key)=>{
  // While scrubbed, display-layer toggles are view-local overrides on the historical frame
  // (manual driving still goes to the server).
  if(!isLive&&SCRUB_VIEW_KEYS.has(key)){
    const s=tlGet(scrubIdx);
    if(!s)return;
    const cur=(key in scrubOverride)?scrubOverride[key]:s[key];
    scrubOverride[key]=key==='depth'?((Number(cur)||0)+1)%3:key==='floor'?((Number(cur)||0)+1)%4:!cur;
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
  ['SMOOTH_V',0,1,0.05,'Previous-speed weight: 0 = no smoothing, 1 = hold previous speed'],
  ['SMOOTH_W',0,1,0.05,'Previous-turn-rate weight: 0 = no smoothing, 1 = hold previous turn rate'],
  ['GOAL_TOLERANCE',0,0.3,0.01,'Arrival radius — how close counts as reaching the goal (m)'],
  ['HEADING_OMEGA',0,1.0,0.05,'Final heading turn speed (rad/s) — constant, slam_reloc style'],
  ['HEADING_TOL',0.02,0.5,0.01,'Final heading tolerance — aligned when |error| below this (rad)'],
  ['STUCK_TIME',1,60,1,'Seconds of no progress before it rotates in place to rescan'],
  ['ROTATE_TIME',0.5,10,0.5,'Seconds spent rotating per rescan before replanning'],
  ['ROTATE_FRAC',0.1,1,0.05,'Rescan rotate speed as a fraction of MAX_OMEGA'],
  ['PROGRESS_EPS',0.02,0.5,0.01,'Robot movement (m) that resets the stuck timer'],
  ['N_ROTATIONS',1,20,1,'Rescans before navigation reports failed'],
];
function buildSliders(){
  document.getElementById('sliders').innerHTML=PARAM_META.map(([k,mn,mx,st,expl],i)=>
    `<div class="prow" title="${expl}" onmouseover="showTip(${i})" onmouseout="clearTip()">`+
    `<label>${k}<span class="pval" id="pv_${k}">--</span></label>`+
    `<input type="range" id="ps_${k}" min="${mn}" max="${mx}" step="${st}" oninput="onSlider('${k}')"></div>`
  ).join('');
}
window.showTip=(i)=>{document.getElementById('ptip').textContent=PARAM_META[i][0]+': '+PARAM_META[i][4];};
window.clearTip=()=>{document.getElementById('ptip').textContent='Changes save and apply live; hover for details';};
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
};
window.loadParams=()=>{if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'load_params'}));};
buildSliders();

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
function updateFloor(raw,n,points=floorPts){
  const cf=new Float32Array(raw.buffer,raw.byteOffset,n*3);
  const pos=new Float32Array(n*3);
  for(let i=0;i<n;i++){pos[i*3]=cf[i*3];pos[i*3+1]=cf[i*3+2];pos[i*3+2]=-cf[i*3+1];}
  points.geometry.dispose();
  points.geometry=new THREE.BufferGeometry();
  points.geometry.setAttribute('position',new THREE.Float32BufferAttribute(pos,3));
  if(n===0)points.visible=false;
}
function updateHeatmap(raw,n){
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
  heatPts.geometry=heatGeo;heatPts.visible=n>0;
}
// Depth packet: [pos_x f32][pos_y f32][yaw f32][pitch_rad f32][depth u16 x w*h, row-major, mm].
// Unprojects camera-frame pinhole rays -> base frame (base_from_cam) -> nav frame (robot pose at
// capture) -> three.js axes (same X=navX,Y=navZ,Z=-navY convention as everywhere else here).
// forward(h)=(-sin h,cos h) matches robotGrp.rotation.y and the server's own control-loop
// velocity integration — verified against both before writing this transform.
function updateDepth(raw){
  if(!depthCalib)return;
  const{fx,fy,cx,cy,w,h,base_from_cam}=depthCalib;
  const maxd=depthCalib.max_d||5;   // grayscale display scale
  const dv=new DataView(raw.buffer,raw.byteOffset,16);
  const px=dv.getFloat32(0,true),py=dv.getFloat32(4,true),ph=dv.getFloat32(8,true),pd=dv.getFloat32(12,true);
  const depth=new Uint16Array(raw.buffer,raw.byteOffset+16,w*h);
  const hasRaw=raw.byteLength>=16+w*h*4;   // RAW mode: the unfiltered frame rides behind the normal one
  const rawd=hasRaw?new Uint16Array(raw.buffer,raw.byteOffset+16+w*h*2,w*h):null;
  // Apply the signed absolute IMU pitch shipped in radians with this frame.
  const cpd=Math.cos(pd),spd=Math.sin(pd);
  const R01=cpd*base_from_cam[1]-spd*base_from_cam[2],  R02=spd*base_from_cam[1]+cpd*base_from_cam[2];
  const R11=cpd*base_from_cam[5]-spd*base_from_cam[6],  R12=spd*base_from_cam[5]+cpd*base_from_cam[6];
  const R21=cpd*base_from_cam[9]-spd*base_from_cam[10], R22=spd*base_from_cam[9]+cpd*base_from_cam[10];
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
    const bx=base_from_cam[0]*xc+R01*yc+R02*z+base_from_cam[3];               // camera -> base (pitch-corrected)
    const by=base_from_cam[4]*xc+R11*yc+R12*z+base_from_cam[7];
    const bz=base_from_cam[8]*xc+R21*yc+R22*z+base_from_cam[11];
    // base -> nav: base Y is forward, base X is lateral (verified numerically against
    // base_from_cam — a center-pixel ray comes out almost entirely on the Y axis, not X).
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
      if(dn!==0)put(u,v,dn,false);   // rejected pixels are already zeroed by the daemon
      if(!hasRaw)continue;
      // raw is the unfiltered twin of normal, so where they agree it is redundant: only what the
      // filter removed (normal=0) or moved by more than DEPTH_DIFF_MM is drawn, in red
      const dr=rawd[i];
      if(dr!==0&&(dn===0||Math.abs(dr-dn)>DEPTH_DIFF_MM))put(u,v,dr,true);
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
  }else if(pathLine){scene.remove(pathLine);pathLine.geometry.dispose();pathLine=null;}
  // Predicted controller trajectory (magenta) — where the current twist leads
  if(s.pred&&s.pred[0].length>1){
    if(predLine){scene.remove(predLine);predLine.geometry.dispose();}
    const pts=s.pred[0].map((x,i)=>new THREE.Vector3(x,0.25,-s.pred[1][i]));
    const curve=new THREE.CatmullRomCurve3(pts);
    predLine=new THREE.Mesh(new THREE.TubeGeometry(curve,pts.length,0.018,6,false),new THREE.MeshBasicMaterial({color:0xff3df0,depthTest:false}));
    scene.add(predLine);
  }else if(predLine){scene.remove(predLine);predLine.geometry.dispose();predLine=null;}
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
  const fb=document.getElementById('floorbtn');fb.className=s.floor?'btn active':'btn';fb.textContent=['Floor: OFF','Floor: RAW','Floor: INFLATED','Floor: BOTH'][s.floor|0];
  const gb=document.getElementById('gradientbtn');gb.className=s.gradient?'btn active':'btn';gb.textContent=s.gradient?'Gradient: ON':'Gradient';
  const db=document.getElementById('depthbtn');db.className=s.depth?'btn active':'btn';db.textContent=['Depth','Depth: NORMAL','Depth: RAW'][s.depth|0]||'Depth';
  const sb2=document.getElementById('slambtn');sb2.className=s.slam_path?'btn active':'btn';sb2.textContent=s.slam_path?'SLAM: ON':'SLAM Path';
  const nmb=document.getElementById('navmapbtn');nmb.className=s.nav_map?'btn active':'btn';nmb.textContent=s.nav_map?'BBMap: ON':'BBMap: OFF';
  voxPts.visible=!!s.nav_map;
  floorPts.visible=(s.floor===1||s.floor===3)&&!!floorPts.geometry.attributes.position&&floorPts.geometry.attributes.position.count>0;
  inflatedFloorPts.visible=(s.floor===2||s.floor===3)&&!!inflatedFloorPts.geometry.attributes.position&&inflatedFloorPts.geometry.attributes.position.count>0;
  heatPts.visible=!!s.gradient&&!!heatGeo.attributes.position&&heatGeo.attributes.position.count>0;
  if(!s.depth)depthPts.visible=false;
  // Loop/start
  const lb=document.getElementById('loopbtn');lb.textContent=s.loop?'Loop: ON':'Loop: OFF';lb.className=s.loop?'btn active':'btn';
  const gbtn=document.getElementById('globalbtn');gbtn.textContent=s.global?'Global Goal: ON':'Global Goal: OFF';gbtn.className=s.global?'btn active':'btn';gbtn.disabled=!!s.resetting;
  const mbtn=document.getElementById('manualbtn');mbtn.textContent=s.manual?'Manual Drive: ON':'Manual Drive: OFF';mbtn.className=s.manual?'btn active':'btn';
  manualDrive=!!s.manual;
  const wm=document.getElementById('wasdmode');if(wm)wm.textContent=s.manual?'DRIVE robot':'fly cam';
  const stb=document.getElementById('startbtn');stb.className=s.running?'btn active':'btn';stb.textContent=s.running?'Running':'Start';
  stb.disabled=!!s.resetting;mbtn.disabled=!!s.resetting;
  const resetbtn=document.getElementById('resetbtn');resetbtn.disabled=!!s.resetting;
  resetbtn.textContent=s.resetting?'Resetting…':'Reset Map';
  const rb=document.getElementById('rebuild'),r=s.rebuild;
  if(rb&&r){
    if(lastRebuildCount<0)lastRebuildCount=r.count;   // first state after (re)connect: no flash for an old rebuild
    else if(r.count!==lastRebuildCount){lastRebuildCount=r.count;lastRebuildAt=Date.now();}
    const fresh=r.count>0&&Date.now()-lastRebuildAt<8000;
    if(r.in_progress){rb.className='busy';rb.textContent='map rebuild in progress (PGO)…';}
    else if(r.count>0){rb.className=fresh?'fresh':'';rb.textContent=(fresh?'MAP REBUILD #':'last rebuild #')+r.count+' merged at frame '+r.frame+': floor moved '+r.moved+', emptied '+r.emptied+', filled '+r.filled+(fresh||r.age===null?'':' ('+Math.round(r.age)+'s ago)');}
    else{rb.className='';rb.textContent='';}
  }
  if(s.map_gen!==undefined && s.map_gen!==lastMapGen){
    lastMapGen=s.map_gen;
    slamTrail=[];
    if(slamLine){scene.remove(slamLine);slamLine.geometry.dispose();slamLine=null;}
  }
  // Status
  const el=document.getElementById('status');
  const st=s.status||'';
  el.textContent=st==='idle'?(s.waypoints?.length?`${s.waypoints.length} waypoints set`:'Double-click map to add waypoints'):st;
  if(s.ready===false)el.textContent+=' — waiting for SLAM';
  el.style.color=st.startsWith('failed')||st.includes('owned')?'#ef4444':s.running?'#10b981':'#f59e0b';
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
        if(m.saved){
          const b=document.getElementById('savep');b.textContent='Saved';setTimeout(()=>b.textContent='Save',1000);
        }
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
      else if(t===7){updateFloor(raw,n,inflatedFloorPts);}
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

@app.get("/export_map")
def export_map():
    reader = Reader("mapping.voxels", keeptime=False)
    try:
        if not reader.ready():
            raise HTTPException(503, "No map is available yet.")
        data = reader.data
        timestamp_ns = int(data['timestamp'].astype('int64'))
        if not 0 <= time.time_ns() - timestamp_ns < 5_000_000_000:
            raise HTTPException(503, "The map is stale. Wait for mapping to update.")
        count = int(data['num_voxels'])
        if count == 0:
            raise HTTPException(503, "The map is empty. Capture some of the scene first.")
        calibration = Path(Config('depth').calib_path).read_bytes()
        metadata = dict(schema_version=1, frame='slam_map', units='metres', up_axis='z',
                        colors='RGB uint8, 0..255', source='mapping.voxels',
                        scope='Current published map; distant tiles paged to disk may be absent.',
                        map_directory=str(CFG_M.map_dir),
                        calibration_sha256=hashlib.sha256(calibration).hexdigest(),
                        threejs_transform='[x, y, z] -> [x, z, -y]')
        output = io.BytesIO()
        np.savez_compressed(output, points=data['coords'][:count], colors=data['colors'][:count],
                            labels=data['labels'][:count], robot_pos_xy=data['robot_pos'],
                            robot_heading=data['robot_heading'], timestamp_ns=np.int64(timestamp_ns),
                            voxel_size_m=np.float32(CFG_M.voxel_size_m),
                            metadata_json=np.array(json.dumps(metadata)),
                            calibration_yaml=np.array(calibration.decode()))
        filename = 'bracketbot-map-' + time.strftime('%Y%m%d-%H%M%S', time.gmtime()) + '-UTC.npz'
        return Response(output.getvalue(), media_type='application/octet-stream',
                        headers={'Content-Disposition': f'attachment; filename="{filename}"',
                                 'Cache-Control': 'no-store'})
    finally:
        reader.__exit__(None, None, None)

@app.get("/depth_calib")
async def serve_depth_calib():
    return depth_calib

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
            while True:
                raw = await ws.receive_text()
                msg = json.loads(raw)
                t = msg.get('type')
                if t in ('add_wp','start','stop','loop','clear','remove_last','toggle',
                         'set_param','save_params','load_params','teleop','reset_map'):
                    cmd_queue.put_nowait(msg)

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
                        # No decimation here: voxel_loop's delta diffing
                        # runs against the full (undecimated) cloud, so a client that received
                        # a decimated keyframe would get deltas referencing cells it was never
                        # sent — that's a real correctness bug, not just a perf question, so
                        # it needs the diffing baseline changed too, not a quick subsample here.
                        chunks = pack_vox_keyframe_chunks(vox_state['cell'], vox_state['col'])
                        vox_keyframe_cache[0] = chunks
                    floor_pkt = floor_latest[0]; heat_pkt = heat_latest[0]
                    inflated_floor_pkt = inflated_floor_latest[0]
                    while not client['q'].empty():        # drop stale deltas before keyframe
                        try: client['q'].get_nowait()
                        except Exception: break
                if chunks is None:
                    chunks = pack_vox_keyframe_chunks(np.empty((0, 3), np.int32), np.empty((0, 3), np.uint8))
                for pkt in chunks:
                    await ws.send_bytes(pkt)
                    await asyncio.sleep(0)                 # yield: let the browser render each chunk
                if floor_pkt is not None: await ws.send_bytes(floor_pkt)
                if inflated_floor_pkt is not None: await ws.send_bytes(inflated_floor_pkt)
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


def main():
    global robot_mesh_bytes, depth_calib
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    depth_calib = compute_depth_calib()
    print(f"[depth] calib ready: fx={depth_calib['fx']:.1f} fy={depth_calib['fy']:.1f} "
          f"cx={depth_calib['cx']:.1f} cy={depth_calib['cy']:.1f}", flush=True)

    load_params_file()

    mesh_path = Path.home() / "bb-models/bb1/robot_mesh.npz"
    if mesh_path.exists():
        d = np.load(mesh_path)
        mv, mf = d['vertices'].astype(np.float32), d['faces'].astype(np.uint32)
        robot_mesh_bytes = struct.pack('<II', len(mv), len(mf)) + mv.tobytes() + mf.tobytes()

    print("[+] JIT warmup...", flush=True)
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

    client = threading.Thread(target=nav_client_loop, daemon=True)
    client.start()
    threading.Thread(target=map_view_loop, daemon=True).start()
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
    try:
        uvicorn.run(app, host="0.0.0.0", port=8010, log_level="warning", ws_per_message_deflate=False,
                    ws_ping_interval=10.0, ws_ping_timeout=30.0)
    finally:
        stopping.set()
        client.join()


if __name__ == "__main__":
    main()

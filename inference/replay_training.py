#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["bbos", "numpy", "opencv-python-headless", "tqdm"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Replay a recorded teleop episode through the model's normalize -> execute path.

Idea: take a recorded episode's ctrl actions (motor turns), normalize them to the
policy's [-100, 100] action space exactly like preprocess.py does, and then feed
those normalized vectors straight into bracketbot_adapter.send_action() -- i.e. execute them
as if the policy had emitted them. No model runs; this validates the
normalize -> unnormalize -> arm-execute pipeline against real recorded motion.

Dataset download mirrors datasets/teleop/download.py (bb-server presigned S3), but
only fetches the single episode .npz you ask for (no videos needed for replay).

Usage:
  uv run replay_training.py --list                      # list runs
  uv run replay_training.py --run <name>                # list episodes in a run
  uv run replay_training.py --run <name> --episode 3    # dry run (prints, no motion)
  uv run replay_training.py --run <name> --episode 3 --execute          # move arms
  uv run replay_training.py --run <name> --episode 3 --cal robot --execute
  uv run replay_training.py --run <name> --episode 3 --speed 0.5 --execute

Safety: without --execute it is a dry run (prints the normalized actions and the
motor turns they unnormalize to, but does NOT command the arms). Ctrl-C = ESTOP
(handled by bracketbot_adapter). STEP_MODE=1 makes bracketbot_adapter pause before each command.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from tqdm import tqdm

sys.path.insert(0, "/home/bracketbot/bbapps/inference")
import bracketbot_adapter

HERE = Path(__file__).resolve().parent
_API_KEY_PATH = Path("/etc/BB_API_KEY")  # same key the dataset daemon reads
PREFIX = "datasets/quest_teleop/"
OUT_DIR = HERE / "replay_data"
API_URL = "https://api.bracketbot.com"  # same endpoint the dataset daemon uploads to
MAX_WORKERS = 4
MAX_RETRIES = 5
CACHE_LIMIT_MB = 500  # prune oldest episodes once replay_data exceeds this


# ---------------------------------------------------------------------------
# .env / API key (mirrors download.py, but searches robot-side locations)
# ---------------------------------------------------------------------------
def load_env():
    for p in (HERE / ".env", HERE / "bb_relay" / ".env", Path.home() / ".env"):
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_api_key():
    """Resolve the BB API key, preferring /etc/BB_API_KEY (same as the dataset daemon)."""
    if _API_KEY_PATH.is_file():
        return _API_KEY_PATH.read_text().strip()
    return os.environ.get("BB_API_KEY", "")


# ---------------------------------------------------------------------------
# bb-server download API (same endpoints as datasets/teleop/download.py)
# ---------------------------------------------------------------------------
def api_request(path, body):
    api_key = get_api_key()
    req = urllib.request.Request(
        API_URL + path,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def list_items(prefix):
    items, tok = [], None
    while True:
        body = {"prefix": prefix}
        if tok:
            body["continuationToken"] = tok
        data = api_request("/v1/downloads/list", body)
        raw = data.get("keys") or data.get("items") or data.get("objects") or []
        for it in raw:
            items.append({"key": it, "size": 0} if isinstance(it, str) else it)
        tok = data.get("nextContinuationToken") or data.get("continuationToken")
        if not tok or not data.get("isTruncated", False):
            break
    return items


def presign(keys):
    urls = {}
    for i in range(0, len(keys), 1000):
        chunk = keys[i : i + 1000]
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                urls.update(api_request("/v1/downloads/presign-batch", {"keys": chunk}).get("urls", {}))
                break
            except urllib.error.HTTPError:
                if attempt == MAX_RETRIES:
                    print("presign failed", file=sys.stderr)
                time.sleep(attempt * 2)
    return urls


def discover_runs(items):
    runs = {}
    for it in items:
        parts = it["key"].removeprefix(PREFIX).split("/")
        if len(parts) >= 2:
            runs.setdefault(parts[0], []).append(it)
    return runs


def ep_num_of(key):
    m = re.search(r"ep0*(\d+)", key)
    return int(m.group(1)) if m else None


def episode_npz_keys(run_items):
    """{ep_num: key} for the per-episode .npz files in a run."""
    out = {}
    for it in run_items:
        k = it["key"]
        if k.endswith(".npz") and "/episodes/" in k:
            n = ep_num_of(k)
            if n is not None:
                out[n] = k
    return out


def download_keys(keys):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    urls = presign(keys)
    paths = {}

    def _dl(key):
        out = OUT_DIR / key.removeprefix(PREFIX)
        out.parent.mkdir(parents=True, exist_ok=True)
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                urllib.request.urlretrieve(urls[key], out)
                return key, out
            except Exception:
                if attempt == MAX_RETRIES:
                    return key, None
                time.sleep(attempt * 2)

    todo = [k for k in keys if k in urls]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futs = {pool.submit(_dl, k): k for k in todo}
        for fut in tqdm(as_completed(futs), total=len(todo), unit="file"):
            key, out = fut.result()
            if out is not None:
                paths[key] = out
    return paths


def prune_cache(limit_mb=CACHE_LIMIT_MB, keep=()):
    """Evict oldest .npz episodes (by mtime) until replay_data is under limit_mb.

    Paths in `keep` (e.g. the episode just downloaded) are never evicted.
    """
    keep = {Path(p).resolve() for p in keep}
    files = sorted(OUT_DIR.rglob("*.npz"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    limit = limit_mb * 1024 * 1024
    for p in files:
        if total <= limit:
            break
        if p.resolve() in keep:
            continue
        sz = p.stat().st_size
        p.unlink()
        total -= sz
        print(f"[cache] evicted {p.relative_to(OUT_DIR)} ({sz / 1e6:.1f} MB)")
        # tidy now-empty run/episodes dirs
        for d in (p.parent, p.parent.parent):
            try:
                d.rmdir()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Episode -> normalized actions -> execute
# ---------------------------------------------------------------------------
def sensor_ts_ns(arr):
    return arr["timestamp"].astype("datetime64[ns]").view(np.int64)


def resample_zoh(grid_ns, src_ns, values):
    """Zero-order hold onto grid_ns (matches preprocess.py for ctrl channels)."""
    idx = np.clip(np.searchsorted(src_ns, grid_ns, side="right") - 1, 0, len(src_ns) - 1)
    return values[idx]


def cal_for(npz, side, mode):
    """Calibration used for the FORWARD normalization (motor turns -> [-100,100]).

    'episode' uses the calibration embedded in the recording (what preprocess.py
    used for training). 'robot' uses this robot's live calibration (what
    bracketbot_adapter.send_action will invert) -- a pure round-trip replay.
    """
    if mode == "episode":
        return (npz[f"cal_arm_{side}_min"].astype(np.float64),
                npz[f"cal_arm_{side}_max"].astype(np.float64))
    return ((bracketbot_adapter.cal_l_min, bracketbot_adapter.cal_l_max) if side == "left"
            else (bracketbot_adapter.cal_r_min, bracketbot_adapter.cal_r_max))


def build_normalized_actions(npz, cal_mode, grid_hz):
    """Resample both ctrl streams onto a common ZOH grid (like preprocess.py),
    then min/max normalize -> the exact [-100,100] vectors the model trains on."""
    for k in ("arm_left_ctrl", "arm_right_ctrl"):
        if k not in npz:
            raise SystemExit(f"episode missing {k}")
    lt, lp = sensor_ts_ns(npz["arm_left_ctrl"]), npz["arm_left_ctrl"]["pos"].astype(np.float64)
    rt, rp = sensor_ts_ns(npz["arm_right_ctrl"]), npz["arm_right_ctrl"]["pos"].astype(np.float64)
    # left and right are recorded asynchronously (different lengths); put both on
    # one uniform grid, ZOH, exactly as preprocess.py does for ctrl channels.
    t_min, t_max = int(min(lt[0], rt[0])), int(max(lt[-1], rt[-1]))
    dt = int(1e9 / grid_hz)
    grid = np.arange(t_min, t_max + 1, dt, dtype=np.int64)
    lp_g, rp_g = resample_zoh(grid, lt, lp), resample_zoh(grid, rt, rp)
    clmin, clmax = cal_for(npz, "left", cal_mode)
    crmin, crmax = cal_for(npz, "right", cal_mode)
    norm_l = np.stack([bracketbot_adapter.normalize_pos(lp_g[i], clmin, clmax) for i in range(len(grid))])
    norm_r = np.stack([bracketbot_adapter.normalize_pos(rp_g[i], crmin, crmax) for i in range(len(grid))])
    return grid, lp_g.astype(np.float32), rp_g.astype(np.float32), norm_l, norm_r


# Data-collection home pose (motor turns), mirrors bracketbot_adapter.HOME_* on 091.
HOME_L = np.array([0.227, -0.064, -0.041, 0.267, 0.027, -0.014, 0.081, -0.134], dtype=np.float32)
HOME_R = np.array([-0.188, 0.091, 0.050, -0.262, -0.052, -0.013, -0.104, 0.133], dtype=np.float32)


def home_arms(duration=2.5, settle=2.0):
    """Ramp both arms to HOME over `duration`, then hold `settle` so they arrive.

    Implemented here (not via bracketbot_adapter.home_arms) so it works on every robot
    regardless of which bracketbot_adapter version is installed. Busy-wait pacing, no sleep.
    """
    rd_l, rd_r = bracketbot_adapter._readers["left"], bracketbot_adapter._readers["right"]
    wl, wr = bracketbot_adapter._writers["ctrl_l"], bracketbot_adapter._writers["ctrl_r"]
    while not rd_l.ready():
        pass
    while not rd_r.ready():
        pass
    start_l = np.asarray(rd_l.data["pos"], dtype=np.float32).copy()
    start_r = np.asarray(rd_r.data["pos"], dtype=np.float32).copy()
    print(f"[replay] homing arms over {duration:.1f}s (+{settle:.1f}s settle) ...", flush=True)
    t0 = time.monotonic()
    while True:
        elapsed = time.monotonic() - t0
        a = min(elapsed / duration, 1.0)
        rd_l.ready()
        rd_r.ready()
        if wl.ready():
            wl["pos"] = start_l + a * (HOME_L - start_l)
        if wr.ready():
            wr["pos"] = start_r + a * (HOME_R - start_r)
        if elapsed >= duration + settle:
            break
    print("[replay] homing complete", flush=True)


def send_raw(pos_l, pos_r):
    """Write raw motor-turn positions straight to the ctrl writers -- no
    normalize/unnormalize, no calibration. Mirrors send_action's writer guards."""
    wl, wr = bracketbot_adapter._writers["ctrl_l"], bracketbot_adapter._writers["ctrl_r"]
    pos_l = np.asarray(pos_l, dtype=np.float32)
    pos_r = np.asarray(pos_r, dtype=np.float32)
    print(f"[raw] L turns {np.array2string(pos_l, precision=4, suppress_small=True)}")
    print(f"[raw] R turns {np.array2string(pos_r, precision=4, suppress_small=True)}")
    if getattr(bracketbot_adapter, "STEP_MODE", False):
        input("[step] press Enter to execute (Ctrl-C to ESTOP)...")
    if wl.ready():
        wl["pos"] = pos_l
    if wr.ready():
        wr["pos"] = pos_r


def replay(npz, cal_mode, speed, execute, max_steps, grid_hz, raw):
    ts, raw_l, raw_r, norm_l, norm_r = build_normalized_actions(npz, cal_mode, grid_hz)
    T = len(ts)
    if max_steps:
        T = min(T, max_steps)
    jl, jr = bracketbot_adapter.cfg_l.joint_names, bracketbot_adapter.cfg_r.joint_names
    dur = (ts[T - 1] - ts[0]) / 1e9 if T > 1 else 0.0
    mode = "RAW motor turns (no normalization)" if raw else f"normalized (cal={cal_mode})"
    print(f"grid steps={len(ts)} @ {grid_hz:.0f}Hz (replaying {T})  dur={dur:.1f}s  mode={mode}  speed={speed}  execute={execute}")
    if raw:
        print(f"  raw action L[0] turns: {np.array2string(raw_l[0], precision=4, suppress_small=True)}")
        print(f"  raw action R[0] turns: {np.array2string(raw_r[0], precision=4, suppress_small=True)}")
    else:
        print(f"  norm action L[0] [-100,100]: {np.array2string(norm_l[0], precision=1, suppress_small=True)}")
        print(f"  norm action R[0] [-100,100]: {np.array2string(norm_r[0], precision=1, suppress_small=True)}")

    if not execute:
        print("\nDRY RUN (pass --execute to command the arms). Commanded motor turns:")
        for i in sorted(set([0, T // 2, T - 1])):
            if raw:
                ml, mr = raw_l[i], raw_r[i]
            else:
                ml = bracketbot_adapter.unnormalize_pos(norm_l[i].astype(np.float32), bracketbot_adapter.cal_l_min, bracketbot_adapter.cal_l_max)
                mr = bracketbot_adapter.unnormalize_pos(norm_r[i].astype(np.float32), bracketbot_adapter.cal_r_min, bracketbot_adapter.cal_r_max)
            print(f"  step {i:4d}  L turns {np.array2string(np.asarray(ml), precision=4, suppress_small=True)}")
            print(f"            R turns {np.array2string(np.asarray(mr), precision=4, suppress_small=True)}")
        return

    bracketbot_adapter.connect()
    home_arms()
    # If this bracketbot_adapter version lazily homes in send_action, mark it done so it
    # doesn't re-home mid-replay. (Older versions have no such flag — harmless.)
    if hasattr(bracketbot_adapter, "_homed"):
        bracketbot_adapter._homed = True
    base = ts[0]
    t0 = time.monotonic()
    for i in range(T):
        # pace to the recorded timestamp (scaled by --speed); busy-wait, no time.sleep
        target = (ts[i] - base) / 1e9 / max(speed, 1e-6)
        while time.monotonic() - t0 < target:
            pass
        if raw:
            send_raw(raw_l[i], raw_r[i])
        else:
            action = {jl[k]: float(norm_l[i][k]) for k in range(len(jl))}
            action.update({jr[k]: float(norm_r[i][k]) for k in range(len(jr))})
            bracketbot_adapter.send_action(action)
    bracketbot_adapter.disconnect()
    print("replay complete")


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="List available runs and exit")
    ap.add_argument("--run", help="Run name (default: latest)")
    ap.add_argument("--episode", type=int, help="Episode number to replay")
    ap.add_argument("--cal", choices=["episode", "robot"], default="episode",
                    help="Calibration for the forward normalization (default: episode = matches training)")
    ap.add_argument("--speed", type=float, default=1.0, help="Playback speed multiplier (default 1.0)")
    ap.add_argument("--rate", type=float, default=50.0, help="Resample grid rate in Hz (default 50, matches preprocess.py)")
    ap.add_argument("--max-steps", type=int, default=None, help="Cap number of steps replayed")
    ap.add_argument("--raw", action="store_true",
                    help="Send recorded motor turns directly, skipping normalize/unnormalize entirely (ignores --cal)")
    ap.add_argument("--cache-mb", type=float, default=CACHE_LIMIT_MB,
                    help=f"Prune oldest cached episodes once replay_data exceeds this (default {CACHE_LIMIT_MB} MB)")
    ap.add_argument("--execute", action="store_true", help="Actually command the arms (default: dry run)")
    args = ap.parse_args()

    load_env()
    if not get_api_key():
        sys.exit(f"BB API key not found (looked in {_API_KEY_PATH}, inference/.env, inference/bb_relay/.env, ~/.env)")

    print("listing dataset ...")
    runs = discover_runs(list_items(PREFIX))
    if not runs:
        sys.exit("no runs found")

    if args.list:
        for name in sorted(runs):
            eps = episode_npz_keys(runs[name])
            print(f"  {name:40s} episodes={len(eps)}")
        return

    run = args.run or max(runs, key=lambda n: max((it.get("lastModified", "") for it in runs[n]), default=""))
    if run not in runs:
        sys.exit(f"run '{run}' not found. Available: {', '.join(sorted(runs))}")
    eps = episode_npz_keys(runs[run])
    print(f"run '{run}': {len(eps)} episodes -> {sorted(eps)}")

    if args.episode is None:
        print("\nPass --episode <N> to replay one of the above.")
        return
    if args.episode not in eps:
        sys.exit(f"episode {args.episode} not in run '{run}'")

    print(f"downloading episode {args.episode} npz ...")
    paths = download_keys([eps[args.episode]])
    npz_path = paths.get(eps[args.episode])
    if npz_path is None:
        sys.exit("download failed")
    prune_cache(args.cache_mb, keep=[npz_path])
    npz = dict(np.load(npz_path, allow_pickle=True))
    replay(npz, args.cal, args.speed, args.execute, args.max_steps, args.rate, args.raw)


if __name__ == "__main__":
    main()

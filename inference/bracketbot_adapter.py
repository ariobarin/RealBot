"""BracketBot implementation of the policy-client Robot protocol (bbos arm daemons + cameras).

This is one concrete adapter; see example_adapter.py for the template to plug in
a different robot.
"""

import json
import os
import signal
import sys
import time
from pathlib import Path

import numpy as np
from bbos import Config, Reader, Type, Writer

cfg_l = Config("arm_left")
cfg_r = Config("arm_right")
cfg_cam_l = Config("cam_left")
cfg_cam_r = Config("cam_right")
cfg_cam_h = Config("cam_head")

# Load calibration ranges (motor turns → [-100, 100] / [0, 100])
BBOS_DAEMON_DIR = Path("/home/bracketbot/bbos/bbos/daemons")
GRIPPER_IDX = 7

def _load_cal(side):
    path = BBOS_DAEMON_DIR / f"arm_{side}" / "ranges.calibration.json"
    with open(path) as f:
        cal = json.load(f)
    cal_min = np.array(cal["cal_min"], dtype=np.float64)
    cal_max = np.array(cal["cal_max"], dtype=np.float64)
    # A zero span divides by zero in normalize_pos and collapses every action to
    # cal_min in unnormalize_pos -- the joint silently never moves.
    dead = np.where(cal_max - cal_min == 0)[0]
    if len(dead):
        raise ValueError(f"{path}: joint(s) {dead.tolist()} have cal_min == cal_max "
                         f"(zero span) -> uncalibrated; recalibrate arm_{side} before inference")
    return cal_min, cal_max

cal_l_min, cal_l_max = _load_cal("left")
cal_r_min, cal_r_max = _load_cal("right")

# Startup homing is joint-space staged torque enabling (staged_home_arms below):
# J0-J2 first, then the rest, then the waypoint spline to home.
# These waypoints are used forward by the startup spline and in REVERSE for parking.
HOME_SEG_DURATIONS = cfg_l.startup_seg_durations
HOME_WAYPOINTS_L = [*cfg_l.startup_waypoints, cfg_l.home]
HOME_WAYPOINTS_R = [*cfg_r.startup_waypoints, cfg_r.home]

# Parking: ease J0 (the lift) this far further down before cutting torque, so
# the arm parks lower. "Down" sign is per-arm (arm_right's J0 is mirrored).
J0_PARK_DOWN_TURNS = 0.7   # motor turns
J0_PARK_DOWN_SPEED = 0.4   # turns/s
# The descent ends on the FIRST startup waypoint — a bent pose. Cutting torque
# there lets gravity swing the bent arm forward into whatever sits in front of
# the robot (the table slam). So before the J0 park-down, interpolate the arm
# joints (URDF J1..J6) to straight down — the gravity equilibrium, nothing
# swings when torque cuts — while J0 and the gripper hold their parked values.
# Done BEFORE the J0 park-down so the unbend swing happens at waypoint height;
# the J0 phase then lowers an already-hanging arm vertically.
PARK_STRAIGHTEN_S = 2.0    # seconds, bent parked pose -> arm hanging straight down

# Live 'h' halt: joint-space ramp to home over this many seconds (matches the
# 'b'/left-stick home in quest_teleop.py). Keeps torque on, no IK, no parking.
HOME_DURATION = 2.5   # seconds


def normalize_pos(pos, cal_min, cal_max):
    """Motor turns → [-100, 100] (joints) / [0, 100] (gripper). Matches preprocess.py."""
    span = cal_max - cal_min
    frac = np.clip((pos - cal_min) / span, 0, 1)
    out = frac * 200.0 - 100.0
    out[GRIPPER_IDX] = frac[GRIPPER_IDX] * 100.0
    return out.astype(np.float32)


def unnormalize_pos(norm, cal_min, cal_max):
    """[-100, 100] / [0, 100] → motor turns. Inverse of normalize_pos."""
    span = cal_max - cal_min
    frac = np.empty_like(norm, dtype=np.float64)
    frac[:] = (norm + 100.0) / 200.0
    frac[GRIPPER_IDX] = norm[GRIPPER_IDX] / 100.0
    return (cal_min + frac * span).astype(np.float32)


_readers = {}
_writers = {}

observation_features = {
    **{name: float for name in cfg_l.joint_names},
    **{name: float for name in cfg_r.joint_names},
    "arm_left":  (cfg_cam_l.height, cfg_cam_l.width, 3),
    "arm_right": (cfg_cam_r.height, cfg_cam_r.width, 3),
    "head":      (cfg_cam_h.height, cfg_cam_h.width // 2, 3),
}

action_features = {
    **{name: float for name in cfg_l.joint_names},
    **{name: float for name in cfg_r.joint_names},
}


# ============================================================================
# ARM HOMING (INLINED, TODO: move to arm daemon)
# ============================================================================
def _build_path(start, waypoints):
    """Prepend the current pose and resolve nan slots ('hold previous value')."""
    path = [np.asarray(start, dtype=np.float32)]
    for wp in waypoints:
        wp = np.asarray(wp, dtype=np.float32).copy()
        prev = path[-1]
        hold = np.isnan(wp)
        wp[hold] = prev[hold]
        path.append(wp)
    return path


def _durations(seg_durations, n_segments):
    if np.isscalar(seg_durations):
        return [max(float(seg_durations), 1e-3)] * n_segments
    durs = [max(float(d), 1e-3) for d in seg_durations]
    if len(durs) != n_segments:
        raise ValueError(f"seg_durations has {len(durs)} entries, need {n_segments}")
    return durs


def _catmull_rom(path, u):
    """Position at global parameter ``u`` in [0, n] on a Catmull-Rom spline
    through ``path`` (n+1 points). Passes through every point exactly; endpoint
    tangents are clamped to zero so the motion starts and ends at rest, while
    interior waypoints are flowed through with continuous velocity."""
    n = len(path) - 1
    u = min(max(u, 0.0), float(n))
    i = min(int(np.floor(u)), n - 1)
    t = u - i
    p0, p1 = path[i], path[i + 1]
    m0 = np.zeros_like(p0) if i == 0 else 0.5 * (path[i + 1] - path[i - 1])
    m1 = np.zeros_like(p1) if i + 1 == n else 0.5 * (path[i + 2] - path[i])
    t2 = t * t
    t3 = t2 * t
    return ((2 * t3 - 3 * t2 + 1) * p0 + (t3 - 2 * t2 + t) * m0
            + (-2 * t3 + 3 * t2) * p1 + (t3 - t2) * m1)


def _u_of_t(durs, t):
    """Spline parameter at elapsed time ``t``: segment i spans [i, i+1] over
    ``durs[i]`` seconds, so each segment keeps its allotted time and the arm
    moves at a steady pace through the interior (no global slow-down). Velocity
    is zero only at the very start and end (clamped spline tangents)."""
    if t <= 0.0:
        return 0.0
    acc = 0.0
    for i, d in enumerate(durs):
        if t < acc + d:
            return i + (t - acc) / d
        acc += d
    return float(len(durs))


def _command(arm, t):
    """Commanded pose for ``arm`` at elapsed time ``t`` (seconds): a Catmull-Rom
    spline that flows continuously through the interior waypoints and rests only
    at the start and the final waypoint."""
    return _catmull_rom(arm["path"], _u_of_t(arm["durs"], t))


# --- Quaternion / easing helpers (shutdown-park retreat) --------------------


def _slerp(q0, q1, s):
    """Spherical interpolation between xyzw quaternions (shortest arc)."""
    q0 = q0 / np.linalg.norm(q0)
    q1 = q1 / np.linalg.norm(q1)
    d = float(np.dot(q0, q1))
    if d < 0.0:
        q1, d = -q1, -d
    if d > 0.9995:
        out = q0 + s * (q1 - q0)
        return out / np.linalg.norm(out)
    th = np.arccos(np.clip(d, -1.0, 1.0))
    return (np.sin((1.0 - s) * th) * q0 + np.sin(s * th) * q1) / np.sin(th)


def _smoothstep(s):
    s = min(max(s, 0.0), 1.0)
    return s * s * (3.0 - 2.0 * s)


def quat_forward_z(quat):
    """Forward (gripper) axis = body Z-axis of an xyzw quaternion, in the IK target frame."""
    x, y, z, w = quat
    return np.array([
        2 * (x * z + y * w),
        2 * (y * z - x * w),
        1 - 2 * (x * x + y * y),
    ])


def _quat_mul(a, b):
    """Hamilton product a∘b of xyzw quaternions (apply b's rotation, then a's)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ])


def _quat_point_forward(quat):
    """Rotate an xyzw EE quat by the MINIMAL world rotation that takes its
    forward (gripper, body-Z) axis onto world +x. Preserves the roll about the
    gripper axis, so only the pointing direction changes."""
    quat = np.asarray(quat, dtype=np.float64)
    f = quat_forward_z(quat)
    f = f / np.linalg.norm(f)
    x = np.array([1.0, 0.0, 0.0])
    axis = np.cross(f, x)
    s = float(np.linalg.norm(axis))
    c = float(np.clip(np.dot(f, x), -1.0, 1.0))
    if s < 1e-9:
        if c > 0.0:
            return quat.copy()          # already pointing forward
        axis, s = np.array([0.0, 0.0, 1.0]), 1.0   # exactly backward: flip about world z
    half = 0.5 * np.arctan2(s, c)
    r = np.append(np.sin(half) * axis / s, np.cos(half))
    out = _quat_mul(r, quat)
    return out / np.linalg.norm(out)


# --- Staged homing (what connect() uses) ------------------------------------
# Ported verbatim from quest_teleop.py — keep the two in sync. An IK-planned
# tuck-lift-unfold homing was tried first; on hardware it kept pressing
# table-rested arms into the table (relaxed-IK treats the pose as a soft
# target and leaves the links between base and EE unconstrained), so
# joint-space staging won.
# Two torque-enable stages; the distal joints hang LIMP until their stage:
#   stage 1: J0-J2 energize at their live pose; J0 lifts to the top WHILE
#            J1/J2 swing to their first-waypoint values, the rest of the arm
#            hanging limp — minimal moment on the lift carriage. J0's target
#            is backed off the top hardstop: pressing the stop under load
#            stalls J0 into its hard current limit, the daemon cuts the lift,
#            and the cooldown re-catch was the original startup jerk.
#   stage 2: the remaining joints energize at their live (gravity-settled)
#            pose and ramp to the first waypoint. The flush before each enable
#            tracks the LIVE state (see _enable) — the limp joints re-orient
#            under gravity during stage 1, and flushing a stale pose here was
#            the old J3 cut-in snap. Stage 1's motion also leaves the forearm
#            SWINGING, so _enable additionally waits for the new joints to
#            hang still and re-flushes to the first post-stall state sample
#            (the caught pose) — see the STAGED_STILL block below.
# Then the arm flows through the remaining waypoints to home via home_arms.
STAGED_J01_RAMP_S = 1.75   # seconds, stage-1 J0 lift + J1/J2 swing together
STAGED_WP_RAMP_S = 1.75    # seconds, stage-2 ramp to the first waypoint
STAGED_J0_BACKOFF_TURNS = 0.05  # keep the staged J0 target this far below the top
# Tail (home_arms through the remaining waypoints): app-side overrides of the
# config's startup_seg_durations/settle, startup only — the dehome descent
# keeps the config values.
STAGED_TAIL_SEG_S = 0.9      # seconds per tail spline segment (cfg default 1.25)
STAGED_TAIL_SETTLE_S = 0.25  # hold at home while the daemon LPF converges (~50 ms)
# Torque-enable synchronization. The arm daemon blocks ~0.55 s in
# set_operating_mode (4x 0.1 s register-settle sleeps + torque restore) before
# Torque_Enable reaches the motors, and its state publishing stalls for that
# whole window. So instead of a worst-case fixed settle, hold the flushed pose
# and watch the state stream: a publishing gap > GAP_S followed by a fresh
# sample means the daemon just finished the mode switch and torque is on
# (Torque_Enable is written before the next state tick). Then hold MARGIN_S
# more so the first post-enable goal writes latch, and ramp. TIMEOUT_S caps the
# wait (covers a set_operating_mode retry; on timeout we proceed exactly like
# the old fixed settle).
STAGED_FLUSH_S = 0.1             # live-pose flush before each torque enable
STAGED_ENABLE_GAP_S = 0.25       # state silence >= this = daemon is in the mode-switch stall
STAGED_ENABLE_MARGIN_S = 0.15    # extra hold after publishing resumes
STAGED_ENABLE_TIMEOUT_S = 1.5    # hard cap on the whole enable wait
# Anti-jerk at torque cut-in. Stage 1's motion sets the still-limp distal
# joints (J3 carries the dangling forearm) swinging, and the daemon's state
# stream is SILENT for the whole ~0.55 s mode-switch stall — so a flush taken
# before the enable is stale by up to half a pendulum period, and the catch
# yanks the joint back to it. Two measures in _enable:
#   1. wait for the to-be-enabled joints to hang STILL (position drift under
#      STAGED_STILL_EPS_TURNS across a STAGED_STILL_WINDOW_S window, bounded
#      by STAGED_STILL_TIMEOUT_S) before flushing — a still joint cannot go
#      stale during the blind stall;
#   2. the first state sample after the stall is read with torque already on:
#      re-flush the new joints to IT (the true caught pose), so the command
#      equals the actual position and the ramp starts from the catch.
STAGED_STILL_WINDOW_S = 0.25     # how long the pose must hold to count as still
STAGED_STILL_EPS_TURNS = 0.025   # max drift within the window (~9 deg)
STAGED_STILL_TIMEOUT_S = 0.5     # give up waiting and enable anyway (old behavior)


def _smootherstep(s):
    """Quintic ease: zero velocity AND zero acceleration at both ends — a
    gentler start than _smoothstep, for joints that jerk at torque cut-in."""
    s = min(max(s, 0.0), 1.0)
    return s * s * s * (s * (6.0 * s - 15.0) + 10.0)


def staged_home_arms(specs, settle_s=0.4, rate_hz=200.0):
    """Drive every arm in ``specs`` to home in two torque-enable stages
    (J0-J2, then the rest — limp until their stage), simultaneously (blocking).

    Each spec is a dict:
        cfg       Config for the arm.
        r_state   open Reader("<arm>.state").
        w_ctrl    open Writer("<arm>.ctrl", Type("arm_ctrl")).
        w_torque  open Writer("<arm>.torque", Type("arm_torque")).
        tau_mode  optional (dof,) bool array applied after homing. Default: position mode.

    Energizing joints mid-sequence is safe: the arm daemon reseeds each
    newly-enabled joint's command filter to its actual position on the
    OFF->ON transition, and we flush ctrl to that same live pose first."""
    # Kill any stale drive from a previous run BEFORE staging. A killed session
    # leaves torque enabled with a stale command in the ctrl buffer, and the
    # daemon keeps driving the arm toward it; it only reseeds its command
    # filter to the actual pose on an OFF->ON transition, so disable first.
    for s in specs:
        with s["w_torque"].buf() as b:
            b["enable"][:] = np.zeros(s["cfg"].dof, dtype=np.bool_)

    arms = []
    for s in specs:
        cfg = s["cfg"]
        while not s["r_state"].ready():
            pass
        start = np.array(s["r_state"].data["pos"], dtype=np.float64)
        wp1 = np.asarray(cfg.startup_waypoints[0], dtype=np.float64).copy()
        wp1[np.isnan(wp1)] = start[np.isnan(wp1)]
        # J0's staged target: home (the top) backed off the hardstop by a
        # margin. "Down" sign is per-arm (the right arm is mirrored); the first
        # startup waypoint is always below home, so it gives the direction.
        down = float(np.sign(float(cfg.startup_waypoints[0][0]) - float(cfg.home[0]))) or 1.0
        wp1[0] = float(cfg.home[0]) + down * STAGED_J0_BACKOFF_TURNS
        arms.append({
            "spec": s, "cfg": cfg, "dof": cfg.dof, "r_state": s["r_state"],
            "w_ctrl": s["w_ctrl"], "w_torque": s["w_torque"],
            "wp1": wp1, "cmd": start.copy(),
            "enabled": np.zeros(cfg.dof, dtype=np.bool_),
        })

    paced = [a["w_ctrl"] for a in arms] + [a["w_torque"] for a in arms]
    saved = [w._keeptime for w in paced]
    dt = 1.0 / rate_hz
    for w in paced:
        w._keeptime = False

    def _write_cmd(arm, pos):
        arm["cmd"] = np.asarray(pos, dtype=np.float64)
        with arm["w_ctrl"].buf() as b:
            b["pos"][:] = arm["cmd"].astype(np.float32)
            b["vel"][:] = np.zeros(arm["dof"], dtype=np.float32)
            b["tau"][:] = np.zeros(arm["dof"], dtype=np.float32)
            b["alpha"] = 0.0

    def _enable(joints):
        """Energize ``joints`` on every arm at their live pose, jerk-free (see
        the STAGED_STILL block): wait for the new joints to hang still, flush
        ctrl to the LIVE pose (Reader.data only refreshes on ready(), so poll
        it), write the enable, keep tracking the live pose until the daemon's
        state stream goes silent for the mode-switch stall, then re-flush to
        the first sample after the gap — it is read with torque already on, so
        it IS the caught pose, and commanding it means the daemon has nothing
        to yank the joint toward."""
        new_js = [[j for j in joints if j < a["dof"] and not a["enabled"][j]]
                  for a in arms]

        # 1. Wait for the to-be-enabled joints to stop swinging: restart each
        # arm's stillness window whenever any of them drifts past the eps.
        t0 = time.monotonic()
        win_pose = [None] * len(arms)
        win_t = [t0] * len(arms)
        while True:
            now = time.monotonic()
            still = True
            for i, a in enumerate(arms):
                a["r_state"].ready()
                obs = np.array(a["r_state"].data["pos"], dtype=np.float64)
                _write_cmd(a, np.where(a["enabled"], a["cmd"], obs))
                js = new_js[i]
                if js and (win_pose[i] is None
                           or np.max(np.abs(obs[js] - win_pose[i][js])) > STAGED_STILL_EPS_TURNS):
                    win_pose[i] = obs
                    win_t[i] = now
                if now - win_t[i] < STAGED_STILL_WINDOW_S:
                    still = False
            if still:
                break
            if now - t0 >= STAGED_STILL_TIMEOUT_S:
                print("[staged_home] still-wait timed out — enabling on a moving arm", flush=True)
                break
            time.sleep(dt)

        # 2. Flush, then enable.
        t0 = time.monotonic()
        while time.monotonic() - t0 < STAGED_FLUSH_S:
            for a in arms:
                a["r_state"].ready()
                obs = np.array(a["r_state"].data["pos"], dtype=np.float64)
                _write_cmd(a, np.where(a["enabled"], a["cmd"], obs))
            time.sleep(dt)
        for a in arms:
            a["enabled"][[j for j in joints if j < a["dof"]]] = True
            with a["w_torque"].buf() as b:
                b["enable"][:] = a["enabled"]
                b["tau_mode"][:] = np.zeros(a["dof"], dtype=np.bool_)
                b["compliance_mode"] = False

        # 3. Track the (still-limp) joints while the daemon's stream is alive,
        # detect the mode-switch stall by its silence, and snap the command to
        # the first post-gap sample — the true caught pose.
        t_en = time.monotonic()
        last_fresh = [t_en] * len(arms)
        resumed = [None] * len(arms)
        while True:
            now = time.monotonic()
            for i, a in enumerate(arms):
                if a["r_state"].ready():
                    if resumed[i] is None:
                        if now - last_fresh[i] >= STAGED_ENABLE_GAP_S:
                            resumed[i] = now
                        # pre-stall: freshest limp pose; post-gap: the catch.
                        cmd = a["cmd"].copy()
                        cmd[new_js[i]] = np.array(a["r_state"].data["pos"],
                                                  dtype=np.float64)[new_js[i]]
                        a["cmd"] = cmd
                    last_fresh[i] = now
                _write_cmd(a, a["cmd"])
            if (all(r is not None for r in resumed)
                    and now - max(resumed) >= STAGED_ENABLE_MARGIN_S):
                break
            if now - t_en >= STAGED_ENABLE_TIMEOUT_S:
                break
            time.sleep(dt)

    def _ramp(joints, duration):
        """Smoothstep-ramp ``joints`` from their current command to their wp1
        values over ``duration`` seconds on one shared timeline; all other
        joints keep holding their current command."""
        froms = [a["cmd"].copy() for a in arms]
        tos = []
        for a, p0 in zip(arms, froms):
            p1 = p0.copy()
            js = [j for j in joints if j < a["dof"]]
            p1[js] = a["wp1"][js]
            tos.append(p1)
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            f = _smoothstep(t / max(duration, 1e-3))
            # J3 carries the forearm, so it eases in on a quintic (zero
            # initial acceleration) instead of the cubic.
            f3 = _smootherstep(t / max(duration, 1e-3))
            for a, p0, p1 in zip(arms, froms, tos):
                blend = np.full(a["dof"], f)
                if a["dof"] > 3:
                    blend[3] = f3
                _write_cmd(a, p0 + blend * (p1 - p0))
            if t >= duration:
                break
            time.sleep(dt)

    try:
        rest = sorted({j for a in arms for j in range(3, a["dof"])})
        print("[staged_home] stage 1: J0 -> top + J1/J2 -> first waypoint (rest limp)", flush=True)
        _enable([0, 1, 2])
        _ramp([0, 1, 2], STAGED_J01_RAMP_S)
        print("[staged_home] stage 2: remaining joints -> first waypoint", flush=True)
        _enable(rest)
        _ramp(rest, STAGED_WP_RAMP_S)
    finally:
        for w, kt in zip(paced, saved):
            w._keeptime = kt

    # Finish exactly like the IK flow: flow through the remaining waypoints to
    # home. home_arms restarts from the (now first-waypoint) pose, keeps torque
    # on, and applies each spec's tau_mode at the end. Startup-only pacing
    # overrides (STAGED_TAIL_*); the dehome descent keeps the config values.
    home_arms([
        dict(a["spec"],
             waypoints=[*[np.asarray(w) for w in a["cfg"].startup_waypoints[1:]],
                        a["cfg"].home],
             seg_durations=STAGED_TAIL_SEG_S)
        for a in arms
    ], settle_s=min(settle_s, STAGED_TAIL_SETTLE_S), rate_hz=rate_hz)


# Shutdown park: before J0 drops, back the EE this far backward (-x) and down
# (-z) via IK from the lowest waypoint, so the descending arm clears whatever
# sits in front of the robot (e.g. a table edge). The retreat poses are
# APPENDED to the descent spline (see home_arms setup), so the arm flows
# through the lowest waypoint into the retreat without stopping. J0 is PINNED
# at its parked coordinate through the retreat: the solver's J0 centering
# (nominal = top, heavily weighted) otherwise raises the lift as soon as the
# retreat frees the target — the teardown jerk. The arm joints alone realize
# the back/down move (elbow folds); the park-down phase owns all lift descent.
PARK_RETREAT_M = 0.1       # meters, backward (-x)
PARK_RETREAT_DOWN_M = 0.05  # meters, downward (-z); raise/lower the retreat goal here
PARK_RETREAT_SPEED = 0.10    # m/s along the retreat line


def straight_down_turns(cfg, turns):
    """The same pose with the ARM joints (URDF 1..6) straightened to hang
    straight down — the gravity equilibrium, so nothing swings when torque is
    cut. J0 (the lift) and the gripper keep their values. Motor turns in/out."""
    u = cfg.q2urdf(np.asarray(turns, dtype=np.float64).copy())
    u[1:cfg.dof - 1] = 0.0
    return cfg.urdf2q(u)


def home_arms(specs, settle_s=0.4, rate_hz=200.0, disable_torque_after=False):
    """Drive every arm in ``specs`` to home through its waypoints, simultaneously.

    The arm flows continuously through the interior waypoints via a Catmull-Rom
    spline, resting only at the start and the end of the path. For the
    shutdown park (``disable_torque_after``) the IK retreat is appended to the
    path, so the arm flows through the lowest waypoint straight into the
    retreat and rests at the retreated pose.

    Each spec is a dict:
        cfg          Config for the arm.
        r_state      open Reader("<arm>.state").
        w_ctrl       open Writer("<arm>.ctrl", Type("arm_ctrl")).
        w_torque     open Writer("<arm>.torque", Type("arm_torque")).
        waypoints    optional list of (dof,) arrays to pass through, in order; the last should be home. Default: [cfg.home].
        seg_durations  optional scalar or per-segment list of seconds. Default 2.5.
        tau_mode     optional (dof,) bool array applied to the torque writer AFTER homing. Default: leaves all joints in position mode.

    disable_torque_after  if True, park for shutdown: after the spline (and IK
        retreat) the arm joints straighten to hang straight down (gravity
        equilibrium, so nothing swings at torque-off), J0 eases further down,
        and torque is cut so the arm rests hanging.
    """
    arms = []
    for s in specs:
        cfg = s["cfg"]
        dof = cfg.dof
        waypoints = s.get("waypoints") or [np.asarray(cfg.home, dtype=np.float32)]
        r_state = s["r_state"]
        while not r_state.ready():
            pass
        start = np.array(r_state.data["pos"], dtype=np.float32)
        path = _build_path(start, waypoints)
        durs = _durations(s.get("seg_durations", 2.5), len(path) - 1)
        # Shutdown park: extend the descent spline with the IK retreat, so the
        # arm FLOWS through the lowest waypoint into the retreat (Catmull-Rom
        # rests only at path ends) instead of stopping there and restarting.
        # The retreat depends only on the (known) final waypoint, so it can be
        # precomputed here.
        if disable_torque_after and PARK_RETREAT_M:
            retreat_vec = np.array([-PARK_RETREAT_M, 0.0, -PARK_RETREAT_DOWN_M])
            retreat_dist = float(np.linalg.norm(retreat_vec))
            retreat_dur = max(retreat_dist / PARK_RETREAT_SPEED, 1e-3)
            q_wp = cfg.q2urdf(np.array(path[-1], dtype=np.float64).copy())
            ee_pos, ee_quat = cfg.ik.fk(list(q_wp[:7]))
            ee_pos = np.asarray(ee_pos, dtype=np.float64)
            goal = ee_pos + retreat_vec
            n = max(int(np.ceil(retreat_dist / PARK_RETREAT_SPEED * 50.0)), 1)
            cfg.ik.reset(list(q_wp[:7]))
            samples = [q_wp[:7].copy()]
            q = q_wp[:7].copy()
            fails = 0
            # Orientation: ease the gripper to point straight forward (+x) by
            # the end of the retreat — minimal rotation from the parked
            # orientation (roll preserved), slerped along the line.
            quat0 = np.asarray(ee_quat, dtype=np.float64)
            quat_fwd = _quat_point_forward(quat0)
            for i in range(1, n + 1):
                p = ee_pos + (i / n) * (goal - ee_pos)
                sol = cfg.ik.solve(list(p), list(_slerp(quat0, quat_fwd, i / n)))
                if sol is not None and len(sol) >= 7:
                    q = np.asarray(sol[:7], dtype=np.float64)
                    # Pin the lift (same as the IK unfold): the solver's J0
                    # centering (nominal = top, heavily weighted) otherwise
                    # raises the lift as soon as the retreat frees the target —
                    # the arm joints alone realize the back/down move, and all
                    # J0 descent belongs to the park-down phase.
                    q[0] = q_wp[0]
                else:
                    fails += 1  # hold the previous sample; the line continues
                samples.append(q.copy())
            if fails:
                print(f"[park] {cfg.ee_frame}: retreat {fails}/{n} IK misses", flush=True)
            # Two retreat waypoints (mid + end) keep the character of the IK
            # line while the spline does the smoothing; the solver's noisy
            # first samples never become commands.
            for q7 in (samples[len(samples) // 2], samples[-1]):
                full = q_wp.copy()
                full[:7] = q7
                path.append(cfg.urdf2q(full).astype(np.float32))
            durs = durs + [retreat_dur / 2.0, retreat_dur / 2.0]
        arms.append({
            "dof": dof,
            "cfg": cfg,
            "w_ctrl": s["w_ctrl"],
            "w_torque": s["w_torque"],
            "path": path,
            "durs": durs,
            "total": sum(durs),
            "tau_mode": s.get("tau_mode"),
        })

    paced = [a["w_ctrl"] for a in arms] + [a["w_torque"] for a in arms]
    saved = [w._keeptime for w in paced]
    dt = 1.0 / rate_hz
    for w in paced:
        w._keeptime = False
    try:
        # Safe-enable each arm: flush ctrl to the current pose, then energize.
        for a in arms:
            dof = a["dof"]
            with a["w_ctrl"].buf() as b:
                b["pos"][:] = a["path"][0]
                b["vel"][:] = np.zeros(dof, dtype=np.float32)
                b["tau"][:] = np.zeros(dof, dtype=np.float32)
                b["alpha"] = 0.0
            with a["w_torque"].buf() as b:
                b["enable"][:] = np.ones(dof, dtype=np.bool_)
                b["tau_mode"][:] = np.zeros(dof, dtype=np.bool_)
                b["compliance_mode"] = False

        # Ramp all arms along one shared timeline.
        total = max(a["total"] for a in arms)
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            for a in arms:
                cmd = _command(a, t).astype(np.float32)
                with a["w_ctrl"].buf() as b:
                    b["pos"][:] = cmd
                    b["tau"][:] = np.zeros(a["dof"], dtype=np.float32)
                    b["alpha"] = 0.0
            if t >= total:
                break
            time.sleep(dt)

        # Settle: hold the final waypoint while the daemon's LPF converges.
        t0 = time.monotonic()
        while time.monotonic() - t0 < settle_s:
            for a in arms:
                with a["w_ctrl"].buf() as b:
                    b["pos"][:] = a["path"][-1]
            time.sleep(dt)

        # Shutdown park: the spline ends on a BENT pose — cutting torque there
        # lets gravity swing the arm forward into the table. Interpolate the
        # arm joints to straight down (gravity equilibrium) here, at waypoint
        # height where the unbend swing has clearance; the J0 park-down below
        # then lowers an already-hanging arm vertically.
        if disable_torque_after and PARK_STRAIGHTEN_S:
            for a in arms:
                a["straight"] = straight_down_turns(
                    a["cfg"], np.array(a["path"][-1], dtype=np.float64))
            t0 = time.monotonic()
            while True:
                t = time.monotonic() - t0
                frac = _smoothstep(t / PARK_STRAIGHTEN_S)
                for a in arms:
                    p0 = np.asarray(a["path"][-1], dtype=np.float64)
                    with a["w_ctrl"].buf() as b:
                        b["pos"][:] = (p0 + frac * (a["straight"] - p0)).astype(np.float32)
                        b["tau"][:] = np.zeros(a["dof"], dtype=np.float32)
                        b["alpha"] = 0.0
                if t >= PARK_STRAIGHTEN_S:
                    break
                time.sleep(dt)
            # Hold straight-down so the LPF converges before J0 descends.
            t0 = time.monotonic()
            while time.monotonic() - t0 < settle_s:
                for a in arms:
                    with a["w_ctrl"].buf() as b:
                        b["pos"][:] = a["straight"].astype(np.float32)
                time.sleep(dt)

        # The pose (motor turns) the J0 park-down starts from and holds — the
        # straightened pose (shutdown park), else the end of the spline.
        for a in arms:
            a["park_pose"] = np.array(a.get("straight", a["path"][-1]), dtype=np.float64)

        # After settling (and retreating), ease J0 (the lift) further down
        # before cutting torque, so the arm parks lower. "Down" sign is per-arm
        # (arm_right's J0 is mirrored).
        if disable_torque_after and J0_PARK_DOWN_TURNS:
            targets = []
            for a in arms:
                j0_from = float(a["park_pose"][0])
                down = float(np.sign(j0_from - float(a["cfg"].home[0]))) or 1.0
                targets.append((j0_from, j0_from + down * J0_PARK_DOWN_TURNS))
            ramp_s = max(J0_PARK_DOWN_TURNS / J0_PARK_DOWN_SPEED, 1e-3)
            t0 = time.monotonic()
            while True:
                t = time.monotonic() - t0
                frac = _smoothstep(t / ramp_s)
                for a, (j0_from, j0_to) in zip(arms, targets):
                    pos = np.array(a["park_pose"], dtype=np.float32).copy()
                    pos[0] = j0_from + frac * (j0_to - j0_from)
                    with a["w_ctrl"].buf() as b:
                        b["pos"][:] = pos
                        b["tau"][:] = np.zeros(a["dof"], dtype=np.float32)
                        b["alpha"] = 0.0
                if t >= ramp_s:
                    break
                time.sleep(dt)
            # Hold the lowered pose so the LPF converges before torque is cut.
            t0 = time.monotonic()
            while time.monotonic() - t0 < settle_s:
                for a, (j0_from, j0_to) in zip(arms, targets):
                    pos = np.array(a["park_pose"], dtype=np.float32).copy()
                    pos[0] = j0_to
                    with a["w_ctrl"].buf() as b:
                        b["pos"][:] = pos
                time.sleep(dt)

        # Final torque state, written here while still ungated so the scheduler
        # can't drop it once keeptime is restored. Either cut torque (shutdown
        # park) or apply each arm's requested tau_mode (e.g. gripper compliance)
        # while keeping it enabled.
        for a in arms:
            if disable_torque_after:
                with a["w_torque"].buf() as b:
                    b["enable"][:] = np.zeros(a["dof"], dtype=np.bool_)
            elif a["tau_mode"] is not None:
                with a["w_torque"].buf() as b:
                    b["enable"][:] = np.ones(a["dof"], dtype=np.bool_)
                    b["tau_mode"][:] = np.asarray(a["tau_mode"], dtype=np.bool_)
                    b["compliance_mode"] = False
    finally:
        for w, kt in zip(paced, saved):
            w._keeptime = kt


def park_arms(specs, settle_s=0.4, rate_hz=200.0):
    """Reverse of :func:`home_arms`, for shutdown.

    Drives every arm from its current pose back DOWN through its homing
    waypoints in REVERSE order, straightens the arm joints to hang straight
    down, eases J0 lower, and cuts torque — so the arm rests hanging at its
    gravity equilibrium instead of falling forward from a bent pose.

    Each spec is the same dict as :func:`home_arms`. Pass the SAME startup
    ``waypoints`` (whose last element is home): they are reversed internally, so
    the arm flows current -> home -> ... -> first waypoint and powers off there.
    A per-segment ``seg_durations`` list is reversed to match; a scalar is used
    as-is. Any ``tau_mode`` is ignored (torque is disabled at the end).
    """
    rev = []
    for s in specs:
        s = dict(s)
        wps = s.get("waypoints")
        if wps:
            s["waypoints"] = list(wps)[::-1]
            sd = s.get("seg_durations")
            if sd is not None and not np.isscalar(sd):
                s["seg_durations"] = list(sd)[::-1]
        rev.append(s)
    home_arms(rev, settle_s=settle_s, rate_hz=rate_hz, disable_torque_after=True)


# End of ARM HOMING (INLINED, TODO: move to arm daemon)

# Shutdown progresses through stages so a second Ctrl-C escalates:
#   0 = running, 1 = graceful park done / in progress, 2 = hard ESTOP.
_shutdown_stage = 0


def _estop_now():
    """Cut torque immediately (emergency / second Ctrl-C) and exit."""
    print("\n[ESTOP] Disabling arm torque!", flush=True)
    for key in ("torque_l", "torque_r"):
        w = _writers.get(key)
        if w is not None:
            # Ungate the write so the scheduler's keeptime gate can't drop it.
            w._keeptime = False
            with w.buf() as b:
                b["enable"][:] = np.zeros(cfg_l.dof, dtype=np.bool_)
    sys.exit(1)


def _park_and_off():
    """Drive both arms back down through the homing waypoints in reverse, then
    cut torque so they rest limp at the lowest waypoint (mirror of startup)."""
    if "left" not in _readers:  # already torn down
        return
    print("[bracketbot_adapter] inference ending — parking arms, then torque off...", flush=True)
    park_arms([
        dict(cfg=cfg_l, r_state=_readers["left"], w_ctrl=_writers["ctrl_l"],
             w_torque=_writers["torque_l"], waypoints=HOME_WAYPOINTS_L,
             seg_durations=HOME_SEG_DURATIONS),
        dict(cfg=cfg_r, r_state=_readers["right"], w_ctrl=_writers["ctrl_r"],
             w_torque=_writers["torque_r"], waypoints=HOME_WAYPOINTS_R,
             seg_durations=HOME_SEG_DURATIONS),
    ])
    print("[bracketbot_adapter] parked — torque off", flush=True)


def _on_signal(*_):
    """First Ctrl-C / SIGTERM: graceful park then power off. A second one that
    arrives during the park escalates to an immediate ESTOP."""
    global _shutdown_stage
    _shutdown_stage += 1
    if _shutdown_stage >= 2:
        _estop_now()
    else:
        _park_and_off()
        sys.exit(0)


def connect(calibrate=True):
    _readers["left"] = Reader("arm_left.state").__enter__()
    _readers["right"] = Reader("arm_right.state").__enter__()
    _readers["cam_left"] = Reader("camera.left.jpeg").__enter__()
    _readers["cam_right"] = Reader("camera.right.jpeg").__enter__()
    _readers["cam_head"] = Reader("camera.head.jpeg").__enter__()
    _writers["torque_l"] = Writer("arm_left.torque", Type("arm_torque")).__enter__()
    _writers["ctrl_l"] = Writer("arm_left.ctrl", Type("arm_ctrl")).__enter__()
    _writers["torque_r"] = Writer("arm_right.torque", Type("arm_torque")).__enter__()
    _writers["ctrl_r"] = Writer("arm_right.ctrl", Type("arm_ctrl")).__enter__()
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)
    # Bring both arms to home via staged torque enabling before the policy takes over.
    print("[bracketbot_adapter] homing to start pose (staged torque enable)...", flush=True)
    staged_home_arms([
        dict(cfg=cfg_l, r_state=_readers["left"], w_ctrl=_writers["ctrl_l"],
             w_torque=_writers["torque_l"]),
        dict(cfg=cfg_r, r_state=_readers["right"], w_ctrl=_writers["ctrl_r"],
             w_torque=_writers["torque_r"]),
    ])
    print("[bracketbot_adapter] homing complete — connected + homed, Ctrl-C = ESTOP")


def home(rate_hz=200.0):
    """Ramp both arms from their current pose straight to ``cfg.home`` over
    HOME_DURATION seconds, keeping torque ON — matches the 'b'/left-stick home in
    quest_teleop.py. No IK, no torque toggle, no parking: it just goes to the
    position. Blocks on the calling (control-loop) thread. No-op if not connected."""
    if "left" not in _readers:
        return
    print("[bracketbot_adapter] halt — ramping to home (torque stays on)...", flush=True)
    for r in (_readers["left"], _readers["right"]):
        while not r.ready():
            pass
    start_l = cfg_l.q2urdf(np.array(_readers["left"].data["pos"], dtype=np.float64))
    start_r = cfg_r.q2urdf(np.array(_readers["right"].data["pos"], dtype=np.float64))
    home_l = cfg_l.q2urdf(np.asarray(cfg_l.home, dtype=np.float64).copy())
    home_r = cfg_r.q2urdf(np.asarray(cfg_r.home, dtype=np.float64).copy())
    wl, wr = _writers["ctrl_l"], _writers["ctrl_r"]
    saved = (wl._keeptime, wr._keeptime)
    wl._keeptime = wr._keeptime = False
    dt = 1.0 / rate_hz
    try:
        t0 = time.monotonic()
        while True:
            alpha = min((time.monotonic() - t0) / HOME_DURATION, 1.0)
            ql = cfg_l.urdf2q(start_l + alpha * (home_l - start_l)).astype(np.float32)
            qr = cfg_r.urdf2q(start_r + alpha * (home_r - start_r)).astype(np.float32)
            with wl.buf() as b:
                b["pos"][:] = ql
                b["tau"][:] = np.zeros(cfg_l.dof, dtype=np.float32)
                b["alpha"] = 0.0
            with wr.buf() as b:
                b["pos"][:] = qr
                b["tau"][:] = np.zeros(cfg_r.dof, dtype=np.float32)
                b["alpha"] = 0.0
            if alpha >= 1.0:
                break
            time.sleep(dt)
    finally:
        wl._keeptime, wr._keeptime = saved
    print("[bracketbot_adapter] at home — holding (torque on)", flush=True)


def disconnect():
    global _shutdown_stage
    # Normal end: park gracefully and power off
    if _shutdown_stage == 0:
        _shutdown_stage = 1
        _park_and_off()
    else:
        for key in ("torque_l", "torque_r"):
            if key in _writers:
                _writers[key]["enable"] = np.zeros(cfg_l.dof, dtype=np.bool_)
    for r in _readers.values():
        r.__exit__(None, None, None)
    for w in _writers.values():
        w.__exit__(None, None, None)
    _readers.clear()
    _writers.clear()
    print("[bracketbot_adapter] disconnected")


def _raw_jpeg(data):
    return bytes(data["jpeg"][:int(data["jpeg_len"])])


def get_observation():
    for r in _readers.values():
        r.ready()

    left_raw = _readers["left"].data["pos"]
    right_raw = _readers["right"].data["pos"]
    left_norm = normalize_pos(left_raw, cal_l_min, cal_l_max)
    right_norm = normalize_pos(right_raw, cal_r_min, cal_r_max)

    obs = {}
    for i, name in enumerate(cfg_l.joint_names):
        obs[name] = float(left_norm[i])
    for i, name in enumerate(cfg_r.joint_names):
        obs[name] = float(right_norm[i])

    obs["arm_left"]  = _raw_jpeg(_readers["cam_left"].data)
    obs["arm_right"] = _raw_jpeg(_readers["cam_right"].data)
    obs["head"]      = _raw_jpeg(_readers["cam_head"].data)

    return obs


STEP_MODE = os.environ.get("STEP_MODE", "0") == "1"


def send_action(action):
    norm_l = np.clip([action[name] for name in cfg_l.joint_names], -100, 100).astype(np.float32)
    norm_r = np.clip([action[name] for name in cfg_r.joint_names], -100, 100).astype(np.float32)
    pos_l = unnormalize_pos(norm_l, cal_l_min, cal_l_max)
    pos_r = unnormalize_pos(norm_r, cal_r_min, cal_r_max)
    # Per-step action/state visualization now lives in robot_client.py
    # (DEBUG_PRINT), where the whole chunk is available.
    if STEP_MODE:
        input("[step] press Enter to execute (Ctrl-C to ESTOP)...")
    if _writers["ctrl_l"].ready():
        _writers["ctrl_l"]["pos"] = pos_l
    if _writers["ctrl_r"].ready():
        _writers["ctrl_r"]["pos"] = pos_r
    return action

"""How the arms follow your hands: reference frame, disconnect guard, clutch, speed caps.

The pipeline the loop applies per frame, in order: put the raw controller pose in a frame the
robot understands (head plane, or the no-headset anchor), drop glitched samples, scale motion
while a grip is squeezed, then clamp and speed-cap it. Out comes an IK goal.
"""
import numpy as np

from bbos import Config

from .quat import quat_forward_z, quat_mul, slerp

CFG_L = Config("arm_left")
CFG_R = Config("arm_right")
CFG_Q = Config("quest")

ROBOT_REF = float(CFG_Q.robot_shoulder_height)  # robot shoulder: the plane the head maps onto
HANDOFF_OFFSET = 0.08           # shift the goal along the gripper axis, so the arms can cross
POSE_JUMP_MAX_M = 1.0           # a bigger one-tick jump is a dropped controller, not a move
ANCHOR_MIN_LEVEL = 0.34         # min |XY| of the gripper axis at capture (~70 deg off vertical)
PRECISION_SCALE = 0.3           # goal motion per unit of hand motion while a grip is held
PRECISION_SNAP_TAU = 0.35       # s, glide back to 1:1 on release

# Slow mode clamps the goal into an annular shell around the base and speed-caps it.
CYL_CX = 0.0
CYL_CY = 0.0
CYL_R_INNER = 0.1155
CYL_R_OUTER = 0.3751
CYL_HEIGHT = 1.50
CYL_TRIM_BOTTOM = 0.50          # -> z band [0.50, 1.30]
CYL_TRIM_TOP = 0.20
SLOW_MAX_SPEED = 0.15           # m/s, a cap on the end effector, NOT a scaling
SLOW_MAX_ANG_SPEED = 90.0       # deg/s


def height_remap(pose, ref_h):
    """Keep XY, map the head plane `ref_h` onto the robot shoulder."""
    p = np.asarray(pose[:3], dtype=np.float64).copy()
    p[2] = p[2] - ref_h + ROBOT_REF
    return p


def ground_frame(T_head):
    """The daemon's head ground frame: yaw from the head's forward axis, head XY, z = 0."""
    T_head = np.asarray(T_head, dtype=np.float64)
    f = T_head[:2, 2]
    n = float(np.linalg.norm(f))
    f = f / n if n > 1e-6 else np.array([0.0, 1.0])
    T = np.eye(4)
    T[0, 0], T[1, 0], T[0, 1], T[1, 1] = f[0], f[1], -f[1], f[0]
    T[:2, 3] = T_head[:2, 3]
    return T


def to_global(pose, T_head):
    """Head-recentred pose -> global frame. T_head ships in the same buffer write as the
    poses, so head jitter cancels exactly."""
    T = ground_frame(T_head)
    pos = T[:3, :3] @ np.asarray(pose[:3], dtype=np.float64) + T[:3, 3]
    half = 0.5 * np.arctan2(T[1, 0], T[0, 0])
    q = quat_mul(np.array([0.0, 0.0, np.sin(half), np.cos(half)]),
                  np.asarray(pose[3:], dtype=np.float64))
    return np.concatenate([pos, q])


def ee_anchor(cfg, q_urdf):
    """EE for a pose in URDF radians, HANDOFF_OFFSET backed out: the loop re-adds it to every
    goal, so anchoring on the raw EE would walk the arms forward 8 cm per engage."""
    pos, quat = cfg.ik.fk(list(np.asarray(q_urdf, dtype=np.float64)[:7]))
    return (np.asarray(pos, dtype=np.float64)
            - HANDOFF_OFFSET * quat_forward_z(np.asarray(quat, dtype=np.float64)))


def ground_bearing(quat):
    """Ground bearing (rad) of an xyzw quaternion's gripper (body-Z) axis."""
    f = quat_forward_z(quat)
    return float(np.arctan2(f[1], f[0]))


def capture_anchor(g_left, g_right, ee_left, ee_right):
    """Freeze the user's frame: yaw = circular mean of both ground bearings, each hand pinned
    to its EE. None if a gripper axis is too near vertical, where its bearing is noise."""
    fl, fr = quat_forward_z(g_left[3:]), quat_forward_z(g_right[3:])
    if min(float(np.hypot(fl[0], fl[1])), float(np.hypot(fr[0], fr[1]))) < ANCHOR_MIN_LEVEL:
        return None
    yl, yr = ground_bearing(g_left[3:]), ground_bearing(g_right[3:])
    return {
        "yaw": float(np.arctan2(np.sin(yl) + np.sin(yr), np.cos(yl) + np.cos(yr))),
        "p0_L": np.asarray(g_left[:3], dtype=np.float64).copy(),
        "p0_R": np.asarray(g_right[:3], dtype=np.float64).copy(),
        "ee_L": np.asarray(ee_left, dtype=np.float64),
        "ee_R": np.asarray(ee_right, dtype=np.float64),
        "z": 0.5 * (float(g_left[2]) + float(g_right[2])),
    }


def anchor_apply(anchor, g, side):
    """Global pose -> goal: rotate the motion since the anchor out of the user's yaw, add it
    to the anchored EE."""
    c, s = np.cos(anchor["yaw"]), np.sin(anchor["yaw"])
    d = np.asarray(g[:3], dtype=np.float64) - anchor["p0_" + side]
    d = np.array([c * d[0] + s * d[1], -s * d[0] + c * d[1], d[2]])   # rotate by -yaw
    q_inv = np.array([0.0, 0.0, -np.sin(0.5 * anchor["yaw"]), np.cos(0.5 * anchor["yaw"])])
    return anchor["ee_" + side] + d, quat_mul(q_inv, np.asarray(g[3:], dtype=np.float64))


def new_pose_gate():
    return {"good": None, "holding": False}


def pose_gate(st, pose, engaged, label):
    """Hold the last good pose when a sample jumps or goes non-finite. Only
    while ENGAGED; disengaged, every finite sample passes and refreshes the
    baseline, so the pre-tracking placeholder never latches."""
    if np.all(np.isfinite(pose)) and (not engaged or st["good"] is None
            or np.linalg.norm(pose[:3] - st["good"][:3]) < POSE_JUMP_MAX_M):
        if st["holding"]:
            print(f"[pose] {label} controller recovered - tracking resumes.",
                  flush=True)
        # copy: never alias the reader buffer
        st["good"], st["holding"] = pose.copy(), False
        return pose
    if st["good"] is None:
        return pose                       # nothing good yet
    if not st["holding"]:
        st["holding"] = True
        print(f"[pose] {label} controller jumped >{POSE_JUMP_MAX_M:.0f} m in "
              f"one tick (disconnect?) - holding last good pose.", flush=True)
    return st["good"]


def new_precision_state():
    # off: target offset from the raw 1:1 controller position (the "carried error").
    return {"off": np.zeros(3), "prev_raw": None, "prev_grip": False}


def precision_reset(st):
    st["off"][:] = 0.0; st["prev_raw"] = None; st["prev_grip"] = False


def precision_step(st, raw, grip_held, scale, ease):
    """Advance the per-hand clutch and return the goal position. Holding the grip moves the
    goal `scale` x the hand and builds an offset; releasing eases that offset to zero, so no
    error is ever carried outside precision mode."""
    if grip_held:
        if st["prev_grip"]:
            st["off"] += (scale - 1.0) * (raw - st["prev_raw"])
        st["prev_raw"] = raw.copy()
    else:
        st["off"] *= (1.0 - ease)                            # smooth glide back to zero-error 1:1
        if np.linalg.norm(st["off"]) < 0.002:
            st["off"][:] = 0.0
    st["prev_grip"] = grip_held
    return raw + st["off"]


def home_ik():
    """Reset both solvers' warm-start memory to home."""
    CFG_L.ik.reset(list(CFG_L.q2urdf(np.asarray(CFG_L.home, dtype=np.float64))[:7]))
    CFG_R.ik.reset(list(CFG_R.q2urdf(np.asarray(CFG_R.home, dtype=np.float64))[:7]))
    print("IK memory reset to HOME", flush=True)


def cyl_bounds():
    """(cx, cy, r_inner, r_outer, z_min, z_max) for the slow-mode shell."""
    z_min = CYL_TRIM_BOTTOM
    z_max = max(CYL_HEIGHT - CYL_TRIM_TOP, z_min)
    return (CYL_CX, CYL_CY, CYL_R_INNER, CYL_R_OUTER, z_min, z_max)


def constrain_to_cylinders(pos, side=None):
    """Clamp a target position into the annular shell: radius -> [r_in, r_out], z -> [z_min, z_max].
    If `side` is 'left'/'right', also restrict to that HALF of the ring, split by the forward (x)
    axis, so the left arm gets the +y half and the right the -y half and they cannot cross."""
    cx, cy, r_in, r_out, z_min, z_max = cyl_bounds()
    dx, dy = float(pos[0]) - cx, float(pos[1]) - cy
    if side == "left":
        dy = max(dy, 0.0)      # left arm confined to the +y (left) half-ring
    elif side == "right":
        dy = min(dy, 0.0)      # right arm confined to the -y (right) half-ring
    r = np.hypot(dx, dy)
    theta = np.arctan2(dy, dx)
    r_c = min(max(r, r_in), r_out)
    return np.array([cx + r_c * np.cos(theta), cy + r_c * np.sin(theta),
                     min(max(float(pos[2]), z_min), z_max)])


def rate_limit(cur, target, max_step):
    """Step `cur` toward `target` by at most `max_step`; snap when within reach."""
    target = np.asarray(target, dtype=np.float64)
    if cur is None:
        return target.copy()
    delta = target - cur
    dist = float(np.linalg.norm(delta))
    if dist <= max_step or dist < 1e-9:
        return target.copy()
    return cur + delta * (max_step / dist)


def slerp_limit(cur, target, max_angle):
    """Rotate `cur` toward `target` by at most `max_angle` rad of true 3-D rotation."""
    target = np.asarray(target, dtype=np.float64)
    target = target / np.linalg.norm(target)
    if cur is None:
        return target.copy()
    cur = cur / np.linalg.norm(cur)
    d = float(np.dot(cur, target))
    if d < 0.0:
        target, d = -target, -d
    full_rot = 2.0 * np.arccos(min(max(d, -1.0), 1.0))
    if full_rot <= max_angle or full_rot < 1e-6:
        return target.copy()
    return slerp(cur, target, max_angle / full_rot)

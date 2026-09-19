# /// script
# requires-python = "==3.10.*"
# dependencies = ["bbos", "viser", "yourdfpy", "numpy<2"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Quest visualizer (viser) with IK overlay.

Reads the daemon's head-relative poses (quest.controllers) and shows three balls per hand:
  GREEN = raw world hand — the head-relative pose inverted back into room coordinates
  CYAN  = the daemon pose itself (head-relative XY, real/absolute height)
  RED   = height-normalized IK target (head plane -> robot shoulder height)
  WHITE = your head.  Flat grid at robot height = "your head plane, mapped to the robot".

Hold the left thumbstick 1s to lock the height plane; X toggles IK.
Run: uv run ~/bbapps/examples/view_quest.py
"""
import time
import numpy as np
import trimesh
import viser
from viser.extras import ViserUrdf
import yourdfpy
from bbos import Reader, Writer, Type, Config

def _quat_wxyz(R):
    t = np.trace(R)
    if t > 0:
        s = 0.5 / np.sqrt(t + 1.0); w = 0.25 / s
        x = (R[2, 1] - R[1, 2]) * s; y = (R[0, 2] - R[2, 0]) * s; z = (R[1, 0] - R[0, 1]) * s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = 2.0 * np.sqrt(1 + R[0, 0] - R[1, 1] - R[2, 2]); w = (R[2, 1] - R[1, 2]) / s
        x = 0.25 * s; y = (R[0, 1] + R[1, 0]) / s; z = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = 2.0 * np.sqrt(1 + R[1, 1] - R[0, 0] - R[2, 2]); w = (R[0, 2] - R[2, 0]) / s
        x = (R[0, 1] + R[1, 0]) / s; y = 0.25 * s; z = (R[1, 2] + R[2, 1]) / s
    else:
        s = 2.0 * np.sqrt(1 + R[2, 2] - R[0, 0] - R[1, 1]); w = (R[1, 0] - R[0, 1]) / s
        x = (R[0, 2] + R[2, 0]) / s; y = (R[1, 2] + R[2, 1]) / s; z = 0.25 * s
    return (w, x, y, z)


def to_world(pose_pos, T_head):
    """Invert the daemon's head re-center: head-relative pose -> world (robot-axes) position."""
    yx = T_head[:2, 2]; n = np.linalg.norm(yx); yx = yx / n if n > 1e-6 else np.array([0., 1.])  # col 2 = head FORWARD (col 1 is UP -> degenerate)
    R = np.array([[yx[0], -yx[1]], [yx[1], yx[0]]])      # T_ground rotation (head yaw)
    xy = R @ np.asarray(pose_pos[:2], dtype=np.float64) + np.asarray(T_head[:2, 3], dtype=np.float64)
    return np.array([xy[0], xy[1], float(pose_pos[2])])


configs = {n: Config(n) for n in ("arm_left", "arm_right")}
qcfg = Config("quest")
ROBOT_REF_DEFAULT = float(qcfg.robot_shoulder_height)   # robot reference height (by definition)

# ============================================================================
# MODE TOGGLE  —  'normal' (default) vs 'slow'
# ============================================================================
# Slow mode is toggled live with the "slow mode" checkbox in viser (no flag).
# When on, every IK target is clamped into the annular region between two concentric,
# vertical (z-axis) cylinders — cylindrical coords (r, theta, z):
#   radius  r  is clamped to [CYL_R_INNER, CYL_R_OUTER]   (theta is left free)
#   height  z  is clamped to [CYL_TRIM_BOTTOM, CYL_HEIGHT - CYL_TRIM_TOP]
# The shared axis is vertical, passing through (CYL_CX, CYL_CY) — the origin by default,
# straight through the robot base (x = forward, y = left). The shell spans the nominal
# CYL_HEIGHT minus a trim off the top and bottom, so users can't reach too high or low
# (defaults: 1.5 m height, 0.50 m off the bottom, 0.20 m off the top -> an 80 cm band from
# 0.50 to 1.30 m). Slow mode ALSO caps how fast the IK target may move (SLOW_MAX_SPEED):
# the target chases the live hand at up to that speed — 1:1 when you move slowly, trailing
# and catching up when you move fast (a speed limit, NOT a precision-style scaling). Tune it
# all live in the viser "slow cylinders" panel.
SLOW = False           # checkbox default; toggle at runtime in viser
CYL_CX = 0.00              # cylinder axis: forward offset (m) — origin, like the robot
CYL_CY = 0.00              # cylinder axis: left offset (m) — origin, like the robot
CYL_R_INNER = 0.1155       # inner cylinder radius (m)
CYL_R_OUTER = 0.3751       # outer cylinder radius (m)
CYL_HEIGHT = 1.50          # nominal full height (m) of the region
CYL_TRIM_BOTTOM = 0.50     # trim off the bottom (m) -> shell z starts here
CYL_TRIM_TOP = 0.20        # trim off the top (m) -> shell z ends at (height - this)
SLOW_MAX_SPEED = 0.15       # slow end-effector LINEAR speed cap (m/s) — target chases the hand at up to this
SLOW_MAX_ANG_SPEED = 90.0   # slow end-effector ANGULAR speed cap (deg/s) — orientation slerps toward the hand at up to this

print(f"Loading robot URDF: {configs['arm_left'].urdf_path}", flush=True)
urdf = yourdfpy.URDF.load(configs["arm_left"].urdf_path, load_meshes=True, load_collision_meshes=False,
                          build_scene_graph=True, build_collision_scene_graph=False)
all_joints = list(urdf.joint_map.keys())

# IK setup (mirrors view_ik.py): init the solvers, and build each arm's kinematic chain
# so the solver output (URDF-space joints) maps to the right joint names — NO q2urdf.
for n in ("arm_left", "arm_right"):
    configs[n].ik.init()


def chain_for(cfg):
    jd = {j.name: (j.parent, j.child) for j in urdf.robot.joints
          if j.type in ("revolute", "prismatic", "continuous") and j.name in cfg.joint_names}
    roots = {p for p, _ in jd.values()} - {c for _, c in jd.values()}
    cur = list(roots)[0] if roots else "main_extrusion_eu4040"
    chain = []
    while True:
        nxt = next((jn for jn, (p, c) in jd.items() if p == cur and jn not in chain), None)
        if nxt is None:
            break
        chain.append(nxt); cur = jd[nxt][1]
    return chain


chains = {n: chain_for(configs[n]) for n in ("arm_left", "arm_right")}

# Home config in the solver's space (URDF radians, chain order, first 7) for ik.reset().
home_reset = {}
home_cfg = {}                          # full per-joint home pose (URDF space) for the homing animation
for _n in ("arm_left", "arm_right"):
    _hu = configs[_n].q2urdf(np.asarray(configs[_n].home, dtype=np.float64))
    _jn = list(configs[_n].joint_names)
    home_reset[_n] = [float(_hu[_jn.index(j)]) for j in chains[_n][:7]]
    for _i, _j in enumerate(_jn):
        home_cfg[_j] = float(_hu[_i])
HOME_DURATION = 2.5


def home_ik():
    for n in ("arm_left", "arm_right"):
        configs[n].ik.reset(home_reset[n])
    print("[view_quest] IK memory reset to HOME", flush=True)


def pad(r, n):
    if r is None:
        return None
    r = np.asarray(r, dtype=np.float64)
    return np.concatenate([r, np.zeros(max(0, n - len(r)))]) if len(r) < n else r


server = viser.ViserServer()
viser_urdf = ViserUrdf(server, urdf_or_path=urdf, root_node_name="/robot")
server.scene.add_grid("/grid", width=3.0, height=3.0, cell_size=0.1, position=(0.0, 0.0, 0.0))
server.scene.add_frame("/world", axes_length=0.5, axes_radius=0.012)   # RED +x fwd, GREEN +y left, BLUE +z up
server.scene.add_label("/world_x", "+x fwd", position=(0.55, 0.0, 0.0))
server.scene.add_label("/world_y", "+y left", position=(0.0, 0.55, 0.0))
server.scene.add_label("/world_z", "+z up", position=(0.0, 0.0, 0.55))

# "head plane mapped to robot height": a small flat grid sitting at robot_ref, centered on the robot.
robot_plane = server.scene.add_grid("/robot_plane", width=1.2, height=1.2, cell_size=0.1,
                                    position=(0.0, 0.0, ROBOT_REF_DEFAULT),
                                    section_color=(120, 120, 255), cell_color=(180, 180, 255))
server.scene.add_label("/robot_plane_l", "head plane @ robot height", position=(0.0, 0.7, ROBOT_REF_DEFAULT))
# Frozen head plane (world z = captured head height). Shown only once a plane is locked.
locked_plane = server.scene.add_grid("/locked_plane", width=1.2, height=1.2, cell_size=0.1,
                                     position=(0.0, 0.0, ROBOT_REF_DEFAULT),
                                     section_color=(80, 200, 80), cell_color=(150, 220, 150))
locked_plane.visible = False
locked_plane_l = server.scene.add_label("/locked_plane_l", "locked head plane", position=(0.0, -0.7, ROBOT_REF_DEFAULT))
locked_plane_l.visible = False

head_m = server.scene.add_icosphere("/head", radius=0.06, color=(235, 235, 235))
head_f = server.scene.add_frame("/head_f", axes_length=0.15, axes_radius=0.006)
head_lbl = server.scene.add_label("/head_l", "head")

# Per controller: green raw -> cyan step1 (re-centered, real height) -> red step2 (height-normalized).
left_raw = server.scene.add_icosphere("/left_raw", radius=0.035, color=(60, 220, 60))
right_raw = server.scene.add_icosphere("/right_raw", radius=0.035, color=(60, 220, 60))
left_mid = server.scene.add_icosphere("/left_mid", radius=0.035, color=(40, 210, 220))
right_mid = server.scene.add_icosphere("/right_mid", radius=0.035, color=(40, 210, 220))
left_tgt = server.scene.add_icosphere("/left_tgt", radius=0.04, color=(230, 50, 50))
right_tgt = server.scene.add_icosphere("/right_tgt", radius=0.04, color=(230, 50, 50))
lbl = {
    "left_raw": server.scene.add_label("/left_raw_l", "L raw"),
    "left_mid": server.scene.add_label("/left_mid_l", "L recenter"),
    "left_tgt": server.scene.add_label("/left_tgt_l", "L target"),
    "right_raw": server.scene.add_label("/right_raw_l", "R raw"),
    "right_mid": server.scene.add_label("/right_mid_l", "R recenter"),
    "right_tgt": server.scene.add_label("/right_tgt_l", "R target"),
}

gui_robot_ref = server.gui.add_number("robot ref height (m)", initial_value=round(ROBOT_REF_DEFAULT, 3),
                                      min=0.5, max=2.0, step=0.01)
gui_headh = server.gui.add_text("head height (live, m)", initial_value="--", disabled=True)
HOLD_SEC = 1.0
TRIG_TH = 0.5      # trigger press threshold (0..1) for the slow-toggle combo
TOGGLE_HOLD = 2.0  # seconds to hold (both gripper triggers + B) to toggle slow mode
ref_state = {"h": None, "mode": "auto"}   # mode: auto (default, grabbed on first X/IK enable) | locked (held) | live
gui_lock = server.gui.add_text("height plane", initial_value="press X to set height plane", disabled=True)
ik_state = {"on": False}
gui_ik = server.gui.add_text("IK (press X to toggle)", initial_value="OFF", disabled=True)
gui_home = server.gui.add_button("reset IK to home")


@gui_home.on_click
def _on_home(_):
    home_ik()


gui_unlock = server.gui.add_button("use live head (dynamic)")


@gui_unlock.on_click
def _on_unlock(_):
    ref_state["mode"] = "live"; ref_state["h"] = None


gui_L = server.gui.add_markdown("**Left:** waiting...")
gui_R = server.gui.add_markdown("**Right:** waiting...")

# --- Slow mode: two concentric vertical cylinders, toggled + tuned live in viser ----------
# Tick "slow mode" to clamp the IK target into the shell between them (see the top of file).
cyl_gui = {}
cyl_handles = []
gui_slow = server.gui.add_checkbox("slow mode", initial_value=SLOW)
with server.gui.add_folder("slow cylinders"):
    cyl_gui["cx"] = server.gui.add_number("center x (fwd, m)", initial_value=CYL_CX, min=-0.5, max=1.0, step=0.01)
    cyl_gui["cy"] = server.gui.add_number("center y (left, m)", initial_value=CYL_CY, min=-1.0, max=1.0, step=0.01)
    cyl_gui["r_in"] = server.gui.add_number("inner radius (m)", initial_value=CYL_R_INNER, min=0.0, max=1.0, step=0.005)
    cyl_gui["r_out"] = server.gui.add_number("outer radius (m)", initial_value=CYL_R_OUTER, min=0.0, max=1.5, step=0.005)
    cyl_gui["height"] = server.gui.add_number("height (m)", initial_value=CYL_HEIGHT, min=0.05, max=2.5, step=0.01)
    cyl_gui["trim_bot"] = server.gui.add_number("trim bottom (m)", initial_value=CYL_TRIM_BOTTOM, min=0.0, max=2.0, step=0.01)
    cyl_gui["trim_top"] = server.gui.add_number("trim top (m)", initial_value=CYL_TRIM_TOP, min=0.0, max=2.0, step=0.01)
    cyl_gui["max_speed"] = server.gui.add_number("max speed (m/s)", initial_value=SLOW_MAX_SPEED, min=0.02, max=1.0, step=0.01)
    cyl_gui["max_ang_speed"] = server.gui.add_number("max ang speed (deg/s)", initial_value=SLOW_MAX_ANG_SPEED, min=5.0, max=720.0, step=5.0)


def cyl_bounds():
    """Live cylinder params: (cx, cy, r_inner, r_outer, z_min, z_max). The shell spans `height`
    minus a trim off the bottom and top: z in [trim_bottom, height - trim_top]. Defaults give an
    80 cm band (1.5 m height, 0.50 m off the bottom, 0.20 m off the top -> [0.50, 1.30])."""
    h = float(cyl_gui["height"].value)
    z_min = float(cyl_gui["trim_bot"].value)
    z_max = max(h - float(cyl_gui["trim_top"].value), z_min)
    return (float(cyl_gui["cx"].value), float(cyl_gui["cy"].value),
            float(cyl_gui["r_in"].value), float(cyl_gui["r_out"].value), z_min, z_max)


def constrain_to_cylinders(pos, side=None):
    """Clamp a target position into the annular shell between the two concentric vertical
    cylinders: radius -> [r_in, r_out], height -> [z_min, z_max]. If `side` is 'left'/'right',
    also restrict to that HALF of the ring, split by the forward (x) axis: the left arm gets the
    +y (left) half, the right arm gets the -y (right) half — so the two arms can't cross."""
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
    """Step `cur` toward `target` by at most `max_step` (a speed cap, NOT a scaling): if the
    target is within reach this tick, snap to it (1:1 tracking); otherwise advance along the
    line to it at the capped step. `cur` None -> initialize on the target."""
    target = np.asarray(target, dtype=np.float64)
    if cur is None:
        return target.copy()
    delta = target - cur
    dist = float(np.linalg.norm(delta))
    if dist <= max_step or dist < 1e-9:
        return target.copy()
    return cur + delta * (max_step / dist)


def _slerp(q0, q1, s):
    """Spherical interpolation between xyzw quaternions along the shortest arc (fraction s in [0,1])."""
    q0 = q0 / np.linalg.norm(q0)
    q1 = q1 / np.linalg.norm(q1)
    d = float(np.dot(q0, q1))
    if d < 0.0:                     # take the short way around (q and -q are the same rotation)
        q1, d = -q1, -d
    if d > 0.9995:                  # nearly identical -> linear blend + renormalize
        out = q0 + s * (q1 - q0)
        return out / np.linalg.norm(out)
    th = np.arccos(np.clip(d, -1.0, 1.0))
    return (np.sin((1.0 - s) * th) * q0 + np.sin(s * th) * q1) / np.sin(th)


def slerp_limit(cur, target, max_angle):
    """Rotate `cur` toward `target` (xyzw quats) by at most `max_angle` radians of true 3-D rotation
    (an angular speed cap): if the target is within reach this tick, snap to it (1:1 tracking);
    otherwise slerp partway. `cur` None -> initialize on the target."""
    target = np.asarray(target, dtype=np.float64)
    target = target / np.linalg.norm(target)
    if cur is None:
        return target.copy()
    cur = cur / np.linalg.norm(cur)
    d = float(np.dot(cur, target))
    if d < 0.0:
        target, d = -target, -d
    full_rot = 2.0 * np.arccos(min(max(d, -1.0), 1.0))   # 3-D rotation angle between the two orientations
    if full_rot <= max_angle or full_rot < 1e-6:
        return target.copy()
    return _slerp(cur, target, max_angle / full_rot)


def rebuild_cylinders(*_):
    """(Re)draw the two wireframe cylinders from the live GUI params (only while slow is on)."""
    for h in cyl_handles:
        h.remove()
    cyl_handles.clear()
    if not gui_slow.value:
        return
    cx, cy, r_in, r_out, z_min, z_max = cyl_bounds()
    for r, color, nm in ((r_in, (230, 120, 120), "inner"), (r_out, (120, 160, 230), "outer")):
        if r <= 1e-4:
            continue
        m = trimesh.creation.cylinder(radius=r, height=max(z_max - z_min, 1e-3), sections=48)
        m.apply_translation([cx, cy, 0.5 * (z_min + z_max)])
        cyl_handles.append(server.scene.add_mesh_simple(
            f"/cyl_{nm}", m.vertices, m.faces, color=color, wireframe=True, opacity=0.6))
    # Divider plane on the forward (x) axis: left arm keeps +y, right arm keeps -y.
    d = trimesh.creation.box(extents=[2.0 * r_out, 0.004, max(z_max - z_min, 1e-3)])
    d.apply_translation([cx, cy, 0.5 * (z_min + z_max)])
    cyl_handles.append(server.scene.add_mesh_simple(
        "/cyl_divider", d.vertices, d.faces, color=(210, 200, 120), opacity=0.25))


for g in cyl_gui.values():
    g.on_update(rebuild_cylinders)
gui_slow.on_update(rebuild_cylinders)
rebuild_cylinders()

# Pure simulation: the arm is driven ENTIRELY by this app, never by the real robot state.
# It starts at home (t=0) and only moves when IK is on; toggling IK off (X) freezes it in
# place (the loop stops touching joint_cfg), and the B button animates home.
joint_cfg = {n: home_cfg.get(n, 0.0) for n in all_joints}
# Haptic feedback patterns (written to quest.haptic; the daemon buzzes the headset).
HAPTIC_SHORT = {"freq": 160.0, "amp": 0.85, "dur": 0.15}
HAPTIC_LONG = {"freq": 160.0, "amp": 0.85, "dur": 0.45}
HAPTIC_GAP = 0.16   # seconds between pulses within a pattern
print("[view_quest] GREEN=raw  CYAN=recentered  RED=target. Open the viser URL.", flush=True)

with Reader("quest.controllers", Type("quest_controllers")) as r_ctrl, \
     Writer("quest.haptic", Type("quest_haptic"), keeptime=False) as w_hap:
    hold_start = None; hold_captured = False; prev_la = False
    homing = False; homed = True; home_t = 0.0; home_start_cfg = {}   # start held at home
    episode_active = False; prev_ra = False; prev_rb = False; haptic_queue = []
    cmd_pos = {"left": None, "right": None}   # slow speed-cap: rate-limited target position per hand
    cmd_quat = {"left": None, "right": None}  # slow speed-cap: rate-limited target orientation per hand
    last_rl_t = None                          # for measuring dt of the rate limiter
    combo_start = None; b_consumed = False    # slow-toggle combo (2 triggers + B held 2s)
    while True:
        if r_ctrl.ready():
            now = time.time()
            dt = min(now - last_rl_t, 0.1) if last_rl_t is not None else 0.02
            last_rl_t = now
            T_head = np.asarray(r_ctrl.data["T_head"], dtype=np.float64)
            head_h = float(T_head[2, 3])
            hp = tuple(T_head[:3, 3]); hq = _quat_wxyz(T_head[:3, :3])
            head_m.position = hp; head_f.position = hp; head_f.wxyz = hq
            head_lbl.position = (hp[0], hp[1], hp[2] + 0.12)

            # Right A -> episode-feedback haptics (mirrors quest_teleop, but no recording — just buzzes):
            # start = 1 buzz, stop = long + short.  Right B -> home / slow-toggle combo (below).
            ra = bool(r_ctrl.data["right_a"]); rb = bool(r_ctrl.data["right_b"])
            if ra and not prev_ra:
                episode_active = not episode_active
                t = time.time()
                if episode_active:
                    haptic_queue.append((t, HAPTIC_SHORT))
                    print("[view_quest] episode START -> 1 buzz", flush=True)
                else:
                    haptic_queue += [(t, HAPTIC_LONG), (t + HAPTIC_GAP, HAPTIC_SHORT)]
                    print("[view_quest] episode STOP -> long + short", flush=True)
            # Right B: quick tap -> home.  Both gripper triggers + B held 2s -> toggle slow mode.
            lt = float(r_ctrl.data["left_trigger"]); rt = float(r_ctrl.data["right_trigger"])
            combo = rb and lt > TRIG_TH and rt > TRIG_TH
            if rb and not prev_rb:
                b_consumed = False                     # fresh B press
            if combo:
                if combo_start is None:
                    combo_start = now
                if not b_consumed and now - combo_start >= TOGGLE_HOLD:
                    gui_slow.value = not gui_slow.value   # flip slow <-> normal
                    rebuild_cylinders()                           # show/hide the shell
                    # Home on every mode switch so the constraint change isn't an immediate jerk:
                    # reset IK to home, pause IK, animate the arms home; re-enable with X to track.
                    home_ik()
                    ik_state["on"] = False; gui_ik.value = "OFF"
                    homing = True; homed = False; home_t = now; home_start_cfg = dict(joint_cfg)
                    cmd_pos["left"] = cmd_pos["right"] = None      # re-sync speed-cap state fresh
                    cmd_quat["left"] = cmd_quat["right"] = None
                    b_consumed = True                             # combo fired -> don't home on release
                    haptic_queue.append((time.time(), HAPTIC_LONG))
                    print(f"[view_quest] SLOW MODE {'ON' if gui_slow.value else 'OFF'} + HOME "
                          f"(2 triggers + B held {TOGGLE_HOLD:.0f}s)", flush=True)
            else:
                combo_start = None
            if prev_rb and not rb:                     # B released
                if not b_consumed:
                    # plain B tap -> home: reset IK to home, pause IK, animate the arms home.
                    home_ik()
                    ik_state["on"] = False; gui_ik.value = "OFF"
                    homing = True; homed = False; home_t = now; home_start_cfg = dict(joint_cfg)
                    print("[view_quest] HOMING to home pose (B)", flush=True)
                b_consumed = False
            prev_ra = ra; prev_rb = rb

            # Press-and-hold LEFT thumbstick-click ~1s -> lock the head plane at the current head height.
            if bool(r_ctrl.data["left_thumbstick_click"]):
                if hold_start is None:
                    hold_start = now; hold_captured = False
                if not hold_captured and now - hold_start >= HOLD_SEC:
                    ref_state["h"] = head_h; ref_state["mode"] = "locked"; hold_captured = True
                    print(f"[view_quest] height plane LOCKED @ {head_h:.3f} m", flush=True)
            else:
                hold_start = None; hold_captured = False
            la = bool(r_ctrl.data["left_a"])          # X button toggles IK on/off
            if la and not prev_la:
                ik_state["on"] = not ik_state["on"]
                gui_ik.value = "ON" if ik_state["on"] else "OFF"
                if ik_state["on"]:
                    homing = False; homed = False     # IK takes over from the home pose
                if (ik_state["on"] and ref_state["mode"] == "auto" and ref_state["h"] is None
                        and 0.4 < head_h < 2.5):
                    ref_state["h"] = head_h     # default height plane: grab head height the first time IK starts
                    print(f"[view_quest] HEIGHT PLANE SET @ {head_h:.3f} m (first IK enable)", flush=True)
                print(f"[view_quest] IK {'ON' if ik_state['on'] else 'OFF'}", flush=True)
            prev_la = la

            robot_ref = float(gui_robot_ref.value)
            robot_plane.position = (0.0, 0.0, robot_ref)
            gui_headh.value = f"{head_h:.3f}"
            locked = ref_state["mode"] in ("auto", "locked") and ref_state["h"] is not None
            ref_h = ref_state["h"] if locked else head_h          # constant when locked/auto, else live
            tag = {"auto": "AUTO", "locked": "LOCKED"}.get(ref_state["mode"], "LIVE")
            gui_lock.value = (f"{tag} @ {ref_state['h']:.3f} m" if locked
                              else f"LIVE {head_h:.3f} m - hold L-stick 1s to lock")
            locked_plane.visible = locked; locked_plane_l.visible = locked
            if locked:
                locked_plane.position = (0.0, 0.0, ref_state["h"])
                locked_plane_l.position = (0.0, -0.7, ref_state["h"])
            for raw_m, mid_m, tgt_m, gui, key in (
                    (left_raw, left_mid, left_tgt, gui_L, "left"),
                    (right_raw, right_mid, right_tgt, gui_R, "right")):
                pose = np.asarray(r_ctrl.data[f"{key}_pose"], dtype=np.float64)
                mid = pose[:3]                                          # daemon head-relative pose, real height
                raw = to_world(pose[:3], T_head)                        # inverted back to world
                quat = pose[3:].copy()                                  # xyzw hand orientation
                tgt = np.array([mid[0], mid[1], mid[2] - ref_h + robot_ref])  # frozen head plane -> robot height
                if gui_slow.value:
                    tgt = constrain_to_cylinders(tgt, key)              # WHERE it can go: this arm's half-shell
                    cmd_pos[key] = rate_limit(cmd_pos[key], tgt, float(cyl_gui["max_speed"].value) * dt)
                    tgt = cmd_pos[key]                                  # HOW FAST it gets there: linear speed cap
                    cmd_quat[key] = slerp_limit(cmd_quat[key], quat, np.radians(float(cyl_gui["max_ang_speed"].value)) * dt)
                    quat = cmd_quat[key]                                # HOW FAST it turns: angular speed cap
                else:
                    cmd_pos[key] = tgt.copy()                           # keep synced -> no jump when slow toggles on
                    cmd_quat[key] = quat.copy()
                raw_m.position = tuple(raw)
                mid_m.position = tuple(mid)
                tgt_m.position = tuple(tgt)
                lbl[f"{key}_raw"].position = (raw[0], raw[1], raw[2] + 0.07)
                lbl[f"{key}_mid"].position = (mid[0], mid[1], mid[2] + 0.07)
                lbl[f"{key}_tgt"].position = (tgt[0], tgt[1], tgt[2] + 0.07)
                tgt_label = "target (cyl)" if gui_slow.value else "target (robot)"
                gui.content = (f"**{key.capitalize()} (x=fwd, y=left, z=up):**  \n"
                               f"- raw (world)     : {np.round(raw, 3).tolist()}  \n"
                               f"- head-relative   : {np.round(mid, 3).tolist()}  \n"
                               f"- {tgt_label}  : {np.round(tgt, 3).tolist()}")
                # IK: apply solver output DIRECTLY to the URDF chain joints (no q2urdf — solve() returns URDF space).
                if ik_state["on"]:
                    arm = f"arm_{key}"
                    try:
                        sol = pad(configs[arm].ik.solve(tgt.tolist(), quat.tolist()), len(chains[arm]))
                    except Exception:
                        sol = None
                    if sol is not None:
                        for i, jn in enumerate(chains[arm]):
                            joint_cfg[jn] = sol[i]

        # Homing (B button): animate the arms to home, then hold there until X re-enables IK.
        if homing:
            a = min((time.time() - home_t) / HOME_DURATION, 1.0)
            for jn, hv in home_cfg.items():
                joint_cfg[jn] = (1.0 - a) * home_start_cfg.get(jn, hv) + a * hv
            if a >= 1.0:
                homing = False; homed = True
        elif homed:
            for jn, hv in home_cfg.items():
                joint_cfg[jn] = hv

        # Emit at most one queued haptic pulse per tick — writing several in one tick
        # would coalesce into a single daemon read (and one buzz), losing the pattern.
        if haptic_queue:
            haptic_queue.sort(key=lambda x: x[0])
            if haptic_queue[0][0] <= time.time():
                _, p = haptic_queue.pop(0)
                with w_hap.buf() as hb:
                    hb["hand"] = 2; hb["frequency"] = p["freq"]; hb["amplitude"] = p["amp"]; hb["duration"] = p["dur"]

        viser_urdf.update_cfg(np.array([joint_cfg.get(n, 0.0) for n in all_joints]))
        time.sleep(0.02)

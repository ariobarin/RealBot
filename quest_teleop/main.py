# /// script
# dependencies = [
#   "bbos",
#   "numpy<2",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Bimanual Quest teleop.

The daemon publishes head-relative poses; the only app-level step is the height remap — the head
plane (set on X, locked by holding the left stick) maps onto the robot's shoulder height.

quest.py turns each controller frame into named button events; this file executes them.
"""
import sys
import time
import signal
import argparse
import contextlib
import numpy as np
from bbos import Reader, Writer, Type

from scripts.homing import (J0_PARK_DOWN_SPEED, J0_PARK_DOWN_TURNS, PARK_STRAIGHTEN_S,
                    park_arms, park_trajectory, smoothstep, staged_home_arms,
                    straight_down_turns)
from scripts.quat import quat_forward_z
from scripts.tracking import (CFG_L, CFG_R, HANDOFF_OFFSET, PRECISION_SCALE,
                      PRECISION_SNAP_TAU, SLOW_MAX_ANG_SPEED, SLOW_MAX_SPEED,
                      capture_anchor, constrain_to_cylinders, ee_anchor, height_remap,
                      home_ik, new_pose_gate, new_precision_state, anchor_apply,
                      pose_gate, precision_reset, precision_step, rate_limit,
                      slerp_limit, to_global)
from scripts.quest import (HAPTIC_GAP, HAPTIC_GAP_LONG, HAPTIC_LONG, HAPTIC_SHORT, PURR_THRESH,
                   Quest)
from scripts.sound import Sound

# Drive speeds
SPEED_LIN = 0.5
SPEED_ANG = 2.0

# Dataset: S3 collection that recorded episodes are filed under (datasets/<prefix>/<name>/...).
DATASET_PREFIX = "quest_teleop"

# Handoff: printed the instant we're booted and waiting for the arm writers, so inference/quest.py hands
# them over right then instead of after a fixed timer. Must match READY_LINE in inference/quest.py.
READY_LINE = "QUEST_TELEOP_READY"

# Motion timing (seconds)
INTERP_DURATION = 2.5   # ramp to the live target when teleop is enabled
HOME_DURATION = 2.5     # left-stick click: direct joint-space ramp to the home pose

# Startup homing is joint-space staged torque enabling (staged_home_arms below):
# J0-J2 first, then the rest, then the waypoint spline to home.
# A left-stick click homes with a plain joint-space ramp instead (HOME_DURATION).
# These waypoints are used forward by the startup spline and in REVERSE for dehoming.
STARTUP_SEG_DURATIONS = CFG_L.startup_seg_durations
STARTUP_WAYPOINTS_L = [*CFG_L.startup_waypoints, CFG_L.home]
STARTUP_WAYPOINTS_R = [*CFG_R.startup_waypoints, CFG_R.home]

# Gripper
GRIPPER_IDX = CFG_L.dof - 1
GRIPPER_DEBUG_SEC = 0.25       # gripper diagnostic log period

# Gripper — position control (default; also the logged action target in torque mode)
GRIPPER_OPEN_POS = 0.8
GRIPPER_OPEN_POS_WIDE = 1.2    # extra-open target held by the left stick (see STICK_MOD_DEADZONE)
GRIPPER_CLOSED_POS_R = -0.15
GRIPPER_CLOSED_POS_L = -0.15   # separate per-arm value: the left/right gripper zeros can differ
# Gripper trigger shaping: two-piece linear — steep to the knee, shallow after (fine control).
GRIPPER_TRIGGER_BREAK_IN  = 0.5   # button fraction at the knee
GRIPPER_TRIGGER_BREAK_OUT = 0.7   # physical-close fraction at the knee

# Gripper — torque control (--gripper-compliance)
GRIPPER_CLOSE_TAU = 0.70       # Nm grasp torque at full trigger (~3.4A at kt=0.204); keep a held grasp < ~2.0 Nm
GRIPPER_OPEN_TAU = -0.45       # Nm torque to break the jaw open from a grasp (~2A)
GRIPPER_OPEN_HOLD = -0.10      # Nm gentle hold once already open; caps over-extension

SLOW_MODE_DEFAULT = False

# --- Left thumbstick modifier ----------------------------------------------------------------
# The RIGHT stick drives, so the LEFT stick is free as an analog modifier. It momentarily widens
# the gripper open end (GRIPPER_OPEN_POS .. GRIPPER_OPEN_POS_WIDE) in proportion to the push past
# the deadzone; dominant axis wins: UP -> both grippers, LEFT/RIGHT -> that side's alone.
STICK_MOD_DEADZONE = 0.35

DEBUG_FRAME = True             # print the per-frame IK target positions


# --- Gripper control: isolated from the arm loop ---------------------------------------------
# compliance=False: position control (trigger -> joint target). compliance=True: stays in tau/current
# mode (no per-grasp mode switch that stalls the daemon ~0.4-0.8s); trigger -> signed open/close torque.

def gripper_tau_mode(dof, compliance):
    """Per-joint tau-mode mask for the arm.torque writer (the gripper joint only, when compliant)."""
    mask = np.zeros(dof, dtype=np.bool_)
    if compliance:
        mask[GRIPPER_IDX] = True
    return mask


def _shape_trigger(t):
    """Two-piece linear trigger->close map: steep to the knee, shallow after (fine control)."""
    bx, by = GRIPPER_TRIGGER_BREAK_IN, GRIPPER_TRIGGER_BREAK_OUT
    if t <= bx:
        return by * (t / bx)
    return by + (1.0 - by) * (t - bx) / (1.0 - bx)


def stick_frac(v, deadzone=STICK_MOD_DEADZONE):
    """One thumbstick axis as a 0..1 fraction past ``deadzone`` (negatives -> 0)."""
    v = float(v)
    if v <= deadzone:
        return 0.0
    return min((v - deadzone) / (1.0 - deadzone), 1.0)


def stick_up(thumbstick, deadzone=STICK_MOD_DEADZONE):
    """How far a thumbstick is pushed UP, 0..1 (raw y is negative-up, as ``-fwdback`` assumes)."""
    return stick_frac(-float(thumbstick[1]), deadzone)


def gripper_open_pos(left_thumbstick):
    """(left, right) gripper open ends for this frame, GRIPPER_OPEN_POS .. GRIPPER_OPEN_POS_WIDE."""
    x, y = float(left_thumbstick[0]), float(left_thumbstick[1])
    span = GRIPPER_OPEN_POS_WIDE - GRIPPER_OPEN_POS
    if abs(x) > abs(y):                        # sideways -> one gripper (raw x is +right)
        wide = GRIPPER_OPEN_POS + stick_frac(abs(x)) * span
        return (wide, GRIPPER_OPEN_POS) if x < 0 else (GRIPPER_OPEN_POS, wide)
    both = GRIPPER_OPEN_POS + stick_up(left_thumbstick) * span   # up -> both
    return both, both


def gripper_command(cfg, q, tau, trigger, act_pos, closed_pos, engaged, compliance,
                    open_pos=GRIPPER_OPEN_POS):
    """Set the gripper command for one arm. engaged=False forces it open (trigger ignored).
    ``open_pos`` is the trigger-released (fully open) target."""
    # Shape the trigger: fast close early, fine control near full press.
    trig = _shape_trigger(trigger) if engaged else 0.0
    if compliance:
        # Signed torque: trigger 0 -> open, trigger 1 -> close grasp. No mode switching.
        t = GRIPPER_OPEN_TAU + trig * (GRIPPER_CLOSE_TAU - GRIPPER_OPEN_TAU)
        if t < 0 and act_pos >= open_pos:
            t = max(t, GRIPPER_OPEN_HOLD)   # gentle hold once already open; caps over-extension
        tau[GRIPPER_IDX] = cfg.gripper_sign * t
        # Daemon ignores pos for a tau-mode joint; write it only to log the action (open_pos=open .. 0=closed).
        q[GRIPPER_IDX] = open_pos + trig * (closed_pos - open_pos)
    elif engaged:
        # Plain position control; urdf2q() applies gripper_sign, so do NOT apply it here.
        q[GRIPPER_IDX] = open_pos + trig * (closed_pos - open_pos)


def gripper_debug(compliance, engaged, homing, left, right,
                  open_l=GRIPPER_OPEN_POS, open_r=GRIPPER_OPEN_POS):
    """Print the [grip] diagnostic. left/right are (trigger, tau, act_pos, current_A) tuples."""
    lt, ltau, lact, lcur = left
    rt, rtau, ract, rcur = right
    print(
        f"[grip] comp={int(compliance)} engaged={int(engaged)} homing={int(homing)} "
        f"open={open_l:.2f}/{open_r:.2f} | "
        f"L trig={lt:.2f} tau={ltau:+.3f} act={lact:+.3f} cur={lcur:+.2f}A | "
        f"R trig={rt:.2f} tau={rtau:+.3f} act={ract:+.3f} cur={rcur:+.2f}A",
        flush=True,
    )


def print_controls(dataset_name):
    """Print a boxed control reference at launch. Colors auto-disable when not a TTY."""
    tty = sys.stdout.isatty()
    BD = "\033[90m" if tty else ""    # dim border
    HD = "\033[1;36m" if tty else ""  # bold cyan headers / title
    KY = "\033[36m" if tty else ""    # cyan keys
    OF = "\033[0m" if tty else ""
    KEYW = 20

    def row(key, desc):
        return f"{key:<{KEYW}}{desc}", f"{KY}{key:<{KEYW}}{OF}{desc}"

    def styled(text, code):
        return text, f"{code}{text}{OF}"

    SEP = None
    lines = [
        styled("QUEST TELEOP  ·  bimanual", HD),
        styled(f"dataset: {dataset_name}", BD),
        SEP,
        styled("NO-HEADSET mode (both triggers 2s, then X) drives on the", BD),
        styled("controllers alone: the head frame is ignored. Hold them as", BD),
        styled("the arms are posed, grippers roughly LEVEL, and press X to", BD),
        styled("drop the anchor. Everything after is relative to it and the", BD),
        styled("arms do not move on the press. X again to release.", BD),
        SEP,
        styled("LEFT CONTROLLER", HD),
        row("X (A)", "toggle teleop play / pause  ·  no-headset: anchor"),
        row("thumbstick hold 1s", "lock height plane to head (headset mode)"),
        row("thumbstick up", f"modifier: open BOTH grippers wider ({GRIPPER_OPEN_POS} -> {GRIPPER_OPEN_POS_WIDE})"),
        row("thumbstick left/right", "same, but that side's gripper alone"),
        row("trigger", "left gripper"),
        row("Y", "dehome (descend + torque off)  ·  press again = ESTOP"),
        SEP,
        styled("RIGHT CONTROLLER", HD),
        row("A", "tap = start / stop episode  ·  hold 2s = drop"),
        row("B", "tap = home + reset IK"),
        row("both triggers 2s", "buzz = mode select, then B = SLOW  ·  X = NO-HEADSET"),
        row("grip (side)", "hold = precision mode (purr grows with drift)"),
        row("trigger", "right gripper"),
        row("thumbstick", "drive  —  fwd / back  ·  turn"),
        row("thumbstick click", "full-speed drive"),
        SEP,
        styled("TERMINAL", HD),
        row("Ctrl-C", "teardown: dehome + torque off  ·  twice = shut off now"),
    ]
    w = max(len(plain) for plain in (ln[0] for ln in lines if ln))
    print(f"{BD}╭{'─' * (w + 2)}╮{OF}")
    for ln in lines:
        if ln is SEP:
            print(f"{BD}├{'─' * (w + 2)}┤{OF}")
            continue
        plain, colored = ln
        print(f"{BD}│{OF} {colored}{' ' * (w - len(plain))} {BD}│{OF}")
    print(f"{BD}╰{'─' * (w + 2)}╯{OF}")


def episode_banner(state):
    """Compact solid ASCII block so the just-pressed episode action is readable from across
    the room without color. state: 'STARTED' (#) | 'STOPPED' (0) | 'DROPPED' (X)."""
    ch = {"STARTED": "#", "STOPPED": "0", "DROPPED": "X"}[state]
    W = 42
    label = f"  {state}"
    right = f"{time.strftime('%H:%M:%S')}  "
    inner = label + " " * max(W - 4 - len(label) - len(right), 1) + right
    bar = ch * W
    print(f"\n{bar}\n{ch * 2}{inner}{ch * 2}\n{bar}", flush=True)


@contextlib.contextmanager
def writer_wait(topic, type_, timeout=45.0, on_wait=None, **kw):
    """Like Writer(topic, type_) but WAIT (retry) while the topic still has another writer, instead of
    crashing -- lets another process hold the arms and hand them over (quest.py keeps arm.ctrl frozen
    until this process is ready to take it). Must outlast quest.py's HANDOFF_BOOT_S hold. Standalone it
    opens immediately; raises after `timeout`. on_wait (if given) fires once the first time the topic is
    found held -- i.e. we're now spinning for it -- which is exactly when it's safe for the holder to let go."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            w = Writer(topic, type_, **kw)
            break
        except RuntimeError:                 # single-writer: topic still held -> wait and retry
            if on_wait is not None:          # first miss: signal the holder we're here and waiting
                on_wait()
                on_wait = None
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    with w:
        yield w


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--gripper-compliance", action="store_true",
                        help="Keep the gripper in torque (current) mode for a compliant grasp; arm admittance always stays OFF")
    parser.add_argument("--dataset", type=str, default=None, help="Dataset name (auto-generated if omitted)")
    parser.add_argument("--text", type=str, default="",
                        help='Language/task instruction embedded in every episode of this run, '
                             'e.g. --text "Pick up the plate and place it in the gray bin". '
                             'Empty -> preprocess falls back to its yaml task.')
    parser.add_argument("--no-startup-home", action="store_true",
                        help="Skip the startup homing trajectory; launch with arms free (torque off)")
    args = parser.parse_args()

    dataset_name = args.dataset or f"{DATASET_PREFIX}_{time.strftime('%Y%m%d_%H%M%S')}"
    print_controls(dataset_name)

    # Encode the instruction to UTF-8 bytes for the S500 dataset_flag field. Assigning a
    # Python str into a numpy 'S' field goes through the ASCII codec and RAISES on any
    # non-ASCII char (accents, curly quotes, em-dash, emoji), which would abort teleop at
    # launch — so we hand numpy bytes instead. errors="replace" keeps even malformed argv
    # (lone surrogates from surrogateescape) from raising here. Truncate on a UTF-8 char
    # boundary so the daemon's .decode() can't choke on a split multibyte char, and warn.
    dataset_text_bytes = args.text.encode("utf-8", "replace")
    if len(dataset_text_bytes) > 500:
        dataset_text_bytes = dataset_text_bytes[:500]
        while dataset_text_bytes:
            try:
                dataset_text_bytes.decode("utf-8")
                break
            except UnicodeDecodeError:
                dataset_text_bytes = dataset_text_bytes[:-1]
        print(f"WARNING: --text is longer than 500 bytes; truncated to "
              f"{len(dataset_text_bytes)} bytes.", flush=True)
    if args.text:
        print(f'task: "{dataset_text_bytes.decode("utf-8")}"', flush=True)

    teleop_active = False
    episode_active = False
    # Precision-mode clutch state (per arm). See precision_step().
    prec_L = new_precision_state(); prec_R = new_precision_state()
    gate_L = new_pose_gate(); gate_R = new_pose_gate()
    last_clutch_t = None
    # Slow mode: runtime toggle + per-arm rate-limiter memory.
    slow_mode = SLOW_MODE_DEFAULT
    cmd_pos_L = None; cmd_pos_R = None; cmd_quat_L = None; cmd_quat_R = None
    # Reached from quest.py's MENU_SELECT (both triggers held, then X).
    no_headset = False
    ref_h = None                 # frozen head-plane height (None until first teleop enable / left-stick lock)
    anchor = None                # no-headset: user frame, frozen on X (see capture_anchor)
    anchor_ok = False            # no-headset: last disengaged frame captured a usable anchor
    homing = False
    home_start_time = None
    home_start_joints_left = None
    home_start_joints_right = None
    dehoming = False
    dehome_start_time = None
    dehome_traj_left = None
    dehome_traj_right = None
    dehome_straight = False      # dehome sub-phase: straightening the arm before the J0 ease-down
    dehome_straight_start = None
    dehome_straight_from_left = None   # bent pose (motor turns) at the end of the descent
    dehome_straight_from_right = None
    dehome_straight_to_left = None     # straight-down pose (arm hanging, J0/gripper kept)
    dehome_straight_to_right = None
    dehome_j0 = False            # dehome sub-phase: easing J0 down before torque-off
    dehome_j0_start = None
    dehome_j0_park_left = None   # parked pose (motor turns) when the straighten finishes
    dehome_j0_park_right = None
    dehome_j0_to_left = None     # J0 target = parked J0 + a further step down
    dehome_j0_to_right = None

    current_joints_left = np.zeros(CFG_L.dof)
    current_joints_right = np.zeros(CFG_R.dof)
    with Reader("arm_left.state") as r_left:
        while not r_left.ready():
            pass
        current_joints_left = CFG_L.q2urdf(r_left.data['pos'])
    with Reader("arm_right.state") as r_right:
        while not r_right.ready():
            pass
        current_joints_right = CFG_R.q2urdf(r_right.data['pos'])
    print(f"Initial joints: L={current_joints_left}, R={current_joints_right}")

    CFG_L.ik.init()
    CFG_R.ik.init()

    interp_start_time = None
    interp_start_joints_left = None
    interp_start_joints_right = None
    interpolating = False

    # Handoff only: tell quest.py to release the arms the moment we start waiting for the first one
    # (it's holding them frozen). Standalone launch opens immediately, so this never fires.
    ready_signal = (lambda: print(READY_LINE, flush=True)) if args.no_startup_home else None

    try:
        with writer_wait("arm_left.torque", Type("arm_torque"), on_wait=ready_signal) as w_left_torque, \
             writer_wait("arm_right.torque", Type("arm_torque")) as w_right_torque, \
             writer_wait("arm_left.ctrl", Type("arm_ctrl")) as w_left, \
             writer_wait("arm_right.ctrl", Type("arm_ctrl")) as w_right:
            # The gripper is handled by the gripper_* functions; the arm loop only feeds them inputs.
            tau_mode_L = gripper_tau_mode(CFG_L.dof, args.gripper_compliance)
            tau_mode_R = gripper_tau_mode(CFG_R.dof, args.gripper_compliance)
            grip_cur_L = grip_cur_R = 0.0
            last_grip_dbg = 0.0
            grip_err_t = 0.0

            # We already hold the arm ctrl+torque writers (outer `with`). If we're taking a handed-over
            # pose (--no-startup-home), re-energize the arms RIGHT HERE -- before opening the slower
            # writers below (dataset.flag's open runs a systems check that can pause a few seconds).
            # Otherwise torque stays off through that pause and a handed-over arm drops.
            q_left = current_joints_left.copy()
            q_right = current_joints_right.copy()
            torque_on = False
            if args.no_startup_home:
                w_left["pos"] = CFG_L.urdf2q(q_left)     # command the current pose, THEN enable torque
                w_right["pos"] = CFG_R.urdf2q(q_right)
                with w_left_torque.buf() as b:
                    b['enable'] = np.ones(CFG_L.dof, dtype=np.bool_)
                with w_right_torque.buf() as b:
                    b['enable'] = np.ones(CFG_R.dof, dtype=np.bool_)
                torque_on = True

            with Writer("dataset.flag", Type("dataset_flag")) as w_ds, \
                 Writer("drive.ctrl", Type("drive_ctrl")) as w_ctrl, \
                 Writer("arm_left.target", Type("arm_target"), keeptime=False) as w_left_target, \
                 Writer("arm_right.target", Type("arm_target"), keeptime=False) as w_right_target, \
                 Writer("quest.joystick", Type("quest_joystick"), keeptime=False) as w_joystick, \
                 Reader("arm_left.state") as r_left_state, \
                 Reader("arm_right.state") as r_right_state, \
                 Sound() as sound, \
                 Quest() as quest:

                w_ds["prefix"] = DATASET_PREFIX
                w_ds["name"] = dataset_name
                w_ds["text"] = dataset_text_bytes   # UTF-8 bytes (str would ASCII-crash on non-ASCII)

                if not args.no_startup_home:
                    # Bring both arms to home via staged torque enabling (see staged_home_arms).
                    print("Homing to start pose (staged torque enable)...", flush=True)
                    staged_home_arms([
                        dict(cfg=CFG_L, r_state=r_left_state, w_ctrl=w_left,
                             w_torque=w_left_torque, tau_mode=tau_mode_L),
                        dict(cfg=CFG_R, r_state=r_right_state, w_ctrl=w_right,
                             w_torque=w_right_torque, tau_mode=tau_mode_R),
                    ])
                    # Arms are now physically at home; sync the loop's command state so the idle
                    # (teleop-off) loop holds home instead of driving back toward the startup droop.
                    current_joints_left = CFG_L.q2urdf(np.asarray(CFG_L.home, dtype=np.float64))
                    current_joints_right = CFG_R.q2urdf(np.asarray(CFG_R.home, dtype=np.float64))
                    q_left = current_joints_left.copy()
                    q_right = current_joints_right.copy()
                    torque_on = True
                    print("Homing complete — torque ON, arms at home.", flush=True)

                def enable_torque():
                    """Enable arm torque, flushing ctrl to the current pose first. Idempotent."""
                    nonlocal torque_on, q_left, q_right
                    if torque_on:
                        return
                    # Zero-tau flush to the current pose so the daemon holds it, not a stale command.
                    q_left = current_joints_left.copy()
                    q_right = current_joints_right.copy()
                    w_left["pos"] = CFG_L.urdf2q(q_left)
                    w_left["tau"] = np.zeros(CFG_L.dof, dtype=np.float32)
                    w_left["alpha"] = 0.0
                    w_right["pos"] = CFG_R.urdf2q(q_right)
                    w_right["tau"] = np.zeros(CFG_R.dof, dtype=np.float32)
                    w_right["alpha"] = 0.0
                    with w_left_torque.buf() as b:
                        b['enable'] = np.ones(CFG_L.dof, dtype=np.bool_)
                        b['tau_mode'] = tau_mode_L
                        b['compliance_mode'] = False   # arm admittance always OFF
                    with w_right_torque.buf() as b:
                        b['enable'] = np.ones(CFG_R.dof, dtype=np.bool_)
                        b['tau_mode'] = tau_mode_R
                        b['compliance_mode'] = False   # arm admittance always OFF
                    torque_on = True
                    print("Torque ENABLED", flush=True)

                def start_homing():
                    """Pause teleop and animate both arms to home (shared by B-tap and the slow-mode
                    toggle). Resets IK warm-start, precision clutches, and the slow-mode rate limiter."""
                    nonlocal teleop_active, interpolating, homing
                    nonlocal home_start_joints_left, home_start_joints_right, home_start_time
                    nonlocal cmd_pos_L, cmd_pos_R, cmd_quat_L, cmd_quat_R
                    enable_torque()               # homing uses the arm -> ensure torque is on
                    teleop_active = False         # homing pauses teleop; press X to resume (re-interpolates)
                    interpolating = False
                    homing = True
                    home_start_joints_left = current_joints_left.copy()
                    home_start_joints_right = current_joints_right.copy()
                    home_start_time = time.monotonic()
                    home_ik()                     # reset IK warm-start memory to home
                    precision_reset(prec_L); precision_reset(prec_R)
                    cmd_pos_L = cmd_pos_R = cmd_quat_L = cmd_quat_R = None   # fresh speed-cap on re-enable

                if args.gripper_compliance:
                    print("Gripper compliance ENABLED (arm admittance OFF)")
                print(f"Torque OFF — home or press X to enable (closed={GRIPPER_CLOSED_POS_R}, "
                      f"open={GRIPPER_OPEN_POS}, left-stick-up open={GRIPPER_OPEN_POS_WIDE})")

                # Ctrl-C teardown: first SIGINT/SIGTERM stops the base and runs dehomeing, second signal escalates to immediate torque-off
                teardown_stage = 0

                def _on_sigint(signum, frame):
                    nonlocal teardown_stage
                    teardown_stage += 1
                    # Stop the base immediately on any teardown signal (ungated so
                    # the scheduler's keeptime gate can't drop it).
                    w_ctrl._keeptime = False
                    with w_ctrl.buf() as b:
                        b["twist"][:] = np.zeros(2, dtype=np.float32)
                    if teardown_stage >= 2:
                        print("\n^C again - shutting everything off (torque OFF)", flush=True)
                        for wt in (w_left_torque, w_right_torque):
                            wt._keeptime = False
                            with wt.buf() as b:
                                b["enable"][:] = np.zeros(CFG_L.dof, dtype=np.bool_)
                        sys.exit(1)
                    print("\n^C - TEARDOWN: dehoming, then torque off (Ctrl-C again to kill now)", flush=True)
                    park_arms([
                        dict(cfg=CFG_L, r_state=r_left_state, w_ctrl=w_left, w_torque=w_left_torque,
                             waypoints=STARTUP_WAYPOINTS_L, seg_durations=STARTUP_SEG_DURATIONS),
                        dict(cfg=CFG_R, r_state=r_right_state, w_ctrl=w_right, w_torque=w_right_torque,
                             waypoints=STARTUP_WAYPOINTS_R, seg_durations=STARTUP_SEG_DURATIONS),
                    ])
                    print("Teardown complete — torque OFF.", flush=True)
                    sys.exit(0)

                signal.signal(signal.SIGINT, _on_sigint)
                signal.signal(signal.SIGTERM, _on_sigint)

                twist = np.array([0.0, 0.0], dtype=np.float32)
                grip_open_L = grip_open_R = GRIPPER_OPEN_POS   # widened by the left stick
                running = True
                last_dbg = 0.0

                while running:
                    if r_left_state.ready():
                        current_joints_left = CFG_L.q2urdf(r_left_state.data['pos'])
                        grip_cur_L = float(r_left_state.data['current'][GRIPPER_IDX])
                    if r_right_state.ready():
                        current_joints_right = CFG_R.q2urdf(r_right_state.data['pos'])
                        grip_cur_R = float(r_right_state.data['current'][GRIPPER_IDX])
                    # Pause on either EDGE, not on the level: the pose stream is discontinuous
                    # across both (a wake can relocalise the stage origin, shifting every
                    # anchored target at once, and pose_gate cannot see that from the
                    # head-relative frame). A level check would also make X impossible to hold
                    # whenever the headset is asleep, which is most of the time.
                    up = quest.link_edge()
                    if up is not None and no_headset and teleop_active:
                        teleop_active = False
                        interpolating = False
                        precision_reset(prec_L); precision_reset(prec_R)
                        print(f"HEADSET LINK {'BACK' if up else 'LOST'} - teleop paused; "
                              f"press X to re-anchor", flush=True)
                    if quest.poll():
                        # Daemon already gives head-relative poses; just remap height onto the robot.
                        T_head = quest.T_head
                        head_h = float(T_head[2, 3])
                        ref = ref_h if ref_h is not None else head_h
                        left_pose = quest.left_pose
                        right_pose = quest.right_pose
                        # Disconnect guard: gate teleport jumps in the head-relative pose
                        # BEFORE anything consumes it (IK align gate, precision clutch, IK).
                        # Stays ahead of the no-headset lift: this frame survives a headset
                        # relocalisation, the global one does not.
                        left_pose = pose_gate(gate_L, left_pose, teleop_active, "L")
                        right_pose = pose_gate(gate_R, right_pose, teleop_active, "R")
                        if no_headset:
                            g_left = to_global(left_pose, T_head)
                            g_right = to_global(right_pose, T_head)
                            if not teleop_active:      # anchor follows you and the arms until X
                                ea_L = ee_anchor(CFG_L, current_joints_left)
                                ea_R = ee_anchor(CFG_R, current_joints_right)
                                fresh = capture_anchor(g_left, g_right, ea_L, ea_R)
                                anchor_ok = fresh is not None
                                if fresh is not None:
                                    anchor = fresh
                                elif anchor is None:   # nothing usable yet; X refused below
                                    left_pos, right_pos = ea_L, ea_R
                                    left_quat, right_quat = g_left[3:], g_right[3:]
                            if anchor is not None:
                                left_pos, left_quat = anchor_apply(anchor, g_left, "L")
                                right_pos, right_quat = anchor_apply(anchor, g_right, "R")
                                ref = anchor["z"]
                        else:
                            left_quat = left_pose[3:]      # daemon orientation (xyzw)
                            right_quat = right_pose[3:]
                            left_pos = height_remap(left_pose, ref)
                            right_pos = height_remap(right_pose, ref)

                        # Handoff: shift targets forward along the gripper axis.
                        left_pos = left_pos + HANDOFF_OFFSET * quat_forward_z(left_quat)
                        right_pos = right_pos + HANDOFF_OFFSET * quat_forward_z(right_quat)

                        # Precision mode (per hand; purr = on): hold a grip (side) button -> the IK
                        # target moves only PRECISION_SCALE x your controller motion. On release the
                        # target returns to the pre-precision spot, then re-syncs to the live hand so
                        # no offset is ever carried out of precision mode.
                        now_c = time.monotonic()
                        dt_c = min(now_c - last_clutch_t, 0.1) if last_clutch_t is not None else 0.02
                        last_clutch_t = now_c
                        ease = 1.0 - np.exp(-dt_c / PRECISION_SNAP_TAU)
                        left_pos = precision_step(prec_L, left_pos, teleop_active and quest.left_squeeze > PURR_THRESH, PRECISION_SCALE, ease)
                        right_pos = precision_step(prec_R, right_pos, teleop_active and quest.right_squeeze > PURR_THRESH, PRECISION_SCALE, ease)

                        # Slow mode: clamp into the cylinder shell, then cap linear + angular speed
                        # (speed limit, NOT a scaling). Applied last, before IK; the limited values
                        # are what feed ik.solve AND get recorded to arm_*.target (dataset stays honest).
                        if slow_mode and teleop_active:
                            left_pos = constrain_to_cylinders(left_pos, "left")
                            right_pos = constrain_to_cylinders(right_pos, "right")
                            cmd_pos_L = rate_limit(cmd_pos_L, left_pos, SLOW_MAX_SPEED * dt_c); left_pos = cmd_pos_L
                            cmd_pos_R = rate_limit(cmd_pos_R, right_pos, SLOW_MAX_SPEED * dt_c); right_pos = cmd_pos_R
                            _ang = np.radians(SLOW_MAX_ANG_SPEED) * dt_c
                            cmd_quat_L = slerp_limit(cmd_quat_L, left_quat, _ang); left_quat = cmd_quat_L
                            cmd_quat_R = slerp_limit(cmd_quat_R, right_quat, _ang); right_quat = cmd_quat_R
                        else:
                            cmd_pos_L = np.asarray(left_pos, dtype=np.float64).copy()
                            cmd_pos_R = np.asarray(right_pos, dtype=np.float64).copy()
                            cmd_quat_L = np.asarray(left_quat, dtype=np.float64).copy()
                            cmd_quat_R = np.asarray(right_quat, dtype=np.float64).copy()

                        if DEBUG_FRAME and (time.monotonic() - last_dbg) > 0.5:
                            last_dbg = time.monotonic()
                            if no_headset:
                                plane = f"ANCHOR yaw={np.degrees(anchor['yaw']):+.0f}" if teleop_active else "follow"
                            else:
                                plane = "LOCK" if ref_h is not None else "live"
                            print(f"[teleop] plane={plane}@{ref:.2f} "
                                  f"L={np.round(left_pos,3).tolist()} R={np.round(right_pos,3).tolist()}", flush=True)
                        grip_open_L, grip_open_R = gripper_open_pos(quest.left_thumbstick)

                        toggle_pulse = False
                        drop_pulse = False
                        for ev in quest.events(no_headset):
                            if ev == "dehome" and not dehoming:
                                # First Y: dehome — descend back through the startup
                                # waypoints in reverse, then cut torque
                                enable_torque()
                                teleop_active = False
                                interpolating = False
                                homing = False
                                dehome_traj_left = park_trajectory(
                                    CFG_L.urdf2q(current_joints_left), STARTUP_WAYPOINTS_L, STARTUP_SEG_DURATIONS)
                                dehome_traj_right = park_trajectory(
                                    CFG_R.urdf2q(current_joints_right), STARTUP_WAYPOINTS_R, STARTUP_SEG_DURATIONS)
                                dehome_start_time = time.monotonic()
                                dehoming = True
                                dehome_straight = False
                                dehome_j0 = False
                                print("Y - DEHOMING: descending through waypoints; press Y again to ESTOP", flush=True)
                            elif ev == "dehome":
                                # Second Y during the descent: ESTOP now.
                                print("Y again - ESTOP: torque off + drive stop", flush=True)
                                with w_left_torque.buf() as b:
                                    b['enable'] = np.zeros(CFG_L.dof, dtype=np.bool_)
                                with w_right_torque.buf() as b:
                                    b['enable'] = np.zeros(CFG_R.dof, dtype=np.bool_)
                                dehoming = False
                                dehome_straight = False
                                dehome_j0 = False
                                running = False

                            elif ev == "episode_drop":
                                drop_pulse = True
                                episode_active = False
                                episode_banner("DROPPED")
                                quest.buzz(HAPTIC_SHORT)                             # drop: 2 buzzes
                                quest.buzz(HAPTIC_SHORT, HAPTIC_GAP)
                                sound.say("Recording dropped")
                            elif ev == "episode_toggle":
                                toggle_pulse = True
                                episode_active = not episode_active
                                episode_banner("STARTED" if episode_active else "STOPPED")
                                if episode_active:
                                    quest.buzz(HAPTIC_SHORT)                         # start: 1 buzz
                                else:
                                    quest.buzz(HAPTIC_SHORT)                         # stop: short | longer pause | long
                                    quest.buzz(HAPTIC_LONG, HAPTIC_GAP_LONG)
                                sound.say("Recording started" if episode_active
                                          else "Recording stopped")

                            elif ev == "height_lock":
                                ref_h = head_h
                                print(f"HEIGHT PLANE LOCKED @ {head_h:.3f} m", flush=True)

                            elif ev == "slow":
                                slow_mode = not slow_mode
                                start_homing()                    # home so the change isn't a jerk
                                quest.buzz(HAPTIC_LONG)
                                sound.say("Tutorial mode" if slow_mode else "Normal mode")
                                print(f"SLOW MODE {'ON' if slow_mode else 'OFF'} + HOME",
                                      flush=True)
                            elif ev == "home":
                                start_homing()                    # plain B tap -> home
                                print("HOMING (B) - interpolating to home position", flush=True)

                            elif ev == "no_headset":
                                # Mode select: swapping the reference frame mid-motion would jump
                                # the arms, so only while stopped.
                                if teleop_active:
                                    print("NO-HEADSET unchanged - press X to pause teleop first",
                                          flush=True)
                                else:
                                    no_headset = not no_headset
                                    anchor = None            # stale in either frame
                                    anchor_ok = False
                                    ref_h = None             # re-capture the head plane on re-enable
                                    # 4 buzzes into no-headset, 1 back out: countable without looking.
                                    for i in range(4 if no_headset else 1):
                                        quest.buzz(HAPTIC_SHORT, i * HAPTIC_GAP)
                                    sound.say("No headset mode" if no_headset
                                              else "Headset mode")
                                    print(f"NO-HEADSET {'ON' if no_headset else 'OFF'} - "
                                          + ("X anchors on the live end-effectors"
                                             if no_headset else "head frame drives again"),
                                          flush=True)
                            elif ev == "teleop":
                                if homing:
                                    homing = False
                                    print("HOMING CANCELLED")
                                if no_headset and not teleop_active and not anchor_ok:
                                    # Gripper axes too near vertical to give a yaw. Refuse, don't guess.
                                    print("TELEOP NOT ENABLED - point the grippers nearer level "
                                          "(hands out front), then press X", flush=True)
                                else:
                                    if not teleop_active:
                                        enable_torque()          # teleop uses the arm -> ensure torque is on
                                        if no_headset:
                                            print(f"ANCHOR DROPPED @ z={anchor['z']:.3f} m, "
                                                  f"yaw={np.degrees(anchor['yaw']):+.1f} deg", flush=True)
                                        elif ref_h is None and 0.4 < head_h < 2.5:
                                            ref_h = head_h       # default plane: head height on first enable
                                            print(f"HEIGHT PLANE SET @ {head_h:.3f} m (first teleop enable)", flush=True)
                                        interp_start_joints_left = current_joints_left.copy()
                                        interp_start_joints_right = current_joints_right.copy()
                                        interp_start_time = time.monotonic()
                                        interpolating = True
                                        precision_reset(prec_L); precision_reset(prec_R)  # fresh 1:1 on enable
                                        print("TELEOP ENABLED - interpolating to target")
                                    else:
                                        interpolating = False
                                        precision_reset(prec_L); precision_reset(prec_R)
                                        print("TELEOP DISABLED")
                                    teleop_active = not teleop_active

                        with w_ds.buf() as b:
                            b["toggle_episode"] = toggle_pulse
                            b["drop_episode"] = drop_pulse

                        if teleop_active:
                            # Left target
                            q_arm_left = CFG_L.ik.solve(left_pos.tolist(), left_quat.tolist())
                            if q_arm_left is not None and len(q_arm_left) > 0:
                                q_left[:7] = q_arm_left[:7]
                                if interpolating:
                                    elapsed = time.monotonic() - interp_start_time
                                    alpha = min(elapsed / INTERP_DURATION, 1.0)
                                    q_left = interp_start_joints_left + alpha * (q_left - interp_start_joints_left)

                            # Right target
                            q_arm_right = CFG_R.ik.solve(right_pos.tolist(), right_quat.tolist())
                            if q_arm_right is not None and len(q_arm_right) > 0:
                                q_right[:7] = q_arm_right[:7]
                                if interpolating:
                                    elapsed = time.monotonic() - interp_start_time
                                    alpha = min(elapsed / INTERP_DURATION, 1.0)
                                    q_right = interp_start_joints_right + alpha * (q_right - interp_start_joints_right)
                            if interpolating:
                                elapsed = time.monotonic() - interp_start_time
                                if elapsed >= INTERP_DURATION:
                                    interpolating = False
                                    print("Interpolation complete - tracking active")

                        if homing:
                            home_target_left = CFG_L.q2urdf(CFG_L.home.copy())
                            home_target_right = CFG_R.q2urdf(CFG_R.home.copy())
                            elapsed = time.monotonic() - home_start_time
                            alpha = min(elapsed / HOME_DURATION, 1.0)
                            q_left = home_start_joints_left + alpha * (home_target_left - home_start_joints_left)
                            q_right = home_start_joints_right + alpha * (home_target_right - home_start_joints_right)
                            if alpha >= 1.0:
                                homing = False
                                print("Homing complete")

                        if dehoming and not dehome_straight and not dehome_j0:
                            # Phase 1: descend through the reversed waypoints.
                            t_dh = time.monotonic() - dehome_start_time
                            q_left = CFG_L.q2urdf(dehome_traj_left.at(t_dh).astype(np.float64))
                            q_right = CFG_R.q2urdf(dehome_traj_right.at(t_dh).astype(np.float64))
                            if dehome_traj_left.done(t_dh) and dehome_traj_right.done(t_dh):
                                # Descended to the lowest waypoint — a bent pose. Straighten the
                                # arm to hanging (gravity equilibrium) before the J0 ease-down,
                                # so torque-off cannot swing it forward into the table.
                                dehome_straight_from_left = dehome_traj_left.at(dehome_traj_left.total).astype(np.float64)
                                dehome_straight_from_right = dehome_traj_right.at(dehome_traj_right.total).astype(np.float64)
                                dehome_straight_to_left = straight_down_turns(CFG_L, dehome_straight_from_left)
                                dehome_straight_to_right = straight_down_turns(CFG_R, dehome_straight_from_right)
                                dehome_straight = True
                                dehome_straight_start = time.monotonic()
                                print("Dehomed to lowest waypoint — straightening arms to hang before J0 park-down", flush=True)
                        elif dehoming and dehome_straight:
                            # Phase 2: interpolate the arm joints to straight down; J0 and the
                            # gripper hold their parked values (see straight_down_turns).
                            elapsed = time.monotonic() - dehome_straight_start
                            frac = smoothstep(elapsed / PARK_STRAIGHTEN_S)
                            pl = dehome_straight_from_left + frac * (dehome_straight_to_left - dehome_straight_from_left)
                            pr = dehome_straight_from_right + frac * (dehome_straight_to_right - dehome_straight_from_right)
                            q_left = CFG_L.q2urdf(pl)
                            q_right = CFG_R.q2urdf(pr)
                            if elapsed >= PARK_STRAIGHTEN_S + 0.4:   # ramp done + LPF settle
                                # Hanging straight down; ease J0 further down before cutting
                                # torque so the arm parks lower ("down" is per-arm).
                                dehome_j0_park_left = dehome_straight_to_left.copy()
                                dehome_j0_park_right = dehome_straight_to_right.copy()
                                dl = float(np.sign(dehome_j0_park_left[0] - float(CFG_L.home[0]))) or 1.0
                                dr = float(np.sign(dehome_j0_park_right[0] - float(CFG_R.home[0]))) or 1.0
                                dehome_j0_to_left = dehome_j0_park_left[0] + dl * J0_PARK_DOWN_TURNS
                                dehome_j0_to_right = dehome_j0_park_right[0] + dr * J0_PARK_DOWN_TURNS
                                dehome_straight = False
                                dehome_j0 = True
                                dehome_j0_start = time.monotonic()
                                print("Arms hanging — lowering J0 before torque off", flush=True)
                        elif dehoming and dehome_j0:
                            # Phase 2: ramp J0 down, holding the other joints at the parked pose.
                            ramp_s = max(J0_PARK_DOWN_TURNS / J0_PARK_DOWN_SPEED, 1e-3)
                            elapsed = time.monotonic() - dehome_j0_start
                            frac = smoothstep(elapsed / ramp_s)
                            pl = dehome_j0_park_left.copy()
                            pr = dehome_j0_park_right.copy()
                            pl[0] = dehome_j0_park_left[0] + frac * (dehome_j0_to_left - dehome_j0_park_left[0])
                            pr[0] = dehome_j0_park_right[0] + frac * (dehome_j0_to_right - dehome_j0_park_right[0])
                            q_left = CFG_L.q2urdf(pl)
                            q_right = CFG_R.q2urdf(pr)
                            if elapsed >= ramp_s + 0.4:   # ramp done + LPF settle -> torque off
                                with w_left_torque.buf() as b:
                                    b['enable'] = np.zeros(CFG_L.dof, dtype=np.bool_)
                                with w_right_torque.buf() as b:
                                    b['enable'] = np.zeros(CFG_R.dof, dtype=np.bool_)
                                dehoming = False
                                dehome_j0 = False
                                running = False
                                print("Dehomed — torque OFF.", flush=True)

                        speed_mult = 1.0 if quest.right_stick_click else 0.5
                        leftright, fwdback = quest.right_thumbstick[0], quest.right_thumbstick[1]
                        twist = np.array([-fwdback * SPEED_LIN * speed_mult, -leftright * SPEED_ANG * speed_mult], dtype=np.float32)
                        if not running:                      # insta-kill: stop the base this iteration
                            twist = np.array([0.0, 0.0], dtype=np.float32)
                        assert len(q_left) == CFG_L.dof
                        assert len(q_right) == CFG_R.dof

                        # Publish the exact ik.solve() inputs for the dataset. tracking:
                        # was teleop actually driving IK this frame (mask idle/homing in eval).
                        tracking = teleop_active and not homing and not dehoming
                        with w_left_target.buf() as b:
                            b["xyz"] = left_pos.astype(np.float32)
                            b["quat"] = left_quat.astype(np.float32)
                            b["grip"] = np.float32(quest.left_trigger)
                            b["tracking"] = tracking
                        with w_right_target.buf() as b:
                            b["xyz"] = right_pos.astype(np.float32)
                            b["quat"] = right_quat.astype(np.float32)
                            b["grip"] = np.float32(quest.right_trigger)
                            b["tracking"] = tracking

                        # Raw thumbsticks for the dataset: unscaled, no speed_mult.
                        with w_joystick.buf() as b:
                            b["left"] = np.asarray(quest.left_thumbstick, dtype=np.float32)
                            b["right"] = np.asarray(quest.right_thumbstick, dtype=np.float32)

                    # Gripper: delegated to gripper_command(); guarded so a fault can't stop arm tracking.
                    tau_left = np.zeros(CFG_L.dof, dtype=np.float32)
                    tau_right = np.zeros(CFG_R.dof, dtype=np.float32)
                    now = time.monotonic()
                    grip_engaged = teleop_active and not homing and not dehoming  # homing/dehoming own the gripper
                    try:
                        gripper_command(CFG_L, q_left, tau_left, quest.left_trigger, current_joints_left[GRIPPER_IDX],
                                        GRIPPER_CLOSED_POS_L, grip_engaged, args.gripper_compliance, grip_open_L)
                        gripper_command(CFG_R, q_right, tau_right, quest.right_trigger, current_joints_right[GRIPPER_IDX],
                                        GRIPPER_CLOSED_POS_R, grip_engaged, args.gripper_compliance, grip_open_R)
                        if now - last_grip_dbg > GRIPPER_DEBUG_SEC:
                            last_grip_dbg = now
                            gripper_debug(args.gripper_compliance, grip_engaged, homing,
                                          (quest.left_trigger, tau_left[GRIPPER_IDX], current_joints_left[GRIPPER_IDX], grip_cur_L),
                                          (quest.right_trigger, tau_right[GRIPPER_IDX], current_joints_right[GRIPPER_IDX], grip_cur_R),
                                          grip_open_L, grip_open_R)
                    except Exception as e:
                        tau_left[GRIPPER_IDX] = 0.0
                        tau_right[GRIPPER_IDX] = 0.0
                        if now - grip_err_t > 1.0:
                            grip_err_t = now
                            print(f"[grip] ERROR (gripper off this frame): {e}", flush=True)

                    if w_left.ready():
                        w_left["pos"] = CFG_L.urdf2q(q_left)
                        w_left["tau"] = tau_left
                        w_left["alpha"] = quest.left_squeeze
                    if w_right.ready():
                        w_right["pos"] = CFG_R.urdf2q(q_right)
                        w_right["tau"] = tau_right
                        w_right["alpha"] = quest.right_squeeze
                    if w_ctrl.ready():
                        w_ctrl["twist"] = twist

                    # Queued buzzes, else the precision purr — strength grows with each hand's
                    # carried offset (drift from the engage spot).
                    quest.send_haptics(teleop_active,
                                       float(np.linalg.norm(prec_L["off"])),
                                       float(np.linalg.norm(prec_R["off"])))

    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        print("Cleanup complete.")


if __name__ == "__main__":
    main()

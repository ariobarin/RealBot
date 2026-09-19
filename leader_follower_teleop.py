# /// script
# dependencies = [
#   "bbos",
#   "numpy<2",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""
Leader-follower teleop with episode recording.

Controls:
  Right arrow  Start recording / mark good episode + reset
  Left arrow   Mark bad episode + reset (during recording)
  Escape       Quit

Flags:
  --raw         Pass positions directly (default)
  --normalize   Use percentage-space mapping
  --prefix NAME S3 prefix (default: leader_follower)
  --dataset NAME Dataset name (auto-generated if omitted)

Usage: uv run leader_follower_teleop.py [--prefix NAME] [--dataset NAME]
"""
import argparse
import json
import os
import select
import socket
import sys
import termios
import time
import tty
import numpy as np
from bbos import Reader, Writer, Type, Config

SIDES = ["left", "right"]
INTERP_DURATION = 2.0
GRIPPER_IDX = 7

# States
IDLE = "IDLE"
RECORDING = "RECORDING"
RESET_ENV = "RESET_ENV"

def load_cal_ranges(daemon_name):
    base = os.path.join("/home/bracketbot/bbos/bbos/daemons", daemon_name)
    path = os.path.join(base, "ranges.calibration.json")
    with open(path) as f:
        data = json.load(f)
    return np.array(data["cal_min"], dtype=np.float32), np.array(data["cal_max"], dtype=np.float32)

def normalize(pos, cal_min, cal_max):
    norm = np.zeros_like(pos)
    rng = cal_max - cal_min
    safe = np.abs(rng) > 1e-6
    norm[:GRIPPER_IDX] = np.where(
        safe[:GRIPPER_IDX],
        ((pos[:GRIPPER_IDX] - cal_min[:GRIPPER_IDX]) / rng[:GRIPPER_IDX]) * 200 - 100,
        0.0,
    )
    if safe[GRIPPER_IDX]:
        norm[GRIPPER_IDX] = ((pos[GRIPPER_IDX] - cal_min[GRIPPER_IDX]) / rng[GRIPPER_IDX]) * 100
    return norm

def unnormalize(norm, cal_min, cal_max):
    pos = np.zeros_like(norm)
    rng = cal_max - cal_min
    pos[:GRIPPER_IDX] = ((norm[:GRIPPER_IDX] + 100) / 200) * rng[:GRIPPER_IDX] + cal_min[:GRIPPER_IDX]
    pos[GRIPPER_IDX] = (norm[GRIPPER_IDX] / 100) * rng[GRIPPER_IDX] + cal_min[GRIPPER_IDX]
    return pos

def read_key():
    """Non-blocking key read using os.read. Returns 'RIGHT', 'LEFT', 'ESC', or None."""
    fd = sys.stdin.fileno()
    if not select.select([fd], [], [], 0)[0]:
        return None
    ch = os.read(fd, 1)
    if ch == b'\x1b':
        if not select.select([fd], [], [], 0.1)[0]:
            return 'ESC'
        ch2 = os.read(fd, 1)
        if ch2 == b'[' and select.select([fd], [], [], 0.1)[0]:
            ch3 = os.read(fd, 1)
            if ch3 == b'C':
                return 'RIGHT'
            if ch3 == b'D':
                return 'LEFT'
        return None
    return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--normalize", action="store_true")
    parser.add_argument("--raw", action="store_true")
    parser.add_argument("--prefix", type=str, default="leader_follower")
    parser.add_argument("--dataset", type=str, default=None)
    args = parser.parse_args()

    use_normalize = args.normalize and not args.raw
    hostname = socket.gethostname()

    mode_str = "normalize" if use_normalize else "raw"
    prefix = args.prefix
    dataset_name = args.dataset or f"{prefix}_{time.strftime('%Y%m%d_%H%M%S')}"
    BOX_W = max(49, len(f"Dataset: {dataset_name}") + 4)

    leader_cal, follower_cal = {}, {}
    if use_normalize:
        for side in SIDES:
            leader_cal[side] = load_cal_ranges(f"leader_{side}")
            follower_cal[side] = load_cal_ranges(f"arm_{side}")

    dof = Config("arm_left").dof

    init_pos = {}
    for side in SIDES:
        with Reader(f"arm_{side}.state") as r:
            while not r.ready():
                pass
            init_pos[side] = r.data["pos"].copy()

    sys.stdout.flush()
    tty_fd = os.open("/dev/tty", os.O_WRONLY)
    # Set terminal to cbreak mode for character-at-a-time input
    old_settings = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    # Flush any stale input
    while select.select([sys.stdin.fileno()], [], [], 0)[0]:
        os.read(sys.stdin.fileno(), 4096)

    state = IDLE
    ep_count = 0
    good_count = 0
    bad_count = 0
    rec_start = 0.0
    status_msg = ""

    def row(text):
        pad = BOX_W - 3
        return f"\u2502 {text:<{pad}}\u2502"

    def draw_box():
        h = "\u2500"
        top = f"\u250c\u2500 Leader-Follower Teleop {h * (BOX_W - 26)}\u2510"
        mid = f"\u251c{h * (BOX_W - 2)}\u2524"
        bot = f"\u2514{h * (BOX_W - 2)}\u2518"
        if state == RECORDING:
            secs = int(time.monotonic() - rec_start)
            state_text = f"State:  \u25cf RECORDING  {secs // 60}:{secs % 60:02d}"
        elif state == RESET_ENV:
            state_text = "State:  \u25cb RESET"
        else:
            state_text = "State:  - IDLE"
        ep_text = f"Episodes:  {good_count} good  {bad_count} bad  ({ep_count} total)"
        lines = [
            top,
            row(f"Dataset: {dataset_name}"),
            row(f"Mode: {mode_str}"),
            mid,
            row(state_text),
            row(ep_text),
            mid,
            row("->  record/good   <-  bad   Esc  quit"),
            bot,
        ]
        if status_msg:
            lines.append(f"  {status_msg}")
        else:
            lines.append("")
        return lines

    BOX_LINES = len(draw_box())

    def print_status(msg=""):
        nonlocal status_msg
        status_msg = msg
        lines = draw_box()
        # Clear screen, move to top-left, draw box
        out = "\033[2J\033[H" + "".join(f"{l}\n" for l in lines)
        os.write(tty_fd, out.encode())

    def init_display(msg=""):
        print_status(msg)

    try:
        with Writer("arm_left.torque", Type("arm_torque")) as w_lt, \
             Writer("arm_right.torque", Type("arm_torque")) as w_rt, \
             Writer("dataset.flag", Type("dataset_flag")) as w_ds:
            w_lt["enable"] = np.ones(dof, dtype=np.bool_)
            w_rt["enable"] = np.ones(dof, dtype=np.bool_)
            w_ds["prefix"] = prefix
            w_ds["name"] = dataset_name

            with Writer("arm_left.ctrl", Type("arm_ctrl")) as w_lc, \
                 Writer("arm_right.ctrl", Type("arm_ctrl")) as w_rc, \
                 Reader("leader_left.state") as r_ll, \
                 Reader("leader_right.state") as r_lr, \
                 Reader("arm_left.state") as r_fl, \
                 Reader("arm_right.state") as r_fr:

                writers = {"left": w_lc, "right": w_rc}
                leader_readers = {"left": r_ll, "right": r_lr}
                follower_readers = {"left": r_fl, "right": r_fr}

                target_pos = {side: init_pos[side].copy() for side in SIDES}
                interp_start = {side: init_pos[side].copy() for side in SIDES}
                t_start = time.monotonic()
                interpolating = True
                last_display = 0.0

                sys.stdout.flush()
                init_display("Ramping in...")
                # Suppress bbos noise (loop timing, frame drops)
                saved_stdout = os.dup(1)
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.close(devnull)

                while True:
                    key = read_key()

                    if key == 'ESC':
                        # Stop any active recording
                        if state == RECORDING:
                            w_ds["drop_episode"] = True
                            time.sleep(0.05)
                            w_ds["drop_episode"] = False
                            bad_count += 1
                            ep_count += 1
                        break

                    if key == 'RIGHT':
                        if state == IDLE or state == RESET_ENV:
                            # Start recording
                            state = RECORDING
                            rec_start = time.monotonic()
                            w_ds["toggle_episode"] = True
                            time.sleep(0.05)
                            w_ds["toggle_episode"] = False
                            print_status()
                        elif state == RECORDING:
                            # Good episode -> stop recording -> reset env
                            w_ds["toggle_episode"] = True
                            time.sleep(0.05)
                            w_ds["toggle_episode"] = False
                            ep_count += 1
                            good_count += 1
                            state = RESET_ENV
                            print_status("Good! Reset environment, then ->")

                    if key == 'LEFT':
                        if state == RECORDING:
                            # Bad episode -> drop -> reset env
                            w_ds["drop_episode"] = True
                            time.sleep(0.05)
                            w_ds["drop_episode"] = False
                            ep_count += 1
                            bad_count += 1
                            state = RESET_ENV
                            print_status("Bad episode dropped. Reset environment, then ->")

                    # Teleop loop
                    for side in SIDES:
                        r_l = leader_readers[side]
                        r_f = follower_readers[side]
                        w = writers[side]

                        if r_l.ready():
                            leader_pos = r_l.data["pos"]
                            if use_normalize:
                                l_min, l_max = leader_cal[side]
                                f_min, f_max = follower_cal[side]
                                norm = normalize(leader_pos, l_min, l_max)
                                norm[:GRIPPER_IDX] = np.clip(norm[:GRIPPER_IDX], -100, 100)
                                norm[GRIPPER_IDX] = np.clip(norm[GRIPPER_IDX], 0, 100)
                                target_pos[side] = unnormalize(norm, f_min, f_max)
                            else:
                                target_pos[side] = leader_pos.copy()

                        if r_f.ready():
                            pass

                        if w.ready():
                            if interpolating:
                                elapsed = time.monotonic() - t_start
                                alpha = min(elapsed / INTERP_DURATION, 1.0)
                                cmd = interp_start[side] + alpha * (target_pos[side] - interp_start[side])
                                w["pos"] = cmd
                                if alpha >= 1.0 and side == "right":
                                    interpolating = False
                                    print_status("Ready. Press -> to start recording")
                            else:
                                w["pos"] = target_pos[side]

                    if state == RECORDING and time.monotonic() - last_display >= 1.0:
                        last_display = time.monotonic()
                        print_status()

    except KeyboardInterrupt:
        pass
    finally:
        os.dup2(saved_stdout, 1)
        os.close(saved_stdout)
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        state = IDLE
        print_status(f"Done. {ep_count} episodes ({good_count} good, {bad_count} bad)")
        os.close(tty_fd)

if __name__ == "__main__":
    main()

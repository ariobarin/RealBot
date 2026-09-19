# /// script
# dependencies = [
#   "bbos",
#   "numpy<2",
#   "viser",
#   "yourdfpy",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""
Real-time 3D arm visualizer, with optional joint-slider teleop.

Usage: uv run view_arms.py [--control] [daemon_name ...]
  e.g. uv run view_arms.py arm_left arm_right
       uv run view_arms.py leader_left leader_right
       uv run view_arms.py --control arm_left arm_right
Defaults to arm_left arm_right if no args given.

Without --control this is a read-only viewer: it opens no writers and never
touches the arms. With --control each arm also gets an "Enable torque" checkbox
and one slider per joint. While the checkbox is OFF the arm is limp and the
sliders track the real position (so enabling never jumps). Flip it ON and the
sliders become position commands.
"""

import sys
import json
import time
from pathlib import Path
import numpy as np
import viser
from viser.extras import ViserUrdf
import yourdfpy
import bbos
from bbos import Reader, Writer, Type, Config
import socket

# Per-robot calibration ranges (cal_min/cal_max in motor turns) are written by
# each arm's calibrate.py next to its daemon, e.g.
# <bbos>/daemons/arm_left/ranges.calibration.json. Resolve from the bbos package
# so the path is correct regardless of $HOME.
DAEMONS_DIR = Path(bbos.__file__).parent / "daemons"

# How long to wait for a first arm state before giving up on seeding the sliders.
STATE_WAIT_S = 5.0


def load_cal_ranges(name):
    """(cal_min, cal_max) arrays from a daemon's ranges.calibration.json, or
    (None, None) if the arm has not been calibrated on this robot."""
    path = DAEMONS_DIR / name / "ranges.calibration.json"
    if not path.exists():
        return None, None
    with open(path) as f:
        cal = json.load(f)
    return np.array(cal["cal_min"], np.float64), np.array(cal["cal_max"], np.float64)


def limits_table(cal_min, cal_max):
    """Rows of 'Ji  min  max  span' for the calibrated joint ranges.
    span = cal_max - cal_min, the same range bracketbot_adapter.py normalizes over."""
    rows = [f"{'joint':<6}{'min':>10}{'max':>10}{'span':>10}"]
    for i in range(len(cal_min)):
        span = cal_max[i] - cal_min[i]
        rows.append(f"J{i:<5}{cal_min[i]:>10.4f}{cal_max[i]:>10.4f}{span:>10.4f}")
    return rows


args = sys.argv[1:]
control = "--control" in args
arm_names = [a for a in args if not a.startswith("-")] or ["arm_left", "arm_right"]
configs = {name: Config(name) for name in arm_names}

cfg0 = list(configs.values())[0]
print(f"Loading robot from: {cfg0.urdf_path}")
urdf = yourdfpy.URDF.load(
    cfg0.urdf_path,
    load_meshes=True,
    load_collision_meshes=False,
    build_scene_graph=True,
    build_collision_scene_graph=False,
)
all_joints = list(urdf.joint_map.keys())
print(f"URDF joints: {all_joints}")
print(f"Visualizing: {arm_names}{' (control enabled)' if control else ''}")

server = viser.ViserServer()
viser_urdf = ViserUrdf(server, urdf_or_path=urdf, root_node_name="/robot")
server.scene.add_grid("/grid", width=2.0, height=2.0, cell_size=0.1, position=(0.0, 0.0, -0.01))

readers = {name: Reader(f"{name}.state", Type("arm_state")).__enter__() for name in arm_names}

# Control writers are opened only with --control: a live .ctrl/.torque Writer is
# something the arm daemon discovers, so a plain viewer must not create one.
w_ctrl, w_torque = {}, {}
if control:
    for name in arm_names:
        w_ctrl[name] = Writer(f"{name}.ctrl", Type("arm_ctrl")).__enter__()
        w_torque[name] = Writer(f"{name}.torque", Type("arm_torque")).__enter__()

# Seed the sliders from the live pose so enabling torque never jumps the arm.
init_pos = {}
if control:
    print("Waiting for arm state...")
    for name in arm_names:
        r = readers[name]
        deadline = time.monotonic() + STATE_WAIT_S
        while not r.ready() and time.monotonic() < deadline:
            time.sleep(0.01)
        if r.ready():
            init_pos[name] = np.array(r.data["pos"], np.float64)
        else:
            print(f"  {name}: no state after {STATE_WAIT_S:.0f}s, seeding sliders at zero")
            init_pos[name] = np.zeros(configs[name].dof, np.float64)

gui_texts, enables, sliders = {}, {}, {}
print(f"\n=== Calibration limits ({socket.gethostname()}) ===")
for name in arm_names:
    cfg = configs[name]
    cal_min, cal_max = load_cal_ranges(name)
    if cal_min is None:
        print(f"  {name}: no ranges.calibration.json (not calibrated)")
        limits_md = "**Calibration limits**\n\n_no ranges.calibration.json (not calibrated)_"
    else:
        rows = limits_table(cal_min, cal_max)
        print(f"  {name}:")
        for r in rows:
            print(f"    {r}")
        limits_md = "**Calibration limits (min / max / span)**\n\n```\n" + "\n".join(rows) + "\n```"
    with server.gui.add_folder(name, expand_by_default=True):
        gui_texts[name] = server.gui.add_markdown("**Position:** waiting...")
        if control:
            enables[name] = server.gui.add_checkbox("Enable torque", initial_value=False)
            sliders[name] = []
            for i in range(cfg.dof):
                # Prefer the calibrated travel as the slider range; fall back to a
                # narrow window around the current pose for an uncalibrated arm.
                if cal_min is not None:
                    lo = float(min(cal_min[i], cal_max[i]))
                    hi = float(max(cal_min[i], cal_max[i]))
                else:
                    lo, hi = float(init_pos[name][i] - 1.0), float(init_pos[name][i] + 1.0)
                jname = cfg.joint_names[i] if i < len(cfg.joint_names) else f"J{i}"
                init = float(np.clip(init_pos[name][i], lo, hi))
                step = (hi - lo) / 500.0 if hi > lo else 0.001
                sliders[name].append(
                    server.gui.add_slider(jname, min=lo, max=hi, step=step, initial_value=init)
                )
        server.gui.add_markdown(limits_md)

print("\nVisualizer ready!")
print(f"Open: http://{socket.gethostname()}.local:8080")
if control:
    print("Flip 'Enable torque' ON to command an arm; OFF = limp + sliders follow real pose.")

joint_cfg = {name: 0.0 for name in all_joints}
last_pos = {name: None for name in arm_names}

try:
    while True:
        for name in arm_names:
            cfg = configs[name]
            r = readers[name]
            if r.ready():
                last_pos[name] = np.array(r.data["pos"], np.float64)
                gui_texts[name].content = "  \n".join(
                    [f"J{i}: {last_pos[name][i]:.3f}" for i in range(len(last_pos[name]))]
                )
            actual = last_pos[name]
            if actual is None:
                continue

            if control:
                on = enables[name].value
                if on:
                    # Sliders drive the arm.
                    target = np.array([s.value for s in sliders[name]], np.float64)
                else:
                    # Limp: keep sliders on the real pose so enabling never jumps.
                    target = actual
                    for i, s in enumerate(sliders[name]):
                        s.value = float(np.clip(actual[i], s.min, s.max))

                with w_torque[name].buf() as b:
                    b["enable"][:] = np.full(cfg.dof, on, dtype=np.bool_)

                if w_ctrl[name].ready():
                    with w_ctrl[name].buf() as b:
                        b["pos"][:] = target.astype(np.float32)
            else:
                target = actual

            # Visualize the commanded target (== the real pose when limp).
            angles = cfg.q2urdf(target)
            for i, jname in enumerate(cfg.joint_names):
                joint_cfg[jname] = angles[i]

        joint_array = np.array([joint_cfg.get(name, 0.0) for name in all_joints])
        urdf.update_cfg(joint_array)
        viser_urdf.update_cfg(joint_array)
        time.sleep(0.02)
finally:
    for name in arm_names:
        if control:
            # Leave arms limp on exit.
            with w_torque[name].buf() as b:
                b["enable"][:] = np.zeros(configs[name].dof, dtype=np.bool_)
            w_ctrl[name].__exit__(None, None, None)
            w_torque[name].__exit__(None, None, None)
        readers[name].__exit__(None, None, None)

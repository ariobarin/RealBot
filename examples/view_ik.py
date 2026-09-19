# /// script
# dependencies = [
#   "bbos",
#   "numpy",
#   # trimesh<5 holds viser at 1.1.0 (viser caps trimesh<5). yourdfpy 0.0.51 --
#   # what uv picks unpinned -- indexes cfg with a 1-element list, so URDF.load
#   # hands rotation_matrix a (1,) array that numpy>=1.24 rejects. 0.0.56 fixed it.
#   "trimesh<5.0.0",
#   "viser",
#   "yourdfpy>=0.0.56",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""
IK visualizer.
Hold A = freeze/show all | Trigger = cycle | Release A = save.

IK targets follow view_quest's convention: head-relative XY, with the
operator's head plane remapped onto the robot's shoulder height.

Orange translucent shapes are the URDF <collision> geometry -- the obstacles
the IK is meant to avoid. They are read from the URDF, not hardcoded.
"""

import json
import time
import numpy as np
import viser
from viser.extras import ViserUrdf
import yourdfpy
from bbos import Reader, Config
from bbos.tf import rmat_to_quat
import socket

CFG_L = Config("arm_left")
CFG_R = Config("arm_right")
QCFG = Config("quest")
ROBOT_REF_DEFAULT = float(QCFG.robot_shoulder_height)   # robot reference height (by definition)

PREF_PATH = "/home/bracketbot/ik_preferences.json"
TRAJ_PATH = "/home/bracketbot/ik_trajectory.json"
GREY = (0.5, 0.5, 0.5, 0.2)
SEL_COLOR = (0.1, 0.95, 0.2, 0.7)
CAND_SEEDS = 64            # random seeds per freeze, plus the current pose
CAND_GOOD_ERR = 5.0        # mm; a candidate must reach the target this well
CAND_MAX_ERR = 25.0        # mm; below this it is usable but gets flagged
CAND_DEDUP = 0.20          # rad; postures closer than this are the same posture
# Candidate postures: (joint index, offset from nominal_config). One displayed
# candidate per entry. Kept here, not in constants.py -- the solver runs a full
# solve per entry, and a list this size costs ~60% of the 15 ms arm_ctrl tick
# with both arms, so it is a search set for this tool, not a runtime setting.
QP_PERTURBATIONS = [
    ("main",    0,  0.0),
    ("j0+",     0,  0.10),  ("j0-",     0, -0.10),
    ("j3+0.5",  3,  0.50),  ("j3-0.5",  3, -0.50),
    ("j3+1",    3,  1.00),  ("j3-1",    3, -1.00),
    ("j3-2",    3, -2.00),  ("j3-3.14", 3, -3.14),
    ("j1+0.8",  1,  0.80),  ("j1-0.8",  1, -0.80),
    ("j1+1.6",  1,  1.60),  ("j1-1.6",  1, -1.60),
]
N_CAND = len(QP_PERTURBATIONS)
J0_INDEX = 0               # chain index of the mast lift (lj0/rj0, prismatic,
                           # -1.03..0 m). Postures with it near 0 are preferred.
# Centering is relaxed while exploring. The runtime weights are tuned to HOLD a
# preferred posture, the opposite of what exploration needs: at full strength the
# seeds get dragged back before they can settle in another branch (measured: 3 of
# 5 reached the target at full strength, 6 of 6 relaxed).
CAND_CENTERING_SCALE = 0.1

# Candidates come from SEEDING the solve differently, not from perturbing the
# nominal -- centering is far weaker than task tracking, so a nominal nudge leaves
# every candidate at the same posture. Seeds are sampled across the joint ranges
# rather than hand-picked: a fixed set of per-joint kicks only finds the branches
# those joints happen to open. Measured on 040 at a recorded target -- a 13-seed
# hand-picked pool gave ~6 distinct solutions, 32 sampled seeds give 7-8 distinct
# under 5mm with ~4 rad of spread. The RNG is seeded per call, so the same target
# always yields the same candidates.

# Each solve() runs only rik_max_iterations RelaxedIK steps, so a candidate
# needs several calls to settle into the basin its nominal implies.
CAND_ITERS = 25            # each solve() is rik_max_iterations steps
# Seed count is the lever, not iteration count. Measured on 040 at a recorded
# target: 32 seeds gave 3 distinct solutions under 5mm, 64 gave 7; running 60
# iterations instead of 25 gave the same 7. Candidates that land above ~10mm are
# converged local minima that do not reach the target at all -- polishing one for
# 200 further iterations moved it 15.0 -> 14.8mm -- so they are filler, not
# under-solved, and the good tier is preferred over them.
HOME_DURATION = 2.5        # s to animate the arms to home (view_quest uses the same)

# The cost readout mirrors the QP's score_solution(), so it has to use the gains
# the solver was actually built with. Hardcoding them here let the two drift --
# this file scored with [50, 25, 4.5, 5, 2, 3.5, 1.5] while arm_left/constants.py
# ran [50, 25, 15, 5, 10, 12, 1.5], so the numbers on screen ranked postures the
# solver was not ranking. Read them off the solver instead.
LAMBDA_CONT_SCALE = 5.0    # matches qp_solver.rs: lambda = 5 * eps


# joint_centering_gain is not configurable -- it is DiffIkConfig::default() in
# qp_solver.rs. Keep it in step with that default if it ever changes there.
K_GAIN = 10.0


def _gains(ik):
    eps = float(ik._joint_centering_weight)
    return eps, K_GAIN, list(ik._centering_weights)


def xyzw_to_wxyz(q):
    return np.array([q[3], q[0], q[1], q[2]])



def load_urdf():
    return yourdfpy.URDF.load(
        CFG_L.urdf_path, load_meshes=True, load_collision_meshes=False,
        build_scene_graph=True, build_collision_scene_graph=False,
    )


COLLISION_COLOR = (255, 140, 0)


def add_collision_shapes(server, urdf_obj, prefix="/collision"):
    """Draw the URDF's <collision> cylinders -- the geometry the IK treats as
    obstacles. Read straight from the URDF so this can never drift from what the
    solver actually avoids. Returns the scene handles so they can be toggled."""
    import trimesh
    handles = []
    for link in urdf_obj.robot.links:
        for i, col in enumerate(getattr(link, "collisions", None) or []):
            cyl = getattr(col.geometry, "cylinder", None)
            if cyl is None:
                continue  # only cylinders are modelled by the IK today
            T = (urdf_obj.get_transform(frame_to=link.name, frame_from=urdf_obj.base_link)
                 @ np.asarray(col.origin, dtype=float))
            mesh = trimesh.creation.cylinder(radius=cyl.radius, height=cyl.length)
            mesh.apply_transform(T)
            handles.append(server.scene.add_mesh_simple(
                f"{prefix}/{link.name}_{i}",
                vertices=np.asarray(mesh.vertices),
                faces=np.asarray(mesh.faces),
                color=COLLISION_COLOR,
                opacity=0.35,
                side="double",
            ))
    return handles


def pad(result, n):
    if result is None:
        return None
    return np.concatenate([result, np.zeros(max(0, n - len(result)))]) if len(result) < n else result


class QuestIKVisualizer:
    def __init__(self):
        CFG_L.ik.init()
        CFG_R.ik.init()

        self.server = viser.ViserServer()
        self.urdf_ref = load_urdf()
        self.all_joints = list(self.urdf_ref.joint_map.keys())

        self.cand_vu = []
        self.cand_uobj = []
        self.cand_cfg = []
        for i in range(N_CAND):
            u = load_urdf()
            vu = ViserUrdf(self.server, urdf_or_path=u, root_node_name=f"/cand_{i}", mesh_color_override=GREY)
            vu.show_visual = False
            self.cand_vu.append(vu)
            self.cand_uobj.append(u)
            self.cand_cfg.append({n: 0.0 for n in self.all_joints})

        self.sel_uobj = load_urdf()
        self.sel_vu = ViserUrdf(self.server, urdf_or_path=self.sel_uobj, root_node_name="/selected", mesh_color_override=SEL_COLOR)
        self.sel_cfg = {n: 0.0 for n in self.all_joints}

        self.jlim = {}
        for j in self.urdf_ref.robot.joints:
            if getattr(j, "limit", None) is not None and j.type in ("revolute", "prismatic"):
                self.jlim[j.name] = (float(j.limit.lower), float(j.limit.upper))

        self.lnames = self._chain(CFG_L)
        self.rnames = self._chain(CFG_R)
        self.nl = len(self.lnames)
        self.nr = len(self.rnames)

        self.lj = np.zeros(self.nl)
        self.rj = np.zeros(self.nr)

        # Home pose, same construction as view_quest: the solver wants URDF radians
        # in chain order (first 7) for reset(); the display wants one value per
        # chain joint. cfg.home is in motor space, so q2urdf first.
        self.home_reset_l, self.home_arr_l = self._home_pose(CFG_L, self.lnames, self.nl)
        self.home_reset_r, self.home_arr_r = self._home_pose(CFG_R, self.rnames, self.nr)
        self.homing = False
        self.home_t = 0.0
        self.home_start_l = self.lj.copy()
        self.home_start_r = self.rj.copy()
        self.prev_rb = False
        self.prev_lb = False     # Y: toggles trajectory recording
        self.recording = False
        self.traj = []
        self.rec_t0 = 0.0
        self._home_ik()          # start at home, like view_quest does
        self.cl_err = [0.0] * N_CAND
        self.cr_err = [0.0] * N_CAND
        self.cl_lab = [''] * N_CAND
        self.cr_lab = [''] * N_CAND
        self.n_l = N_CAND
        self.n_r = N_CAND
        self.cl = [np.zeros(self.nl) for _ in range(N_CAND)]
        self.cr = [np.zeros(self.nr) for _ in range(N_CAND)]
        self.sel_l = 0
        self.sel_r = 0
        self.frozen_l = False
        self.frozen_r = False
        self.ref_h = None          # operator head plane, grabbed on the first good frame

        self.left_goal = self.server.scene.add_icosphere("/left_goal", radius=0.03, color=(255, 100, 100))
        self.right_goal = self.server.scene.add_icosphere("/right_goal", radius=0.03, color=(100, 100, 255))
        self.head_marker = self.server.scene.add_icosphere("/head", radius=0.05, color=(100, 255, 100))
        self.left_frame = self.server.scene.add_frame("/left_frame", axes_length=0.1, axes_radius=0.005)
        self.right_frame = self.server.scene.add_frame("/right_frame", axes_length=0.1, axes_radius=0.005)
        self.head_frame = self.server.scene.add_frame("/head_frame", axes_length=0.15, axes_radius=0.008)
        self.server.scene.add_grid("/grid", width=2.0, height=2.0, cell_size=0.1, position=(0.0, 0.0, -0.01))

        self.collision_handles = add_collision_shapes(self.server, self.urdf_ref)
        print(f"Collision shapes from URDF: {len(self.collision_handles)}")

        with self.server.gui.add_folder("Status"):
            self.sel_l_label = self.server.gui.add_text("Left", initial_value="solving", disabled=True)
            self.sel_r_label = self.server.gui.add_text("Right", initial_value="solving", disabled=True)
            self.cost_label = self.server.gui.add_text("Costs", initial_value="", disabled=True)
            self.rate_label = self.server.gui.add_text("Rate", initial_value="-- Hz", disabled=True)
            self.rec_label = self.server.gui.add_text("Record", initial_value="idle (Y)", disabled=True)

        gui_home = self.server.gui.add_button("reset IK to home")

        @gui_home.on_click
        def _(_) -> None:
            self._home_ik(animate=True)

        show_col = self.server.gui.add_checkbox("Show collision geometry", initial_value=True)

        @show_col.on_update
        def _(_) -> None:
            for h in self.collision_handles:
                h.visible = show_col.value

        self.freeze_prev_l = np.zeros(self.nl)
        self.freeze_prev_r = np.zeros(self.nr)
        self.prev_la = False
        self.prev_ra = False
        self.prev_lt = False
        self.prev_rt = False
        self.last_t = time.perf_counter()
        self.last_lpos = np.zeros(3)
        self.last_lquat = np.zeros(4)
        self.last_rpos = np.zeros(3)
        self.last_rquat = np.zeros(4)

        print(f"Quest IK ready: http://{socket.gethostname()}.local:8080")
        print("Hold A = freeze | Trigger = cycle | Release A = save")

    def _home_pose(self, cfg, names, n):
        """(solver-space home for ik.reset(), display array in chain order)."""
        hu = cfg.q2urdf(np.asarray(cfg.home, dtype=np.float64))
        jn = list(cfg.joint_names)
        vals = [float(hu[jn.index(j)]) for j in names if j in jn]
        arr = np.zeros(n)
        arr[:len(vals)] = vals
        return vals[:len(cfg.ik._starting_config)], arr

    def _home_ik(self, animate=False):
        """Reset both solvers' memory to home (view_quest's home_ik). Clears the
        warm-start state, so a solver stuck in an awkward branch starts fresh."""
        CFG_L.ik.reset(self.home_reset_l)
        CFG_R.ik.reset(self.home_reset_r)
        if animate:
            self.home_start_l = self.lj.copy()
            self.home_start_r = self.rj.copy()
            self.home_t = time.perf_counter()
            self.homing = True
        else:
            self.lj = self.home_arr_l.copy()
            self.rj = self.home_arr_r.copy()
            self.homing = False
        print("[view_ik] IK memory reset to HOME", flush=True)

    def _chain(self, cfg):
        jd = {}
        for j in self.urdf_ref.robot.joints:
            if j.type in ['revolute', 'prismatic', 'continuous'] and j.name in cfg.joint_names:
                jd[j.name] = {'p': j.parent, 'c': j.child}
        children = {v['c'] for v in jd.values()}
        parents = {v['p'] for v in jd.values()}
        roots = parents - children
        root = list(roots)[0] if roots else 'main_extrusion_eu4040'
        chain, cur = [], root
        while True:
            found = False
            for jn, ji in jd.items():
                if ji['p'] == cur and jn not in chain:
                    chain.append(jn)
                    cur = ji['c']
                    found = True
                    break
            if not found:
                break
        return chain

    def _set_cfg(self, cfg, lj, rj):
        for i, n in enumerate(self.lnames):
            if i < len(lj):
                cfg[n] = lj[i]
        for i, n in enumerate(self.rnames):
            if i < len(rj):
                cfg[n] = rj[i]
        return np.array([cfg.get(n, 0.0) for n in self.all_joints])

    def _compute_cost(self, side, joints, prev_output):
        ik = CFG_L.ik if side == "left" else CFG_R.ik
        eps, k_gain, cw = _gains(ik)
        nominal = list(ik._nominal_config)
        n = min(len(joints), len(nominal), len(cw))
        center = sum(cw[i] * (joints[i] - nominal[i])**2 for i in range(n))
        cont = sum((joints[i] - prev_output[i])**2 for i in range(n))
        total = eps * k_gain * center + LAMBDA_CONT_SCALE * eps * cont
        return center, cont, total

    def _format_costs(self, side):
        cands = self.cl if side == "left" else self.cr
        prev = self.freeze_prev_l if side == "left" else self.freeze_prev_r
        sel = self.sel_l if side == "left" else self.sel_r
        lines = [f"{side.upper()}:"]
        for ci in range(N_CAND):
            cc, co, tot = self._compute_cost(side, cands[ci], prev)
            marker = " <--" if ci == sel else ""
            e = (self.cl_err if side == "left" else self.cr_err)[ci]
            lb = (self.cl_lab if side == "left" else self.cr_lab)[ci]
            if not lb:
                continue
            lines.append(f"  {lb:5s} err={e:6.1f}mm ctr={cc:.4f} cont={co:.4f} tot={tot:.4f}{marker}")
        return "\n".join(lines)

    def _update_cost_label(self):
        parts = []
        if self.frozen_l:
            s = self._format_costs("left")
            parts.append(s)
            print(s)
        if self.frozen_r:
            s = self._format_costs("right")
            parts.append(s)
            print(s)
        self.cost_label.value = "\n".join(parts)

    def _limits(self, names, dof):
        lo, hi = [], []
        for nm in names[:dof]:
            a, b = self.jlim.get(nm, (-2.0, 2.0))
            lo.append(a); hi.append(b)
        return np.array(lo), np.array(hi)

    def _solve_all_cands(self, side, pos, quat):
        """One candidate per QP multi-start, nominal-perturbed the same way the
        QP perturbs its own starts. Deterministic: no sampling, no dedup, no
        error tiers -- slot i is always QP basin i, so cycling compares like
        with like and a basin that disappears is information, not noise."""
        ik = CFG_L.ik if side == "left" else CFG_R.ik
        cands = self.cl if side == "left" else self.cr
        errs = self.cl_err if side == "left" else self.cr_err
        labs = self.cl_lab if side == "left" else self.cr_lab
        n = self.nl if side == "left" else self.nr
        base = list(ik._nominal_config)

        for ci in range(N_CAND):
            errs[ci] = float("inf")
            labs[ci] = ""

        for ci, (lab, jidx, delta) in enumerate(QP_PERTURBATIONS):
            if ci >= N_CAND:
                break
            nc = list(base)
            if jidx < len(nc):
                # No mirroring for the right arm: the QP applies this delta to
                # its own start with no sign flip, and both arms share a nominal.
                nc[jidx] += delta
            try:
                r = ik.solve_with_nominal(pos.tolist(), quat.tolist(), nc)
                r = np.array(r) if r is not None else None
            except Exception:
                r = None
            if r is None:
                continue
            err = 1000.0 * float(np.linalg.norm(np.asarray(ik.fk(list(r))[0]) - pos))
            cands[ci] = pad(r, n)
            errs[ci] = err
            labs[ci] = lab if err <= CAND_MAX_ERR else f"{lab}!"

        shown = sum(1 for ci in range(N_CAND) if labs[ci])
        spread = 0.0
        live = [cands[ci] for ci in range(N_CAND) if labs[ci]]
        if len(live) > 1:
            A = np.array(live)
            spread = float(np.abs(A - A.mean(axis=0)).max())
        print("[view_ik] %s basins: %d/%d | err %s | spread %.3f rad | j3 %s"
              % (side, shown, len(QP_PERTURBATIONS),
                 np.round([errs[ci] for ci in range(N_CAND) if labs[ci]], 1),
                 spread,
                 np.round([cands[ci][3] for ci in range(N_CAND) if labs[ci]], 2)))

        cnt = max(1, shown)
        if side == "left":
            self.n_l = cnt
        else:
            self.n_r = cnt

        try:
            ik.set_nominal(base)
        except Exception:
            pass
        try:
            ik.reset(list(np.asarray(self.lj if side == "left" else self.rj)[:len(base)]))
        except Exception:
            pass

    def _show_all_cands(self):
        # Only count a side that is actually frozen. n_l/n_r start at N_CAND, so a
        # left-only freeze would otherwise show N_CAND ghosts -- the extra slots
        # holding stale zero poses, which render as a pile of collapsed arms.
        shown = max(self.n_l if self.frozen_l else 0,
                    self.n_r if self.frozen_r else 0)
        for ci in range(N_CAND):
            if ci >= shown:
                self.cand_vu[ci].show_visual = False
                continue
            arr = self._set_cfg(self.cand_cfg[ci], self.cl[ci], self.cr[ci])
            self.cand_uobj[ci].update_cfg(arr)
            self.cand_vu[ci].update_cfg(arr)
            self.cand_vu[ci].show_visual = True

    def _hide_all_cands(self):
        for ci in range(N_CAND):
            self.cand_vu[ci].show_visual = False

    def _update_green(self):
        arr = self._set_cfg(self.sel_cfg, self.lj, self.rj)
        self.sel_uobj.update_cfg(arr)
        self.sel_vu.update_cfg(arr)

    def _record_frame(self, lpos, lquat, rpos, rquat):
        """One trajectory sample: the exact ik.solve() inputs plus what came out,
        so the run can be replayed off-robot."""
        def ee_err(cfg, joints, target):
            try:
                fk = np.asarray(cfg.ik.fk(list(np.asarray(joints, dtype=float)))[0])
                return round(1000.0 * float(np.linalg.norm(fk - target)), 3)
            except Exception:
                return None

        self.traj.append({
            "t": round(time.perf_counter() - self.rec_t0, 4),
            "left": {
                "pos": [round(float(v), 6) for v in lpos],
                "quat": [round(float(v), 6) for v in lquat],
                "joints": [round(float(v), 6) for v in self.lj],
                "frozen": bool(self.frozen_l),
                "ee_err_mm": ee_err(CFG_L, self.lj[:self.nl], np.asarray(lpos, dtype=float)),
            },
            "right": {
                "pos": [round(float(v), 6) for v in rpos],
                "quat": [round(float(v), 6) for v in rquat],
                "joints": [round(float(v), 6) for v in self.rj],
                "frozen": bool(self.frozen_r),
                "ee_err_mm": ee_err(CFG_R, self.rj[:self.nr], np.asarray(rpos, dtype=float)),
            },
        })
        if len(self.traj) % 25 == 0:
            self.rec_label.value = "REC " + str(len(self.traj))

    def _save_traj(self):
        """Stop recording and write the run out. Overwrites: one run per file, so
        a bad take is just re-recorded rather than appended to."""
        self.recording = False
        n = len(self.traj)
        dur = self.traj[-1]["t"] if n else 0.0
        out = {
            "recorded": time.time(),
            "frames": n,
            "duration_s": round(dur, 3),
            "hz": round(n / dur, 1) if dur > 0 else None,
            "nominal": {
                "left": list(CFG_L.ik._nominal_config),
                "right": list(CFG_R.ik._nominal_config),
            },
            "centering_weights": list(CFG_L.ik._centering_weights),
            "traj": self.traj,
        }
        try:
            with open(TRAJ_PATH, "w") as f:
                json.dump(out, f)
            self.rec_label.value = "saved " + str(n)
            print("[view_ik] wrote %d frames (%.1fs) -> %s" % (n, dur, TRAJ_PATH), flush=True)
        except Exception as e:
            self.rec_label.value = "SAVE FAILED"
            print("[view_ik] trajectory save failed: %s" % e, flush=True)

    def _save(self, side, pos, quat):
        sel = self.sel_l if side == "left" else self.sel_r
        cands = self.cl if side == "left" else self.cr
        labs = self.cl_lab if side == "left" else self.cr_lab
        errs = self.cl_err if side == "left" else self.cr_err
        cnt = self.n_l if side == "left" else self.n_r
        ik = CFG_L.ik if side == "left" else CFG_R.ik
        alternatives = []
        for ci in range(cnt):
            if ci != sel and labs[ci]:
                alternatives.append({"label": labs[ci], "joints": cands[ci].tolist()})
        entry = {
            "timestamp": time.time(),
            "side": side,
            "target": {"pos": pos.tolist(), "quat": quat.tolist()},
            "selected": {"label": labs[sel], "joints": cands[sel].tolist(),
                         "ee_err_mm": round(errs[sel], 2)},
            "alternatives": alternatives,
        }
        try:
            with open(PREF_PATH, "r") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = []
        data.append(entry)
        with open(PREF_PATH, "w") as f:
            json.dump(data, f, indent=2)
        print(f"Saved {side}: {labs[sel]} err={errs[sel]:.1f}mm (#{len(data)})")

    def run(self):
        with Reader("quest.controllers") as rd:
            while True:
                if rd.ready():
                    lpose = np.array(rd.data['left_pose'])
                    rpose = np.array(rd.data['right_pose'])
                    T_head = np.array(rd.data['T_head'])
                    la = float(rd.data['left_a'])
                    ra = float(rd.data['right_a'])
                    rb = bool(rd.data['right_b'])
                    lb = bool(rd.data['left_b'])
                    lt = float(rd.data['left_trigger'])
                    rt = float(rd.data['right_trigger'])

                    lmid, lquat = lpose[:3], lpose[3:]
                    rmid, rquat = rpose[:3], rpose[3:]
                    hpos = T_head[:3, 3]
                    head_h = float(hpos[2])
                    if self.ref_h is None and 0.4 < head_h < 2.5:
                        self.ref_h = head_h
                        print(f"[view_ik] HEIGHT PLANE SET @ {head_h:.3f} m", flush=True)
                    ref_h = self.ref_h if self.ref_h is not None else head_h
                    # Same convention as view_quest: keep head-relative XY, remap the
                    # operator's head plane onto the robot's shoulder height. Solving on
                    # the raw pose puts targets at the operator's absolute height instead.
                    lpos = np.array([lmid[0], lmid[1], lmid[2] - ref_h + ROBOT_REF_DEFAULT])
                    rpos = np.array([rmid[0], rmid[1], rmid[2] - ref_h + ROBOT_REF_DEFAULT])
                    hquat = rmat_to_quat(T_head[:3, :3])

                    self.left_goal.position = tuple(lpos)
                    self.right_goal.position = tuple(rpos)
                    self.head_marker.position = tuple(hpos)
                    self.left_frame.position = tuple(lpos)
                    self.left_frame.wxyz = tuple(xyzw_to_wxyz(lquat))
                    self.right_frame.position = tuple(rpos)
                    self.right_frame.wxyz = tuple(xyzw_to_wxyz(rquat))
                    self.head_frame.position = tuple(hpos)
                    self.head_frame.wxyz = tuple(xyzw_to_wxyz(hquat))

                    la_held = la > 0.5
                    ra_held = ra > 0.5

                    if la_held and not self.prev_la:
                        self.frozen_l = True
                        self.sel_l = 0
                        self.freeze_prev_l = self.lj.copy()
                        self.last_lpos = lpos.copy()
                        self.last_lquat = lquat.copy()
                        self._solve_all_cands("left", lpos, lquat)
                        self._show_all_cands()
                        self.lj = self.cl[0].copy()
                        self.sel_l_label.value = f"SELECT [{self.sel_l}] {self.cl_lab[self.sel_l]} ({self.cl_err[self.sel_l]:.0f}mm)"
                        self._update_cost_label()

                    if self.frozen_l:
                        lt_p = lt > 0.8
                        if lt_p and not self.prev_lt:
                            self.sel_l = (self.sel_l + 1) % self.n_l
                            self.lj = self.cl[self.sel_l].copy()
                            self.sel_l_label.value = f"SELECT [{self.sel_l}] {self.cl_lab[self.sel_l]} ({self.cl_err[self.sel_l]:.0f}mm)"
                            self._update_cost_label()
                        self.prev_lt = lt_p

                    if not la_held and self.prev_la and self.frozen_l:
                        self._save("left", self.last_lpos, self.last_lquat)
                        self.frozen_l = False
                        self._hide_all_cands()
                        self.sel_l_label.value = f"saved {self.cl_lab[self.sel_l]}"
                        self.cost_label.value = ""

                    if ra_held and not self.prev_ra:
                        self.frozen_r = True
                        self.sel_r = 0
                        self.freeze_prev_r = self.rj.copy()
                        self.last_rpos = rpos.copy()
                        self.last_rquat = rquat.copy()
                        self._solve_all_cands("right", rpos, rquat)
                        self._show_all_cands()
                        self.rj = self.cr[0].copy()
                        self.sel_r_label.value = f"SELECT [{self.sel_r}] {self.cr_lab[self.sel_r]} ({self.cr_err[self.sel_r]:.0f}mm)"
                        self._update_cost_label()

                    if self.frozen_r:
                        rt_p = rt > 0.8
                        if rt_p and not self.prev_rt:
                            self.sel_r = (self.sel_r + 1) % self.n_r
                            self.rj = self.cr[self.sel_r].copy()
                            self.sel_r_label.value = f"SELECT [{self.sel_r}] {self.cr_lab[self.sel_r]} ({self.cr_err[self.sel_r]:.0f}mm)"
                            self._update_cost_label()
                        self.prev_rt = rt_p

                    if not ra_held and self.prev_ra and self.frozen_r:
                        self._save("right", self.last_rpos, self.last_rquat)
                        self.frozen_r = False
                        self._hide_all_cands()
                        self.sel_r_label.value = f"saved {self.cr_lab[self.sel_r]}"
                        self.cost_label.value = ""

                    self.prev_la = la_held
                    self.prev_ra = ra_held

                    if self.prev_rb and not rb and not (self.frozen_l or self.frozen_r):
                        self._home_ik(animate=True)   # plain B tap
                    self.prev_rb = rb

                    # Y toggles recording. Captured AFTER this frame's solve, at
                    # the bottom of the loop, so joints match the target.
                    if lb and not self.prev_lb:
                        if self.recording:
                            self._save_traj()
                        else:
                            self.traj = []
                            self.rec_t0 = time.perf_counter()
                            self.recording = True
                            self.rec_label.value = "REC 0"
                            print("[view_ik] recording trajectory (Y to stop)", flush=True)
                    self.prev_lb = lb


                    if self.homing:
                        a = min((time.perf_counter() - self.home_t) / HOME_DURATION, 1.0)
                        self.lj = (1.0 - a) * self.home_start_l + a * self.home_arr_l
                        self.rj = (1.0 - a) * self.home_start_r + a * self.home_arr_r
                        if a >= 1.0:
                            self.homing = False

                    if not self.frozen_l and not self.homing:
                        try:
                            rl = CFG_L.ik.solve(lpos.tolist(), lquat.tolist())
                            rl = pad(np.array(rl), self.nl)
                            if rl is not None:
                                self.lj = rl
                        except Exception:
                            pass

                    if not self.frozen_r and not self.homing:
                        try:
                            rr = CFG_R.ik.solve(rpos.tolist(), rquat.tolist())
                            rr = pad(np.array(rr), self.nr)
                            if rr is not None:
                                self.rj = rr
                        except Exception:
                            pass

                    if self.recording:
                        self._record_frame(lpos, lquat, rpos, rquat)

                    self._update_green()

                    if self.frozen_l or self.frozen_r:
                        _n = max(self.n_l if self.frozen_l else 0,
                                 self.n_r if self.frozen_r else 0)
                        for ci in range(_n):
                            arr = self._set_cfg(self.cand_cfg[ci], self.cl[ci], self.cr[ci])
                            self.cand_uobj[ci].update_cfg(arr)
                            self.cand_vu[ci].update_cfg(arr)

                    now = time.perf_counter()
                    dt = now - self.last_t
                    self.last_t = now
                    self.rate_label.value = f"{1.0/dt:.0f} Hz" if dt > 0 else "-- Hz"


if __name__ == "__main__":
    QuestIKVisualizer().run()

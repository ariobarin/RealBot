"""Arm homing and parking: waypoint splines, staged torque enable, shutdown park.

Joint-space, not IK: relaxed-IK leaves the links between base and EE unconstrained, so an
IK-planned home pressed table-rested arms into the table. TODO: move to the arm daemon.
"""
import time

import numpy as np

from .quat import quat_forward_z, quat_mul, slerp


STAGED_J01_RAMP_S = 1.75        # stage 1: J0 lift + J1/J2 swing
STAGED_WP_RAMP_S = 1.75         # stage 2: ramp to the first waypoint
STAGED_J0_BACKOFF_TURNS = 0.05  # below the top; the hardstop stalls J0 into its current limit
STAGED_TAIL_SEG_S = 0.9         # startup-only override of cfg.startup_seg_durations
STAGED_TAIL_SETTLE_S = 0.25
STAGED_FLUSH_S = 0.1
STAGED_ENABLE_GAP_S = 0.25      # state silence this long = the daemon's mode-switch stall
STAGED_ENABLE_MARGIN_S = 0.15
STAGED_ENABLE_TIMEOUT_S = 1.5
STAGED_STILL_WINDOW_S = 0.25    # limp joints must hang still this long before the flush
STAGED_STILL_EPS_TURNS = 0.025  # ~9 deg
STAGED_STILL_TIMEOUT_S = 0.5

J0_PARK_DOWN_TURNS = 0.7        # extra descent before torque off; "down" is per-arm
J0_PARK_DOWN_SPEED = 0.4
PARK_STRAIGHTEN_S = 2.0         # bent -> hanging, or gravity swings the arm into the table
PARK_RETREAT_M = 0.1            # back the EE off before J0 drops, to clear a table edge
PARK_RETREAT_DOWN_M = 0.05
PARK_RETREAT_SPEED = 0.10


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
    """Catmull-Rom position at ``u`` in [0, n]; clamped end tangents, so it rests at the ends."""
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
    """Spline parameter at elapsed time ``t``: segment i spans [i, i+1] over ``durs[i]`` s."""
    if t <= 0.0:
        return 0.0
    acc = 0.0
    for i, d in enumerate(durs):
        if t < acc + d:
            return i + (t - acc) / d
        acc += d
    return float(len(durs))


def _command(arm, t):
    """Commanded pose for ``arm`` at elapsed time ``t`` seconds."""
    return _catmull_rom(arm["path"], _u_of_t(arm["durs"], t))


def quat_point_forward(quat):
    """Rotate an EE quat by the minimal world rotation putting its gripper axis on +x."""
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
    out = quat_mul(r, quat)
    return out / np.linalg.norm(out)


def smoothstep(s):
    s = min(max(s, 0.0), 1.0)
    return s * s * (3.0 - 2.0 * s)


def smootherstep(s):
    """Quintic ease: zero velocity and acceleration at both ends, for joints that jerk."""
    s = min(max(s, 0.0), 1.0)
    return s * s * s * (s * (6.0 * s - 15.0) + 10.0)


def straight_down_turns(cfg, turns):
    """The same pose with the arm joints (URDF 1..6) hanging straight down. Motor turns."""
    u = cfg.q2urdf(np.asarray(turns, dtype=np.float64).copy())
    u[1:cfg.dof - 1] = 0.0
    return cfg.urdf2q(u)


def staged_home_arms(specs, settle_s=0.4, rate_hz=200.0):
    """Home every arm in ``specs`` in two torque-enable stages, together, blocking.

    Each spec is a dict:
        cfg       Config for the arm.
        r_state   open Reader("<arm>.state").
        w_ctrl    open Writer("<arm>.ctrl", Type("arm_ctrl")).
        w_torque  open Writer("<arm>.torque", Type("arm_torque")).
        tau_mode  optional (dof,) bool array applied after homing. Default: position mode.

    Energizing mid-sequence is safe: the daemon reseeds a newly-enabled joint's command
    filter to its actual position on the OFF->ON transition, and ctrl is flushed there first.
    """
    # Only an OFF->ON transition reseeds the daemon's command filter, so disable first.
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
        # The first startup waypoint is always below home, so it gives the "down" sign.
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
        """Energize ``joints`` at their live pose. Reader.data refreshes on ready(), so poll."""
        new_js = [[j for j in joints if j < a["dof"] and not a["enabled"][j]]
                  for a in arms]

        # Wait out the swing: a still joint cannot go stale during the blind stall.
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

        # Spot the stall by its silence, then snap to the first post-gap sample: the catch.
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
        """Ramp ``joints`` to wp1 over ``duration`` s; every other joint holds."""
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
            f = smoothstep(t / max(duration, 1e-3))
            # J3 carries the forearm, so it eases in on a quintic instead of the cubic.
            f3 = smootherstep(t / max(duration, 1e-3))
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

    # STAGED_TAIL_* paces startup only; the dehome descent keeps the config values.
    home_arms([
        dict(a["spec"],
             waypoints=[*[np.asarray(w) for w in a["cfg"].startup_waypoints[1:]],
                        a["cfg"].home],
             seg_durations=STAGED_TAIL_SEG_S)
        for a in arms
    ], settle_s=min(settle_s, STAGED_TAIL_SETTLE_S), rate_hz=rate_hz)


def home_arms(specs, settle_s=0.4, rate_hz=200.0, disable_torque_after=False):
    """Drive every arm in ``specs`` to home through its waypoints, together, blocking.

    Each spec is a dict:
        cfg          Config for the arm.
        r_state      open Reader("<arm>.state").
        w_ctrl       open Writer("<arm>.ctrl", Type("arm_ctrl")).
        w_torque     open Writer("<arm>.torque", Type("arm_torque")).
        waypoints    optional (dof,) arrays to pass through, last one home. Default: [cfg.home].
        seg_durations  optional scalar or per-segment list of seconds. Default 2.5.
        tau_mode     optional (dof,) bool array applied after homing. Default: position mode.

    disable_torque_after  park for shutdown: append the IK retreat to the spline, straighten
        to hanging, ease J0 down, and cut torque so the arm rests hanging.
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
        # Appended to the spline, so the arm flows through the lowest waypoint without stopping.
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
            # Ease the gripper to +x by the end, roll preserved.
            quat0 = np.asarray(ee_quat, dtype=np.float64)
            quat_fwd = quat_point_forward(quat0)
            for i in range(1, n + 1):
                p = ee_pos + (i / n) * (goal - ee_pos)
                sol = cfg.ik.solve(list(p), list(slerp(quat0, quat_fwd, i / n)))
                if sol is not None and len(sol) >= 7:
                    q = np.asarray(sol[:7], dtype=np.float64)
                    # Pin it, or the solver's J0 centering raises the lift as the target frees.
                    q[0] = q_wp[0]
                else:
                    fails += 1  # hold the previous sample; the line continues
                samples.append(q.copy())
            if fails:
                print(f"[park] {cfg.ee_frame}: retreat {fails}/{n} IK misses", flush=True)
            # Mid + end only: the solver's noisy first samples never become commands.
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

        t0 = time.monotonic()
        while time.monotonic() - t0 < settle_s:
            for a in arms:
                with a["w_ctrl"].buf() as b:
                    b["pos"][:] = a["path"][-1]
            time.sleep(dt)

        if disable_torque_after and PARK_STRAIGHTEN_S:
            for a in arms:
                a["straight"] = straight_down_turns(
                    a["cfg"], np.array(a["path"][-1], dtype=np.float64))
            t0 = time.monotonic()
            while True:
                t = time.monotonic() - t0
                frac = smoothstep(t / PARK_STRAIGHTEN_S)
                for a in arms:
                    p0 = np.asarray(a["path"][-1], dtype=np.float64)
                    with a["w_ctrl"].buf() as b:
                        b["pos"][:] = (p0 + frac * (a["straight"] - p0)).astype(np.float32)
                        b["tau"][:] = np.zeros(a["dof"], dtype=np.float32)
                        b["alpha"] = 0.0
                if t >= PARK_STRAIGHTEN_S:
                    break
                time.sleep(dt)
            t0 = time.monotonic()
            while time.monotonic() - t0 < settle_s:
                for a in arms:
                    with a["w_ctrl"].buf() as b:
                        b["pos"][:] = a["straight"].astype(np.float32)
                time.sleep(dt)

        for a in arms:
            a["park_pose"] = np.array(a.get("straight", a["path"][-1]), dtype=np.float64)

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
                frac = smoothstep(t / ramp_s)
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
            t0 = time.monotonic()
            while time.monotonic() - t0 < settle_s:
                for a, (j0_from, j0_to) in zip(arms, targets):
                    pos = np.array(a["park_pose"], dtype=np.float32).copy()
                    pos[0] = j0_to
                    with a["w_ctrl"].buf() as b:
                        b["pos"][:] = pos
                time.sleep(dt)

        # Written ungated, so the scheduler can't drop it once keeptime is back.
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
    """Reverse of :func:`home_arms`, for shutdown: descend, straighten, lower J0, cut torque.

    Same spec dict as :func:`home_arms`. Pass the SAME startup ``waypoints``; they are reversed
    internally, so the arm flows current -> home -> ... -> first waypoint and powers off there.
    A per-segment ``seg_durations`` list is reversed to match. ``tau_mode`` is ignored.
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


class Trajectory:
    """A waypoint path sampled by elapsed time, for moving without blocking. Motor turns."""

    def __init__(self, start, waypoints, seg_durations=2.5):
        self._path = _build_path(start, waypoints)
        self._durs = _durations(seg_durations, len(self._path) - 1)
        self.total = sum(self._durs)

    def at(self, t):
        return _command({"path": self._path, "durs": self._durs}, t)

    def done(self, t):
        return t >= self.total


def park_trajectory(start, waypoints, seg_durations=2.5):
    """:class:`Trajectory` for the reverse path, so the arm ends where it should power off."""
    wps = list(waypoints)[::-1]
    if not np.isscalar(seg_durations):
        seg_durations = list(seg_durations)[::-1]
    return Trajectory(start, wps, seg_durations)

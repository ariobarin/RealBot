# /// script
# dependencies = [
#   "bbos",
#   "fastapi",
#   "uvicorn",
#   "wsproto",
#   "numpy",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Velocity-loop and twist feedforward tuner.

Like teleop.py (camera + WASD + velocity/Iq graphs), with live controls for the
ODrive velocity-loop gains and STM twist feedforward. Updates travel through
`drive.tune`, so no daemon restart is needed.

Examples:
    uv run tune.py --vel-gain 3.0 --vel-integrator-gain 0.5
    uv run tune.py --ff-tau-s 0.8 --ff-tau-k 0.6
    uv run tune.py --ff-yaw 0.5
    uv run tune.py --vel-gain 4.0                 # keep current integrator gain
    uv run tune.py --vel-gain 3.0 --no-ui         # apply + confirm, then exit

You can also tune both from the web UI while driving. ODrive gains are not saved
to NVM; feedforward overrides last until the base daemon restarts. Put final
defaults in the corresponding constants.py values.
"""
import argparse
import asyncio
import json
import math
import signal
import numpy as np
import threading
import queue
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

from bbos import Reader, Writer, Type, Config

WHEEL_VEL_COMBOS = {
    'w':    (0.20, 0.20),
    's':    (-0.20, -0.20),
    'a':    (-0.15, 0.15),
    'd':    (0.15, -0.15),
    'wa':   (0.05, 0.28),
    'wd':   (0.28, 0.05),
    'sa':   (-0.05, -0.28),
    'sd':   (-0.28, -0.05),
    '':     (0.0, 0.0),
}

CFG_C = Config("cam_head")
CFG_drive = Config("drive")
CFG_base = Config("base")
MPS_TO_RPS = 1.0 / (CFG_drive.wheel_diam * math.pi)


def _finite(x, default=None):
    """JSON-safe number: non-finite (nan/inf) -> default (null). A single NaN makes
    json.dumps emit the literal `NaN`, which is invalid JSON -> the browser's JSON.parse
    throws and the whole chart blanks. Scrub every telemetry value through this."""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default

ARGS = None
_stop = False

def _sigint(*_):
    global _stop
    _stop = True

signal.signal(signal.SIGINT, _sigint)

jpeg_queue = queue.Queue(maxsize=3)
cmd_queue = queue.Queue()
gain_queue = queue.Queue()

# Live ODrive and STM parameters carried by drive.tune.
TUNE_PARAMS = ("vel_gain", "vel_integrator_gain", "vel_lpf_bandwidth",
               "vel_slew_rate", "vel_deadband", "torque_slew_rate",
               "vel_ramp_rate", "ff_tau_s_nm", "ff_tau_k_nm", "ff_yaw_nm")
VEL_RAMP_DEFAULT = 50.0  # turn/s^2; high = ~instant, lower = smoother accel (0 would freeze)

_telemetry_lock = threading.Lock()
_latest_telemetry = None

# S-curve motion profile: constant-jerk (true symmetric S) on the linear command.
# reader_loop reads these; the websocket handler writes them.
_profile_lock = threading.Lock()
_profile = {
    'enabled': bool(CFG_base.twist_profile_enabled),
    'amax': float(CFG_base.twist_profile_amax),
    'jmax_acc': float(CFG_base.twist_profile_jmax_acc),
    'jmax_dec': float(CFG_base.twist_profile_jmax_dec),
}


def wheel_vels_to_twist(v_left, v_right):
    R = CFG_drive.robot_width * 0.5
    v = (v_left + v_right) / 2.0
    w = (v_right - v_left) / (2.0 * R)
    return v, w


def profile_step(v, a, target, amax, jmax_acc, jmax_dec, dt):
    """One tick of a constant-jerk (true S-curve) velocity profile toward `target`.
    Acceleration ramps toward +/-amax at rate <= jmax, so velocity is an S that
    reaches the target in finite time. Uses jmax_acc while speeding up and jmax_dec
    while slowing down (sign of velocity-error vs velocity), so accel/decel corner
    sharpness can differ. Returns (v, a)."""
    dv = target - v
    # Early-stop detection: if the built-up acceleration is now pushing velocity
    # AWAY from the (new) target -- released or reversed mid-ramp -- drop it so we
    # don't coast past the intended point. Without this, a quick tap overshoots.
    if dv * a < 0.0:
        a = 0.0
    speeding_up = (v == 0.0) or (dv * v >= 0.0)  # heading away from 0 => accelerating
    jmax = jmax_acc if speeding_up else jmax_dec
    # velocity still gained while ramping the current accel back to 0 at max jerk:
    dv_brake = 0.5 * a * abs(a) / jmax
    err = (target - v) - dv_brake
    if err > 0.0:
        a_ref = amax
    elif err < 0.0:
        a_ref = -amax
    else:
        a_ref = 0.0
    da = jmax * dt
    if a < a_ref:
        a = min(a + da, a_ref)
    elif a > a_ref:
        a = max(a - da, a_ref)
    a = max(-amax, min(amax, a))
    v_new = v + a * dt
    # anti-overshoot: once we reach/cross the target, settle exactly
    if (target - v) * (target - v_new) <= 0.0:
        return target, 0.0
    return v_new, a


def wait_current_gains(r_state, timeout=3.0):
    """Block until a drive.state sample with FINITE gains arrives; return
    (vel_gain, vel_integrator_gain), or None on timeout. Never returns a nan gain -- a
    transient nan here would be written straight back to the ODrive via drive.tune and
    latch into the live controller (blank graphs + erratic control)."""
    import time
    t0 = time.time()
    while time.time() - t0 < timeout and not _stop:
        if r_state.ready():
            g = r_state.data['gains']
            if math.isfinite(float(g[0])) and math.isfinite(float(g[1])):
                return float(g[0]), float(g[1])
        time.sleep(0.02)
    return None


def resolve_tune(current):
    """Build the tune dict from CLI flags and current/default values.
    Only vel_gain/vel_integrator_gain have a firmware readback (`current`); the
    other values use their CLI flag or configured default."""
    cur_vg, cur_vig = current if current else (4.0, 4.5)
    def pick(flag, default):
        v = getattr(ARGS, flag)
        return float(v) if v is not None else float(default)
    def ff(flag, default):
        return max(0.0, min(float(CFG_base.action_scale_nm), pick(flag, default)))
    return {
        'vel_gain':           float(ARGS.vel_gain) if ARGS.vel_gain is not None else cur_vg,
        'vel_integrator_gain': float(ARGS.vel_integrator_gain) if ARGS.vel_integrator_gain is not None else cur_vig,
        'vel_lpf_bandwidth':  pick('vel_lpf_bandwidth', 100.0),
        'vel_slew_rate':      pick('vel_slew_rate', 5.0),
        'vel_deadband':       pick('vel_deadband', 0.0),
        'torque_slew_rate':   pick('torque_slew_rate', 0.0),
        'vel_ramp_rate':      pick('vel_ramp_rate', VEL_RAMP_DEFAULT),
        'ff_tau_s_nm':        ff('ff_tau_s', CFG_base.ff_tau_s_nm),
        'ff_tau_k_nm':        ff('ff_tau_k', CFG_base.ff_tau_k_nm),
        'ff_yaw_nm':          ff('ff_yaw', CFG_base.ff_yaw_nm),
    }


def publish_tune(w_tune, vals, repeat=5):
    """Publish a tune command a few times to guarantee the daemon's reader catches it."""
    import time
    for _ in range(repeat):
        with w_tune.buf() as b:
            for name in TUNE_PARAMS:
                b[name] = np.float32(vals[name])
            with _profile_lock:
                b['prof_enabled'] = np.float32(
                    1.0 if _profile['enabled'] else 0.0)
                b['prof_amax'] = np.float32(_profile['amax'])
                b['prof_jmax_acc'] = np.float32(_profile['jmax_acc'])
                b['prof_jmax_dec'] = np.float32(_profile['jmax_dec'])
        time.sleep(0.05)


def apply_gains_cli():
    """--no-ui path: publish gains, confirm via readback, exit."""
    import time
    with Reader('drive.state') as r_state, \
         Writer('drive.tune', Type("drive_tune"), keeptime=False) as w_tune:
        cur = wait_current_gains(r_state)
        if cur is None:
            print("[tune] ERROR: no drive.state -- is the drive daemon running with the tune update?")
            return
        vals = resolve_tune(cur)
        print(f"[tune] current firmware gains: vel_gain={cur[0]:.4f} vel_integrator_gain={cur[1]:.4f}")
        print("[tune] requesting: " + " ".join(f"{k}={vals[k]:.4f}" for k in TUNE_PARAMS))
        publish_tune(w_tune, vals)
        time.sleep(0.4)
        conf = wait_current_gains(r_state)
        if conf:
            print(f"[tune] firmware now:           vel_gain={conf[0]:.4f} vel_integrator_gain={conf[1]:.4f}")
            ok = abs(conf[0] - vals['vel_gain']) < 1e-3 and abs(conf[1] - vals['vel_integrator_gain']) < 1e-3
            print("[tune] RESULT (gains):", "OK" if ok else "MISMATCH (check daemon log)")


def reader_loop():
    with Reader('camera.head.jpeg') as r_rgb, \
         Reader('drive.state') as r_state, \
         Writer('drive.ctrl', Type("drive_ctrl")) as w_ctrl, \
         Writer('drive.tune', Type("drive_tune"), keeptime=False) as w_tune:

        # Resolve the initial tune (CLI flags over current firmware). This dict is
        # (re)published to drive.tune every loop below -- a keeptime Writer discards
        # one-shot writes, so it must be written continuously.
        cur = wait_current_gains(r_state)
        desired = resolve_tune(cur)
        if cur is not None:
            print(f"[tune] current firmware gains: vel_gain={cur[0]:.4f} vel_integrator_gain={cur[1]:.4f}")
        print("[tune] applying: " + " ".join(f"{k}={desired[k]:.4f}" for k in TUNE_PARAMS), flush=True)

        cmd = {'keys': '', 'shift': False, 'gain': 1.0}
        # S-curve profile state: profiled LINEAR velocity and its accel
        pv, pva = 0.0, 0.0
        import time as _t
        last_t = _t.monotonic()
        while not _stop:
            if r_rgb.ready():
                jpeg = bytes(r_rgb.data['jpeg'])
                try:
                    jpeg_queue.put_nowait(jpeg)
                except:
                    try:
                        jpeg_queue.get_nowait()
                        jpeg_queue.put_nowait(jpeg)
                    except:
                        pass

            if r_state.ready():
                vel = r_state.data['vel']
                ctrl = r_state.data['ctrl']
                iq = r_state.data['iq']
                gains = r_state.data['gains']
                global _latest_telemetry
                with _telemetry_lock:
                    _latest_telemetry = {
                        # Display convention only; drive.state remains unchanged.
                        'vel': [_finite(-float(vel[0])),
                                _finite(-float(vel[1]))],
                        'setpoint': [_finite(float(ctrl[0]) * MPS_TO_RPS),
                                     _finite(float(ctrl[1]) * MPS_TO_RPS)],
                        'iq': [_finite(iq[0]), _finite(iq[1])],
                        'gains': [_finite(gains[0]), _finite(gains[1])],
                    }

            # live tune updates from the web UI: just update the desired dict
            try:
                g = gain_queue.get_nowait()
                desired = g
                print("[tune] UI set: " + " ".join(f"{k}={g[k]:.4f}" for k in TUNE_PARAMS), flush=True)
            except queue.Empty:
                pass

            # continuously (re)publish desired to drive.tune, gated to the topic's
            # period so it doesn't fight the drive.ctrl loop pacing. The daemon
            # only re-applies to the ODrive when the values actually change.
            if w_tune.ready():
                with _profile_lock:
                    pe, pam, pjacc, pjdec = _profile['enabled'], _profile['amax'], _profile['jmax_acc'], _profile['jmax_dec']
                with w_tune.buf() as b:
                    for name in TUNE_PARAMS:
                        b[name] = np.float32(desired[name])
                    # route the S-curve profile to the daemon (it owns the profiling now)
                    b['prof_enabled'] = np.float32(1.0 if pe else 0.0)
                    b['prof_amax'] = np.float32(pam)
                    b['prof_jmax_acc'] = np.float32(pjacc)
                    b['prof_jmax_dec'] = np.float32(pjdec)

            try:
                cmd = cmd_queue.get_nowait()
            except queue.Empty:
                pass

            keys = cmd.get('keys', '')
            shift = cmd.get('shift', False)
            gain = cmd.get('gain', 1.0)

            if keys in WHEEL_VEL_COMBOS:
                v_left, v_right = WHEEL_VEL_COMBOS[keys]
            else:
                v_left, v_right = 0.0, 0.0

            speed_mult = 1.0 if shift else 0.5
            total_mult = speed_mult * gain
            v_left *= total_mult
            v_right *= total_mult

            v_tgt, w_tgt = wheel_vels_to_twist(v_left, v_right)

            # Send the RAW twist target. The S-curve profile now lives in the drive
            # daemon (single source, applied to every teleop input); tune.py's profile
            # sliders are routed to it via drive.tune above.
            v_tgt = max(-CFG_drive.max_linear_vel, min(CFG_drive.max_linear_vel, v_tgt))
            with w_ctrl.buf() as buf:
                buf["twist"] = np.array([v_tgt, w_tgt], dtype=np.float32)


def server(port=8009):
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return HTMLResponse(HTML)

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        print("[tune] WebSocket client connected")
        loop = asyncio.get_event_loop()

        async def send_frames():
            while not _stop:
                try:
                    frame = await loop.run_in_executor(None, jpeg_queue.get, True, 0.04)
                    await websocket.send_bytes(frame)
                except queue.Empty:
                    pass

        async def send_telemetry():
            while not _stop:
                with _telemetry_lock:
                    tele = _latest_telemetry
                if tele is not None:
                    try:
                        await websocket.send_text(json.dumps({'type': 'telemetry', **tele}))
                    except Exception:
                        break
                await asyncio.sleep(1.0 / 30.0)

        send_task = asyncio.create_task(send_frames())
        tele_task = asyncio.create_task(send_telemetry())
        try:
            while not _stop:
                try:
                    message = await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
                    data = json.loads(message)
                    if "profile" in data:
                        p = data["profile"]
                        with _profile_lock:
                            if "enabled" in p:
                                _profile['enabled'] = bool(p['enabled'])
                            if "amax" in p:
                                _profile['amax'] = max(0.05, float(p['amax']))
                            if "jmax_acc" in p:
                                _profile['jmax_acc'] = max(0.1, float(p['jmax_acc']))
                            if "jmax_dec" in p:
                                _profile['jmax_dec'] = max(0.1, float(p['jmax_dec']))
                    elif "vel_gain" in data:
                        tune = {name: float(data.get(name, 0.0))
                                for name in TUNE_PARAMS}
                        for name in ("ff_tau_s_nm", "ff_tau_k_nm", "ff_yaw_nm"):
                            tune[name] = max(0.0, min(
                                float(CFG_base.action_scale_nm), tune[name]))
                        gain_queue.put(tune)
                    elif "keys" in data:
                        cmd_queue.put({
                            'keys': data['keys'],
                            'shift': data.get('shift', False),
                            'gain': data.get('gain', 1.0)
                        })
                except asyncio.TimeoutError:
                    pass
        except WebSocketDisconnect:
            print("[tune] WebSocket client disconnected")
            try:
                cmd_queue.put_nowait({'keys': '', 'shift': False, 'gain': 1.0})
            except queue.Full:
                pass
        except Exception as e:
            print(f"[tune] WebSocket error: {e}")
        finally:
            send_task.cancel()
            tele_task.cancel()

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="error",
                access_log=False, ws="wsproto")


HTML = """
<!doctype html><meta charset=utf-8>
<title>[bot] Drive Tuner</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Departure+Mono&display=swap">
<style>
:root {
  --text-primary: #000000;
  --text-muted: #5e5e5e;
  --text-faint: #9ca3af;
  --border: #e5e5e5;
  --border-medium: #d8d8d8;
  --bg: #ffffff;
  --surface: #fbfbfb;
  --primary: #222222;
  --brand-orange: #dc6100;
  --brand-blue: #2563eb;
  --grid: #eeeeee;
  --radius: 3px;
  --font-sans: "Helvetica Neue", Helvetica, Arial, sans-serif;
  --font-mono: "Departure Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text-primary); font-family: var(--font-sans); -webkit-font-smoothing: antialiased; overflow: hidden; }
.topbar { height: 48px; display: flex; align-items: center; gap: 10px; padding: 0 20px; border-bottom: 1px solid var(--border); background: var(--surface); }
.logo { font-family: var(--font-mono); font-size: 20px; font-weight: 500; letter-spacing: -0.01em; }
.page-title { font-size: 14px; font-weight: 500; color: var(--text-muted); }
.gains-live { font-family: var(--font-mono); font-size: 12px; color: var(--text-primary); }
.gains-live .k { color: var(--text-faint); }
.conn-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-faint); display: inline-block; margin-right: 6px; }
.conn-dot.live { background: #16a34a; }
.layout { display: flex; gap: 20px; height: calc(100vh - 48px); padding: 16px 20px; }
.col-main { display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; flex: 0 0 auto; }
.col-charts { display: flex; flex-direction: column; gap: 14px; flex: 1 1 auto; min-width: 340px; }
#feed { max-height: 380px; max-width: 560px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface); }
.info { display: flex; align-items: center; gap: 18px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 9px 16px; font-size: 12px; color: var(--text-muted); }
.info .stat { display: flex; align-items: baseline; gap: 6px; }
.info .label { text-transform: uppercase; letter-spacing: 0.04em; font-size: 11px; color: var(--text-faint); }
.speed-value { font-family: var(--font-mono); font-feature-settings: "tnum" on; font-size: 13px; color: var(--text-primary); }
.speed-mode { font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; color: var(--text-faint); }
.speed-mode.fast { color: var(--brand-orange); }
.controls-info { font-size: 11px; color: var(--text-faint); }
.keyboard-control { display: flex; flex-direction: column; align-items: center; gap: 6px; }
.key-row { display: flex; gap: 6px; }
.key { width: 40px; height: 40px; background: var(--surface); border: 1px solid var(--border-medium); border-radius: var(--radius); box-shadow: 0 1px 0 0 var(--border-medium); display: flex; align-items: center; justify-content: center; font-family: var(--font-mono); font-size: 13px; color: var(--text-muted); transition: all 120ms ease; user-select: none; }
.key.active { background: var(--primary); border-color: var(--primary); color: #fff; box-shadow: none; }
.key.shift-key { width: 60px; }
.gain-control { display: flex; align-items: center; gap: 10px; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 8px 14px; }
.gain-control label { color: var(--text-muted); font-size: 12px; }
.gain-control input[type="range"] { width: 110px; accent-color: var(--primary); }
.gain-control .gain-value { font-family: var(--font-mono); font-size: 12px; min-width: 40px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 12px 14px 10px; display: flex; flex-direction: column; gap: 8px; }
.card.chart-card { flex: 1 1 0; min-height: 0; }
.card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }
.legend { display: flex; gap: 14px; font-size: 11px; color: var(--text-muted); }
.legend .item { display: flex; align-items: center; gap: 6px; }
.legend .swatch { width: 18px; height: 0; border-top-width: 2px; border-top-style: solid; }
.legend .swatch.dashed { border-top-style: dashed; }
.legend .readout { font-family: var(--font-mono); font-feature-settings: "tnum" on; color: var(--text-primary); }
.chart-canvas-wrap { position: relative; flex: 1 1 auto; min-height: 110px; }
canvas.chart { width: 100%; height: 100%; display: block; }
.gain-row { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.gain-field { display: flex; align-items: center; gap: 8px; }
.gain-field label { font-size: 12px; color: var(--text-muted); font-family: var(--font-mono); }
.gain-field input[type="number"] { width: 90px; font-family: var(--font-mono); font-size: 13px; padding: 5px 8px; border: 1px solid var(--border-medium); border-radius: var(--radius); }
.gain-field .live { font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); }
.apply-btn { font-family: var(--font-mono); font-size: 12px; padding: 6px 14px; background: var(--primary); color: #fff; border: none; border-radius: var(--radius); cursor: pointer; }
.apply-btn:active { opacity: 0.8; }
.hint { font-size: 11px; color: var(--text-faint); }
</style>

<div class="topbar">
  <span class="logo">[bot]</span>
  <span class="page-title">Gain Tuner</span>
  <span class="gains-live" id="gains-live"><span class="k">firmware</span> vel_gain=<span id="live-vg">-</span> vel_i_gain=<span id="live-vig">-</span></span>
  <span style="flex:1"></span>
  <span style="font-size:11px;color:var(--text-faint)"><span id="conn-dot" class="conn-dot"></span><span id="conn-text">connecting…</span></span>
</div>
<div class="layout">
  <div class="col-main">
    <img id="feed" alt="Camera Feed">
    <div class="info">
      <div class="stat"><span class="label">Linear</span><span id="linear-speed" class="speed-value">0.00</span><span>m/s</span></div>
      <div class="stat"><span class="label">Angular</span><span id="angular-speed" class="speed-value">0.00</span><span>rad/s</span></div>
      <div id="speed-mode" class="speed-mode">Half Speed</div>
    </div>
    <div class="controls-info">WASD to move, hold Shift for full speed</div>
    <div class="gain-control">
      <label>Drive Speed</label>
      <input type="range" id="gain-slider" min="0.1" max="5.0" step="0.1" value="1.0">
      <span id="gain-value" class="gain-value">1.0x</span>
    </div>
    <div class="gain-control" style="flex-wrap:wrap">
      <label><input type="checkbox" id="prof-en" checked> S-curve (linear)</label>
      <span style="font-size:11px;color:var(--text-muted)">accel</span>
      <input type="range" id="prof-amax" min="0.2" max="6" step="0.1" value="2" style="width:90px">
      <span id="prof-amax-val" class="gain-value">2.0</span>
      <span style="font-size:11px;color:var(--text-muted)">jerk&#8593;</span>
      <input type="range" id="prof-jacc" min="1" max="40" step="0.5" value="8" style="width:90px">
      <span id="prof-jacc-val" class="gain-value">8.0</span>
      <span style="font-size:11px;color:var(--text-muted)">jerk&#8595;</span>
      <input type="range" id="prof-jdec" min="1" max="40" step="0.5" value="5" style="width:90px">
      <span id="prof-jdec-val" class="gain-value">5.0</span>
    </div>
    <div class="keyboard-control">
      <div class="key-row"><div class="key" id="key-w">W</div></div>
      <div class="key-row">
        <div class="key" id="key-a">A</div><div class="key" id="key-s">S</div><div class="key" id="key-d">D</div>
      </div>
      <div class="key-row" style="margin-top: 6px;"><div class="key shift-key" id="key-shift">Shift</div></div>
    </div>
  </div>

  <div class="col-charts">
    <div class="card">
      <div class="card-head"><span class="card-title">Velocity-Loop Gains &middot; live to firmware</span></div>
      <div class="gain-row">
        <div class="gain-field">
          <label>vel_gain</label>
          <input type="number" id="in-vg" step="0.1" min="0">
          <span class="live">now <span id="live-vg2">-</span></span>
        </div>
        <div class="gain-field">
          <label>vel_integrator_gain</label>
          <input type="number" id="in-vig" step="0.1" min="0">
          <span class="live">now <span id="live-vig2">-</span></span>
        </div>
        <div class="gain-field">
          <label>vel_slew_rate</label>
          <input type="number" id="in-vslew" step="5" min="0" value="5">
          <span class="live">turn/s&sup2;</span>
        </div>
        <div class="gain-field">
          <label>vel_lpf_bandwidth</label>
          <input type="number" id="in-lpf" step="5" min="0" value="100">
          <span class="live">1/s</span>
        </div>
        <div class="gain-field">
          <label>vel_deadband</label>
          <input type="number" id="in-deadband" step="0.02" min="0" value="0">
          <span class="live">turn/s</span>
        </div>
        <div class="gain-field">
          <label>torque_slew_rate</label>
          <input type="number" id="in-tslew" step="25" min="0" value="0">
          <span class="live">Nm/s</span>
        </div>
        <div class="gain-field">
          <label>vel_ramp_rate</label>
          <input type="number" id="in-vramp" step="5" min="1" value="50">
          <span class="live">turn/s&sup2; cmd (lower=smoother)</span>
        </div>
        <div class="gain-field">
          <label>ff_tau_s</label>
          <input type="number" id="in-ff-tau-s" step="0.05" min="0" max="6.5" value="0.8">
          <span class="live">Nm breakaway</span>
        </div>
        <div class="gain-field">
          <label>ff_tau_k</label>
          <input type="number" id="in-ff-tau-k" step="0.05" min="0" max="6.5" value="0.6">
          <span class="live">Nm moving</span>
        </div>
        <div class="gain-field">
          <label>ff_yaw</label>
          <input type="number" id="in-ff-yaw" step="0.05" min="0" max="6.5" value="0.5">
          <span class="live">Nm turn scrub</span>
        </div>
        <button class="apply-btn" id="apply-gains">Apply</button>
      </div>
      <div class="hint">Enter or Apply updates the live controller immediately. Feedforward and S-curve values are live STM settings and are not saved to NVM.</div>
    </div>

    <div class="card chart-card">
      <div class="card-head">
        <span class="card-title">Wheel Velocity &middot; turns/s</span>
        <div class="legend">
          <span class="item"><span class="swatch" style="border-top-color:var(--brand-orange)"></span>L meas <span id="ro-vel-l" class="readout">0.00</span></span>
          <span class="item"><span class="swatch dashed" style="border-top-color:var(--brand-orange)"></span>L set <span id="ro-sp-l" class="readout">0.00</span></span>
          <span class="item"><span class="swatch" style="border-top-color:var(--brand-blue)"></span>R meas <span id="ro-vel-r" class="readout">0.00</span></span>
          <span class="item"><span class="swatch dashed" style="border-top-color:var(--brand-blue)"></span>R set <span id="ro-sp-r" class="readout">0.00</span></span>
        </div>
      </div>
      <div class="chart-canvas-wrap"><canvas id="vel-chart" class="chart"></canvas></div>
    </div>

    <div class="card chart-card">
      <div class="card-head">
        <span class="card-title">Motor Current Iq &middot; A</span>
        <div class="legend">
          <span class="item"><span class="swatch" style="border-top-color:var(--brand-orange)"></span>Left <span id="ro-iq-l" class="readout">0.00</span></span>
          <span class="item"><span class="swatch" style="border-top-color:var(--brand-blue)"></span>Right <span id="ro-iq-r" class="readout">0.00</span></span>
        </div>
      </div>
      <div class="chart-canvas-wrap"><canvas id="iq-chart" class="chart"></canvas></div>
    </div>
  </div>
</div>

<script>
const ORANGE = "#dc6100", BLUE = "#2563eb", GRID = "#eeeeee", AXIS = "#c8c8c8", TXT = "#9ca3af";
const WINDOW_S = 10, MAX_POINTS = 1200;

class Chart {
  constructor(canvas, series, opts) {
    this.canvas = canvas; this.ctx = canvas.getContext("2d");
    this.series = series; this.opts = opts || {};
    this.t = []; this.data = series.map(() => []);
    this.dpr = window.devicePixelRatio || 1;
    this._resize(); new ResizeObserver(() => this._resize()).observe(canvas);
  }
  _resize() { const r = this.canvas.getBoundingClientRect(); this.canvas.width = Math.max(1, Math.round(r.width*this.dpr)); this.canvas.height = Math.max(1, Math.round(r.height*this.dpr)); }
  push(tMs, values) { this.t.push(tMs); for (let i=0;i<this.series.length;i++) this.data[i].push(values[i]); while (this.t.length>MAX_POINTS){ this.t.shift(); for (const d of this.data) d.shift(); } }
  draw(nowMs) {
    const ctx=this.ctx, W=this.canvas.width, H=this.canvas.height, dpr=this.dpr;
    ctx.clearRect(0,0,W,H);
    const padL=42*dpr, padR=8*dpr, padT=8*dpr, padB=16*dpr;
    const plotW=W-padL-padR, plotH=H-padT-padB;
    const tMax=nowMs, tMin=nowMs-WINDOW_S*1000;
    let lo=Infinity, hi=-Infinity;
    for (let s=0;s<this.data.length;s++){ const arr=this.data[s]; for (let i=0;i<arr.length;i++){ if(this.t[i]<tMin) continue; const v=arr[i]; if(v<lo)lo=v; if(v>hi)hi=v; } }
    if(!isFinite(lo)){ lo=-1; hi=1; }
    const minSpan=this.opts.minSpan||0.5;
    if(hi-lo<minSpan){ const c=(hi+lo)/2; lo=c-minSpan/2; hi=c+minSpan/2; }
    const pad=(hi-lo)*0.12; lo-=pad; hi+=pad;
    if(lo>0)lo=0; if(hi<0)hi=0;
    const x=t=>padL+((t-tMin)/(tMax-tMin))*plotW;
    const y=v=>padT+(1-(v-lo)/(hi-lo))*plotH;
    ctx.font=(10*dpr)+"px ui-monospace, monospace"; ctx.textBaseline="middle"; ctx.textAlign="right";
    const ticks=4;
    for (let i=0;i<=ticks;i++){ const v=lo+(hi-lo)*(i/ticks); const yy=y(v); ctx.strokeStyle=Math.abs(v)<1e-9?AXIS:GRID; ctx.lineWidth=1; ctx.beginPath(); ctx.moveTo(padL,yy); ctx.lineTo(W-padR,yy); ctx.stroke(); ctx.fillStyle=TXT; ctx.fillText(v.toFixed(2), padL-6*dpr, yy); }
    for (let s=0;s<this.series.length;s++){ const arr=this.data[s]; ctx.strokeStyle=this.series[s].color; ctx.lineWidth=(this.series[s].dashed?1.5:2)*dpr; ctx.setLineDash(this.series[s].dashed?[5*dpr,4*dpr]:[]); ctx.beginPath(); let started=false; for (let i=0;i<arr.length;i++){ if(this.t[i]<tMin) continue; const px=x(this.t[i]), py=y(arr[i]); if(!started){ctx.moveTo(px,py); started=true;} else ctx.lineTo(px,py);} ctx.stroke(); }
    ctx.setLineDash([]);
  }
}

const velChart = new Chart(document.getElementById("vel-chart"), [
  { color: ORANGE, dashed: false }, { color: ORANGE, dashed: true },
  { color: BLUE, dashed: false }, { color: BLUE, dashed: true },
], { minSpan: 0.5 });
const iqChart = new Chart(document.getElementById("iq-chart"), [
  { color: ORANGE, dashed: false }, { color: BLUE, dashed: false },
], { minSpan: 1.0 });

function fmt(v){ return (v>=0?" ":"")+v.toFixed(2); }
function fmtg(v){ return (v===null||v===undefined||isNaN(v))?"-":v.toFixed(3); }

const ws = new WebSocket((location.protocol==="https:"?"wss://":"ws://")+location.host+"/ws");
ws.binaryType = "arraybuffer";
const feedEl = document.getElementById("feed");
let prevUrl = null;
const connDot = document.getElementById("conn-dot"), connText = document.getElementById("conn-text");

// gain/tune input elements
const inVg = document.getElementById("in-vg"), inVig = document.getElementById("in-vig");
const inVslew = document.getElementById("in-vslew"), inLpf = document.getElementById("in-lpf");
const inDeadband = document.getElementById("in-deadband"), inTslew = document.getElementById("in-tslew");
const inVramp = document.getElementById("in-vramp");
const inFfTauS = document.getElementById("in-ff-tau-s"), inFfTauK = document.getElementById("in-ff-tau-k");
const inFfYaw = document.getElementById("in-ff-yaw");
const allInputs = [inVg, inVig, inVslew, inLpf, inDeadband, inTslew, inVramp, inFfTauS, inFfTauK, inFfYaw];
let vgFocused = false, vigFocused = false, gainsInit = false;
inVg.addEventListener("focus", ()=>vgFocused=true); inVg.addEventListener("blur", ()=>vgFocused=false);
inVig.addEventListener("focus", ()=>vigFocused=true); inVig.addEventListener("blur", ()=>vigFocused=false);

function applyGains(){
  const nz = v => { const x = parseFloat(v); return isNaN(x) ? 0 : x; };
  const msg = {
    vel_gain: parseFloat(inVg.value),
    vel_integrator_gain: parseFloat(inVig.value),
    vel_slew_rate: nz(inVslew.value),
    vel_lpf_bandwidth: nz(inLpf.value),
    vel_deadband: nz(inDeadband.value),
    torque_slew_rate: nz(inTslew.value),
    vel_ramp_rate: Math.max(1, nz(inVramp.value) || 50),  // 0 would freeze velocity
    ff_tau_s_nm: Math.max(0, nz(inFfTauS.value)),
    ff_tau_k_nm: Math.max(0, nz(inFfTauK.value)),
    ff_yaw_nm: Math.max(0, nz(inFfYaw.value)),
  };
  if (isNaN(msg.vel_gain) || isNaN(msg.vel_integrator_gain)) return;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}
document.getElementById("apply-gains").addEventListener("click", applyGains);
allInputs.forEach(el => el.addEventListener("keydown", e=>{ if(e.key==="Enter") applyGains(); }));

function setLiveGains(vg, vig){
  // Update the live firmware readouts continuously...
  document.getElementById("live-vg").textContent = fmtg(vg);
  document.getElementById("live-vig").textContent = fmtg(vig);
  document.getElementById("live-vg2").textContent = fmtg(vg);
  document.getElementById("live-vig2").textContent = fmtg(vig);
  // ...but only seed the editable input boxes ONCE, so telemetry never
  // clobbers what you've typed (that was the "Apply does nothing" bug).
  if (!gainsInit) { inVg.value = vg.toFixed(3); inVig.value = vig.toFixed(3); gainsInit = true; }
}

ws.onmessage = (e) => {
  if (e.data instanceof ArrayBuffer) {
    if (prevUrl) URL.revokeObjectURL(prevUrl);
    const blob = new Blob([e.data], { type: "image/jpeg" });
    prevUrl = URL.createObjectURL(blob); feedEl.src = prevUrl; return;
  }
  let msg; try { msg = JSON.parse(e.data); } catch(_) { return; }
  if (msg.type !== "telemetry") return;
  const now = performance.now();
  velChart.push(now, [msg.vel[0], msg.setpoint[0], msg.vel[1], msg.setpoint[1]]);
  iqChart.push(now, [msg.iq[0], msg.iq[1]]);
  document.getElementById("ro-vel-l").textContent = fmt(msg.vel[0]);
  document.getElementById("ro-sp-l").textContent  = fmt(msg.setpoint[0]);
  document.getElementById("ro-vel-r").textContent = fmt(msg.vel[1]);
  document.getElementById("ro-sp-r").textContent  = fmt(msg.setpoint[1]);
  document.getElementById("ro-iq-l").textContent  = fmt(msg.iq[0]);
  document.getElementById("ro-iq-r").textContent  = fmt(msg.iq[1]);
  if (msg.gains) setLiveGains(msg.gains[0], msg.gains[1]);
};
ws.onopen = () => { connDot.classList.add("live"); connText.textContent = "live"; };
ws.onclose = () => { connDot.classList.remove("live"); connText.textContent = "disconnected"; };

function animate(){ const now=performance.now(); velChart.draw(now); iqChart.draw(now); requestAnimationFrame(animate); }
requestAnimationFrame(animate);

const keys = { w:false, a:false, s:false, d:false };
let shiftHeld = false, gain = 1.0;
const gainSlider = document.getElementById("gain-slider"), gainValue = document.getElementById("gain-value");
gainSlider.addEventListener("input", () => { gain = parseFloat(gainSlider.value); gainValue.textContent = gain.toFixed(1)+"x"; updateKeyboardCommand(); });

// S-curve controls are published through drive.tune and applied on the STM.
const profEn = document.getElementById("prof-en");
const profAmax = document.getElementById("prof-amax"), profAmaxVal = document.getElementById("prof-amax-val");
const profJacc = document.getElementById("prof-jacc"), profJaccVal = document.getElementById("prof-jacc-val");
const profJdec = document.getElementById("prof-jdec"), profJdecVal = document.getElementById("prof-jdec-val");
function sendProfile(){
  if (ws && ws.readyState === WebSocket.OPEN)
    ws.send(JSON.stringify({ profile: { enabled: profEn.checked, amax: parseFloat(profAmax.value),
      jmax_acc: parseFloat(profJacc.value), jmax_dec: parseFloat(profJdec.value) } }));
}
profEn.addEventListener("change", sendProfile);
profAmax.addEventListener("input", () => { profAmaxVal.textContent = parseFloat(profAmax.value).toFixed(1); sendProfile(); });
profJacc.addEventListener("input", () => { profJaccVal.textContent = parseFloat(profJacc.value).toFixed(1); sendProfile(); });
profJdec.addEventListener("input", () => { profJdecVal.textContent = parseFloat(profJdec.value).toFixed(1); sendProfile(); });

function updateKeyboardCommand(){
  let combo=''; if(keys.w)combo+='w'; if(keys.s)combo+='s'; if(keys.a)combo+='a'; if(keys.d)combo+='d';
  const comboDisplay = { 'w':{lin:0.20,ang:0.00}, 's':{lin:-0.20,ang:0.00}, 'a':{lin:0.00,ang:0.92}, 'd':{lin:0.00,ang:-0.92}, 'wa':{lin:0.165,ang:0.70}, 'wd':{lin:0.165,ang:-0.70}, 'sa':{lin:-0.165,ang:-0.70}, 'sd':{lin:-0.165,ang:0.70}, '':{lin:0.00,ang:0.00} };
  const base = comboDisplay[combo] || comboDisplay[''];
  const totalMult = (shiftHeld?1.0:0.5) * gain;
  document.getElementById("linear-speed").textContent = (base.lin*totalMult).toFixed(2);
  document.getElementById("angular-speed").textContent = (base.ang*totalMult).toFixed(2);
  const modeEl = document.getElementById("speed-mode");
  if (shiftHeld){ modeEl.textContent="Full Speed"; modeEl.classList.add("fast"); } else { modeEl.textContent="Half Speed"; modeEl.classList.remove("fast"); }
  if (ws && ws.readyState===WebSocket.OPEN) ws.send(JSON.stringify({ keys: combo, shift: shiftHeld, gain: gain }));
}
function setKeyState(key,state){ const k=key.toLowerCase(); if(k in keys){ keys[k]=state; const el=document.getElementById("key-"+k); if(el) el.classList.toggle("active",state); updateKeyboardCommand(); } }
document.addEventListener("keydown",(e)=>{ if(e.target.tagName==="INPUT") return; if(e.repeat) return; if(e.key==="Shift"){ shiftHeld=true; document.getElementById("key-shift").classList.add("active"); updateKeyboardCommand(); return; } setKeyState(e.key,true); });
document.addEventListener("keyup",(e)=>{ if(e.target.tagName==="INPUT") return; if(e.key==="Shift"){ shiftHeld=false; document.getElementById("key-shift").classList.remove("active"); updateKeyboardCommand(); return; } setKeyState(e.key,false); });
window.addEventListener("blur",()=>{ Object.keys(keys).forEach(k=>setKeyState(k,false)); shiftHeld=false; document.getElementById("key-shift").classList.remove("active"); updateKeyboardCommand(); });
</script>
"""


def parse_args():
    p = argparse.ArgumentParser(
        description="Live ODrive velocity-loop and STM twist-feedforward tuner.")
    p.add_argument("--vel-gain", type=float, default=None,
                   help="controller.config.vel_gain (default: keep current firmware value)")
    p.add_argument("--vel-integrator-gain", type=float, default=None,
                   help="controller.config.vel_integrator_gain (default: keep current firmware value)")
    p.add_argument("--vel-lpf-bandwidth", type=float, default=None,
                   help="controller.config.vel_lpf_bandwidth [1/s], 0=off (default 0)")
    p.add_argument("--vel-slew-rate", type=float, default=None,
                   help="controller.config.vel_slew_rate [(turn/s)/s], 0=off (default 0)")
    p.add_argument("--vel-deadband", type=float, default=None,
                   help="controller.config.vel_deadband [turn/s], 0=off (default 0)")
    p.add_argument("--torque-slew-rate", type=float, default=None,
                   help="controller.config.torque_slew_rate [Nm/s], 0=off (default 0)")
    p.add_argument("--vel-ramp-rate", type=float, default=None,
                   help="controller.config.vel_ramp_rate [(turn/s)/s] velocity command slew (default 50; lower=smoother)")
    p.add_argument("--ff-tau-s", type=float, default=None,
                   help="twist breakaway feedforward torque [Nm] (default from base constants)")
    p.add_argument("--ff-tau-k", type=float, default=None,
                   help="twist moving feedforward torque [Nm] (default from base constants)")
    p.add_argument("--ff-yaw", type=float, default=None,
                   help="saturating turn-scrub feedforward torque [Nm] (default from base constants)")
    p.add_argument("--profile-accel", type=float, default=None,
                   help="S-curve max linear accel [m/s^2] (default 2.0)")
    p.add_argument("--profile-jerk-accel", type=float, default=None,
                   help="S-curve jerk while speeding up [m/s^3] (default 8.0)")
    p.add_argument("--profile-jerk-decel", type=float, default=None,
                   help="S-curve jerk while slowing down [m/s^3] (default 5.0)")
    p.add_argument("--no-profile", action="store_true", help="disable the S-curve motion profile")
    p.add_argument("--no-ui", action="store_true", help="apply tune, print confirmation, then exit")
    p.add_argument("--port", type=int, default=8009, help="web UI port (default 8009)")
    return p.parse_args()


def main():
    global ARGS
    ARGS = parse_args()

    # seed the S-curve profile from CLI flags
    with _profile_lock:
        _profile['enabled'] = not ARGS.no_profile
        if ARGS.profile_accel is not None:
            _profile['amax'] = max(0.05, float(ARGS.profile_accel))
        if ARGS.profile_jerk_accel is not None:
            _profile['jmax_acc'] = max(0.1, float(ARGS.profile_jerk_accel))
        if ARGS.profile_jerk_decel is not None:
            _profile['jmax_dec'] = max(0.1, float(ARGS.profile_jerk_decel))

    if ARGS.no_ui:
        apply_gains_cli()
        return

    reader_thread = threading.Thread(target=reader_loop, daemon=True)
    reader_thread.start()

    import socket
    print(f"[tune] Starting gain tuner on http://{socket.gethostname()}.local:{ARGS.port}")
    server(ARGS.port)

    global _stop
    _stop = True
    reader_thread.join()


if __name__ == "__main__":
    main()

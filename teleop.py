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

from drive_commands import WHEEL_VEL_COMBOS, wheel_vels_to_twist as _wheel_vels_to_twist

CFG_C = Config("cam_head")

_stop = False

def _sigint(*_):
    global _stop
    _stop = True

signal.signal(signal.SIGINT, _sigint)

jpeg_queue = queue.Queue(maxsize=3)
cmd_queue = queue.Queue()

CFG_drive = Config("drive")

# ctrl (velocity setpoint) is published by the drive daemon in the "mps" units
# consumed by set_speed_mps_*, which internally divides by (wheel_diam * pi) to
# get motor turns/s. Measured `vel` is already in motor turns/s, so we convert
# the setpoint the same way to overlay the two on one axis.
MPS_TO_RPS = 1.0 / (CFG_drive.wheel_diam * math.pi)

# Latest motor telemetry, refreshed by reader_loop, drained by the websocket.
_telemetry_lock = threading.Lock()
_latest_telemetry = None


def wheel_vels_to_twist(v_left, v_right):
    return _wheel_vels_to_twist(v_left, v_right, CFG_drive.robot_width)

def reader_loop():
    with Reader('camera.head.jpeg') as r_rgb, \
         Reader('drive.state') as r_state, \
         Writer('drive.ctrl', Type("drive_ctrl")) as w_ctrl:
        cmd = {'keys': '', 'shift': False, 'gain': 1.0}
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
                global _latest_telemetry
                with _telemetry_lock:
                    _latest_telemetry = {
                        'vel': [float(vel[0]), float(vel[1])],
                        'setpoint': [float(ctrl[0]) * MPS_TO_RPS,
                                     float(ctrl[1]) * MPS_TO_RPS],
                        'iq': [float(iq[0]), float(iq[1])],
                    }

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

            v, w = wheel_vels_to_twist(v_left, v_right)
            with w_ctrl.buf() as buf:
                buf["twist"] = np.array([v, w], dtype=np.float32)

def server(port=8008):
    app = FastAPI()

    @app.get("/", response_class=HTMLResponse)
    async def root():
        return HTMLResponse("""
<!doctype html><meta charset=utf-8>
<title>[bot] Teleop</title>
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
body {
  margin: 0;
  background: var(--bg);
  color: var(--text-primary);
  font-family: var(--font-sans);
  -webkit-font-smoothing: antialiased;
  overflow: hidden;
}
.topbar {
  height: 48px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 0 20px;
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.logo {
  font-family: var(--font-mono);
  font-size: 20px;
  font-weight: 500;
  letter-spacing: -0.01em;
  color: var(--text-primary);
}
.page-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--text-muted);
}
.layout {
  display: flex;
  gap: 20px;
  height: calc(100vh - 48px);
  padding: 16px 20px;
}
.col-main {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  flex: 0 0 auto;
}
.col-charts {
  display: flex;
  flex-direction: column;
  gap: 16px;
  flex: 1 1 auto;
  min-width: 320px;
  justify-content: center;
}
#feed {
  max-height: 420px;
  max-width: 620px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--surface);
}
.info {
  display: flex;
  align-items: center;
  gap: 20px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 18px;
  font-size: 12px;
  color: var(--text-muted);
}
.info .stat { display: flex; align-items: baseline; gap: 6px; }
.info .label { text-transform: uppercase; letter-spacing: 0.04em; font-size: 11px; color: var(--text-faint); }
.speed-value {
  font-family: var(--font-mono);
  font-feature-settings: "tnum" on, "lnum" on;
  font-size: 13px;
  color: var(--text-primary);
}
.speed-mode {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-faint);
}
.speed-mode.fast { color: var(--brand-orange); }
.controls-info { font-size: 11px; color: var(--text-faint); }
.keyboard-control {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}
.key-row { display: flex; gap: 6px; }
.key {
  width: 44px; height: 44px;
  background: var(--surface);
  border: 1px solid var(--border-medium);
  border-radius: var(--radius);
  box-shadow: 0 1px 0 0 var(--border-medium);
  display: flex; align-items: center; justify-content: center;
  font-family: var(--font-mono);
  font-size: 13px; color: var(--text-muted);
  transition: all 120ms ease; user-select: none;
}
.key.active {
  background: var(--primary);
  border-color: var(--primary);
  color: #ffffff;
  box-shadow: none;
}
.key.shift-key { width: 64px; }
.gain-control {
  display: flex; align-items: center; gap: 12px;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 16px;
}
.gain-control label { color: var(--text-muted); font-size: 12px; }
.gain-control input[type="range"] { width: 120px; accent-color: var(--primary); }
.gain-control .gain-value {
  font-family: var(--font-mono);
  font-feature-settings: "tnum" on;
  font-size: 12px; color: var(--text-primary); min-width: 42px;
}
.chart-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 12px 14px 10px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex: 1 1 0;
  min-height: 0;
}
.chart-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}
.chart-title {
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: var(--text-muted);
}
.legend { display: flex; gap: 14px; font-size: 11px; color: var(--text-muted); }
.legend .item { display: flex; align-items: center; gap: 6px; }
.legend .swatch { width: 18px; height: 0; border-top-width: 2px; border-top-style: solid; }
.legend .swatch.dashed { border-top-style: dashed; }
.legend .readout {
  font-family: var(--font-mono);
  font-feature-settings: "tnum" on;
  color: var(--text-primary);
}
.chart-canvas-wrap { position: relative; flex: 1 1 auto; min-height: 120px; }
canvas.chart { width: 100%; height: 100%; display: block; }
.conn-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--text-faint); display: inline-block; margin-right: 6px;
}
.conn-dot.live { background: #16a34a; }
</style>

<div class="topbar">
  <span class="logo">[bot]</span>
  <span class="page-title">Teleop</span>
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
      <label>Speed Gain</label>
      <input type="range" id="gain-slider" min="0.1" max="5.0" step="0.1" value="1.0">
      <span id="gain-value" class="gain-value">1.0x</span>
    </div>
    <div class="keyboard-control">
      <div class="key-row"><div class="key" id="key-w">W</div></div>
      <div class="key-row">
        <div class="key" id="key-a">A</div>
        <div class="key" id="key-s">S</div>
        <div class="key" id="key-d">D</div>
      </div>
      <div class="key-row" style="margin-top: 6px;">
        <div class="key shift-key" id="key-shift">Shift</div>
      </div>
    </div>
  </div>

  <div class="col-charts">
    <div class="chart-card">
      <div class="chart-head">
        <span class="chart-title">Wheel Velocity &middot; turns/s</span>
        <div class="legend">
          <span class="item"><span class="swatch" style="border-top-color:var(--brand-orange)"></span>L meas <span id="ro-vel-l" class="readout">0.00</span></span>
          <span class="item"><span class="swatch dashed" style="border-top-color:var(--brand-orange)"></span>L set <span id="ro-sp-l" class="readout">0.00</span></span>
          <span class="item"><span class="swatch" style="border-top-color:var(--brand-blue)"></span>R meas <span id="ro-vel-r" class="readout">0.00</span></span>
          <span class="item"><span class="swatch dashed" style="border-top-color:var(--brand-blue)"></span>R set <span id="ro-sp-r" class="readout">0.00</span></span>
        </div>
      </div>
      <div class="chart-canvas-wrap"><canvas id="vel-chart" class="chart"></canvas></div>
    </div>

    <div class="chart-card">
      <div class="chart-head">
        <span class="chart-title">Motor Current Iq &middot; A</span>
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
const WINDOW_S = 10;          // seconds of history shown
const MAX_POINTS = 1200;      // ring buffer cap

// Rolling time-series chart over a fixed time window.
class Chart {
  constructor(canvas, series, opts) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.series = series;      // [{key, color, dashed}]
    this.opts = opts || {};
    this.t = [];               // timestamps (ms, page clock)
    this.data = series.map(() => []);
    this.dpr = window.devicePixelRatio || 1;
    this._resize();
    new ResizeObserver(() => this._resize()).observe(canvas);
  }
  _resize() {
    const r = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round(r.width * this.dpr));
    this.canvas.height = Math.max(1, Math.round(r.height * this.dpr));
  }
  push(tMs, values) {
    this.t.push(tMs);
    for (let i = 0; i < this.series.length; i++) this.data[i].push(values[i]);
    while (this.t.length > MAX_POINTS) {
      this.t.shift();
      for (const d of this.data) d.shift();
    }
  }
  draw(nowMs) {
    const ctx = this.ctx, W = this.canvas.width, H = this.canvas.height, dpr = this.dpr;
    ctx.clearRect(0, 0, W, H);
    const padL = 42 * dpr, padR = 8 * dpr, padT = 8 * dpr, padB = 16 * dpr;
    const plotW = W - padL - padR, plotH = H - padT - padB;
    const tMax = nowMs, tMin = nowMs - WINDOW_S * 1000;

    // y-range from visible data, symmetric-ish, with a sane floor
    let lo = Infinity, hi = -Infinity;
    for (let s = 0; s < this.data.length; s++) {
      const arr = this.data[s];
      for (let i = 0; i < arr.length; i++) {
        if (this.t[i] < tMin) continue;
        const v = arr[i];
        if (v < lo) lo = v; if (v > hi) hi = v;
      }
    }
    if (!isFinite(lo)) { lo = -1; hi = 1; }
    const minSpan = this.opts.minSpan || 0.5;
    if (hi - lo < minSpan) { const c = (hi + lo) / 2; lo = c - minSpan / 2; hi = c + minSpan / 2; }
    const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
    if (lo > 0) lo = 0; if (hi < 0) hi = 0;   // always show zero line

    const x = t => padL + ((t - tMin) / (tMax - tMin)) * plotW;
    const y = v => padT + (1 - (v - lo) / (hi - lo)) * plotH;

    // grid + y ticks
    ctx.font = (10 * dpr) + "px ui-monospace, monospace";
    ctx.textBaseline = "middle"; ctx.textAlign = "right";
    const ticks = 4;
    for (let i = 0; i <= ticks; i++) {
      const v = lo + (hi - lo) * (i / ticks);
      const yy = y(v);
      ctx.strokeStyle = Math.abs(v) < 1e-9 ? AXIS : GRID;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(W - padR, yy); ctx.stroke();
      ctx.fillStyle = TXT;
      ctx.fillText(v.toFixed(2), padL - 6 * dpr, yy);
    }

    // series
    for (let s = 0; s < this.series.length; s++) {
      const arr = this.data[s];
      ctx.strokeStyle = this.series[s].color;
      ctx.lineWidth = (this.series[s].dashed ? 1.5 : 2) * dpr;
      ctx.setLineDash(this.series[s].dashed ? [5 * dpr, 4 * dpr] : []);
      ctx.beginPath();
      let started = false;
      for (let i = 0; i < arr.length; i++) {
        if (this.t[i] < tMin) continue;
        const px = x(this.t[i]), py = y(arr[i]);
        if (!started) { ctx.moveTo(px, py); started = true; } else ctx.lineTo(px, py);
      }
      ctx.stroke();
    }
    ctx.setLineDash([]);
  }
}

const velChart = new Chart(document.getElementById("vel-chart"), [
  { key: "vel_l", color: ORANGE, dashed: false },
  { key: "sp_l",  color: ORANGE, dashed: true },
  { key: "vel_r", color: BLUE,   dashed: false },
  { key: "sp_r",  color: BLUE,   dashed: true },
], { minSpan: 0.5 });

const iqChart = new Chart(document.getElementById("iq-chart"), [
  { key: "iq_l", color: ORANGE, dashed: false },
  { key: "iq_r", color: BLUE,   dashed: false },
], { minSpan: 1.0 });

function fmt(v) { return (v >= 0 ? " " : "") + v.toFixed(2); }

const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
ws.binaryType = "arraybuffer";

const feedEl = document.getElementById("feed");
let prevUrl = null;
const connDot = document.getElementById("conn-dot");
const connText = document.getElementById("conn-text");

ws.onmessage = (e) => {
  if (e.data instanceof ArrayBuffer) {
    if (prevUrl) URL.revokeObjectURL(prevUrl);
    const blob = new Blob([e.data], { type: "image/jpeg" });
    prevUrl = URL.createObjectURL(blob);
    feedEl.src = prevUrl;
    return;
  }
  // text frame => telemetry JSON
  let msg;
  try { msg = JSON.parse(e.data); } catch (_) { return; }
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
};

ws.onopen = () => {
  console.log("[teleop] WebSocket connected");
  connDot.classList.add("live"); connText.textContent = "live";
};
ws.onclose = () => {
  console.log("[teleop] WebSocket disconnected");
  connDot.classList.remove("live"); connText.textContent = "disconnected";
};

function animate() {
  const now = performance.now();
  velChart.draw(now);
  iqChart.draw(now);
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

const keys = { w: false, a: false, s: false, d: false };
let shiftHeld = false;
let gain = 1.0;

const gainSlider = document.getElementById("gain-slider");
const gainValue = document.getElementById("gain-value");
gainSlider.addEventListener("input", () => {
  gain = parseFloat(gainSlider.value);
  gainValue.textContent = gain.toFixed(1) + "x";
  updateKeyboardCommand();
});

function updateKeyboardCommand() {
  let combo = '';
  if (keys.w) combo += 'w';
  if (keys.s) combo += 's';
  if (keys.a) combo += 'a';
  if (keys.d) combo += 'd';

  const comboDisplay = {
    'w':  { lin: 0.20, ang: 0.00 }, 's':  { lin: -0.20, ang: 0.00 },
    'a':  { lin: 0.00, ang: 0.92 }, 'd':  { lin: 0.00, ang: -0.92 },
    'wa': { lin: 0.165, ang: 0.70 }, 'wd': { lin: 0.165, ang: -0.70 },
    'sa': { lin: -0.165, ang: -0.70 }, 'sd': { lin: -0.165, ang: 0.70 },
    '':   { lin: 0.00, ang: 0.00 },
  };

  const base = comboDisplay[combo] || comboDisplay[''];
  const speedMult = shiftHeld ? 1.0 : 0.5;
  const totalMult = speedMult * gain;

  document.getElementById("linear-speed").textContent = (base.lin * totalMult).toFixed(2);
  document.getElementById("angular-speed").textContent = (base.ang * totalMult).toFixed(2);

  const modeEl = document.getElementById("speed-mode");
  if (shiftHeld) { modeEl.textContent = "Full Speed"; modeEl.classList.add("fast"); }
  else { modeEl.textContent = "Half Speed"; modeEl.classList.remove("fast"); }

  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ keys: combo, shift: shiftHeld, gain: gain }));
  }
}

function setKeyState(key, state) {
  const k = key.toLowerCase();
  if (k in keys) {
    keys[k] = state;
    const el = document.getElementById("key-" + k);
    if (el) el.classList.toggle("active", state);
    updateKeyboardCommand();
  }
}

document.addEventListener("keydown", (e) => {
  if (e.repeat) return;
  if (e.key === "Shift") {
    shiftHeld = true;
    document.getElementById("key-shift").classList.add("active");
    updateKeyboardCommand();
    return;
  }
  setKeyState(e.key, true);
});

document.addEventListener("keyup", (e) => {
  if (e.key === "Shift") {
    shiftHeld = false;
    document.getElementById("key-shift").classList.remove("active");
    updateKeyboardCommand();
    return;
  }
  setKeyState(e.key, false);
});

window.addEventListener("blur", () => {
  Object.keys(keys).forEach(k => setKeyState(k, false));
  shiftHeld = false;
  document.getElementById("key-shift").classList.remove("active");
  updateKeyboardCommand();
});
</script>
""")

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket):
        await websocket.accept()
        print("[teleop] WebSocket client connected")
        loop = asyncio.get_event_loop()

        async def send_frames():
            while not _stop:
                try:
                    frame = await loop.run_in_executor(None, jpeg_queue.get, True, 0.04)
                    await websocket.send_bytes(frame)
                except queue.Empty:
                    pass

        async def send_telemetry():
            # push latest motor telemetry at ~30 Hz
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
                    if "keys" in data:
                        cmd_queue.put({
                            'keys': data['keys'],
                            'shift': data.get('shift', False),
                            'gain': data.get('gain', 1.0)
                        })
                except asyncio.TimeoutError:
                    pass
        except WebSocketDisconnect:
            print("[teleop] WebSocket client disconnected")
            try:
                cmd_queue.put_nowait({'keys': '', 'shift': False, 'gain': 1.0})
            except queue.Full:
                pass
        except Exception as e:
            print(f"[teleop] WebSocket error: {e}")
        finally:
            send_task.cancel()
            tele_task.cancel()

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="error",
                access_log=False, ws="wsproto")


def main():
    reader_thread = threading.Thread(target=reader_loop, daemon=True)
    reader_thread.start()

    import socket
    print(f"[teleop] Starting teleop control server on http://{socket.gethostname()}.local:8008")
    server(8008)

    _stop = True
    reader_thread.join()


if __name__ == "__main__":
    main()

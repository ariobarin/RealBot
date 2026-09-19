# /// script
# requires-python = "==3.10.*"
# dependencies = [
#   "bbos",
#   "numpy",
#   "fastapi",
#   "uvicorn",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Move Arms — record/playback arm movements, go to home/zero positions via web UI."""

import numpy as np
import time
import threading
import json
import socket
from pathlib import Path
from bbos import Reader, Writer, Type, Config
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

RECORDINGS_DIR = Path(__file__).parent / "recordings"
RECORDINGS_DIR.mkdir(exist_ok=True)

LEFT_CFG = Config("arm_left")
RIGHT_CFG = Config("arm_right")
DOF = LEFT_CFG.dof

r_left = Reader("arm_left.state", keeptime=False)
r_right = Reader("arm_right.state", keeptime=False)

w_left = Writer("arm_left.ctrl", Type("arm_ctrl"), keeptime=False)
w_right = Writer("arm_right.ctrl", Type("arm_ctrl"), keeptime=False)

w_torque_left = Writer("arm_left.torque", Type("arm_torque"), keeptime=False)
w_torque_right = Writer("arm_right.torque", Type("arm_torque"), keeptime=False)

recorded_frames = []
is_recording = False
is_playing = False
is_looping = False
stop_event = threading.Event()
rec_start_time = 0.0

live_left = np.zeros(DOF, dtype=np.float32)
live_right = np.zeros(DOF, dtype=np.float32)
live_lock = threading.Lock()

INTERP_DURATION = 2.5

app = FastAPI()

JOINT_NAMES_L = LEFT_CFG.joint_names
JOINT_NAMES_R = RIGHT_CFG.joint_names

HTML = r"""
<!DOCTYPE html>
<html>
<head><title>Move Arms</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, sans-serif; max-width: 800px; margin: 0 auto;
         padding: 20px; background: #0d1117; color: #e6edf3; }
  h1 { text-align: center; font-size: 22px; margin-bottom: 5px; }
  .subtitle { text-align: center; color: #555; font-size: 13px; margin-bottom: 20px; }

  .status-bar { display: flex; align-items: center; justify-content: center;
                gap: 10px; padding: 12px; border-radius: 10px; margin-bottom: 16px;
                font-size: 15px; font-weight: 500; transition: all 0.3s; }
  .status-bar.idle { background: #161b22; color: #8b949e; }
  .status-bar.recording { background: #3b1219; color: #f85149; }
  .status-bar.playing { background: #12261e; color: #3fb950; }
  .status-bar.moving { background: #1c1d2e; color: #79c0ff; }
  .rec-dot { width: 12px; height: 12px; border-radius: 50%; background: #f85149; display: none; }
  .status-bar.recording .rec-dot { display: block; animation: blink 1s infinite; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.2} }

  .timer { font-family: monospace; font-size: 28px; text-align: center; margin: 10px 0; }

  .section-label { font-size: 12px; color: #484f58; margin: 16px 0 8px;
                   text-transform: uppercase; letter-spacing: 1px; }

  .btn-row { display: flex; gap: 10px; margin-bottom: 16px; }
  button { flex: 1; font-size: 16px; font-weight: 600; padding: 14px; border: none;
           border-radius: 10px; cursor: pointer; transition: all 0.15s; }
  button:active { transform: scale(0.97); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  #record { background: #da3633; color: white; }
  #record.active { background: #f85149; box-shadow: 0 0 20px rgba(248,81,73,0.3); }
  #play { background: #238636; color: white; }
  #play.active { background: #3fb950; }
  #play:disabled { background: #1a2e1f; color: #3fb950; }
  #loop { background: #6e40c9; color: white; }
  #loop.active { background: #8957e5; box-shadow: 0 0 20px rgba(137,87,229,0.3); }
  #loop:disabled { background: #2d1b4e; color: #8957e5; }
  #stop { background: #da3633; color: white; display: none; }
  .btn-home { background: #1f6feb; color: white; }
  .btn-zeros { background: #f0883e; color: white; }
  .saved-item .loop-btn { background: #6e40c9; color: white; }

  .save-row { display: flex; gap: 10px; margin-bottom: 16px; }
  .save-row input { flex: 1; font-size: 15px; padding: 12px 14px; border: 1px solid #30363d;
                    border-radius: 10px; background: #161b22; color: #e6edf3;
                    font-family: -apple-system, sans-serif; }
  .save-row input::placeholder { color: #484f58; }
  #save { flex: 0 0 auto; background: #1f6feb; color: white; padding: 12px 24px; }
  #save:disabled { background: #162d50; color: #1f6feb; }

  .saved-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 16px; }
  .saved-item { display: flex; align-items: center; gap: 10px; padding: 10px 14px;
                background: #161b22; border-radius: 10px; }
  .saved-item .name { flex: 1; font-size: 14px; font-weight: 500; }
  .saved-item .meta { font-size: 12px; color: #484f58; margin-right: 8px; }
  .saved-item button { flex: 0 0 auto; font-size: 13px; padding: 8px 14px; border-radius: 8px; }
  .saved-item .load-btn { background: #238636; color: white; }
  .saved-item .play-btn { background: #1f6feb; color: white; }
  .saved-item .del-btn { background: #21262d; color: #f85149; }

  .arms { display: flex; gap: 16px; margin-bottom: 16px; }
  .arm-panel { flex: 1; background: #161b22; border-radius: 10px; padding: 14px; }
  .arm-title { font-size: 13px; color: #8b949e; text-transform: uppercase;
               letter-spacing: 1px; margin-bottom: 10px; text-align: center; }
  .joint-row { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; font-size: 13px; }
  .joint-name { width: 30px; color: #8b949e; font-family: monospace; }
  .joint-bar-bg { flex: 1; height: 14px; background: #0d1117; border-radius: 3px; overflow: hidden;
                  position: relative; }
  .joint-bar { height: 100%; border-radius: 3px; transition: width 0.1s; position: absolute; }
  .joint-bar.left { background: #58a6ff; }
  .joint-bar.right { background: #bc8cff; }
  .joint-val { width: 55px; text-align: right; font-family: monospace; color: #8b949e; font-size: 11px; }

  canvas { width: 100%; background: #0d1117; border: 1px solid #21262d;
           border-radius: 8px; display: block; margin-bottom: 4px; }
  .stats { font-size: 12px; color: #484f58; margin-bottom: 8px; }
  .empty-msg { text-align: center; color: #484f58; font-size: 13px; padding: 20px; }
</style>
</head>
<body>
  <h1>Move Arms</h1>
  <div class="subtitle">Move to preset positions, record movements, and play them back</div>

  <div class="status-bar idle" id="statusBar">
    <div class="rec-dot"></div>
    <span id="statusText">Ready</span>
  </div>

  <div class="timer" id="timer">00:00.0</div>

  <div class="section-label">Presets</div>
  <div class="btn-row">
    <button class="btn-home" onclick="movePreset('home')">Home</button>
    <button class="btn-zeros" onclick="movePreset('zeros')">Zeros</button>
  </div>

  <div class="section-label">Record &amp; Playback</div>
  <div class="btn-row">
    <button id="record" onclick="toggleRecord()">Record</button>
    <button id="play" onclick="playback()" disabled>Play</button>
    <button id="loop" onclick="toggleLoop()" disabled>Loop</button>
    <button id="stop" onclick="stopPlayback()">Stop</button>
  </div>

  <div class="save-row">
    <input type="text" id="saveName" placeholder="Name this recording..." />
    <button id="save" onclick="saveRecording()" disabled>Save</button>
  </div>

  <div class="section-label">Saved Recordings</div>
  <div class="saved-list" id="savedList">
    <div class="empty-msg">No saved recordings yet</div>
  </div>

  <div class="arms" id="armsPanel"></div>

  <div class="section-label">Recorded Trajectories</div>
  <canvas id="trajCanvas" width="1600" height="250"></canvas>
  <div class="stats" id="trajStats">-</div>

  <script>
    const JOINT_NAMES_L = JOINT_NAMES_L_PLACEHOLDER;
    const JOINT_NAMES_R = JOINT_NAMES_R_PLACEHOLDER;
    let recording = false, playing = false, looping = false, hasRecording = false;
    let timerStart = 0, timerIv = null;

    function fmt(t) {
      let m = Math.floor(t/60), s = Math.floor(t%60), ms = Math.floor((t%1)*10);
      return String(m).padStart(2,'0')+':'+String(s).padStart(2,'0')+'.'+ms;
    }

    function buildArmPanel() {
      const panel = document.getElementById('armsPanel');
      let html = '';
      html += '<div class="arm-panel"><div class="arm-title">Left Arm</div>';
      for (let i = 0; i < JOINT_NAMES_L.length; i++) {
        html += '<div class="joint-row">' +
          '<span class="joint-name">'+JOINT_NAMES_L[i]+'</span>' +
          '<div class="joint-bar-bg"><div class="joint-bar left" id="lbar'+i+'"></div></div>' +
          '<span class="joint-val" id="lval'+i+'">0.000</span></div>';
      }
      html += '</div>';
      html += '<div class="arm-panel"><div class="arm-title">Right Arm</div>';
      for (let i = 0; i < JOINT_NAMES_R.length; i++) {
        html += '<div class="joint-row">' +
          '<span class="joint-name">'+JOINT_NAMES_R[i]+'</span>' +
          '<div class="joint-bar-bg"><div class="joint-bar right" id="rbar'+i+'"></div></div>' +
          '<span class="joint-val" id="rval'+i+'">0.000</span></div>';
      }
      html += '</div>';
      panel.innerHTML = html;
    }
    buildArmPanel();

    function updateJointBars(left, right) {
      for (let i = 0; i < left.length; i++) {
        let pct = Math.min(100, Math.max(0, (left[i] + 0.5) * 100));
        document.getElementById('lbar'+i).style.width = pct + '%';
        document.getElementById('lval'+i).textContent = left[i].toFixed(3);
      }
      for (let i = 0; i < right.length; i++) {
        let pct = Math.min(100, Math.max(0, (right[i] + 0.5) * 100));
        document.getElementById('rbar'+i).style.width = pct + '%';
        document.getElementById('rval'+i).textContent = right[i].toFixed(3);
      }
    }

    async function pollState() {
      try {
        let r = await fetch('/state');
        let j = await r.json();
        updateJointBars(j.left, j.right);
      } catch(e) {}
    }

    async function movePreset(target) {
      if (recording || playing || looping) return;
      document.getElementById('statusBar').className = 'status-bar moving';
      document.getElementById('statusText').textContent = 'Moving to ' + target + '...';
      try {
        let r = await fetch('/move/' + target, {method: 'POST'});
        let j = await r.json();
        document.getElementById('statusBar').className = 'status-bar idle';
        document.getElementById('statusText').textContent = j.msg;
      } catch(e) {
        document.getElementById('statusBar').className = 'status-bar idle';
        document.getElementById('statusText').textContent = 'Error moving to ' + target;
      }
    }

    async function toggleRecord() {
      if (!recording) {
        await fetch('/record/start', {method:'POST'});
        document.getElementById('record').textContent = 'Stop';
        document.getElementById('record').classList.add('active');
        document.getElementById('play').disabled = true;
        document.getElementById('save').disabled = true;
        document.getElementById('statusBar').className = 'status-bar recording';
        document.getElementById('statusText').textContent = 'Recording...';
        recording = true;
        timerStart = Date.now();
        timerIv = setInterval(() => {
          document.getElementById('timer').textContent = fmt((Date.now()-timerStart)/1000);
        }, 100);
      } else {
        clearInterval(timerIv);
        let r = await fetch('/record/stop', {method:'POST'});
        let j = await r.json();
        document.getElementById('record').textContent = 'Record';
        document.getElementById('record').classList.remove('active');
        document.getElementById('statusBar').className = 'status-bar idle';
        document.getElementById('statusText').textContent =
          'Recorded ' + j.frames + ' frames (' + j.duration + 's)';
        recording = false;
        hasRecording = j.frames > 0;
        document.getElementById('play').disabled = !hasRecording;
        document.getElementById('loop').disabled = !hasRecording;
        document.getElementById('save').disabled = !hasRecording;
        let tr = await fetch('/trajectory');
        let trj = await tr.json();
        drawTrajectory(trj);
      }
    }

    function setPlayingUI(on, label) {
      playing = on;
      document.getElementById('play').classList.toggle('active', on);
      document.getElementById('play').textContent = on ? 'Playing...' : 'Play';
      document.getElementById('record').disabled = on || looping;
      document.getElementById('stop').style.display = (on || looping) ? '' : 'none';
      if (label) {
        document.getElementById('statusBar').className = on || looping ? 'status-bar playing' : 'status-bar idle';
        document.getElementById('statusText').textContent = label;
      }
    }

    async function playback() {
      if (!hasRecording || playing) return;
      setPlayingUI(true, 'Playing back...');
      let playStart = Date.now();
      let playIv = setInterval(() => {
        document.getElementById('timer').textContent = fmt((Date.now()-playStart)/1000);
      }, 100);
      let r = await fetch('/play', {method:'POST'});
      let j = await r.json();
      clearInterval(playIv);
      if (!looping) setPlayingUI(false, j.msg);
    }

    async function toggleLoop() {
      if (looping) { stopPlayback(); return; }
      if (!hasRecording || playing) return;
      looping = true;
      document.getElementById('loop').classList.add('active');
      document.getElementById('loop').textContent = 'Looping...';
      document.getElementById('record').disabled = true;
      document.getElementById('play').disabled = true;
      document.getElementById('stop').style.display = '';
      document.getElementById('statusBar').className = 'status-bar playing';
      document.getElementById('statusText').textContent = 'Looping playback...';
      let playStart = Date.now();
      let playIv = setInterval(() => {
        document.getElementById('timer').textContent = fmt((Date.now()-playStart)/1000);
      }, 100);
      let r = await fetch('/play/loop', {method:'POST'});
      let j = await r.json();
      clearInterval(playIv);
      looping = false;
      document.getElementById('loop').classList.remove('active');
      document.getElementById('loop').textContent = 'Loop';
      document.getElementById('record').disabled = false;
      document.getElementById('play').disabled = !hasRecording;
      document.getElementById('stop').style.display = 'none';
      document.getElementById('statusBar').className = 'status-bar idle';
      document.getElementById('statusText').textContent = j.msg;
    }

    async function stopPlayback() {
      await fetch('/play/stop', {method:'POST'});
    }

    async function saveRecording() {
      let name = document.getElementById('saveName').value.trim();
      if (!name) { alert('Enter a name for the recording'); return; }
      let r = await fetch('/recordings/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: name})
      });
      let j = await r.json();
      if (j.error) { alert(j.error); return; }
      document.getElementById('saveName').value = '';
      document.getElementById('statusText').textContent = 'Saved "' + name + '"';
      loadSavedList();
    }

    async function loadSavedList() {
      let r = await fetch('/recordings');
      let items = await r.json();
      const list = document.getElementById('savedList');
      if (items.length === 0) {
        list.innerHTML = '<div class="empty-msg">No saved recordings yet</div>';
        return;
      }
      let html = '';
      for (let item of items) {
        html += '<div class="saved-item">' +
          '<span class="name">' + item.name + '</span>' +
          '<span class="meta">' + item.frames + ' frames &middot; ' + item.duration + 's</span>' +
          '<button class="load-btn" onclick="loadRecording(\'' + item.filename + '\')">Load</button>' +
          '<button class="play-btn" onclick="playRecording(\'' + item.filename + '\')">Play</button>' +
          '<button class="loop-btn" onclick="loopRecording(\'' + item.filename + '\')">Loop</button>' +
          '<button class="del-btn" onclick="deleteRecording(\'' + item.filename + '\')">Delete</button>' +
          '</div>';
      }
      list.innerHTML = html;
    }

    async function loadRecording(filename) {
      let r = await fetch('/recordings/load', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({filename: filename})
      });
      let j = await r.json();
      if (j.error) { alert(j.error); return; }
      hasRecording = true;
      document.getElementById('play').disabled = false;
      document.getElementById('loop').disabled = false;
      document.getElementById('save').disabled = false;
      document.getElementById('statusText').textContent = 'Loaded "' + j.name + '" (' + j.frames + ' frames)';
      let tr = await fetch('/trajectory');
      let trj = await tr.json();
      drawTrajectory(trj);
    }

    async function playRecording(filename) {
      if (playing) return;
      let r = await fetch('/recordings/load', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({filename: filename})
      });
      let j = await r.json();
      if (j.error) { alert(j.error); return; }
      hasRecording = true;
      document.getElementById('play').disabled = false;
      document.getElementById('loop').disabled = false;
      document.getElementById('save').disabled = false;
      let tr = await fetch('/trajectory');
      let trj = await tr.json();
      drawTrajectory(trj);
      playback();
    }

    async function loopRecording(filename) {
      if (playing || looping) return;
      let r = await fetch('/recordings/load', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({filename: filename})
      });
      let j = await r.json();
      if (j.error) { alert(j.error); return; }
      hasRecording = true;
      document.getElementById('play').disabled = false;
      document.getElementById('loop').disabled = false;
      document.getElementById('save').disabled = false;
      let tr = await fetch('/trajectory');
      let trj = await tr.json();
      drawTrajectory(trj);
      toggleLoop();
    }

    async function deleteRecording(filename) {
      if (!confirm('Delete this recording?')) return;
      await fetch('/recordings/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({filename: filename})
      });
      loadSavedList();
    }

    function drawTrajectory(data) {
      const canvas = document.getElementById('trajCanvas');
      const ctx = canvas.getContext('2d');
      const w = canvas.width, h = canvas.height;
      ctx.clearRect(0, 0, w, h);
      if (!data.times || data.times.length < 2) return;
      const colors_l = ['#58a6ff','#79c0ff','#a5d6ff','#388bfd','#1f6feb','#1158c7','#0d419d','#0a326b'];
      const colors_r = ['#bc8cff','#d2a8ff','#e8c9ff','#a371f7','#8957e5','#6e40c9','#553098','#3c1e70'];
      const n = data.times.length;
      const tMax = data.times[n-1];
      for (let j = 0; j < 8; j++) {
        ctx.strokeStyle = colors_l[j]; ctx.lineWidth = 1.5; ctx.beginPath();
        for (let i = 0; i < n; i++) {
          const x = (data.times[i]/tMax)*w, y = h/2 - data.left[j][i]*h*0.8;
          if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
        }
        ctx.stroke();
        ctx.strokeStyle = colors_r[j]; ctx.beginPath();
        for (let i = 0; i < n; i++) {
          const x = (data.times[i]/tMax)*w, y = h/2 - data.right[j][i]*h*0.8;
          if (i===0) ctx.moveTo(x,y); else ctx.lineTo(x,y);
        }
        ctx.stroke();
      }
      document.getElementById('trajStats').textContent =
        n + ' frames | ' + tMax.toFixed(1) + 's | Blue=Left, Purple=Right';
    }

    loadSavedList();
    setInterval(pollState, 50);
  </script>
</body>
</html>
"""

HTML = HTML.replace("JOINT_NAMES_L_PLACEHOLDER", json.dumps(JOINT_NAMES_L))
HTML = HTML.replace("JOINT_NAMES_R_PLACEHOLDER", json.dumps(JOINT_NAMES_R))


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

@app.get("/state")
def state():
    with live_lock:
        return {"left": live_left.tolist(), "right": live_right.tolist()}


# --- Preset moves (home / zeros) ---

def _move_to(target_left, target_right, label):
    with live_lock:
        start_left = live_left.copy()
        start_right = live_right.copy()

    w_left['pos'] = start_left
    w_right['pos'] = start_right
    time.sleep(0.05)
    w_torque_left['enable'] = np.ones(DOF, dtype=np.bool_)
    w_torque_right['enable'] = np.ones(DOF, dtype=np.bool_)
    time.sleep(0.05)

    t0 = time.monotonic()
    while True:
        elapsed = time.monotonic() - t0
        alpha = min(elapsed / INTERP_DURATION, 1.0)
        w_left["pos"] = start_left + alpha * (target_left - start_left)
        w_right["pos"] = start_right + alpha * (target_right - start_right)
        if alpha >= 1.0:
            break
        time.sleep(0.01)
    return {"msg": f"{label} position reached"}


@app.post("/move/home")
def move_home():
    return _move_to(np.array(LEFT_CFG.home, dtype=np.float32),
                    np.array(RIGHT_CFG.home, dtype=np.float32), "Home")


@app.post("/move/zeros")
def move_zeros():
    return _move_to(np.zeros(DOF, dtype=np.float32),
                    np.zeros(DOF, dtype=np.float32), "Zero")


# --- Recording ---

@app.post("/record/start")
def record_start():
    global is_recording, recorded_frames, rec_start_time
    recorded_frames = []
    rec_start_time = time.time()
    w_torque_left['enable'] = np.zeros(DOF, dtype=np.bool_)
    w_torque_right['enable'] = np.zeros(DOF, dtype=np.bool_)
    is_recording = True
    return {"ok": True}

@app.post("/record/stop")
def record_stop():
    global is_recording
    is_recording = False
    n = len(recorded_frames)
    dur = recorded_frames[-1]["t"] if n > 0 else 0
    return {"frames": n, "duration": f"{dur:.1f}"}

@app.get("/trajectory")
def trajectory():
    if not recorded_frames:
        return {"times": [], "left": [[] for _ in range(DOF)], "right": [[] for _ in range(DOF)]}
    step = max(1, len(recorded_frames) // 500)
    frames = recorded_frames[::step]
    times = [f["t"] for f in frames]
    left = [[f["left"][j] for f in frames] for j in range(DOF)]
    right = [[f["right"][j] for f in frames] for j in range(DOF)]
    return {"times": times, "left": left, "right": right}

def _play_once():
    w_left['pos'] = np.array(recorded_frames[0]["left"], dtype=np.float32)
    w_right['pos'] = np.array(recorded_frames[0]["right"], dtype=np.float32)
    time.sleep(0.1)
    w_torque_left['enable'] = np.ones(DOF, dtype=np.bool_)
    w_torque_right['enable'] = np.ones(DOF, dtype=np.bool_)
    time.sleep(0.1)

    t0 = time.time()
    for frame in recorded_frames:
        if stop_event.is_set():
            return False
        target = t0 + frame["t"]
        now = time.time()
        if target > now:
            stop_event.wait(target - now)
            if stop_event.is_set():
                return False
        w_left['pos'] = np.array(frame["left"], dtype=np.float32)
        w_right['pos'] = np.array(frame["right"], dtype=np.float32)
    return True

@app.post("/play")
def play():
    global is_playing
    if not recorded_frames:
        return JSONResponse({"msg": "Nothing recorded yet"})
    is_playing = True
    stop_event.clear()
    _play_once()
    w_torque_left['enable'] = np.zeros(DOF, dtype=np.bool_)
    w_torque_right['enable'] = np.zeros(DOF, dtype=np.bool_)
    is_playing = False
    dur = recorded_frames[-1]["t"] if recorded_frames else 0
    return {"msg": f"Played {len(recorded_frames)} frames ({dur:.1f}s)"}

@app.post("/play/loop")
def play_loop():
    global is_playing, is_looping
    if not recorded_frames:
        return JSONResponse({"msg": "Nothing recorded yet"})
    is_playing = True
    is_looping = True
    stop_event.clear()
    loops = 0
    while not stop_event.is_set():
        if not _play_once():
            break
        loops += 1
    w_torque_left['enable'] = np.zeros(DOF, dtype=np.bool_)
    w_torque_right['enable'] = np.zeros(DOF, dtype=np.bool_)
    is_playing = False
    is_looping = False
    dur = recorded_frames[-1]["t"] if recorded_frames else 0
    return {"msg": f"Looped {loops} times ({dur:.1f}s each)"}

@app.post("/play/stop")
def play_stop():
    stop_event.set()
    return {"ok": True}


# --- Persistent recordings ---

@app.get("/recordings")
def list_recordings():
    items = []
    for f in sorted(RECORDINGS_DIR.glob("*.json")):
        try:
            with open(f) as fh:
                data = json.load(fh)
            n = len(data["frames"])
            dur = data["frames"][-1]["t"] if n > 0 else 0
            items.append({
                "name": data.get("name", f.stem),
                "filename": f.name,
                "frames": n,
                "duration": f"{dur:.1f}",
            })
        except Exception:
            continue
    return items

@app.post("/recordings/save")
def save_recording(body: dict):
    global recorded_frames
    if not recorded_frames:
        return JSONResponse({"error": "Nothing recorded to save"}, status_code=400)
    name = body.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Name is required"}, status_code=400)
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "" for c in name).strip()
    if not safe_name:
        return JSONResponse({"error": "Invalid name"}, status_code=400)
    filename = safe_name.replace(" ", "_") + ".json"
    filepath = RECORDINGS_DIR / filename
    data = {"name": name, "saved_at": time.time(), "frames": recorded_frames}
    with open(filepath, "w") as f:
        json.dump(data, f)
    return {"ok": True, "filename": filename}

@app.post("/recordings/load")
def load_recording(body: dict):
    global recorded_frames
    filename = body.get("filename", "")
    filepath = RECORDINGS_DIR / filename
    if not filepath.exists() or not filepath.name.endswith(".json"):
        return JSONResponse({"error": "Recording not found"}, status_code=404)
    with open(filepath) as f:
        data = json.load(f)
    recorded_frames = data["frames"]
    return {"ok": True, "name": data.get("name", filename), "frames": len(recorded_frames)}

@app.post("/recordings/delete")
def delete_recording(body: dict):
    filename = body.get("filename", "")
    filepath = RECORDINGS_DIR / filename
    if filepath.exists() and filepath.name.endswith(".json"):
        filepath.unlink()
    return {"ok": True}


def state_loop():
    global live_left, live_right
    while True:
        l_ready = r_left.ready()
        r_ready = r_right.ready()
        if l_ready:
            with live_lock:
                live_left = r_left.data["pos"].copy()
        if r_ready:
            with live_lock:
                live_right = r_right.data["pos"].copy()
        if is_recording and (l_ready or r_ready):
            with live_lock:
                recorded_frames.append({
                    "t": time.time() - rec_start_time,
                    "left": live_left.tolist(),
                    "right": live_right.tolist(),
                })
        time.sleep(1.0 / 150)


PORT = 8014

if __name__ == "__main__":
    with r_left, r_right, w_left, w_right, w_torque_left, w_torque_right:
        print(f"Move Arms at http://{socket.gethostname()}.local:{PORT}", flush=True)
        t = threading.Thread(target=state_loop, daemon=True)
        t.start()
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="error")

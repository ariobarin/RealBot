# /// script
# requires-python = "==3.10.*"
# dependencies = [
#   "fastapi",
#   "uvicorn",
# ]
# ///
"""Web UI to trigger any wav in wavs/."""
import subprocess
import socket
from pathlib import Path
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

WAVS_DIR = Path(__file__).parent / "wavs"
UV = "/home/bracketbot/.local/bin/uv"
PLAY_WAV = Path(__file__).parent / "main.py"

app = FastAPI()
_proc = None


@app.get("/wavs")
def list_wavs():
    return sorted(f.stem for f in WAVS_DIR.glob("*.wav"))


@app.post("/play/{name}")
def play(name: str):
    global _proc
    if _proc and _proc.poll() is None:
        _proc.kill()
    _proc = subprocess.Popen(
        [UV, "run", "--script", str(PLAY_WAV), "--", name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return {"playing": name}


@app.post("/stop")
def stop():
    global _proc
    if _proc and _proc.poll() is None:
        _proc.kill()
        return {"stopped": True}
    return {"stopped": False}


@app.get("/", response_class=HTMLResponse)
def index():
    return """<!DOCTYPE html><html><head><title>Soundboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
* { box-sizing: border-box; }
body { font-family: -apple-system, sans-serif; max-width: 500px; margin: 0 auto;
       padding: 20px; background: #0d1117; color: #e6edf3; }
h1 { text-align: center; font-size: 22px; margin-bottom: 20px; }
.grid { display: flex; flex-direction: column; gap: 8px; }
button { font-size: 16px; font-weight: 600; padding: 16px; border: none;
         border-radius: 10px; cursor: pointer; transition: all 0.15s;
         background: #238636; color: white; }
button:active { transform: scale(0.97); opacity: 0.8; }
#stop-btn { background: #da3633; margin-bottom: 16px; }
.status { text-align: center; color: #8b949e; font-size: 14px; margin-bottom: 12px; }
</style></head><body>
<h1>Soundboard</h1>
<div class="status" id="status">Ready</div>
<button id="stop-btn" onclick="stopAudio()">Stop</button>
<div class="grid" id="grid"></div>
<script>
async function load() {
  const r = await fetch('/wavs');
  const wavs = await r.json();
  const grid = document.getElementById('grid');
  grid.innerHTML = wavs.map(w =>
    `<button onclick="play('${w}')">${w.replace(/_/g, ' ')}</button>`
  ).join('');
}
async function play(name) {
  document.getElementById('status').textContent = 'Playing: ' + name;
  await fetch('/play/' + name, {method: 'POST'});
}
async function stopAudio() {
  await fetch('/stop', {method: 'POST'});
  document.getElementById('status').textContent = 'Stopped';
}
load();
</script></body></html>"""


if __name__ == "__main__":
    print(f"Soundboard at http://{socket.gethostname()}.local:8015", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=8015, log_level="error")

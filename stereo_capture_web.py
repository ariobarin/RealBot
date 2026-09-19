# /// script
# requires-python = ">=3.10,<3.11"
# dependencies = [
#   "bbos",
#   "numpy",
#   "opencv-python-headless",
#   "fastapi",
#   "uvicorn",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Stereo calibration capture over a web UI (head cam only).

Each pair is saved under ~/calibration_captures/<timestamp>/ on the robot
before the browser downloads it to your machine.

    # on the robot: uv run stereo_capture_web.py
    # on your mac:  open http://<robot>.local:8005/

By default each capture downloads to the browser's download folder, so point
that at your calibration folder (Safari > Settings > General > File download
location). Chrome served over localhost also offers "choose folder", a native
directory picker that writes straight into any folder you pick; for that,
tunnel with `ssh -L 8005:localhost:8005 bracketbot@<robot>.local` and open
http://localhost:8005/ instead.

Press Enter per capture. Each capture splits the head
stereo frame in half and writes both eyes as lossless PNGs named
camera0_image-<timestamp>.png (left) and camera1_image-<timestamp>.png.
"""
import asyncio
import socket
import tempfile
import threading
import time
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import uvicorn
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from bbos import Reader

# The head cam publishes ~273KB at 29fps (8MB/s) - far more than wifi can
# drain, so the preview is shrunk and rate-limited. Captures stay full-res.
PREVIEW_FPS = 12
PREVIEW_DECODE = cv2.IMREAD_REDUCED_COLOR_4  # 2560x960 -> 640x240
PREVIEW_QUALITY = 70
CAPTURE_DIR = Path.home() / "calibration_captures"

latest = {"jpeg": b"", "preview": b"", "seq": 0}
shots = OrderedDict()  # ts -> (left_png, right_png), most recent last

def camera_reader():
    next_preview = 0.0
    with Reader("camera.head.jpeg") as r:
        while True:
            if r.ready():
                latest["jpeg"] = bytes(r.data["jpeg"][:r.data["jpeg_len"]])
                now = time.monotonic()
                if now >= next_preview:
                    next_preview = now + 1.0 / PREVIEW_FPS
                    small = cv2.imdecode(np.frombuffer(latest["jpeg"], np.uint8),
                                         PREVIEW_DECODE)
                    if small is not None:
                        ok, enc = cv2.imencode(".jpg", small,
                                               [cv2.IMWRITE_JPEG_QUALITY, PREVIEW_QUALITY])
                        if ok:
                            latest["preview"] = enc.tobytes()
                            latest["seq"] += 1

app = FastAPI()

class Req(BaseModel):
    swap: bool = False

@app.get("/stream")
async def stream():
    async def generate():
        seen = -1
        while True:
            if latest["seq"] != seen and latest["preview"]:
                seen = latest["seq"]
                yield (b"--frame\r\n"
                       b"Content-Type: image/jpeg\r\n\r\n" + latest["preview"] + b"\r\n")
            else:
                await asyncio.sleep(0.005)
    return StreamingResponse(
        generate(),
        media_type="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

@app.post("/capture")
def capture(req: Req):
    if not latest["jpeg"]:
        return {"error": "no camera frames yet"}

    frame = cv2.imdecode(np.frombuffer(latest["jpeg"], np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        return {"error": "failed to decode frame"}

    mid = frame.shape[1] // 2
    first, second = frame[:, :mid], frame[:, mid:]
    left, right = (second, first) if req.swap else (first, second)

    png = [cv2.IMWRITE_PNG_COMPRESSION, 1]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    images = tuple(cv2.imencode(".png", eye, png)[1].tobytes()
                   for eye in (left, right))
    names = [f"camera0_image-{ts}.png", f"camera1_image-{ts}.png"]
    try:
        CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=CAPTURE_DIR, prefix=".capture-") as tmp:
            for name, image in zip(names, images):
                (Path(tmp) / name).write_bytes(image)
            Path(tmp).rename(CAPTURE_DIR / ts)
    except OSError as err:
        return {"error": f"could not save capture on robot: {err}"}
    shots[ts] = images
    while len(shots) > 4:
        shots.popitem(last=False)

    h, w = left.shape[:2]
    return {"ts": ts, "size": f"{w}x{h}",
            "names": names, "saved_to": str(CAPTURE_DIR / ts)}

@app.get("/shot/{ts}/{idx}")
def shot(ts: str, idx: int):
    if ts not in shots or idx not in (0, 1):
        return Response(status_code=404)
    return Response(content=shots[ts][idx], media_type="image/png")

@app.get("/")
async def index():
    html = """
    <html>
    <head><title>stereo calibration capture</title>
    <style>
      body { margin:0; padding:20px; background:#000; color:#fff; font-family:sans-serif; }
      img { max-width:100%; height:auto; display:block; margin-top:10px; }
      button { font-size:1em; padding:4px 10px; }
      pre { white-space:pre-wrap; }
    </style></head>
    <body>
      <h2>head cam &mdash; stereo calibration capture</h2>
      <div>
        <button id="pick">choose folder</button>
        <span id="dir">using browser download folder</span>
        <label><input id="swap" type="checkbox"> swap left/right</label>
      </div>
      <p>press <b>Enter</b> to capture &mdash; left half is camera0</p>
      <img id="feed" src="/stream" />
      <pre id="log"></pre>
      <script>
        let dirHandle = null, count = 0, busy = false;
        const log = m => document.getElementById('log').textContent =
          m + "\\n" + document.getElementById('log').textContent;

        document.getElementById('pick').addEventListener('click', async () => {
          if (!window.showDirectoryPicker) {
            log("folder picker needs a secure context - you are on " +
                location.origin + ". reach the app at http://localhost:8005/ " +
                "via `ssh -L 8005:localhost:8005 bracketbot@<robot>.local`, or " +
                "just press Enter: captures download to your browser's " +
                "download folder instead.");
            return;
          }
          try {
            dirHandle = await window.showDirectoryPicker({mode: 'readwrite'});
            document.getElementById('dir').textContent = dirHandle.name;
            log("saving into " + dirHandle.name);
          } catch (err) { /* picker dismissed */ }
        });

        async function save(name, blob) {
          if (dirHandle) {
            try {
              if (await dirHandle.queryPermission({mode:'readwrite'}) !== 'granted') {
                await dirHandle.requestPermission({mode:'readwrite'});
              }
              const fh = await dirHandle.getFileHandle(name, {create:true});
              const w = await fh.createWritable();
              await w.write(blob);
              await w.close();
              return;
            } catch (err) {
              // Handle went stale (folder moved/unmounted) or permission lapsed.
              log(`folder write failed (${err.name}) - falling back to downloads`);
              dirHandle = null;
              document.getElementById('dir').textContent =
                'using browser download folder';
            }
          }
          {
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = name;
            document.body.appendChild(a);
            a.click();
            a.remove();
            setTimeout(() => URL.revokeObjectURL(a.href), 30000);
          }
        }

        document.addEventListener('keydown', async e => {
          if (e.key !== 'Enter' || busy) return;
          e.preventDefault();
          busy = true;
          // Drop the stream so the ~3MB of png has the link to itself; a
          // live mjpeg connection and the transfer otherwise starve each other.
          const feed = document.getElementById('feed');
          feed.src = '';
          try {
            const r = await fetch('/capture', {method:'POST',
              headers:{'Content-Type':'application/json'},
              body: JSON.stringify({swap: document.getElementById('swap').checked})});
            const j = await r.json();
            if (j.error) { log(j.error); return; }
            log(`saved on robot: ${j.saved_to}`);
            for (let i = 0; i < 2; i++) {
              const blob = await (await fetch(`/shot/${j.ts}/${i}`)).blob();
              await save(j.names[i], blob);
            }
            const dest = dirHandle ? dirHandle.name : "downloads";
            log(`#${++count} ${j.size} -> ${dest}/{${j.names.join(', ')}}`);
          } catch (err) {
            log("capture failed: " + err);
          } finally {
            feed.src = '/stream?' + count;
            busy = false;
          }
        });
      </script>
    </body>
    </html>
    """
    return Response(content=html, media_type="text/html")

def main():
    threading.Thread(target=camera_reader, daemon=True).start()
    print(f"[+] head cam capture on http://{socket.gethostname()}.local:8005/")
    uvicorn.run(app, host="0.0.0.0", port=8005, log_level="error", access_log=False)

if __name__ == "__main__":
    main()

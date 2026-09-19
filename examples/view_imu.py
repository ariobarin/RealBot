# /// script
# requires-python = "==3.10.*"
# dependencies = ["numpy", "bbos"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""Web visualizer for offset-corrected pitch and raw accel/gyro readings."""

from bbos import Config, Reader
from bbos.daemons.base.driver import load_calibration
from bbos.time import Loop
import math, time, json, threading
import http.server

Loop._silent = True

latest = {
    "pitch": 0.0,
    "raw_pitch": 0.0,
    "roll": 0.0,
    "accel": [0.0, 0.0, 0.0],
    "gyro": [0.0, 0.0, 0.0],
    "t": 0.0,
}

HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><title>IMU Pitch</title>
<style>
#raw{position:fixed;top:18px;left:18px;z-index:2;background:#000b;border:1px solid #333;
     border-radius:6px;padding:10px 13px;line-height:1.55;white-space:pre;min-width:420px}
.dim{color:#888}.accel{color:#8ab4f8}.gyro{color:#f6c85f}
</style></head><body style="margin:0;background:#111;color:#eee;font-family:monospace">
<div id="raw">waiting for imu.orientation and imu.raw...</div>
<canvas id="c" style="width:100%;height:100vh"></canvas>
<script>
const c=document.getElementById('c'),ctx=c.getContext('2d');
const raw=document.getElementById('raw');
let data=[];const MAX=1000;
function resize(){c.width=window.innerWidth;c.height=window.innerHeight}
window.onresize=resize;resize();
function draw(){
  ctx.fillStyle='#111';ctx.fillRect(0,0,c.width,c.height);
  if(!data.length)return;
  const h=c.height,w=c.width,mid=h/2;
  // grid
  ctx.strokeStyle='#333';ctx.lineWidth=1;
  for(let deg=-90;deg<=90;deg+=15){
    let y=mid-deg*(mid/90);
    ctx.beginPath();ctx.moveTo(0,y);ctx.lineTo(w,y);ctx.stroke();
    if(deg%30===0){ctx.fillStyle='#555';ctx.fillText(deg+'°',5,y-2)}
  }
  // zero line
  ctx.strokeStyle='#555';ctx.lineWidth=2;
  ctx.beginPath();ctx.moveTo(0,mid);ctx.lineTo(w,mid);ctx.stroke();
  // pitch line
  ctx.strokeStyle='#0f0';ctx.lineWidth=2;ctx.beginPath();
  for(let i=0;i<data.length;i++){
    let x=w*(i/(MAX-1)),y=mid-data[i].pitch*(mid/45);
    i?ctx.lineTo(x,y):ctx.moveTo(x,y);
  }
  ctx.stroke();
  // current value
  let last=data[data.length-1];
  ctx.fillStyle='#0f0';ctx.font='bold 48px monospace';
  ctx.fillText(last.pitch.toFixed(2)+'°',w-320,60);
  ctx.font='16px monospace';ctx.fillStyle='#888';
  ctx.fillText('corrected pitch',w-320,80);
  ctx.font='20px monospace';ctx.fillStyle='#aaa';
  ctx.fillText('raw '+last.raw_pitch.toFixed(2)+'°',w-320,112);
  const a=last.accel||[0,0,0],g=last.gyro||[0,0,0];
  const an=Math.hypot(...a),gn=Math.hypot(...g);
  const vec=v=>v.map(x=>(Number(x)||0).toFixed(5)).join('  ');
  raw.innerHTML='<span class="dim">fused roll / corrected pitch (deg)</span>  '+
    last.roll.toFixed(3)+'  '+last.pitch.toFixed(3)+'\\n'+
    '<span class="accel">raw accel (m/s²) [x y z]</span>  '+vec(a)+'  |a|='+an.toFixed(5)+'\\n'+
    '<span class="gyro">raw gyro (rad/s) [x y z]</span> '+vec(g)+'  |ω|='+gn.toFixed(5);
}
setInterval(()=>{
  fetch('/data').then(r=>r.json()).then(d=>{
    data.push(d);if(data.length>MAX)data.shift();draw()
  }).catch(()=>{})
},10);
</script></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/data":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(latest).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML.encode())

    def log_message(self, *a):
        pass


def imu_loop():
    import traceback
    try:
        calibration = load_calibration(Config("base").calibration_path)
        pitch_offset_deg = math.degrees(float(calibration.get("pitch_offset_rad", 0.0)))
        print(f"Pitch correction: subtracting {pitch_offset_deg:+.3f} degrees")
        with Reader("imu.orientation") as r_orientation, Reader("imu.raw") as r_raw:
            print("Orientation and raw IMU readers created; waiting for data...")
            t0 = time.time()
            while True:
                if r_orientation.ready():
                    rpy = r_orientation.data['rpy']
                    # [roll, pitch, yaw] since 2026-08-15. The pre-STM imu
                    # daemon returned [pitch, roll, yaw] under the same name,
                    # which is why these two indices used to be reversed.
                    roll = float(rpy[0])
                    # Orientation is in degrees; calibration stores radians.
                    raw_pitch = float(rpy[1])
                    pitch = raw_pitch - pitch_offset_deg
                    latest["pitch"] = round(pitch, 3)
                    latest["raw_pitch"] = round(raw_pitch, 3)
                    latest["roll"] = round(roll, 3)
                    latest["t"] = round(time.time() - t0, 3)
                if r_raw.ready():
                    latest["accel"] = [round(float(v), 6) for v in r_raw.data["accel"]]
                    latest["gyro"] = [round(float(v), 6) for v in r_raw.data["gyro"]]
                time.sleep(0.005)
    except Exception as e:
        print(f"IMU loop error: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    import socket
    hostname = socket.gethostname()
    threading.Thread(target=imu_loop, daemon=True).start()
    port = 8888
    print(f"http://{hostname}.local:{port}")
    http.server.HTTPServer(("0.0.0.0", port), Handler).serve_forever()

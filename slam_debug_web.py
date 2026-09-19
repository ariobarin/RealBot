# /// script
# requires-python = ">=3.10"
# dependencies = ["bbos", "fastapi", "uvicorn", "numpy"]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
"""slam_v1.debug recorder + live history viewer with CSV export. uv run -> :8020

Records every slam_v1.debug frame (incl. the vo_ms/slam_ms pipeline split) plus
accurate per-process CPU/MEM and system GPU load, plots the whole history live,
exports full-resolution CSV.

Also lists the daemon's library trace: slam_v1 tees fd 1 (where the cuVSLAM
release build printf's its Error/Warning/Message lines) and publishes whatever
accumulated since the last frame as trace/trace_len on the same topic. Those
lines get their own timestamped, filterable panel, and clicking one pins a
marker across every chart at the frame it came from.
"""
import math
import os
import threading
import time

from bbos import Reader
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# fixed column order == CSV header == row schema the frontend unpacks
COLS = ["t", "track_ms", "vo_ms", "slam_ms", "lc_ms", "pgo_ms",
        "vo_valid", "kf", "lc_attempted", "lc_found", "lc_committed",
        "pgo_status", "pgo_count", "p1_inliers", "p2_inliers",
        "vo_x", "vo_y", "vo_z", "pos_x", "pos_y", "pos_z", "qx", "qy", "qz", "qw",
        "cpu_pct", "gpu_pct", "mem_pct", "rss_mb",
        "kf_count"]   # appended: the JS column map indexes COLS positionally

# RAM-only ring buffer — NO per-frame disk IO (that added ~30 write()+json.dumps/s
# of syscall/kernel load on an already-saturated box). The browser accumulates the
# full history client-side; the server only retains a rolling window for catch-up.
RING = 120_000       # ~66 min at 30Hz; ~90MB RAM. Plenty for reconnect/backfill.
hist = []            # rolling window of recent rows (each a list matching COLS)
dropped = [0]        # how many rows evicted off the front (for absolute indexing)
lock = threading.Lock()
t0 = [None]          # epoch-ns of the first recorded frame (for wall-clock + relative t)

# library trace: kept OUT of the numeric rows (bursty, variable-length, and the
# CSV stays a clean numeric matrix). Same absolute-index scheme as `hist`.
TRACE_RING = 40_000
traces = []          # [t_seconds, line]
tdropped = [0]
has_trace = [None]   # None = not probed yet; False = daemon predates the trace field

CLK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1
GPU_LOAD = "/sys/devices/platform/gpu.0/load"   # Orin: per-mille (0-1000)
stats = {"cpu": None, "gpu": None, "mem": None, "rss": None, "pid": None}


def _memtotal_kb():
    for line in open("/proc/meminfo"):
        if line.startswith("MemTotal:"):
            return int(line.split()[1])
    return 0


MEMTOTAL_KB = _memtotal_kb()


def find_slam_pid():
    """PID of `python daemon.py slam_v1` (re-resolved on restart)."""
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            cl = open(f"/proc/{p}/cmdline", "rb").read().replace(b"\0", b" ").decode()
        except Exception:
            continue
        if "daemon.py slam" in cl and "python" in cl:  # matches slam and slam_v1
            return int(p)
    return None


def stats_loop():
    """Accurate sampling: CPU from utime+stime DELTAS over wall time (not the
    since-boot average `ps` reports); RSS from /proc/pid/status; GPU from sysfs."""
    pid, last = None, None
    while True:
        if pid is None or not os.path.exists(f"/proc/{pid}"):
            pid, last = find_slam_pid(), None
            stats["pid"] = pid
        if pid:
            try:
                # comm field can contain spaces/parens -> split after the LAST ')'
                f = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
                jiffies = int(f[11]) + int(f[12])          # utime + stime
                now, cpu_s = time.monotonic(), jiffies / CLK
                if last:
                    dt = now - last[0]
                    if dt > 0:
                        stats["cpu"] = round((cpu_s - last[1]) / dt * 100.0, 1)
                last = (now, cpu_s)
                for line in open(f"/proc/{pid}/status"):
                    if line.startswith("VmRSS:"):
                        kb = int(line.split()[1])
                        stats["rss"] = round(kb / 1024.0, 1)
                        stats["mem"] = round(kb / MEMTOTAL_KB * 100.0, 1) if MEMTOTAL_KB else None
                        break
            except Exception:
                pid, last = None, None
        try:
            stats["gpu"] = round(int(open(GPU_LOAD).read().strip()) / 10.0, 1)
        except Exception:
            stats["gpu"] = None
        time.sleep(0.5)


def num(x, nd):
    """finite -> rounded float; NaN/Inf (e.g. VO tracking-loss pose) -> None (valid JSON null)."""
    x = float(x)
    return round(x, nd) if math.isfinite(x) else None


def gi(d, name):
    """int field, or None if this slam_v1 build doesn't publish it yet."""
    try:
        return int(d[name])
    except Exception:
        return None


def split_trace(d):
    """The daemon joins the frame's stdout lines with ' | ' into a fixed-width
    S1900 field; trace_len is the true byte length (the field is NUL-padded, and
    an over-long burst is truncated from the FRONT by the daemon)."""
    if has_trace[0] is False:
        return []
    try:
        n = int(d["trace_len"])
        has_trace[0] = True
        if n <= 0:
            return []
        raw = d["trace"].tobytes()[:n]
        out = []
        for part in raw.split(b" | "):
            part = part.strip().rstrip(b"\x00")
            if part:
                out.append(part.decode("utf-8", "replace"))
        return out
    except Exception:
        if has_trace[0] is None:                      # daemon without trace fields
            has_trace[0] = False
            print("[trace] slam_v1.debug has no trace field — panel stays empty", flush=True)
        return []


def reader_loop():
    # 023's renamed daemon publishes slam.debug; 091 still slam_v1.debug.
    with Reader(os.environ.get("SLAM_DEBUG_TOPIC", "slam_v1.debug"), keeptime=False) as r:
        last_t = None
        while True:
            if r.ready():
                d = r.data
                t_ns = int(d["timestamp"].view("i8"))
                if t_ns == last_t:
                    time.sleep(0.002); continue
                last_t = t_ns
                if t0[0] is None:
                    t0[0] = t_ns
                vp, p, q = d["vo_pos"], d["pos"], d["quat"]
                row = [round((t_ns - t0[0]) / 1e9, 3),
                       num(d["track_ms"], 1), num(d["vo_ms"], 1), num(d["slam_ms"], 1),
                       num(d["lc_ms"], 2), num(d["pgo_ms"], 2),
                       int(d["vo_valid"]), int(d["kf"]), int(d["lc_attempted"]),
                       int(d["lc_found"]), int(d["lc_committed"]), int(d["pgo_status"]),
                       int(d["pgo_count"]), int(d["p1_inliers"]), int(d["p2_inliers"]),
                       num(vp[0], 3), num(vp[1], 3), num(vp[2], 3),
                       num(p[0], 3), num(p[1], 3), num(p[2], 3),
                       num(q[0], 4), num(q[1], 4), num(q[2], 4), num(q[3], 4),
                       stats["cpu"], stats["gpu"], stats["mem"], stats["rss"],
                       gi(d, "kf_count")]
                lines = split_trace(d)
                with lock:
                    hist.append(row)
                    if len(hist) > RING + 4000:       # trim in chunks (cheap, amortized)
                        cut = len(hist) - RING
                        del hist[:cut]; dropped[0] += cut
                    if lines:
                        traces.extend([row[0], ln] for ln in lines)
                        if len(traces) > TRACE_RING + 2000:
                            cut = len(traces) - TRACE_RING
                            del traces[:cut]; tdropped[0] += cut
            time.sleep(0.002)


app = FastAPI()
RUN = [str(time.time())]      # bumped on /clear so OTHER open tabs resync too


@app.get("/series")
def series(since: int = 0, max: int = 20000):
    """Incremental diff by ABSOLUTE row index (survives ring eviction).
    since = absolute index the client already has; returns rows from there on."""
    with lock:
        total = dropped[0] + len(hist)      # absolute count ever recorded
        if since > total:                   # client ahead => server cleared/restarted
            since = 0
        start = since if since >= dropped[0] else dropped[0]   # clamp into the window
        rows = hist[start - dropped[0]: start - dropped[0] + max]
    return JSONResponse({"cols": COLS, "run": RUN[0], "total": total,
                         "start": start, "rows": rows, "pid": stats["pid"],
                         "mem_total_mb": round(MEMTOTAL_KB / 1024),
                         "t0_ms": (t0[0] / 1e6) if t0[0] else None,
                         "has_trace": has_trace[0]},
                        headers={"Cache-Control": "no-store"})


@app.get("/trace")
def trace(since: int = 0, max: int = 5000):
    """Library-trace lines, absolute-indexed exactly like /series."""
    with lock:
        total = tdropped[0] + len(traces)
        if since > total:
            since = 0
        start = since if since >= tdropped[0] else tdropped[0]
        out = traces[start - tdropped[0]: start - tdropped[0] + max]
    return JSONResponse({"run": RUN[0], "total": total, "start": start, "lines": out},
                        headers={"Cache-Control": "no-store"})


@app.post("/clear")
def clear():
    with lock:
        hist.clear(); dropped[0] = 0; t0[0] = None
        traces.clear(); tdropped[0] = 0
        RUN[0] = str(time.time())
    return {"ok": True}


HTML = """<!doctype html><meta charset=utf-8><title>slam_v1.debug history</title>
<style>
body{font:13px ui-monospace,monospace;background:#0d0d0d;color:#ddd;margin:0;padding:16px}
h1{font-size:15px;color:#8ab4f8;margin:0 0 4px} .sub{color:#666;margin:0 0 12px}
button{font:12px ui-monospace;background:#222;color:#ddd;border:1px solid #444;padding:5px 10px;border-radius:4px;cursor:pointer;margin-right:6px}
button:hover{background:#333}
.chart{margin:10px 0} .lbl{color:#999;font-size:11px;margin-bottom:2px}
canvas{background:#151515;border:1px solid #262626;border-radius:4px;width:100%;display:block}
.row{display:flex;gap:20px;flex-wrap:wrap;margin-bottom:8px}
.legend{font-size:11px;color:#888}
.g{color:#2ecc71}.b{color:#5b9bf8}.o{color:#e8a33a}.r{color:#e8443a}.p{color:#c58af9}.w{color:#ddd}
#trace{height:320px;overflow:auto;background:#151515;border:1px solid #262626;border-radius:4px;
padding:6px 8px;font:11px ui-monospace,monospace;white-space:pre}
.tl{padding:0 2px;border-radius:2px;cursor:pointer}
.tl:hover{background:#ffffff10}
.tl.pin{background:#00e5ff22;box-shadow:inset 2px 0 0 #00e5ff}
.tt{color:#8ab4f8}.ts{color:#666}
#tfilt{background:#151515;border:1px solid #444;color:#ddd;border-radius:4px;
padding:3px 6px;font:11px ui-monospace,monospace;width:200px}
.tbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:3px}
.tbar label{color:#999;font-size:11px;cursor:pointer}
</style>
<h1>slam_v1.debug <span class=sub id=stat></span></h1>
<div class=row>
 <button onclick="paused=!paused;this.textContent=paused?'▶ Resume':'⏸ Pause'">⏸ Pause</button>
 <button onclick="dlCSV()">⬇ Download CSV</button>
 <button id=copybtn onclick="copyCsv(this)">⧉ Copy CSV</button>
 <button onclick="fetch('/clear',{method:'POST'}).then(()=>{rows=[];total=0;nextSeq=0;traces=[];tNext=0;pinT=null;tSig='';draw();renderTrace()})">✕ Clear</button>
</div>
<div class=chart><div class=lbl>VO tracking &mdash; <span class=g>valid</span> / <span class=r>LOST</span></div><canvas id=vv height=24></canvas></div>
<div class=chart><div class=lbl>loop closure &mdash; <span style="color:#5a6b8c">attempted</span> / <span class=o>found</span> / <span class=g>COMMITTED</span></div><canvas id=lcev height=24></canvas></div>
<div class=chart><div class=lbl>PGO runs <span class=legend>(fires on LC commits AND periodic planar ticks &mdash; independent of loop closure)</span></div><canvas id=pgoev height=24></canvas></div>
<div class=chart><div class=lbl>XY trajectory &mdash; <span class=g>pos (corrected)</span> vs <span class=b>vo_pos (raw)</span> <span class=legend>(red = tracking lost)</span></div><canvas id=xy height=340></canvas></div>
<div class=chart><div class=lbl>pipeline ms &mdash; <span class=w>track (total)</span> / <span class=b>vo</span> / <span class=p>slam</span></div><canvas id=trk height=110></canvas></div>
<div class=chart><div class=lbl>loop-closure &amp; PGO ms &mdash; <span class=o>lc_ms</span> (detection, grows with map) / <span class=r>pgo_ms</span> (optimisation) <span class=legend>(0 on non-keyframes)</span></div><canvas id=lcpgo height=90></canvas></div>
<div class=chart><div class=lbl><span class=o>slam CPU%</span> <span class=legend>(% of one core)</span></div><canvas id=cpu height=80></canvas></div>
<div class=chart><div class=lbl><span class=g>GPU%</span> <span class=legend>(system-wide)</span></div><canvas id=gpu height=80></canvas></div>
<div class=chart><div class=lbl><span class=b>slam memory (MB RSS)</span> <span class=legend id=memtot></span></div><canvas id=mem height=80></canvas></div>
<div class=chart><div class=lbl>map keyframes <span class=legend>(kf_count from get_map_size, polled every HIST_EVERY frames &mdash; slope = admission rate; a DROP means the map was reset/reloaded)</span></div><canvas id=kf height=90></canvas></div>
<div class=chart><div class=lbl>pgo_count  <span class=legend>(loop closures = steps)</span></div><canvas id=pgo height=90></canvas></div>
<div class=chart><div class=lbl><span class=o>p1_inliers</span> / <span class=r>p2_inliers</span></div><canvas id=inl height=90></canvas></div>
<div class=chart><div class=lbl>LC correction |c| per commit <span class=legend>(from LC-FORENSICS trace lines; a growing trend = the map is deforming under you)</span></div><canvas id=fcorr height=90></canvas></div>
<div class=chart><div class=lbl>anchor drift <span class=legend>(grey = your trajectory; dots = LC anchor keyframes; an anchor re-seen across laps grows a TRAIL &mdash; trails all crawling one way = the map deforming under you)</span></div>
<div id=fverdict style="font-size:12px;margin:1px 0 3px 2px;color:#8a94a6">waiting for LC commits…</div><canvas id=fanchor height=340></canvas></div>
<canvas id=taxis height=20 style="margin-top:-6px"></canvas>
<div class=chart>
 <div class="lbl tbar">
  <span>library trace &mdash; <span class=r>[Error]</span> / <span class=o>[Warning]</span> /
   <span class=b>[Message]</span> / <span style="color:#d5a021">[SLOW]</span> /
   <span class=p>[RECOVERY]</span> <span class=legend>(daemon stdout + cuVSLAM, per frame)</span></span>
  <input id=tfilt placeholder="filter…" autocomplete=off>
  <label><input type=checkbox id=tonly> errors + warnings only</label>
  <label><input type=checkbox id=tzoom checked> follow zoom window</label>
  <span id=tstat class=legend></span>
 </div>
 <div id=trace></div>
</div>
<div id=tip style="position:fixed;pointer-events:none;background:#000e;border:1px solid #555;padding:6px 8px;border-radius:4px;font-size:11px;white-space:pre;display:none;z-index:20;color:#eee"></div>
<script>
const C={t:0,track_ms:1,vo_ms:2,slam_ms:3,lc_ms:4,pgo_ms:5,vv:6,kf:7,lca:8,lcf:9,lcc:10,pgos:11,pgo_count:12,
         p1:13,p2:14,vox:15,voy:16,px:18,py:19,cpu:25,gpu:26,mem:27,rss:28,kfc:29};
const fin=v=>Number.isFinite(v);
let rows=[], paused=false, total=0, pid=null, memTotalMB=0, t0ms=null, nextSeq=0, colNames=[];
let view=null;          // {lo,hi} in t-seconds; null = full history
let sel=null;           // {a,b} live selection while picking a window
let dom={t0:0,t1:1};    // time domain currently plotted (for mouse<->time mapping)
let hoverT=null, mx=0, my=0, rafPending=false;
let traces=[], tNext=0, traceRun=null, tInflight=false, tSig='';
let pinT=null;          // trace line clicked -> cyan marker across every chart
const dpr=devicePixelRatio;
function fmtTime(t){
 if(t0ms==null)return t.toFixed(1)+'s';
 const d=new Date(t0ms+t*1000);
 const p=(n,w=2)=>String(n).padStart(w,'0');
 return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(),3)}`;
}
function nearestRow(t){
 if(!rows.length)return null;
 let lo=0,hi=rows.length-1;
 while(lo<hi){const m=(lo+hi)>>1;if(rows[m][C.t]<t)lo=m+1;else hi=m;}
 if(lo>0&&Math.abs(rows[lo-1][C.t]-t)<Math.abs(rows[lo][C.t]-t))lo--;
 return rows[lo];
}
function scheduleDraw(){if(!rafPending){rafPending=true;requestAnimationFrame(()=>{rafPending=false;draw();});}}
function drawAxis(){
 const cv=document.getElementById('taxis');if(!cv)return;
 const ctx=cv.getContext('2d'),W=cv.width/dpr,H=cv.height/dpr,pad=4;
 ctx.clearRect(0,0,W,H); if(dom.t1<=dom.t0)return;
 ctx.fillStyle='#999';ctx.font='10px monospace';
 const NT=6;
 for(let i=0;i<=NT;i++){const x=pad+(i/NT)*(W-2*pad),t=dom.t0+(dom.t1-dom.t0)*i/NT;
  ctx.strokeStyle='#333';ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,4);ctx.stroke();
  ctx.textAlign=i===0?'left':i===NT?'right':'center';ctx.fillText(fmtTime(t),Math.max(2,Math.min(W-2,x)),15);}
 ctx.textAlign='left';
}
function drawPin(){
 if(pinT==null||pinT<dom.t0||pinT>dom.t1)return;
 for(const id of TIMECV){const cv=document.getElementById(id);if(!cv)continue;
  const ctx=cv.getContext('2d'),W=cv.width/dpr,H=cv.height/dpr,pad=padFor(id);
  const x=pad+(pinT-dom.t0)/((dom.t1-dom.t0)||1)*(W-2*pad);
  ctx.strokeStyle='#00e5ff';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();}
}
function drawCursor(){
 if(hoverT==null)return;
 for(const id of TIMECV){const cv=document.getElementById(id);if(!cv)continue;
  const ctx=cv.getContext('2d'),W=cv.width/dpr,H=cv.height/dpr,pad=padFor(id);
  const x=pad+(hoverT-dom.t0)/((dom.t1-dom.t0)||1)*(W-2*pad);
  ctx.strokeStyle='#ffffff88';ctx.lineWidth=1;ctx.beginPath();ctx.moveTo(x,0);ctx.lineTo(x,H);ctx.stroke();}
 const r=nearestRow(hoverT), tip=document.getElementById('tip');
 if(!r){tip.style.display='none';return;}
 const g=k=>r[C[k]], f=(k,d=1)=>{const v=g(k);return v==null?'—':(+v).toFixed(d);};
 tip.textContent=
  `${fmtTime(g('t'))}   (t=${g('t')}s)\n`+
  `track ${f('track_ms')}ms  vo ${f('vo_ms')}  slam ${f('slam_ms')}  lc ${f('lc_ms',2)}  pgo ${f('pgo_ms',2)}\n`+
  `vo ${g('vv')?'OK':'LOST'}   kf ${g('kf')?'Y':'-'} (map ${g('kfc')??'—'})   lc a${g('lca')}/f${g('lcf')}/c${g('lcc')}   pgo_count ${g('pgo_count')}\n`+
  `inliers p1 ${g('p1')} / p2 ${g('p2')}\n`+
  `pos (${f('px',2)}, ${f('py',2)})   vo (${f('vox',2)}, ${f('voy',2)})\n`+
  `cpu ${f('cpu')}%  gpu ${f('gpu')}%  mem ${f('rss')}MB`;
 tip.style.display='block';
 const tw=tip.offsetWidth,th=tip.offsetHeight;
 tip.style.left=Math.min(mx+14,innerWidth-tw-6)+'px';
 tip.style.top=Math.max(6,my-th-10)+'px';
}
const TIMECV=['vv','lcev','pgoev','trk','lcpgo','cpu','gpu','mem','kf','pgo','inl'];
const padFor=id=>(id==='vv'||id==='pgoev'||id==='lcev')?0:4;  // strips draw edge-to-edge, line charts have 4px pad
function timeAt(cv,evt){
 const r=cv.getBoundingClientRect(),pad=padFor(cv.id);
 const frac=(evt.clientX-r.left-pad)/Math.max(1,(r.width-2*pad));
 return dom.t0+Math.max(0,Math.min(1,frac))*(dom.t1-dom.t0);
}
function overlaySel(){
 if(!sel)return;
 const lo=Math.min(sel.a,sel.b),hi=Math.max(sel.a,sel.b);
 for(const id of TIMECV){
  const cv=document.getElementById(id);if(!cv)continue;
  const ctx=cv.getContext('2d'),W=cv.width/devicePixelRatio,H=cv.height/devicePixelRatio,pad=padFor(id);
  const X=t=>pad+(t-dom.t0)/((dom.t1-dom.t0)||1)*(W-2*pad);
  ctx.fillStyle='#8ab4f82e';ctx.fillRect(X(lo),0,Math.max(1,X(hi)-X(lo)),H);
  ctx.strokeStyle='#8ab4f8';ctx.lineWidth=1;
  for(const t of [lo,hi]){ctx.beginPath();ctx.moveTo(X(t),0);ctx.lineTo(X(t),H);ctx.stroke();}
 }
}
function wireZoom(){
 for(const id of TIMECV){
  const cv=document.getElementById(id);if(!cv)continue;
  cv.style.cursor='crosshair';
  cv.addEventListener('click',e=>{
   const t=timeAt(cv,e);
   if(!sel){sel={a:t,b:t};}                       // 1st click: anchor
   else{const lo=Math.min(sel.a,sel.b),hi=Math.max(sel.a,sel.b);
        if(hi-lo>1e-3){view={lo,hi};tSig='';}     // 2nd click: commit window
        sel=null;}
   draw();renderTrace();
  });
  cv.addEventListener('mousemove',e=>{mx=e.clientX;my=e.clientY;hoverT=timeAt(cv,e);if(sel)sel.b=hoverT;scheduleDraw();});
  cv.addEventListener('mouseleave',()=>{hoverT=null;document.getElementById('tip').style.display='none';scheduleDraw();});
 }
 addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;            // don't eat typing in the trace filter
  if(e.key==='r'||e.key==='R'){view=null;sel=null;pinT=null;tSig='';draw();renderTrace();}
  if(e.key==='Escape'){sel=null;draw();}
 });
}
function toCSV(){
 const head=(colNames.length?colNames:[]).join(',');
 return head+'\\n'+rows.map(r=>r.map(v=>v==null?'':v).join(',')).join('\\n');
}
function dlCSV(){
 const b=new Blob([toCSV()],{type:'text/csv'});
 const a=document.createElement('a');a.href=URL.createObjectURL(b);
 a.download='slam_v1_debug.csv';a.click();URL.revokeObjectURL(a.href);
}
async function copyCsv(btn){
 const txt=toCSV();
 try{ await navigator.clipboard.writeText(txt); }
 catch(e){ const ta=document.createElement('textarea');ta.value=txt;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove(); }
 const o=btn.textContent;btn.textContent='✓ Copied '+rows.length+' rows';setTimeout(()=>btn.textContent=o,1500);
}
function fit(cv){const r=cv.getBoundingClientRect();cv.width=r.width*devicePixelRatio;cv.height=cv.height*devicePixelRatio;cv.getContext('2d').scale(devicePixelRatio,devicePixelRatio);}
window.addEventListener('resize',()=>{[...document.querySelectorAll('canvas')].forEach(c=>{c.width=c.width;});draw();});

function line(cv,xs,series,lost){
 const ctx=cv.getContext('2d'),W=cv.width/devicePixelRatio,H=cv.height/devicePixelRatio,pad=4;
 ctx.clearRect(0,0,W,H);
 if(!xs.length)return;
 let lo=1e30,hi=-1e30;for(const s of series)for(const v of s.d){if(fin(v)){if(v<lo)lo=v;if(v>hi)hi=v;}}
 if(lo>hi){lo=0;hi=1;} if(lo===hi){hi=lo+1;lo-=1;} const xr=xs[xs.length-1]-xs[0]||1;
 const X=x=>pad+(x-xs[0])/xr*(W-2*pad), Y=v=>H-pad-(v-lo)/(hi-lo)*(H-2*pad);
 if(lost){ctx.fillStyle='#e8443a33';const bw=Math.max(2,(W-2*pad)/xs.length+1);
  for(let i=0;i<lost.length;i++){if(lost[i])ctx.fillRect(X(xs[i])-1,0,bw,H);}}
 for(const s of series){ctx.strokeStyle=s.c;ctx.lineWidth=1;ctx.beginPath();let pen=false;
  for(let i=0;i<s.d.length;i++){if(!fin(s.d[i])){pen=false;continue;}const x=X(xs[i]),y=Y(s.d[i]);pen?ctx.lineTo(x,y):ctx.moveTo(x,y);pen=true;}
  ctx.stroke();}
 ctx.fillStyle='#666';ctx.font='10px monospace';ctx.fillText(hi.toFixed(1),2,10);ctx.fillText(lo.toFixed(1),2,H-2);
}
function strip(cv,xs,lost){
 const ctx=cv.getContext('2d'),W=cv.width/devicePixelRatio,H=cv.height/devicePixelRatio;
 ctx.clearRect(0,0,W,H); if(!xs.length)return;
 const xr=xs[xs.length-1]-xs[0]||1, X=x=>(x-xs[0])/xr*W;
 ctx.fillStyle='#2ecc71';ctx.fillRect(0,0,W,H);              // green base (no unpainted gaps)
 ctx.fillStyle='#e8443a';
 const bw=Math.max(3,W/xs.length+1);                          // min 3px so 1 lost frame is visible
 for(let i=0;i<lost.length;i++){if(lost[i])ctx.fillRect(Math.max(0,X(xs[i])-1),0,bw,H);}
}
function lcstrip(cv,xs,att,found,comm){
 const ctx=cv.getContext('2d'),W=cv.width/devicePixelRatio,H=cv.height/devicePixelRatio;
 ctx.clearRect(0,0,W,H); if(!xs.length)return;
 const xr=xs[xs.length-1]-xs[0]||1, X=x=>(x-xs[0])/xr*W;
 ctx.fillStyle='#1b1b1b';ctx.fillRect(0,0,W,H);
 const bw=Math.max(3,W/xs.length+1);
 // paint weakest->strongest so a commit always wins the pixel
 for(const [arr,col] of [[att,'#5a6b8c'],[found,'#e8a33a'],[comm,'#2ecc71']])
  {ctx.fillStyle=col;for(let i=0;i<arr.length;i++){if(arr[i])ctx.fillRect(Math.max(0,X(xs[i])-1),0,bw,H);}}
}
function events(cv,xs,ev,col){
 const ctx=cv.getContext('2d'),W=cv.width/devicePixelRatio,H=cv.height/devicePixelRatio;
 ctx.clearRect(0,0,W,H); if(!xs.length)return;
 const xr=xs[xs.length-1]-xs[0]||1, X=x=>(x-xs[0])/xr*W;
 ctx.fillStyle='#1b1b1b';ctx.fillRect(0,0,W,H);
 ctx.fillStyle=col;
 const bw=Math.max(3,W/xs.length+1);
 for(let i=0;i<ev.length;i++){if(ev[i])ctx.fillRect(Math.max(0,X(xs[i])-1),0,bw,H);}
}
function xyplot(cv,rows){
 const ctx=cv.getContext('2d'),W=cv.width/devicePixelRatio,H=cv.height/devicePixelRatio,pad=20;
 ctx.clearRect(0,0,W,H); if(!rows.length)return;
 let lo=1e30,hi=-1e30,lyo=1e30,lyh=-1e30;
 for(const r of rows){for(const x of [r[C.px],r[C.vox]]){if(fin(x)){if(x<lo)lo=x;if(x>hi)hi=x;}}for(const y of [r[C.py],r[C.voy]]){if(fin(y)){if(y<lyo)lyo=y;if(y>lyh)lyh=y;}}}
 if(lo>hi){lo=-1;hi=1;lyo=-1;lyh=1;}
 const exW=hi-lo, exH=lyh-lyo, MIN=1.0;
 if(hi-lo<MIN){const c=(hi+lo)/2;lo=c-MIN/2;hi=c+MIN/2;}
 if(lyh-lyo<MIN){const c=(lyh+lyo)/2;lyo=c-MIN/2;lyh=c+MIN/2;}
 const s=Math.min((W-2*pad)/((hi-lo)||1),(H-2*pad)/((lyh-lyo)||1));
 const X=x=>pad+(x-lo)*s,Y=y=>H-pad-(y-lyo)*s;
 const path=(ci,cj,col)=>{ctx.strokeStyle=col;ctx.lineWidth=1.3;ctx.beginPath();let pen=false;
  for(let i=0;i<rows.length;i++){const vx=rows[i][ci],vy=rows[i][cj];if(!fin(vx)||!fin(vy)){pen=false;continue;}
   const x=X(vx),y=Y(vy);pen?ctx.lineTo(x,y):ctx.moveTo(x,y);pen=true;}ctx.stroke();};
 path(C.vox,C.voy,'#5b9bf8');
 ctx.lineWidth=1.3;let pen=false,px=0,py=0;
 for(let i=0;i<rows.length;i++){const vx=rows[i][C.px],vy=rows[i][C.py];if(!fin(vx)||!fin(vy)){pen=false;continue;}
  const x=X(vx),y=Y(vy),ok=rows[i][C.vv];
  ctx.strokeStyle=ok?'#2ecc71':'#e8443a';ctx.beginPath();ctx.moveTo(pen?px:x,pen?py:y);ctx.lineTo(x,y);ctx.stroke();
  if(!ok){ctx.fillStyle='#e8443a';ctx.beginPath();ctx.arc(x,y,3,0,7);ctx.fill();}
  px=x;py=y;pen=true;}
 const last=rows[rows.length-1];
 if(fin(last[C.px])&&fin(last[C.py])){ctx.fillStyle=last[C.vv]?'#2ecc71':'#e8443a';ctx.beginPath();ctx.arc(X(last[C.px]),Y(last[C.py]),4,0,7);ctx.fill();}
 ctx.fillStyle='#888';ctx.font='11px monospace';
 ctx.fillText(`extent ${exW.toFixed(2)}m x ${exH.toFixed(2)}m  ·  view ${(hi-lo).toFixed(1)}m`,8,H-8);
}
function draw(){
 // zoom window: plot/stat only the rows inside it (full history stays in `rows`)
 const src=view?rows.filter(r=>r[C.t]>=view.lo&&r[C.t]<=view.hi):rows;
 const MAXPTS=2000, N=src.length, step=Math.max(1,Math.ceil(N/MAXPTS));
 // kf_count is the daemon's own map keyframe total — a level, not an event, so it is
 // read straight off the row instead of re-summing the per-frame kf flag.
 let lostTotal=0,kfFirst=null,kfLast=null;
 for(const r of src){if(!r[C.vv])lostTotal++;
  const k=r[C.kfc]; if(k!=null){if(kfFirst===null)kfFirst=k; kfLast=k;}}
 // decimate for rendering, but OR vo-loss across each bucket so a dropout can't be sampled away
 let pr=[],lost=[],pgoEv=[],lcA=[],lcF=[],lcC=[];
 let bucketLost=false,bucketPgo=false,bA=false,bF=false,bC=false;
 let nA=0,nF=0,nC=0,nPgo=0;   // separate running totals
 for(let i=0;i<N;i++){
   if(!src[i][C.vv])bucketLost=true;
   if(src[i][C.lca]){bA=true;nA++;}
   if(src[i][C.lcf]){bF=true;nF++;}
   if(src[i][C.lcc]){bC=true;nC++;}
   // PGO ran = optimisation time recorded, or the daemon's own counter ticked
   if(src[i][C.pgo_ms]>0||(i>0&&src[i][C.pgo_count]!==src[i-1][C.pgo_count])){bucketPgo=true;nPgo++;}
   if(i%step===0||i===N-1){pr.push(src[i]);lost.push(bucketLost);pgoEv.push(bucketPgo);
    lcA.push(bA);lcF.push(bF);lcC.push(bC);
    bucketLost=false;bucketPgo=false;bA=false;bF=false;bC=false;}
 }
 const xs=pr.map(r=>r[C.t]);
 if(xs.length)dom={t0:xs[0],t1:xs[xs.length-1]};
 strip(document.getElementById('vv'),xs,lost);
 lcstrip(document.getElementById('lcev'),xs,lcA,lcF,lcC);
 events(document.getElementById('pgoev'),xs,pgoEv,'#8ab4f8');
 xyplot(document.getElementById('xy'),pr);
 line(document.getElementById('trk'),xs,[
   {d:pr.map(r=>r[C.track_ms]),c:'#ddd'},
   {d:pr.map(r=>r[C.vo_ms]),c:'#5b9bf8'},
   {d:pr.map(r=>r[C.slam_ms]),c:'#c58af9'}],lost);
 line(document.getElementById('lcpgo'),xs,[{d:pr.map(r=>r[C.lc_ms]),c:'#e8a33a'},{d:pr.map(r=>r[C.pgo_ms]),c:'#e8443a'}],lost);
 line(document.getElementById('cpu'),xs,[{d:pr.map(r=>r[C.cpu]),c:'#e8a33a'}],lost);
 line(document.getElementById('gpu'),xs,[{d:pr.map(r=>r[C.gpu]),c:'#2ecc71'}],lost);
 line(document.getElementById('mem'),xs,[{d:pr.map(r=>r[C.rss]),c:'#5b9bf8'}],lost);
 line(document.getElementById('kf'),xs,[{d:pr.map(r=>r[C.kfc]),c:'#2ecc71'}],lost);
 line(document.getElementById('pgo'),xs,[{d:pr.map(r=>r[C.pgo_count]),c:'#8ab4f8'}],lost);
 line(document.getElementById('inl'),xs,[{d:pr.map(r=>r[C.p1]),c:'#e8a33a'},{d:pr.map(r=>r[C.p2]),c:'#e8443a'}],lost);
 const l=pr.length?pr[pr.length-1]:null;
 if(l)document.getElementById('memtot').textContent=`— ${l[C.rss]} MB of ${memTotalMB} MB total (${l[C.mem]}%)`;
 drawAxis();
 overlaySel();
 drawPin();
 drawCursor();
 drawForens();
 document.getElementById('stat').textContent=`${total} frames (${pr.length} plotted)`+(l?
   ` · t=${l[C.t]}s · track=${l[C.track_ms]}ms (vo ${l[C.vo_ms]} / slam ${l[C.slam_ms]} / lc ${l[C.lc_ms]} / pgo ${l[C.pgo_ms]})`+
   ` · cpu ${l[C.cpu]}% · gpu ${l[C.gpu]}% · mem ${l[C.rss]}MB`+
   ` · map kf=${kfLast==null?'—':kfLast}${(kfFirst!=null&&kfLast!=null&&kfLast!==kfFirst)?` (${kfLast-kfFirst>=0?'+':''}${kfLast-kfFirst} in view)`:''}`+
   ` · lc ${nA}att/${nF}found/${nC}commit · pgo ${nPgo} runs (count=${l[C.pgo_count]})`+` · vo ${l[C.vv]?'OK':'LOST'} · ${lostTotal} lost`:'')
   +(pid?` · pid ${pid}`:' · slam not found')
   +(view?` · 🔍 ${view.lo.toFixed(1)}–${view.hi.toFixed(1)}s (r=reset)`:(sel?' · click again to zoom (Esc cancels)':' · click a chart to start a zoom window'));
}
const ESC={'&':'&amp;','<':'&lt;','>':'&gt;'};
const esc=s=>s.replace(/[&<>]/g,c=>ESC[c]);
const SEV=s=>/\\[Error\\]/i.test(s)?'#e8443a':/\\[Warning\\]/i.test(s)?'#e8a33a'
            :/\\[SLOW\\]/.test(s)?'#d5a021':/\\[RECOVERY\\]/.test(s)?'#c58af9'
            :/\\[Message\\]/i.test(s)?'#5b9bf8':'#b8c0cc';
const MAXTL=1500;       // DOM cap — the ring holds far more, we list the newest
function renderTrace(){
 const el=document.getElementById('trace');
 const q=document.getElementById('tfilt').value.trim().toLowerCase();
 const only=document.getElementById('tonly').checked;
 const follow=document.getElementById('tzoom').checked;
 let src=traces;
 if(follow&&view)src=src.filter(x=>x[0]>=view.lo&&x[0]<=view.hi);
 if(only)src=src.filter(x=>/\\[(Error|Warning)\\]/i.test(x[1]));
 if(q)src=src.filter(x=>x[1].toLowerCase().includes(q));
 const show=src.slice(-MAXTL);
 // re-rendering 1500 nodes on every 1Hz poll is wasteful when nothing changed
 const sig=src.length+'|'+(show.length?show[show.length-1][0]+show[show.length-1][1]:'')+'|'+pinT;
 if(sig!==tSig){
  tSig=sig;
  const stick=el.scrollTop+el.clientHeight>=el.scrollHeight-6;
  el.innerHTML=show.map(x=>
   `<div class="tl${x[0]===pinT?' pin':''}" data-t="${x[0]}">`+
   `<span class=tt>${fmtTime(x[0])}</span> <span class=ts>t=${x[0].toFixed(2)}s</span> `+
   `<span style="color:${SEV(x[1])}">${esc(x[1])}</span></div>`).join('')
   ||'<span style="color:#666">(no trace lines'+(traces.length?' match this filter':' yet')+')</span>';
  if(stick)el.scrollTop=el.scrollHeight;
 }
 document.getElementById('tstat').textContent=
  `${src.length} shown${src.length>show.length?` (last ${MAXTL} listed)`:''} · ${traces.length} captured`
  +(follow&&view?' · zoom window':'')+(pinT!=null?` · 📌 t=${pinT.toFixed(2)}s`:'');
}
function wireTrace(){
 for(const id of ['tfilt','tonly','tzoom'])
  document.getElementById(id).addEventListener('input',()=>{tSig='';renderTrace();});
 document.getElementById('trace').addEventListener('click',e=>{
  const d=e.target.closest('.tl'); if(!d)return;
  const t=+d.dataset.t;
  pinT=(pinT===t)?null:t;                    // click the pinned line again to unpin
  tSig='';renderTrace();draw();
 });
}
async function tickTrace(){
 if(paused||tInflight)return;
 tInflight=true;
 try{
  const d=await (await fetch('/trace?since='+tNext,{cache:'no-store'})).json();
  if(d.run!==traceRun){traceRun=d.run;traces=[];tNext=0;tSig='';}
  for(const l of d.lines)traces.push(l);
  tNext=d.start+d.lines.length;
  renderTrace();
  drawForens();
  if(tNext<d.total)setTimeout(tickTrace,0);          // backfill in chunks
 }catch(e){}
 finally{tInflight=false;}
}
let inflight=false, myRun=null;
async function tick(){
 if(paused||inflight)return;
 inflight=true;
 try{
  const d=await (await fetch('/series?since='+nextSeq,{cache:'no-store'})).json();
  if(d.run!==myRun){myRun=d.run;rows=[];nextSeq=0;draw();setTimeout(tick,0);return;}  // server restart/clear -> resync from 0
  colNames=d.cols;
  for(const r of d.rows)rows.push(r);              // loop, NOT spread (spread blows the stack)
  nextSeq=d.start+d.rows.length;                   // absolute index of next wanted row
  total=d.total; pid=d.pid; memTotalMB=d.mem_total_mb; if(d.t0_ms!=null)t0ms=d.t0_ms;
  draw();
  if(nextSeq<total)setTimeout(tick,0);             // still backfilling -> keep pulling chunks
 }catch(e){}
 finally{inflight=false;}
}
// ---- LC forensics: parse LC-FORENSICS trace lines into per-commit records ----
let forens=[],fParsedLen=0,fRun=null;
function parseForens(){
 if(traceRun!==fRun){fRun=traceRun;forens=[];fParsedLen=0;}
 for(let i=fParsedLen;i<traces.length;i++){
  const l=traces[i][1];
  const m=l.match(/LC-FORENSICS corr=\\(([-0-9.]+),([-0-9.]+),([-0-9.]+)deg\\) \\|c\\|=([0-9.]+)m at=\\(([-0-9.]+),([-0-9.]+)\\) anchors=([0-9]+)(.*)/);
  if(!m)continue;
  const anchors=[];const re=/a[0-9]+=([0-9]+)\\(([-0-9.]+),([-0-9.]+),([-0-9.]+)\\)/g;let am;
  while((am=re.exec(m[8]))!==null)anchors.push({id:+am[1],x:+am[2],y:+am[3],yaw:+am[4]});
  forens.push({t:traces[i][0],dx:+m[1],dy:+m[2],dyaw:+m[3],n:+m[4],ax:+m[5],ay:+m[6],anchors});
 }
 fParsedLen=traces.length;
}
function drawForens(){
 parseForens();
 const src=view?forens.filter(f=>f.t>=view.lo&&f.t<=view.hi):forens;
 line(document.getElementById('fcorr'),src.map(f=>f.t),[{d:src.map(f=>f.n),c:'#e8a33a'}]);
 const cv=document.getElementById('fanchor'),ctx=cv.getContext('2d');
 const W=cv.width/devicePixelRatio,H=cv.height/devicePixelRatio,pad=20;
 ctx.clearRect(0,0,W,H);
 // Anchors are logged in cuvslam's WORLD ground plane; the trajectory rows are
 // BASE frame. Self-calibrate the 2D rigid transform between them from the
 // per-commit pairs (robot world-ground position `at` <-> base pos at that t).
 let pairs=[];
 for(const f of src){const r=nearestRow(f.t);
  if(r&&fin(r[C.px])&&fin(r[C.py])&&fin(f.ax))pairs.push([f.ax,f.ay,r[C.px],r[C.py]]);}
 let W2B=null;
 if(pairs.length>=2){
  let cax=0,cay=0,cbx=0,cby=0;
  for(const q of pairs){cax+=q[0];cay+=q[1];cbx+=q[2];cby+=q[3];}
  const n=pairs.length;cax/=n;cay/=n;cbx/=n;cby/=n;
  let sc=0,ss=0;
  for(const q of pairs){const ax=q[0]-cax,ay=q[1]-cay,bx=q[2]-cbx,by=q[3]-cby;
   sc+=ax*bx+ay*by;ss+=ax*by-ay*bx;}
  const th=Math.atan2(ss,sc),c=Math.cos(th),si=Math.sin(th);
  W2B={c,si,tx:cbx-(c*cax-si*cay),ty:cby-(si*cax+c*cay)};
 }
 // Render in the WORLD frame: anchors keep their native coords so a dot only
 // moves on screen when the GRAPH moves it (the signal). The trajectory is the
 // thing re-projected as the alignment refines with each new commit pair.
 const b2w=(x,y)=>W2B?{x:W2B.c*(x-W2B.tx)+W2B.si*(y-W2B.ty),y:-W2B.si*(x-W2B.tx)+W2B.c*(y-W2B.ty)}:null;
 const byId={};
 for(const f of src)for(const a of f.anchors)(byId[a.id]=byId[a.id]||[]).push(a);
 const ids=Object.keys(byId), v=document.getElementById('fverdict');
 if(!ids.length){v.textContent=forens.length?'no anchors parsed':'no LC-FORENSICS lines yet \u2014 forensics lib not running?';v.style.color='#8a94a6';return;}
 // extent covers the anchors AND the driven trajectory (spatial context)
 let lo=1e30,hi=-1e30,lyo=1e30,lyh=-1e30;
 const grow=(x,y)=>{if(fin(x)&&fin(y)){if(x<lo)lo=x;if(x>hi)hi=x;if(y<lyo)lyo=y;if(y>lyh)lyh=y;}};
 for(const id of ids)for(const p of byId[id])grow(p.x,p.y);
 const tstep=Math.max(1,Math.ceil(rows.length/2000));
 for(let i=0;i<rows.length;i+=tstep){const q=b2w(rows[i][C.px],rows[i][C.py]);if(q)grow(q.x,q.y);}
 const MIN=1.0;
 if(hi-lo<MIN){const c=(hi+lo)/2;lo=c-MIN/2;hi=c+MIN/2;}
 if(lyh-lyo<MIN){const c=(lyh+lyo)/2;lyo=c-MIN/2;lyh=c+MIN/2;}
 const s=Math.min((W-2*pad)/(hi-lo),(H-2*pad)/(lyh-lyo));
 const X=x=>pad+(x-lo)*s,Y=y=>H-pad-(y-lyo)*s;
 // trajectory underlay, re-projected base->world (wobbles slightly while the
 // alignment converges over the first handful of commits; anchors never do)
 ctx.strokeStyle='#3c4250';ctx.lineWidth=1;ctx.beginPath();let pen=false;
 for(let i=0;i<rows.length;i+=tstep){const x=rows[i][C.px],y=rows[i][C.py];
  if(!fin(x)||!fin(y)){pen=false;continue;}
  const q=b2w(x,y);if(!q){pen=false;continue;}
  pen?ctx.lineTo(X(q.x),Y(q.y)):ctx.moveTo(X(q.x),Y(q.y));pen=true;}
 ctx.stroke();
 // 50cm scale bar
 ctx.strokeStyle='#666';ctx.lineWidth=2;ctx.beginPath();
 ctx.moveTo(pad,H-6);ctx.lineTo(pad+0.5*s,H-6);ctx.stroke();
 ctx.fillStyle='#888';ctx.font='10px monospace';ctx.fillText('0.5m',pad+0.5*s+4,H-4);
 let worst=null;const fLabels=[];
 for(const id of ids){
  const ps=byId[id],col=`hsl(${(+id*137)%360},70%,60%)`;
  ctx.strokeStyle=col;ctx.lineWidth=1.6;ctx.beginPath();
  ps.forEach((p,i)=>i?ctx.lineTo(X(p.x),Y(p.y)):ctx.moveTo(X(p.x),Y(p.y)));ctx.stroke();
  const l=ps[ps.length-1];
  ctx.fillStyle=col;ctx.beginPath();ctx.arc(X(l.x),Y(l.y),3,0,7);ctx.fill();
  const d=ps.length>1?Math.hypot(l.x-ps[0].x,l.y-ps[0].y):0;
  // Label ONLY what's informative (repeats or movement), never overprint:
  // single-sighting anchors stay anonymous dots — the corner count covers them.
  if(ps.length>=2||d>0.05){
   const txt='#\u2026'+String(id).slice(-4)+'\u00d7'+ps.length+(d>0.05?' '+(d*100).toFixed(0)+'cm':'');
   const lx=X(l.x)+5,ly=Y(l.y)-4,w=txt.length*6.2,h=11;
   let clash=false;
   for(const r of fLabels){if(lx<r.x+r.w&&lx+w>r.x&&ly-h<r.y&&ly>r.y-h){clash=true;break;}}
   if(!clash){fLabels.push({x:lx,y:ly,w,h});
    ctx.fillStyle=d>0.05?'#eee':'#8a94a6';ctx.font='10px monospace';ctx.fillText(txt,lx,ly);}
  }
  if(ps.length>=3){
   if(!worst||d>worst.d)worst={id,d,n:ps.length,ang:Math.atan2(l.y-ps[0].y,l.x-ps[0].x)*57.3};
  }
 }
 ctx.fillStyle='#888';ctx.font='11px monospace';
 ctx.fillText(`${ids.length} anchors \u00b7 ${src.length} commits \u00b7 view ${(hi-lo).toFixed(1)}m`,8,12);
 if(!worst){v.style.color='#666';v.textContent=` ${ids.length} anchors, too few repeats to judge`;}
 else if(worst.d<0.10){v.style.color='#2ecc71';v.textContent=` anchors stable (max drift ${(worst.d*100).toFixed(0)}cm over ${worst.n} sightings)`;}
 else{v.style.color='#e8443a';v.textContent=` ⚠ MAP DEFORMING: anchor ${worst.id} drifted ${(worst.d*100).toFixed(0)}cm over ${worst.n} commits toward ${worst.ang.toFixed(0)}°`;}
}
[...document.querySelectorAll('canvas')].forEach(fit);
wireZoom();wireTrace();
setInterval(tick,400);tick();
setInterval(tickTrace,1000);tickTrace();
</script>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


if __name__ == "__main__":
    threading.Thread(target=stats_loop, daemon=True).start()
    threading.Thread(target=reader_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8020, log_level="warning")

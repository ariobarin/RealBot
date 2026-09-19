#!/usr/bin/env python3
# /// script
# dependencies = [
#   "bbos",
#   "fastapi",
#   "uvicorn",
#   "numpy",
#   "websockets",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
import asyncio
import threading
from queue import Queue
import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response
from fastapi.responses import HTMLResponse
import uvicorn
from bbos import Reader

points_queue = Queue(maxsize=2)

def pointcloud_reader():
    with Reader('camera.points') as r:
        while True:
            if r.ready():
                data = r.data
                print(data['num_points'])
                try:
                    points_queue.put_nowait(data)
                except:
                    try:
                        points_queue.get_nowait()
                        points_queue.put_nowait(data)
                    except:
                        pass

app = FastAPI()

@app.get("/", response_class=HTMLResponse)
async def index():
    html = '''
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Point Cloud Stream</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    body { margin: 0; padding: 0; background: #0a0a0a; font-family: -apple-system, sans-serif; color: #fff; overflow: hidden; }
    #viewer { width: 100vw; height: 100vh; }
    #info { 
      position: absolute; top: 10px; left: 10px; background: rgba(0,0,0,0.8); 
      padding: 10px; border-radius: 5px; font-size: 12px; font-family: monospace;
    }
    #status { position: absolute; top: 10px; right: 10px; padding: 5px 10px; border-radius: 5px; font-size: 12px; }
    .connected { background: #10b981; color: #000; }
    .disconnected { background: #ef4444; color: #fff; }
  </style>
</head>
<body>
<div id="viewer"></div>
<div id="info">
  <div>Points: <span id="numPoints">0</span></div>
  <div>FPS: <span id="fps">0</span></div>
</div>
<div id="status" class="disconnected">Disconnected</div>

<script type="importmap">
{ "imports": { "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js" } }
</script>

<script type="module">
import * as THREE from 'three';
import { OrbitControls } from 'https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/controls/OrbitControls.js';

// Float16 to Float32 conversion
const float16ToFloat32 = (h) => {
  const s = (h & 0x8000) >> 15;
  const e = (h & 0x7C00) >> 10;
  const f = h & 0x03FF;
  
  if(e === 0) {
    return (s ? -1 : 1) * Math.pow(2, -14) * (f / Math.pow(2, 10));
  } else if (e === 0x1F) {
    return f ? NaN : ((s ? -1 : 1) * Infinity);
  }
  
  return (s ? -1 : 1) * Math.pow(2, e - 15) * (1 + (f / Math.pow(2, 10)));
};

// Initialize scene
const viewer = document.getElementById('viewer');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0a0a);

const camera = new THREE.PerspectiveCamera(75, window.innerWidth / window.innerHeight, 0.01, 100);
camera.position.set(0, -0.05, 3);
camera.lookAt(0, 0, 0);
camera.up.set(0, 0, 1);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setSize(window.innerWidth, window.innerHeight);
renderer.setPixelRatio(window.devicePixelRatio);
viewer.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.object.up.set(0, 0, 1);
controls.screenSpacePanning = false;
controls.enableDamping = true;
controls.dampingFactor = 0.05;
controls.rotateSpeed = 0.8;
controls.zoomSpeed = 0.6;
controls.panSpeed = 0.6;

// Lighting
scene.add(new THREE.AmbientLight(0xffffff, 0.6));
const directionalLight = new THREE.DirectionalLight(0xffffff, 0.4);
directionalLight.position.set(5, 5, 5);
scene.add(directionalLight);

// Grid and axes
const grid = new THREE.GridHelper(10, 10, 0x444444, 0x222222);
grid.rotation.x = Math.PI / 2;
scene.add(grid);
scene.add(new THREE.AxesHelper(1));

// Point cloud
const geometry = new THREE.BufferGeometry();
const material = new THREE.PointsMaterial({ 
  size: 0.01, 
  vertexColors: true, 
  sizeAttenuation: true 
});
const points = new THREE.Points(geometry, material);
scene.add(points);

// Handle window resize
window.addEventListener('resize', () => {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
});

// FPS counter
let frameCount = 0;
let lastTime = performance.now();
const fpsElement = document.getElementById('fps');
const numPointsElement = document.getElementById('numPoints');
const statusElement = document.getElementById('status');

// WebSocket connection
let ws = null;
let reconnectTimer = null;

function connect() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(`${protocol}//${window.location.host}/ws/points`);
  ws.binaryType = 'arraybuffer';
  
  ws.onopen = () => {
    console.log('Connected to point cloud stream');
    statusElement.textContent = 'Connected';
    statusElement.className = 'connected';
  };
  
  ws.onmessage = (event) => {
    if (event.data instanceof ArrayBuffer) {
      const buffer = event.data;
      const view = new DataView(buffer);
      
      // Read header
      const numPoints = view.getInt32(0, true);
      
      if (numPoints > 0 && numPoints < 200000) {
        const pointsOffset = 4;
        const pointsSize = numPoints * 3 * 2;
        const colorsOffset = pointsOffset + pointsSize;
        
        // Read float16 positions and convert to float32
        const float16Positions = new Uint16Array(buffer, pointsOffset, numPoints * 3);
        const positions = new Float32Array(numPoints * 3);
        
        for (let i = 0; i < numPoints * 3; i++) {
          positions[i] = float16ToFloat32(float16Positions[i]);
        }
        
        // Update geometry
        geometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
        
        // Read colors if available
        if (buffer.byteLength > colorsOffset) {
          const colors8 = new Uint8Array(buffer, colorsOffset, numPoints * 3);
          const colors = new Float32Array(numPoints * 3);
          for (let i = 0; i < numPoints * 3; i++) {
            colors[i] = colors8[i] / 255.0;
          }
          geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
        }
        
        geometry.attributes.position.needsUpdate = true;
        if (geometry.attributes.color) {
          geometry.attributes.color.needsUpdate = true;
        }
        geometry.computeBoundingSphere();
        
        numPointsElement.textContent = numPoints.toLocaleString();
      }
    }
  };
  
  ws.onerror = (error) => {
    console.error('WebSocket error:', error);
  };
  
  ws.onclose = () => {
    console.log('WebSocket closed');
    statusElement.textContent = 'Disconnected';
    statusElement.className = 'disconnected';
    
    // Reconnect after 1 second
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connect, 1000);
  };
}

// Start connection
connect();

// Render loop
function animate() {
  requestAnimationFrame(animate);
  
  controls.update();
  renderer.render(scene, camera);
  
  // Update FPS counter
  frameCount++;
  const currentTime = performance.now();
  if (currentTime - lastTime >= 1000) {
    fpsElement.textContent = frameCount;
    frameCount = 0;
    lastTime = currentTime;
  }
}

animate();
</script>
</body>
</html>
    '''
    return HTMLResponse(content=html)

@app.websocket("/ws/points")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connection established for point cloud")
    
    try:
        while True:
            try:
                # Get latest point cloud data
                data = points_queue.get(timeout=0.05)
                
                # Pack binary data
                num_points = int(data['num_points'])
                if num_points > 0 and num_points < 200000:
                    # Header: 4 bytes (num_points as int32)
                    header = np.array([num_points], dtype=np.int32).tobytes()
                    # Points: num_points * 3 * 2 bytes (float16)
                    points_data = data['points'][:num_points].tobytes()
                    # Colors: num_points * 3 * 1 byte (uint8)
                    colors_data = data['colors'][:num_points].tobytes() if 'colors' in data.dtype.names else b''
                    
                    # Send as binary message
                    await websocket.send_bytes(header + points_data + colors_data)
                    
            except:
                await asyncio.sleep(0.01)
                
    except WebSocketDisconnect:
        print("WebSocket disconnected")
    except Exception as e:
        print(f"WebSocket error: {e}")

@app.get("/status")
async def get_status():
    """Get current point cloud status"""
    try:
        # Peek at the queue without removing
        if not points_queue.empty():
            return {"status": "active", "queue_size": points_queue.qsize()}
        else:
            return {"status": "waiting", "queue_size": 0}
    except:
        return {"status": "error"}

def main():
    reader_thread = threading.Thread(target=pointcloud_reader, daemon=True)
    reader_thread.start()
    
    import socket
    hostname = socket.gethostname()
    print(f"[+] Starting point cloud stream server on http://{hostname}.local:8004")
    print(f"[+] View stream at http://{hostname}.local:8004/")
    print(f"[+] Status endpoint at http://{hostname}.local:8004/status")
    
    uvicorn.run(app, host="0.0.0.0", port=8004, log_level="error", 
                access_log=False)

if __name__ == "__main__":
    main()

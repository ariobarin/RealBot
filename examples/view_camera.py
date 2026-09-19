# /// script
# dependencies = [
#   "bbos",
#   "fastapi",
#   "uvicorn",
# ]
# [tool.uv.sources]
# bbos = { path = "/home/bracketbot/bbos", editable = true }
# ///
import asyncio
import contextlib
import threading
from queue import Queue, Empty
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse
import uvicorn
from bbos import Reader
import socket

CAMS = ["head", "left", "right"]
FPS = 30
queues = {cam: Queue(maxsize=2) for cam in CAMS}

# One loop for every Reader: Loop's global pacing state is not thread-safe.
def camera_reader():
    with contextlib.ExitStack() as stack:
        readers = {c: stack.enter_context(Reader(f"camera.{c}.jpeg")) for c in CAMS}
        while True:
            for cam, r in readers.items():
                if not r.ready():
                    continue
                jpeg_bytes = bytes(r.data['jpeg'][:r.data['jpeg_len']])
                q = queues[cam]
                try:
                    q.put_nowait(jpeg_bytes)
                except:
                    try:
                        q.get_nowait()
                        q.put_nowait(jpeg_bytes)
                    except:
                        pass

app = FastAPI()

def make_stream(cam):
    q = queues[cam]
    async def generate():
        while True:
            try:
                frame = q.get_nowait()
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')
            except Empty:
                pass
            await asyncio.sleep(1 / FPS)
    return generate

for _cam in CAMS:
    _gen = make_stream(_cam)

    def _make_routes(cam, gen):
        @app.get(f"/{cam}/stream")
        async def stream(g=gen):
            return StreamingResponse(
                g(),
                media_type="multipart/x-mixed-replace; boundary=frame",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        @app.get(f"/{cam}/frame")
        async def frame(c=cam):
            try:
                f = queues[c].get(timeout=0.5)
                return Response(content=f, media_type="image/jpeg")
            except:
                return Response(status_code=503)

    _make_routes(_cam, _gen)

@app.get("/")
async def index():
    imgs = "\n".join(
        f'<div><h2>{cam}</h2><img src="/{cam}/stream" /></div>'
        for cam in CAMS
    )
    html = f'''
    <html>
    <head>
        <title>Camera Streams</title>
        <style>
            body {{ margin: 0; padding: 20px; background: #000; color: #fff; font-family: sans-serif; }}
            div {{ margin-bottom: 20px; }}
            h2 {{ margin: 5px 0; }}
            img {{ max-width: 100%; height: auto; display: block; }}
        </style>
    </head>
    <body>{imgs}</body>
    </html>
    '''
    return Response(content=html, media_type="text/html")

def main():
    threading.Thread(target=camera_reader, daemon=True).start()

    host = socket.gethostname()
    print(f"[+] Streaming all cameras on http://{host}.local:8004/")

    uvicorn.run(app, host="0.0.0.0", port=8004, log_level="error",
                access_log=False, timeout_graceful_shutdown=1)

if __name__ == "__main__":
    main()

# /// script
# requires-python = ">=3.10"
# dependencies = ["grpcio>=1.80", "protobuf>=4.25"]
# ///
"""
bb-relay client: robot-side TCP tunnel.

Listens on a local TCP port. For each accepted TCP connection, opens a
gRPC Bridge stream to the cloud bb-relay with role=ROBOT + session_id +
Authorization, then pumps raw bytes between the TCP socket and the
Bridge stream in both directions.

policy_client's robot_client.py dials this local port as if it were the
policy server. It speaks normal gRPC; we just forward the underlying
TCP bytes through the relay. Native HTTP/2 semantics (stream
multiplexing, flow control, errors, deadlines) flow end to end because
both ends terminate gRPC locally; the tunnel sees only opaque bytes.

Usable two ways:
    # as a package module (inside the inference venv)
    python -m bb_relay.client --interactive
    # or standalone (self-contained uv script)
    BB_API_KEY=... uv run bb_relay/client.py --interactive

Or imported and driven programmatically:
    from bb_relay import run_tunnel, lookup_session
    run_tunnel(session_id, lookup_session(api_key, session_id), api_key)
"""

import argparse
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Work both as a package (`bb_relay.client`) and as a standalone uv script.
try:
    from . import relay_pb2, relay_pb2_grpc
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    import relay_pb2
    import relay_pb2_grpc

import grpc

ROLE_ROBOT = 0
CLOUD_URL = "https://api.bracketbot.com"


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a .env file next to this module. Existing env vars win."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _http_json(method: str, path: str, api_key: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(CLOUD_URL + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", "bb-relay-tunnel/1")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def pick_session_interactively(api_key: str) -> tuple[str, str]:
    """List sessions for the caller's team, let them pick one or create a new one.

    Returns (session_id, relay_addr) — both come from bb-server.
    """
    listing = _http_json("GET", "/v1/inference/sessions", api_key)
    sessions = listing.get("sessions", [])
    now_ms = int(time.time() * 1000)
    print()
    print("sessions for your team:")
    for i, s in enumerate(sessions, 1):
        age = max(0, (now_ms - int(s.get("created_at") or 0)) // 1000)
        sides = []
        if s.get("side_robot_at"):
            sides.append("robot")
        if s.get("side_gpu_at"):
            sides.append("gpu")
        sides_s = ",".join(sides) if sides else "none"
        name = s.get("name") or "(unnamed)"
        print(f"  {i}.  {name:30s}  age {age:>5d}s   sides: {sides_s}")
    new_idx = len(sessions) + 1
    print(f"  {new_idx}.  + create new")
    print()
    while True:
        choice = input(f"choose [1-{new_idx}]: ").strip()
        if choice.isdigit():
            n = int(choice)
            if 1 <= n <= len(sessions):
                s = sessions[n - 1]
                print(f"using session {s.get('name') or s['session_id'][:8]}")
                return s["session_id"], s["relay_addr"]
            if n == new_idx:
                name = input("name (optional, enter to skip): ").strip()
                resp = _http_json(
                    "POST", "/v1/inference/sessions", api_key, {"name": name}
                )
                print(f"created session {name or resp['session_id'][:8]}")
                return resp["session_id"], resp["relay_addr"]
        print("invalid choice")


def lookup_session(api_key: str, session_id: str) -> str:
    """Return relay_addr for an existing session, or raise."""
    listing = _http_json("GET", "/v1/inference/sessions", api_key)
    for s in listing.get("sessions", []):
        if s.get("session_id") == session_id:
            return s["relay_addr"]
    raise SystemExit(f"session {session_id} not found in your team's active sessions")


def wait_for_gpu_side(api_key: str, session_id: str, timeout_s: float = 30.0,
                      poll_interval: float = 0.5) -> bool:
    """Block until side_gpu_at is set on this session, or timeout. Returns True if
    the GPU bridged in time. We must not start the local exec (live_inference.py) before
    this: if the robot's gRPC sends bytes while the relay is not yet paired, the
    relay drops them and gRPC times out waiting for the policy server's SETTINGS.
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            listing = _http_json("GET", "/v1/inference/sessions", api_key)
        except urllib.error.URLError as e:
            print(f"[client] poll error: {e}", flush=True)
            time.sleep(poll_interval)
            continue
        target = next(
            (s for s in listing.get("sessions", []) if s.get("session_id") == session_id),
            None,
        )
        if target and target.get("side_gpu_at"):
            return True
        time.sleep(poll_interval)
    return False


READ_CHUNK = 64 * 1024


def pump_tcp_to_bridge(
    sock: socket.socket,
    outbox: "queue.Queue[relay_pb2.RelayFrame | None]",
    session_id: str,
    stop: threading.Event,
) -> None:
    """Read bytes off local TCP socket, push them into the Bridge outbox."""
    total = 0
    try:
        while not stop.is_set():
            data = sock.recv(READ_CHUNK)
            if not data:
                break
            total += len(data)
            print(f"[client] tcp->bridge {len(data)} bytes (total {total})", flush=True)
            outbox.put(
                relay_pb2.RelayFrame(
                    session_id=session_id, stream_id=ROLE_ROBOT, payload=data
                )
            )
    except OSError as e:
        print(f"[client] tcp->bridge oserror: {e}", flush=True)
    finally:
        print(f"[client] tcp->bridge end (total {total})", flush=True)
        stop.set()
        outbox.put(None)


def pump_bridge_to_tcp(
    stream, sock: socket.socket, stop: threading.Event
) -> None:
    """Read RelayFrames from Bridge, write payload bytes to local TCP socket."""
    total = 0
    try:
        for frame in stream:
            if stop.is_set():
                break
            if frame.payload:
                total += len(frame.payload)
                print(f"[client] bridge->tcp {len(frame.payload)} bytes (total {total})", flush=True)
                sock.sendall(frame.payload)
    except grpc.RpcError as e:
        print(f"[client] bridge closed: {e.code().name}", flush=True)
    except OSError as e:
        print(f"[client] bridge->tcp oserror: {e}", flush=True)
    finally:
        print(f"[client] bridge->tcp end (total {total})", flush=True)
        stop.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


def handle_connection(
    sock: socket.socket,
    peer: tuple,
    relay_addr: str,
    session_id: str,
    api_key: str,
) -> None:
    """One TCP connection → one Bridge stream → byte-pump in both directions."""
    outbox: "queue.Queue[relay_pb2.RelayFrame | None]" = queue.Queue()
    stop = threading.Event()

    def gen():
        yield relay_pb2.RelayFrame(
            session_id=session_id, stream_id=ROLE_ROBOT, payload=b""
        )
        while True:
            f = outbox.get()
            if f is None:
                return
            yield f

    channel = grpc.insecure_channel(
        relay_addr,
        options=[
            ("grpc.max_send_message_length", 8 * 1024 * 1024),
            ("grpc.max_receive_message_length", 8 * 1024 * 1024),
        ],
    )
    stub = relay_pb2_grpc.RelayStub(channel)
    metadata = (("authorization", f"Bearer {api_key}"),)
    stream = stub.Bridge(gen(), metadata=metadata)

    print(
        f"[client] tunnel up: local {peer} <-> relay session={session_id[:8]}",
        flush=True,
    )

    t1 = threading.Thread(
        target=pump_tcp_to_bridge,
        args=(sock, outbox, session_id, stop),
        daemon=True,
    )
    t2 = threading.Thread(
        target=pump_bridge_to_tcp,
        args=(stream, sock, stop),
        daemon=True,
    )
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    try:
        sock.close()
    except OSError:
        pass
    try:
        channel.close()
    except Exception:
        pass
    print(f"[client] tunnel down: local {peer}", flush=True)


def run_tunnel(
    session_id: str,
    relay_addr: str,
    api_key: str,
    listen_host: str = "127.0.0.1",
    listen_port: int = 8080,
    exec_cmd: str | None = None,
    ready: "threading.Event | None" = None,
) -> int:
    """Bind a local TCP listener and tunnel each accepted connection through the
    cloud relay to the paired GPU side. ``policy_client`` dials this local port.

    If ``exec_cmd`` is given: wait for the GPU to bridge, launch the command, and
    close the listener when it exits. Otherwise serve until KeyboardInterrupt.
    If ``ready`` is given, it is set once the listener is bound and listening, so
    a caller running this in a thread can wait before dialing the local port.
    Returns a process-style exit code.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((listen_host, listen_port))
    listener.listen(5)
    if ready is not None:
        ready.set()
    print(
        f"[client] listening {listen_host}:{listen_port} -> "
        f"relay={relay_addr} session={session_id[:8]}",
        flush=True,
    )

    if exec_cmd:
        print("[client] waiting for GPU to bridge into this session...", flush=True)
        if wait_for_gpu_side(api_key, session_id):
            print("[client] GPU bridged; launching exec", flush=True)
        else:
            print("[client] WARNING: GPU did not bridge in 30s; launching exec anyway", flush=True)

    exec_proc: subprocess.Popen | None = None
    if exec_cmd:
        print(f"[client] exec: {exec_cmd}", flush=True)
        exec_proc = subprocess.Popen(exec_cmd, shell=True)

        def _close_listener_on_exec_exit() -> None:
            exec_proc.wait()
            print(
                f"[client] exec finished (code {exec_proc.returncode}); closing listener",
                flush=True,
            )
            try:
                listener.close()
            except OSError:
                pass

        threading.Thread(target=_close_listener_on_exec_exit, daemon=True).start()

    try:
        while True:
            sock, peer = listener.accept()
            t = threading.Thread(
                target=handle_connection,
                args=(sock, peer, relay_addr, session_id, api_key),
                daemon=True,
            )
            t.start()
    except OSError:
        pass
    except KeyboardInterrupt:
        print("[client] shutting down", flush=True)
    finally:
        if exec_proc is not None and exec_proc.poll() is None:
            exec_proc.terminate()
            try:
                exec_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                exec_proc.kill()
        try:
            listener.close()
        except OSError:
            pass
    return 0


def main() -> int:
    _load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--listen-port", type=int, default=8080)
    p.add_argument(
        "--listen-host",
        default="127.0.0.1",
        help="local host to bind (default localhost; use 0.0.0.0 to expose)",
    )
    p.add_argument("--session", default=None,
                   help="session uuid; required unless --interactive")
    p.add_argument("--interactive", action="store_true",
                   help="list team's sessions and pick or create one")
    p.add_argument("--api-key", default=os.environ.get("BB_API_KEY", ""))
    p.add_argument("--exec", dest="exec_cmd",
                   default=os.environ.get("BB_RELAY_EXEC", ""),
                   help="shell command to run once the local listener is bound; "
                        "tunnel exits when this command exits")
    args = p.parse_args()

    if not args.api_key:
        print("ERROR: BB_API_KEY required (env, .env file next to this module, or --api-key)", file=sys.stderr)
        return 1
    if args.interactive and args.session:
        print("ERROR: --interactive and --session are mutually exclusive", file=sys.stderr)
        return 1
    if not args.interactive and not args.session:
        print("ERROR: either --session or --interactive is required", file=sys.stderr)
        return 1
    if args.interactive:
        session_id, relay_addr = pick_session_interactively(args.api_key)
    else:
        session_id, relay_addr = args.session, lookup_session(args.api_key, args.session)

    return run_tunnel(
        session_id,
        relay_addr,
        args.api_key,
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        exec_cmd=args.exec_cmd or None,
    )


if __name__ == "__main__":
    sys.exit(main())

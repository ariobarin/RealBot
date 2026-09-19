# /// script
# requires-python = ">=3.10"
# dependencies = ["grpcio>=1.80", "protobuf>=4.25"]
# ///
"""
bb-relay server: GPU-side TCP tunnel.

Opens a gRPC Bridge stream to the cloud bb-relay with role=GPU +
session_id + Authorization, dials a local upstream TCP socket (where
policy_client's policy_server.py is listening), and pumps raw bytes
between the two in both directions.

The relay address is returned by bb-server in the session payload;
the caller never types it. Auth is the user's BB_API_KEY.

Usable two ways:
    # as a package module (inside the inference venv)
    python -m bb_relay.server --interactive
    # or standalone (self-contained uv script)
    BB_API_KEY=... uv run bb_relay/server.py --interactive
"""

import argparse
import json
import os
import queue
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

# Work both as a package (`bb_relay.server`) and as a standalone uv script.
try:
    from . import relay_pb2, relay_pb2_grpc
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    import relay_pb2
    import relay_pb2_grpc

import grpc

ROLE_GPU = 1
READ_CHUNK = 64 * 1024
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


def wait_for_next_session(api_key: str, poll_interval: float = 0.5) -> tuple[str, str]:
    """Poll bb-server until a session in the team needs a GPU.

    Grabs the most recently-created session that has no GPU bridged yet (regardless
    of whether the robot has bridged). The GPU side bridges in eagerly so that when
    the robot's first byte arrives, the relay is already paired and won't drop frames.
    Returns (session_id, relay_addr).
    """
    print("[server] waiting for next session (polling, ctrl-c to stop)...", flush=True)
    while True:
        try:
            listing = _http_json("GET", "/v1/inference/sessions", api_key)
        except urllib.error.URLError as e:
            print(f"[server] poll error: {e}", flush=True)
            time.sleep(poll_interval)
            continue
        candidates = [
            s for s in listing.get("sessions", [])
            if not s.get("side_gpu_at")
        ]
        candidates.sort(key=lambda s: int(s.get("created_at") or 0), reverse=True)
        if candidates:
            s = candidates[0]
            print(
                f"[server] picked up session {s.get('name') or s['session_id'][:8]}",
                flush=True,
            )
            return s["session_id"], s["relay_addr"]
        time.sleep(poll_interval)


def _gather_lan_ips() -> list[str]:
    ips: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ips.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    out: list[str] = []
    for ip in ips:
        if ip.startswith("127.") or ip.startswith("169.254.") or ip in out:
            continue
        out.append(ip)
    return out[:5]


def _pump_sock_to_sock(src: socket.socket, dst: socket.socket,
                       stop: threading.Event) -> None:
    try:
        while not stop.is_set():
            data = src.recv(READ_CHUNK)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        stop.set()
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class DirectListener:
    def __init__(self, upstream_host: str, upstream_port: int, listen_port: int = 0):
        self.upstream = (upstream_host, upstream_port)
        self.active: set[socket.socket] = set()
        self.lock = threading.Lock()
        self.closed = False
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind(("0.0.0.0", listen_port))
        self.listener.listen(5)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self) -> None:
        while True:
            try:
                sock, peer = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._pump, args=(sock, peer), daemon=True).start()

    def _pump(self, sock: socket.socket, peer: tuple) -> None:
        with self.lock:
            if self.closed:
                try:
                    sock.close()
                except OSError:
                    pass
                return
            self.active.add(sock)
        upstream: socket.socket | None = None
        stop = threading.Event()
        try:
            data = sock.recv(READ_CHUNK)
            if data:
                print(f"[server] direct conn from {peer[0]}:{peer[1]}; dialing upstream", flush=True)
                upstream = dial_upstream(*self.upstream)
                t = threading.Thread(target=_pump_sock_to_sock, args=(upstream, sock, stop), daemon=True)
                t.start()
                while data:
                    upstream.sendall(data)
                    if stop.is_set():
                        break
                    data = sock.recv(READ_CHUNK)
                stop.set()
                t.join()
        except (OSError, RuntimeError) as e:
            print(f"[server] direct conn error: {e}", flush=True)
        finally:
            stop.set()
            with self.lock:
                self.active.discard(sock)
            for s in (sock, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def close(self) -> None:
        with self.lock:
            self.closed = True
            active, self.active = self.active, set()
        try:
            self.listener.close()
        except OSError:
            pass
        for s in active:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def dial_upstream(host: str, port: int, retries: int = 20, sleep_s: float = 0.25) -> socket.socket:
    """Dial upstream policy server with a short retry, since serve.py may not
    be listening at the exact moment the bridge becomes ready."""
    last_err: Exception | None = None
    for _ in range(retries):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((host, port))
            return s
        except OSError as e:
            last_err = e
            time.sleep(sleep_s)
    raise RuntimeError(f"could not dial upstream {host}:{port}: {last_err}")


def pump_tcp_to_bridge(
    sock: socket.socket,
    outbox: "queue.Queue[relay_pb2.RelayFrame | None]",
    session_id: str,
    stop: threading.Event,
) -> None:
    total = 0
    try:
        while not stop.is_set():
            data = sock.recv(READ_CHUNK)
            if not data:
                break
            total += len(data)
            print(f"[server] tcp->bridge {len(data)} bytes (total {total})", flush=True)
            outbox.put(
                relay_pb2.RelayFrame(
                    session_id=session_id, stream_id=ROLE_GPU, payload=data
                )
            )
    except OSError as e:
        print(f"[server] tcp->bridge oserror: {e}", flush=True)
    finally:
        print(f"[server] tcp->bridge end (total {total})", flush=True)
        stop.set()
        outbox.put(None)



def run_one_session(
    session_id: str,
    relay_addr: str,
    api_key: str,
    upstream_host: str,
    upstream_port: int,
) -> None:
    """Open one Bridge stream and pump bytes until either side closes."""
    outbox: "queue.Queue[relay_pb2.RelayFrame | None]" = queue.Queue()
    stop = threading.Event()

    direct: DirectListener | None = None
    ips = _gather_lan_ips()
    if ips:
        try:
            direct = DirectListener(upstream_host, upstream_port, listen_port=0)
            print(f"[server] direct listener 0.0.0.0:{direct.port} ips={ips}", flush=True)
            try:
                _http_json("POST", f"/v1/inference/sessions/{session_id}/candidates",
                           api_key, {"ips": ips, "port": direct.port})
            except Exception as e:
                print(f"[server] candidates publish failed (relay-only session): {e}", flush=True)
        except OSError as e:
            print(f"[server] direct listener unavailable: {e}", flush=True)

    def gen():
        yield relay_pb2.RelayFrame(
            session_id=session_id, stream_id=ROLE_GPU, payload=b""
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
        f"[server] bridge open session={session_id[:8]} "
        f"upstream={upstream_host}:{upstream_port}",
        flush=True,
    )

    # Lazy upstream dial: we wait for the FIRST bridge frame before dialing
    # localhost so that the upstream policy server's HTTP/2 SETTINGS frame
    # isn't emitted into the void while the robot side hasn't bridged yet.
    sock_holder: dict = {"sock": None}
    upstream_ready = threading.Event()

    def bridge_to_tcp_lazy():
        total = 0
        try:
            for frame in stream:
                if stop.is_set():
                    break
                if not frame.payload:
                    continue
                if sock_holder["sock"] is None:
                    print("[server] first bridge byte received; dialing upstream", flush=True)
                    sock_holder["sock"] = dial_upstream(upstream_host, upstream_port)
                    upstream_ready.set()
                total += len(frame.payload)
                print(
                    f"[server] bridge->tcp {len(frame.payload)} bytes (total {total})",
                    flush=True,
                )
                sock_holder["sock"].sendall(frame.payload)
        except grpc.RpcError as e:
            print(f"[server] bridge closed: {e.code().name}", flush=True)
        except OSError as e:
            print(f"[server] bridge->tcp oserror: {e}", flush=True)
        finally:
            print(f"[server] bridge->tcp end (total {total})", flush=True)
            stop.set()
            upstream_ready.set()
            outbox.put(None)
            if sock_holder["sock"] is not None:
                try:
                    sock_holder["sock"].shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def tcp_to_bridge_after_ready():
        upstream_ready.wait()
        sock = sock_holder["sock"]
        if sock is None:
            return
        pump_tcp_to_bridge(sock, outbox, session_id, stop)

    t1 = threading.Thread(target=tcp_to_bridge_after_ready, daemon=True)
    t2 = threading.Thread(target=bridge_to_tcp_lazy, daemon=True)
    t1.start()
    t2.start()

    t2.join()
    t1.join()

    if direct is not None:
        direct.close()
    if sock_holder["sock"] is not None:
        try:
            sock_holder["sock"].close()
        except OSError:
            pass
    try:
        channel.close()
    except Exception:
        pass
    print(f"[server] tunnel down session={session_id[:8]}", flush=True)


def main() -> int:
    _load_dotenv()
    p = argparse.ArgumentParser()
    p.add_argument("--upstream-host", default="127.0.0.1")
    p.add_argument("--upstream-port", type=int, default=8080)
    p.add_argument("--session", default=None,
                   help="session uuid; required unless --interactive or --permanent")
    p.add_argument("--interactive", action="store_true",
                   help="list team's sessions and pick or create one")
    p.add_argument("--permanent", action="store_true",
                   help="stay running across sessions; after one ends, auto-accept "
                        "the next pending session for your team")
    p.add_argument("--api-key", default=os.environ.get("BB_API_KEY", ""))
    args = p.parse_args()

    if not args.api_key:
        print("ERROR: BB_API_KEY required (env, .env file next to this script, or --api-key)", file=sys.stderr)
        return 1
    if args.interactive and args.session:
        print("ERROR: --interactive and --session are mutually exclusive", file=sys.stderr)
        return 1
    if not args.interactive and not args.session and not args.permanent:
        print("ERROR: one of --session, --interactive, or --permanent is required", file=sys.stderr)
        return 1

    if args.interactive:
        session_id, relay_addr = pick_session_interactively(args.api_key)
    elif args.session:
        session_id = args.session
        relay_addr = lookup_session(args.api_key, session_id)
    else:
        session_id, relay_addr = wait_for_next_session(args.api_key)

    while True:
        try:
            run_one_session(
                session_id, relay_addr, args.api_key,
                args.upstream_host, args.upstream_port,
            )
        except KeyboardInterrupt:
            print("[server] shutting down", flush=True)
            return 0

        if not args.permanent:
            return 0

        try:
            session_id, relay_addr = wait_for_next_session(args.api_key)
        except KeyboardInterrupt:
            print("[server] shutting down", flush=True)
            return 0


if __name__ == "__main__":
    sys.exit(main())

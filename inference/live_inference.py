#!/usr/bin/env python3
"""Run async inference on BracketBot via a remote policy server.

By default connects directly to $POLICY_SERVER (e.g. an SSH tunnel on
localhost:8080). With --interactive it walks you through the full setup over
the bb_relay cloud tunnel: pick a session, pick a checkpoint, type a task,
then run inference through the tunnel.

Usage:
    uv run live_inference.py                      # direct to $POLICY_SERVER (env config)
    uv run live_inference.py --interactive        # pick session + checkpoint + task
    uv run live_inference.py --session <uuid>     # specific session, env config
"""

import argparse
import json
import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)                     # inference/client: bracketbot_adapter, policy_client
sys.path.insert(0, os.path.dirname(_HERE))    # inference/: shared bb_relay package

# bb API host (same host as the bb_relay inference/sessions API). Override with
# BB_SERVER_URL if checkpoints live elsewhere.
BB_SERVER_URL = os.environ.get("BB_SERVER_URL", "https://api.bracketbot.com")
BB_API_KEY_FILE = "/etc/BB_API_KEY"
DEFAULT_TASK = "Pick up bottle and place in blue bin"


def _resolve_api_key(cli_key: str) -> str:
    """Resolve the API key: --api-key > $BB_API_KEY > /etc/BB_API_KEY.

    The file may hold the raw key or a ``BB_API_KEY=...`` line.
    """
    if cli_key:
        return cli_key
    if os.environ.get("BB_API_KEY"):
        return os.environ["BB_API_KEY"]
    try:
        txt = Path(BB_API_KEY_FILE).read_text()
    except OSError:
        return ""
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            if k.strip() == "BB_API_KEY":
                return v.strip().strip('"').strip("'")
        else:
            return line  # raw key
    return ""


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from a .env file next to this script. Existing env vars win."""
    env_path = Path(__file__).parent / '.env'
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, _, v = line.partition('=')
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _http_get_json(path: str, api_key: str) -> dict:
    req = urllib.request.Request(BB_SERVER_URL + path, method="GET")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", "bb-live-inference/1")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _http_post_json(path: str, body: dict, api_key: str) -> dict:
    req = urllib.request.Request(BB_SERVER_URL + path, data=json.dumps(body).encode(),
                                 method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("User-Agent", "bb-live-inference/1")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def list_steps(name: str, api_key: str) -> list[str]:
    """Steps available for a checkpoint name ("{policy_type}/{run}"), oldest first.

    /v1/checkpoints only rolls a run up to a name and a total size, so the steps come
    from the object listing the downloader uses: checkpoints/{type}/{run}/{step}.tar.
    """
    steps, token = [], None
    while True:
        body = {"prefix": f"checkpoints/{name.strip('/')}/"}
        if token:
            body["continuationToken"] = token
        data = _http_post_json("/v1/downloads/list", body, api_key)
        for item in data.get("keys") or data.get("items") or data.get("objects") or []:
            key = item if isinstance(item, str) else (item or {}).get("key", "")
            if key.endswith((".tar", ".tar.gz")):
                steps.append(key.rsplit("/", 1)[-1].removesuffix(".tar.gz").removesuffix(".tar"))
        token = data.get("nextContinuationToken") or data.get("continuationToken")
        if not token or not data.get("isTruncated", False):
            break
    # Numeric steps in numeric order, so "003000" sorts above "500" rather than below it.
    return sorted(set(steps), key=lambda s: (s.isdigit(), int(s) if s.isdigit() else 0, s))


def pick_checkpoint(api_key: str) -> tuple[str, str]:
    """Choose a checkpoint -> (policy_type, GPU path the policy server loads).

    Lists the team's checkpoints (GET /v1/checkpoints), then which step of the chosen run
    to load. Option 'm' lets you type a full GPU path directly, for when the cloud
    listing doesn't have it yet (temporary, until upload/download is wired up properly).
    """
    try:
        cks = _http_get_json("/v1/checkpoints", api_key).get("checkpoints", [])
    except Exception as e:
        print(f"[live] could not list checkpoints ({e}); enter a path manually")
        cks = []
    cks.sort(key=lambda c: c.get("lastModified", ""), reverse=True)
    print()
    print("checkpoints for your team:")
    for i, c in enumerate(cks, 1):
        size_gb = (c.get("totalSize") or 0) / 1e9
        print(f"  {i}.  {c.get('name', '?'):50s}  {size_gb:7.1f} GB  "
              f"{c.get('files', '?')} files   {c.get('lastModified', '')}")
    print("  m.  <enter a full GPU checkpoint path manually>")
    print()
    while True:
        choice = input(f"choose [1-{len(cks)} or m]: ").strip().lower()
        if choice == "m":
            path = input("full checkpoint path on the GPU "
                         "(e.g. /workspace/checkpoints/<run>/<step>/pretrained_model): ").strip()
            if not path:
                continue
            policy_type = input("policy_type [pi0]: ").strip() or "pi0"
            return policy_type, path
        if choice.isdigit() and 1 <= int(choice) <= len(cks):
            name = cks[int(choice) - 1]["name"]
            return checkpoint_to_gpu(name, pick_step(name, api_key))
        print("invalid choice")


def pick_step(name: str, api_key: str) -> str:
    """Choose which step of a run to load. Empty input takes the newest."""
    try:
        steps = list_steps(name, api_key)
    except Exception as e:
        print(f"[live] could not list steps ({e})")
        steps = []
    if not steps:
        return input("step (e.g. 003000): ").strip()

    print()
    print(f"steps for {name}:")
    for i, step in enumerate(steps, 1):
        print(f"  {i}.  {step}{'   (latest)' if step == steps[-1] else ''}")
    print()
    while True:
        choice = input(f"choose step [1-{len(steps)}, blank = latest]: ").strip()
        if not choice:
            return steps[-1]
        if choice.isdigit() and 1 <= int(choice) <= len(steps):
            return steps[int(choice) - 1]
        print("invalid choice")


def checkpoint_to_gpu(name: str, step: str) -> tuple[str, str]:
    """Map a checkpoint name + step -> (policy_type, GPU path the policy server loads).

    HARDCODED for now: the API only returns the name (e.g.
    "pi0/pi0_20260621_225842_buzzzin_1k"); we derive the on-GPU path from it.
      policy_type = first segment ("pi0")
      run_id      = last segment  ("pi0_20260621_225842_buzzzin_1k")
      path        = /workspace/checkpoints/<run_id>/<step>/pretrained_model

    The step level matches what download_checkpoint.py extracts, so several steps of one
    run can sit on the GPU at once.
    """
    parts = name.split("/")
    policy_type = parts[0]
    run_id = parts[-1]
    return policy_type, f"/workspace/checkpoints/{run_id}/{step}/pretrained_model"


def _start_relay(api_key: str, session_id: str, relay_addr: str, listen_port: int) -> None:
    """Run the bb_relay tunnel in a background thread and block until the GPU side
    bridges (so robot_client's first gRPC bytes aren't dropped by an unpaired relay)."""
    import bb_relay

    bound = threading.Event()
    threading.Thread(
        target=bb_relay.run_tunnel,
        args=(session_id, relay_addr, api_key),
        kwargs={"listen_host": "127.0.0.1", "listen_port": listen_port, "ready": bound},
        daemon=True,
    ).start()
    bound.wait()  # don't proceed until the local listener is actually bound
    print(f"[live] relay tunnel on localhost:{listen_port}; waiting for GPU to bridge...", flush=True)
    if bb_relay.wait_for_gpu_side(api_key, session_id):
        print("[live] GPU bridged", flush=True)
    else:
        print("[live] WARNING: GPU did not bridge in 30s; starting anyway", flush=True)


def _find_direct_gpu(api_key: str, session_id: str, timeout_s: float = 30.0) -> str | None:
    deadline = time.time() + timeout_s
    cand = None
    while time.time() < deadline:
        try:
            listing = _http_get_json("/v1/inference/sessions", api_key)
        except Exception as e:
            print(f"[live] session poll error: {e}", flush=True)
            time.sleep(0.5)
            continue
        row = next((s for s in listing.get("sessions", [])
                    if s.get("session_id") == session_id), None)
        if row and row.get("side_gpu_at"):
            cand = row.get("candidates")
            break
        time.sleep(0.5)
    if not cand:
        return None
    for ip in cand.get("ips", []):
        try:
            socket.create_connection((ip, cand["port"]), timeout=1.5).close()
            return f"{ip}:{cand['port']}"
        except OSError:
            continue
    print(f"[live] no advertised GPU address reachable: {cand}", flush=True)
    return None


def main() -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(description="Run BracketBot async inference.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--interactive", action="store_true",
                   help="pick session + checkpoint + task over the bb-relay cloud tunnel")
    g.add_argument("--session", default=None,
                   help="use a specific bb-relay session uuid (env config for checkpoint/task)")
    p.add_argument("--listen-port", type=int, default=8080,
                   help="local port for the relay tunnel (default 8080)")
    p.add_argument("--provider", choices=["remote", "local"],
                   default=os.environ.get("BB_INFERENCE_PROVIDER", "remote"),
                   help="'local' = GPU is on this network: connect direct over "
                        "the LAN and fail if unreachable (default: remote via relay)")
    p.add_argument("--api-key", default="")
    args = p.parse_args()
    api_key = _resolve_api_key(args.api_key)

    import bracketbot_adapter
    from policy_client.async_inference.robot_client import RobotClientConfig

    server = os.environ.get("POLICY_SERVER", "localhost:8080")
    policy_type = os.environ.get("POLICY_TYPE", "pi0")
    checkpoint = os.environ.get("CHECKPOINT", "/workspace/training/baseline_head_pi0/checkpoints/003000/pretrained_model")
    task = os.environ.get("TASK", DEFAULT_TASK)

    if args.interactive or args.session:
        if not api_key:
            print("ERROR: BB_API_KEY required for --interactive/--session "
                  "(--api-key, $BB_API_KEY, or /etc/BB_API_KEY)", file=sys.stderr)
            return 1
        import bb_relay
        if args.interactive:
            session_id, relay_addr = bb_relay.pick_session_interactively(api_key)
        else:
            session_id, relay_addr = args.session, bb_relay.lookup_session(api_key, args.session)
        if args.provider == "local":
            server = _find_direct_gpu(api_key, session_id)
            if server is None:
                sys.exit("[live] provider=local but the GPU is not reachable on this "
                         "network; fix the network (or BB_DIRECT_IPS on the GPU), or "
                         "use --provider remote")
            print(f"[live] DIRECT path {server} (no tunnel)", flush=True)
        else:
            _start_relay(api_key, session_id, relay_addr, args.listen_port)
            server = f"localhost:{args.listen_port}"

    if args.interactive:
        policy_type, checkpoint = pick_checkpoint(api_key)
        print(f"[live] checkpoint -> {checkpoint} (policy_type={policy_type})", flush=True)
        entered = input(f"task prompt [{DEFAULT_TASK}]: ").strip()
        task = entered or DEFAULT_TASK

    fps = int(os.environ.get("FPS", "30"))
    chunk_size = int(os.environ.get("CHUNK_SIZE", "50"))

    config = RobotClientConfig(
        policy_type=policy_type,
        pretrained_name_or_path=checkpoint,
        actions_per_chunk=chunk_size,
        task=task,
        server_address=server,
        policy_device="cuda",
        chunk_size_threshold=0.5,
        fps=fps,
        aggregate_fn_name="weighted_average",
    )

    print(f"server={server} policy={policy_type} checkpoint={checkpoint} task={task!r} fps={fps}", flush=True)

    from quest import Quest
    Quest(config, bracketbot_adapter).run()   # left -> teleop, right -> inference
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Run VLM-driven async inference on BracketBot via a remote policy server.

Same wiring as live_inference.py, but the task is not fixed: a VLM picks it from
the head camera each cycle and decides when to home. Logic lives in vlm.py.

$TASK is ignored — prompts come from task_manifest.json and the VLM chooses among
them using the prose in prompt.md.

Usage:
    uv run vlm_inference.py                        # direct to $POLICY_SERVER
    uv run vlm_inference.py --interactive          # pick session + checkpoint
    uv run vlm_inference.py --session <uuid>
    uv run vlm_inference.py -n 2 -k 45
    uv run vlm_inference.py --provider openai            # gpt-4o-mini
    uv run vlm_inference.py --provider overshoot         # Qwen/Qwen3.6-27B-FP8 (fast real-time)
    uv run vlm_inference.py --provider local             # Qwen3.5-35B on the lab Spark (no cloud)
    uv run vlm_inference.py --prompt-file other.md        # different guidance
    uv run vlm_inference.py --manifest ckpt_tasks.json    # a different checkpoint's tasks
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import yaml

import vlm
from live_inference import _load_dotenv, _resolve_api_key, pick_checkpoint, _start_relay


def _load_env_file(path: str) -> None:
    """Merge KEY=VALUE lines into os.environ (existing vars win)."""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


# Keys are read from the environment only: these files first, then what's already
# exported. Never passed on the command line.
ENV_FILES = (os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
             os.path.expanduser("~/.env"))


def main() -> int:
    _load_dotenv()                                   # inference/.env
    _load_env_file(os.path.expanduser("~/.env"))     # robot-wide keys (GEMINI_API_KEY)

    p = argparse.ArgumentParser(description="VLM-driven BracketBot async inference.")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--interactive", action="store_true",
                   help="pick session + checkpoint over the bb-relay cloud tunnel")
    g.add_argument("--session", default=None,
                   help="use a specific bb-relay session uuid (env config for checkpoint)")
    p.add_argument("--listen-port", type=int, default=8080,
                   help="local port for the relay tunnel (default 8080)")
    # VLM controls
    p.add_argument("-n", "--vlm-interval", type=float, default=2.0,
                   help="seconds between VLM task checks (N, default 2)")
    p.add_argument("-k", "--home-every", type=float, default=25.0,
                   help="episode bound: home the arms and retry every K seconds "
                        "since the last home (default 25)")
    p.add_argument("--episode-floor", type=float, default=5.0,
                   help="ignore VLM picks this long after a home — the arms are "
                        "still leaving it (default 5; a home alone takes ~3s)")
    p.add_argument("--switch-streak", type=int, default=3,
                   help="consecutive cycles the VLM must pick the same different "
                        "task before the switch happens (default 3)")
    p.add_argument("--manifest", default=None,
                   help="the training pipeline's task manifest (JSON/JSONL from the "
                        f"checkpoint) — the prompts the VLM chooses among. Default "
                        f"{vlm.DEFAULT_MANIFEST_FILE}")
    p.add_argument("--prompt-file", default=vlm.DEFAULT_GUIDANCE_FILE,
                   help="plain-prose file saying how to choose among them "
                        f"(default {vlm.DEFAULT_GUIDANCE_FILE})")
    p.add_argument("--provider", choices=vlm.PROVIDERS, default="google",
                   help="which VLM backend to use (default google); the model for "
                        f"each is fixed in vlm.py -> {dict(vlm.MODEL)}")
    p.add_argument("--home-timeout", type=float, default=30.0,
                   help="max seconds to wait for a home to finish")
    args = p.parse_args()

    manifest_path = args.manifest or vlm.DEFAULT_MANIFEST_FILE
    try:
        tasks = vlm.load_task_config(manifest_path, args.prompt_file)
    except (OSError, ValueError, TypeError, AttributeError, KeyError,
            RecursionError, yaml.YAMLError) as e:
        print(f"ERROR: could not load tasks: {e}", file=sys.stderr)
        return 1
    print(f"[vlm] {len(tasks.tasks)} tasks from {manifest_path}; "
          f"guidance from {args.prompt_file}", flush=True)
    for t in tasks.tasks:
        print(f"[vlm]   {t.index}. {t.prompt}", flush=True)

    env_var = vlm.PROVIDER_ENV[args.provider]
    api_key = os.environ.get(env_var, "")
    if not api_key:
        if args.provider == "local":
            api_key = "unused"  # the Spark's vLLM server does not check keys
        else:
            print(f"ERROR: {env_var} not set. Add `{env_var}=...` to one of:\n"
                  + "\n".join(f"    {f}" for f in ENV_FILES), file=sys.stderr)
            return 1

    vlm_cfg = vlm.VlmConfig(
        tasks=tasks,
        provider=args.provider,
        api_key=api_key,
        vlm_interval=args.vlm_interval,
        home_every=args.home_every,
        home_timeout=args.home_timeout,
        episode_floor=args.episode_floor,
        switch_streak=args.switch_streak,
    )

    bb_key = _resolve_api_key("")   # $BB_API_KEY, else /etc/BB_API_KEY

    import bracketbot_adapter
    from policy_client.async_inference.robot_client import RobotClientConfig

    # No defaults on purpose — every run states its wiring, e.g.:
    #   POLICY_SERVER=localhost:8080 CHECKPOINT=/workspace/.../pretrained_model \
    #       BB_VLM_URL=http://... uv run vlm_inference.py --provider local
    server = os.environ.get("POLICY_SERVER", "")
    policy_type = os.environ.get("POLICY_TYPE", "pi0")
    checkpoint = os.environ.get("CHECKPOINT", "")

    if args.interactive or args.session:
        if not bb_key:
            print("ERROR: BB_API_KEY required for --interactive/--session "
                  "($BB_API_KEY or /etc/BB_API_KEY)", file=sys.stderr)
            return 1
        import bb_relay
        if args.interactive:
            session_id, relay_addr = bb_relay.pick_session_interactively(bb_key)
        else:
            session_id, relay_addr = args.session, bb_relay.lookup_session(bb_key, args.session)
        _start_relay(bb_key, session_id, relay_addr, args.listen_port)
        server = f"localhost:{args.listen_port}"

    if args.interactive:
        policy_type, checkpoint = pick_checkpoint(bb_key)
        print(f"[vlm] checkpoint -> {checkpoint} (policy_type={policy_type})", flush=True)

    missing = [n for n, v in (("POLICY_SERVER", server), ("CHECKPOINT", checkpoint))
               if not v]
    if args.provider == "local" and not vlm.LOCAL_BASE_URL:
        missing.append("BB_VLM_URL")
    if missing:
        print(f"ERROR: {', '.join(missing)} not set — there are no defaults; "
              "prefix the run with them", file=sys.stderr)
        return 1

    fps = int(os.environ.get("FPS", "30"))
    chunk_size = int(os.environ.get("CHUNK_SIZE", "50"))

    config = RobotClientConfig(
        policy_type=policy_type,
        pretrained_name_or_path=checkpoint,
        actions_per_chunk=chunk_size,
        task=tasks.tasks[0].prompt,  # placeholder; VLM homes + overwrites on cycle 1
        server_address=server,
        policy_device="cuda",
        chunk_size_threshold=0.5,
        fps=fps,
        aggregate_fn_name="weighted_average",
    )

    print(f"server={server} policy={policy_type} checkpoint={checkpoint} fps={fps} "
          f"vlm_interval={vlm_cfg.vlm_interval}s home_every={vlm_cfg.home_every}s "
          f"provider={vlm_cfg.provider} model={vlm_cfg.model}", flush=True)

    from quest import Quest
    # driver = the VLM task selector, run against the inference client each phase
    Quest(config, bracketbot_adapter,
          driver=lambda c, stop: vlm.vlm_controller(c, vlm_cfg, stop)).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

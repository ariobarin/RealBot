#!/usr/bin/env python3
"""VLM task driver: the vision model that decides what the policy does.

vlm_inference.py is only the runner (CLI + policy-server wiring); all the logic
is here. Nothing in this module touches argparse — the runner builds a VlmConfig
and calls run_client.

The VLM runs in its own thread so inference keeps streaming at fps while it
thinks. Every `vlm_interval` seconds it picks a task from the head cam. A pick
that differs from the running task must repeat for `switch_streak` consecutive
checks before the robot homes and switches. Because each task's goal scene is
the trigger scene of a different task, that debounce doubles as completion
detection: the moment a task finishes, the pick flips and the switch follows a
few checks later — there is no episode timer to wait out. Picks made while a
gripper holds the bowl are ignored entirely (the model also reports `holding`):
mid-carry the scene reads as the next task's trigger for seconds at a time,
long enough to fake a streak. `home_every` bounds an episode — if no switch has
happened by then, home and retry so the arms never dwell out of distribution.
Homing reuses the 'h' halt+home logic without the keypress. Manual 'h'/'t' are
disabled.

Inputs: task_manifest.json (the training pipeline's per-dataset prompts) and
prompt.md (your prose on how to choose). The VLM gets both plus the frame and
answers with an index, so the policy only ever runs an instruction it was
trained on and you never name or key a dataset.
"""

import json
import os
import re
import threading
import time
from dataclasses import dataclass, field

import yaml

# cv2 / numpy / google.genai are imported lazily so the config layer below can be
# tested anywhere — no camera, no robot stack, no VLM SDK.

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_GUIDANCE_FILE = os.path.join(MODULE_DIR, "prompt.md")
DEFAULT_MANIFEST_FILE = os.path.join(MODULE_DIR, "task_manifest.json")

# overshoot.ai is an OpenAI-compatible endpoint (fast real-time VLM hosting).
OVERSHOOT_BASE_URL = "https://api.overshoot.ai/v1beta"

LOCAL_BASE_URL = os.environ.get("BB_VLM_URL", "http://spark-3efa.local:8001/v1")

# The model used for each provider. Hardcoded on purpose — there is no flag and
# no override; change it here and every robot picks it up with the file.
#   google    : gemini-flash-lite-latest       (fast, cheap, the default)
#               gemini-robotics-er-1.5-preview is the robotics-tuned option
#   overshoot : OpenAI-compatible host, good real-time latency
#   local     : Qwen3.5-35B-A3B-FP8 on the Spark — no cloud, no billing;
OUT_FILE = "/tmp/vlm_inference.txt"   # live status dump; watch -n1 cat it
CAMERA_TOPIC = "camera.head.jpeg"     # stereo head cam; right half is the right eye

# Long edge the right eye is scaled to before it is sent. The eye is 1280x960
# raw, which the providers bill as four 768px tiles; one tile is a quarter of
# the tokens and a much smaller upload, and bowl orientation/side is still
# obvious at this size.
MAX_IMAGE_EDGE = 768

MODEL = {
    "google": "gemini-flash-lite-latest",
    "openai": "gpt-4o-mini",
    "overshoot": "Qwen/Qwen3.6-27B-FP8",
    "local": "Qwen3.5-35B-A3B-FP8",
}

# Where each provider's key is read from by default. The local vLLM server
# never checks keys, so its env var is optional (any value satisfies it).
PROVIDER_ENV = {
    "google": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "overshoot": "OVERSHOOT_API_KEY",
    "local": "BB_VLM_API_KEY",
}

PROVIDERS = tuple(MODEL)


# ── Inputs ──────────────────────────────────────────────────────────────────
#
#   task_manifest.json — from the training pipeline, one entry per dataset.
#     Machine-generated; never edited. When the policy server can serve it,
#     only load_manifest() changes.
#   prompt.md — yours. The whole file is the prose sent to the VLM: no keys,
#     no YAML, no structure. Names no task and keys no task.
#
# The VLM returns an index into the manifest, so the choices always come from the
# model and the choosing rule always comes from you. Labels are auto-derived from
# the prompt for logs only. Actions are never modified.

@dataclass
class Task:
    index: int            # position in the manifest; what the VLM returns
    prompt: str           # instruction sent to the policy — verbatim from training
    label: str = ""       # auto-slug of the prompt, for logs only
    task_index: object = None  # manifest's own id, if it has one
    datasets: object = None
    extra: dict = field(default_factory=dict)  # medians etc., untouched


@dataclass
class TaskConfig:
    guidance: str         # your prose from prompt.md
    tasks: list           # list[Task], in manifest order

    def by_index(self, i):
        return next((t for t in self.tasks if t.index == i), None)


_PROMPT_KEYS = ("prompt", "task", "text", "instruction")
_MANIFEST_META = ("task_index", "index", "datasets", "dataset") + _PROMPT_KEYS

DEFAULT_GUIDANCE = ("Pick the task that matches what the camera shows right "
                    "now.\n\n{tasks}\n")


def load_manifest(path: str) -> list:
    """Read the task manifest -> [{prompt, task_index, datasets, extra}].

    Tolerant about shape, because training owns the format:
      {"tasks": [{"task_index": 0, "prompt": "...", "datasets": [...]}, ...]}
      [{"task_index": 0, "task": "..."}, ...]
      {"tasks": {"0": "prompt text", ...}}
      one JSON object per line (.jsonl, the LeRobot meta/tasks.jsonl format)
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if path.endswith(".jsonl"):
        raw = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    else:
        raw = json.loads(text)
    if isinstance(raw, dict):
        # Require the key explicitly: falling back to the whole dict would turn
        # metadata ("checkpoint", "policy_type", ...) into prompts.
        for k in ("tasks", "states"):
            if k in raw:
                raw = raw[k]
                break
        else:
            raise ValueError(f"{path}: no 'tasks' key (found {sorted(raw)[:6]})")
    if isinstance(raw, dict):  # {"0": "prompt", ...} — keys must be indices
        bad = [k for k in raw if not str(k).strip().lstrip("-").isdecimal()]
        if bad:
            raise ValueError(f"{path}: task keys must be numeric indices, got {bad[:5]}")
        raw = [{"task_index": k, "prompt": v}
               for k, v in sorted(raw.items(), key=lambda kv: int(str(kv[0])))]
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"{path}: expected a non-empty list of tasks")

    out = {}
    for e in raw:
        if isinstance(e, str):
            e = {"prompt": e}
        if not isinstance(e, dict):
            raise ValueError(f"{path}: manifest entry is not an object: {e!r}")
        # NOT stripped: the prompt must reach the policy exactly as trained.
        prompt = next((e[k] for k in _PROMPT_KEYS
                       if isinstance(e.get(k), str) and e[k].strip()), None)
        if prompt is None:
            raise ValueError(f"{path}: manifest entry has no usable prompt: {e!r}")
        ds = e.get("datasets") or ([e["dataset"]] if e.get("dataset") else [])
        ds = [ds] if isinstance(ds, str) else list(ds)
        extra = {k: v for k, v in e.items() if k not in _MANIFEST_META}
        if prompt in out:  # one prompt taught by several datasets — fold them
            out[prompt]["datasets"] += [d for d in ds if d not in out[prompt]["datasets"]]
            for k, v in extra.items():           # keep both entries' fields
                out[prompt]["extra"].setdefault(k, v)
            continue
        out[prompt] = {"prompt": prompt,
                       "task_index": e.get("task_index", e.get("index")),
                       "datasets": ds, "extra": extra}
    if len(out) < 2:
        raise ValueError(f"{path}: need at least 2 distinct prompts, got {len(out)}")
    return list(out.values())


def _slug(prompt: str, taken=()) -> str:
    """Log label from the prompt. Widened until unique — sibling prompts share
    long prefixes."""
    words = "".join(c if c.isalnum() or c.isspace() else " " for c in prompt.lower()).split()
    if not words:
        words = ["task"]
    for n in range(min(4, len(words)), len(words) + 1):
        name = "_".join(words[:n])
        if name not in taken:
            return name
    name, i = "_".join(words), 2
    while f"{name}_{i}" in taken:
        i += 1
    return f"{name}_{i}"


def load_guidance(path: str) -> str:
    """Read the guidance prose the VLM is shown.

    Normally a plain .md/.txt file whose entire contents are the prose. A .yaml
    file with a `guidance:` key is also accepted for older setups."""
    if not path:
        return DEFAULT_GUIDANCE
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.strip():
        raise ValueError(f"{path}: prompt file is empty")
    if not path.lower().endswith((".yaml", ".yml")):
        return text.strip()
    raw = yaml.safe_load(text) or {}
    if isinstance(raw, str):                      # a YAML file that is just prose
        return raw.strip() or DEFAULT_GUIDANCE
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: expected `guidance:` or plain prose, got {type(raw).__name__}")
    g = raw.get("guidance", raw.get("question"))
    if not isinstance(g, str) or not g.strip():
        # Silently substituting a stub would leave the VLM choosing with no
        # instructions, so say so instead.
        raise ValueError(f"{path}: no `guidance:` prose found (keys: {sorted(raw)})")
    return g.strip()


def build_task_config(manifest: list, guidance: str) -> TaskConfig:
    """Number the manifest's prompts. Nothing is attached or modified."""
    if TASKS_SLOT not in guidance:
        raise ValueError(
            f"the prompt file has no {TASKS_SLOT} slot, so the model would never "
            f"be shown the task list. Add {TASKS_SLOT} on its own line where the "
            "numbered instructions should appear.")
    tasks, taken = [], set()
    for i, m in enumerate(manifest):
        label = _slug(m["prompt"], taken)
        taken.add(label)
        tasks.append(Task(index=i, prompt=m["prompt"], label=label,
                          task_index=m["task_index"], datasets=m["datasets"],
                          extra=m["extra"]))
    return TaskConfig(guidance.strip(), tasks)


def load_task_config(manifest_path: str, guidance_path: str = "") -> TaskConfig:
    """The whole config: the model's prompts + your selection guidance."""
    return build_task_config(load_manifest(manifest_path), load_guidance(guidance_path))

@dataclass
class VlmConfig:
    """What the driver needs, so this module never sees argparse."""
    tasks: TaskConfig
    provider: str = "google"
    api_key: str = field(default="", repr=False)   # never let a key into a repr
    vlm_interval: float = 2.0      # seconds between task checks
    home_every: float = 25.0       # episode bound: home + retry this often if no switch happened
    home_timeout: float = 30.0     # max seconds to wait for a home to finish
    episode_floor: float = 5.0     # ignore picks this long after a home — a home alone takes ~3s
    switch_streak: int = 3         # consecutive differing picks before a switch is acted on

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ValueError(f"unknown provider {self.provider!r} (choose from {PROVIDERS})")

    @property
    def model(self) -> str:
        """Fixed per provider — see MODEL at the top."""
        return MODEL[self.provider]


def state_schema(tasks: TaskConfig, google: bool) -> dict:
    """Enum over the manifest's task indices: never a name, never free text,
    never an abstention. `reason` comes first so the model says what it sees
    before committing.

    The two providers need different spellings. Gemini's enums are STRING-only
    (an INTEGER enum is rejected by the SDK before the request is sent), so the
    index goes over as a decimal string; _parse_index accepts either. OpenAI
    strict mode requires every property in `required`."""
    if google:
        return {"type": "OBJECT",
                "properties": {
                    "reason": {"type": "STRING"},
                    "holding": {"type": "BOOLEAN"},
                    "task_index": {"type": "STRING", "format": "enum",
                                   "enum": [str(t.index) for t in tasks.tasks]},
                    "confidence": {"type": "NUMBER"}},
                "required": ["reason", "holding", "task_index", "confidence"],
                "property_ordering": ["reason", "holding", "task_index",
                                      "confidence"]}
    return {"type": "object",
            "properties": {"reason": {"type": "string"},
                           "holding": {"type": "boolean"},
                           "task_index": {"type": "integer",
                                          "enum": [t.index for t in tasks.tasks]},
                           "confidence": {"type": "number"}},
            "required": ["reason", "holding", "task_index", "confidence"],
            "additionalProperties": False}


def write_latest(path: str, **fields) -> None:
    """Overwrite atomically, so a `watch cat` never sees a torn write."""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(f"{k}: {v}" for k, v in fields.items()) + "\n")
        os.replace(tmp, path)
    except OSError as e:
        print(f"[vlm] could not write {path}: {e}", flush=True)


# Slots prompt.md can use. {tasks} is where the manifest's numbered prompt list
# is spliced in and is required — without it the model would be asked to choose
# from a list it was never shown.
TASKS_SLOT = "{tasks}"


def build_prompt(tasks: TaskConfig) -> str:
    """Fill prompt.md's {tasks} slot. The file is the entire query — the numbered
    task list from the manifest lands wherever {tasks} sits, so nothing is
    appended behind your back. Plain str.replace, not str.format, so braces
    elsewhere in the prose are left alone.

    Deliberately no timing slots: the model is asked what it sees in this frame
    and nothing else. Stickiness (don't switch on one noisy read) is the
    controller's debounce, not the prompt's — telling the model about elapsed
    time made it hold stale picks."""
    options = "\n".join(f"  {t.index}. {t.prompt}" for t in tasks.tasks)
    return tasks.guidance.replace(TASKS_SLOT, options)


def grab_right_eye(cam, quality: int = 85, timeout: float = 5.0,
                   max_edge: int = MAX_IMAGE_EDGE) -> bytes:
    """Latest head-cam frame -> JPEG of the right eye (stereo pair, right half),
    scaled down to max_edge on its long side.

    Bounded: ready() also reads False when the camera daemon is absent or the
    topic name is wrong, and an unbounded spin there would hang the thread
    silently with nothing in the log."""
    import cv2
    import numpy as np

    t0 = time.monotonic()
    while not cam.ready():
        if time.monotonic() - t0 > timeout:
            raise RuntimeError(f"no {CAMERA_TOPIC} frame in {timeout:.0f}s "
                               "(camera daemon down, or wrong topic name?)")
        time.sleep(0.01)
    n = int(cam.data["jpeg_len"])
    if n <= 0:
        raise RuntimeError("empty camera frame")
    raw = bytes(cam.data["jpeg"][:n])
    stereo = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)  # BGR
    right = stereo[:, stereo.shape[1] // 2:]
    h, w = right.shape[:2]
    if max_edge and max(h, w) > max_edge:
        s = max_edge / max(h, w)
        # INTER_AREA is the right filter for shrinking; anything else aliases
        # the bowl rim, which is exactly the cue the model is asked to read.
        right = cv2.resize(right, (round(w * s), round(h * s)),
                           interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", right, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        raise RuntimeError("failed to JPEG-encode right eye")
    return enc.tobytes()


def make_vlm_client(cfg: VlmConfig):
    """Build the client for the chosen VLM backend."""
    if cfg.provider == "openai":
        from openai import OpenAI
        return OpenAI(api_key=cfg.api_key)
    if cfg.provider == "overshoot":
        from openai import OpenAI  # OpenAI-compatible, just a different base_url
        return OpenAI(api_key=cfg.api_key, base_url=OVERSHOOT_BASE_URL)
    if cfg.provider == "local":
        from openai import OpenAI  # the Spark's vLLM server is OpenAI-compatible
        if not LOCAL_BASE_URL:
            raise RuntimeError("BB_VLM_URL is not set (it has no default) — e.g. "
                               "BB_VLM_URL=http://spark-3efa.local:8002/v1")
        return OpenAI(api_key=cfg.api_key or "unused", base_url=LOCAL_BASE_URL,
                      timeout=60.0, max_retries=0)
    from google import genai
    return genai.Client(api_key=cfg.api_key)


def _google_state(client, model, prompt, jpeg, schema):
    from google.genai import types

    resp = client.models.generate_content(
        model=model,
        contents=[types.Part.from_bytes(data=jpeg, mime_type="image/jpeg"), prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json", response_schema=schema),
    )
    return json.loads(resp.text or "{}")


def _openai_state(client, model, prompt, jpeg, schema, extra_body=None,
                  prefill=""):
    import base64
    b64 = base64.b64encode(jpeg).decode()
    kwargs = {}
    if schema is not None:
        kwargs["response_format"] = {"type": "json_schema", "json_schema": {
            "name": "scene_state", "strict": True, "schema": schema}}
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
    ]}]
    if prefill:
        # Partial assistant turn the server continues in place (vLLM's
        # continue_final_message). Only valid with thinking off — with
        # thinking on, content comes back None.
        messages.append({"role": "assistant", "content": prefill})
        extra_body = dict(extra_body or {}, continue_final_message=True,
                          add_generation_prompt=False)
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        extra_body=extra_body,
        **kwargs,
    )
    text = resp.choices[0].message.content or ""
    # The server returns only the continuation, without the prefix.
    return _parse_json_reply(prefill + text if prefill else text or "{}")


def _parse_json_reply(text):
    """The reply should be bare JSON; tolerate code fences or stray prose
    around it (the schema-free local path relies on the prompt alone). Raises
    JSONDecodeError on garbage — the caller skips that cycle."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*|\s*```\s*$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, re.DOTALL)
        if not m:
            raise
        return json.loads(m.group(0))


_LOCAL_EXTRA_BODY = {"chat_template_kwargs": {"enable_thinking": False},
                     "max_tokens": 200, "temperature": 0.0}

# The Spark's NVFP4 Qwen occasionally opens with a ```json fence even at
# temperature 0 (first token is a near coin-flip between {" and ```).
# Prefilling the reply with the object's opening makes fencing structurally
# impossible and moves those tokens from decode to prefill. Must match the
# shape _LOCAL_JSON_SUFFIX asks for (reason first); requires thinking off.
_LOCAL_PREFILL = '{"reason": "'

_LOCAL_JSON_SUFFIX = (
    '\n\nAlso report `holding`: true if a robot gripper is grasping or touching '
    'the bowl right now, including mid-transfer, else false.'
    '\n\nAnswer with ONLY a JSON object, no code fences, no other text:\n'
    '{"reason": "<one or two sentences>", "holding": <true or false>, '
    '"task_index": <integer>}')


def select_task(cfg: VlmConfig, client, jpeg):
    """Ask the VLM; return (prompt, label, confidence, reason, holding).
    prompt=None only means the call failed or returned something the enum should
    have prevented — the caller then skips that cycle and asks again. holding
    defaults to False, so a provider that omits the field degrades to acting on
    every pick rather than stalling."""
    prompt = build_prompt(cfg.tasks)
    schema = state_schema(cfg.tasks, google=(cfg.provider == "google"))
    # overshoot and local are OpenAI-compatible, so they share the
    # chat/completions path. local skips the schema (grammar decoding is the
    # Spark server's bottleneck) and instead asks for JSON in the prompt,
    # with thinking mode off, temperature 0, and the reply prefilled so it
    # can only be the bare object.
    if cfg.provider == "google":
        data = _google_state(client, cfg.model, prompt, jpeg, schema)
    elif cfg.provider == "local":
        data = _openai_state(client, cfg.model, prompt + _LOCAL_JSON_SUFFIX,
                             jpeg, None, _LOCAL_EXTRA_BODY,
                             prefill=_LOCAL_PREFILL)
    else:
        data = _openai_state(client, cfg.model, prompt, jpeg, schema)
    if not isinstance(data, dict):
        return None, "unusable_answer(not an object)", None, "", False
    reason = str(data.get("reason", "")).strip()
    holding = data.get("holding")
    holding = holding is True or str(holding).strip().lower() == "true"
    idx = _parse_index(data.get("task_index"))
    task = cfg.tasks.by_index(idx) if idx is not None else None
    label = task.label if task else f"unusable_answer({data.get('task_index')!r})"
    return ((task.prompt if task else None), label, data.get("confidence"),
            reason, holding)


def _parse_index(value):
    """An exact integer, or its decimal-string form. Rejects bools and floats —
    a provider that ignores the schema must not truncate 2.7 into task 2."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())          # rejects "2.0", "0x2", "²", "" ...
        except ValueError:
            return None
    return None


def _halt_everything(client, stop: threading.Event) -> None:
    """End the run. Our own Event only gates this thread — control_loop spins on
    client.shutdown_event, so without setting that too the policy would keep
    executing whatever task was last set (on startup failure, the placeholder)
    with nobody selecting and nobody re-homing."""
    stop.set()
    try:
        client.shutdown_event.set()
    except Exception as e:  # noqa: BLE001 — best effort; we are already failing
        print(f"[vlm] could not signal the client to stop: {e!r}", flush=True)


def _wait_until(stop: threading.Event, cond, timeout: float) -> bool:
    """Poll ``cond`` until true, stop is set, or timeout elapses."""
    t0 = time.monotonic()
    while not cond():
        if stop.is_set() or time.monotonic() - t0 > timeout:
            return False
        stop.wait(0.02)
    return True


def home_and_continue(controls, stop: threading.Event, timeout: float) -> bool:
    """Home and keep running: toggle once (halt + home), wait for it to land,
    toggle again (resume). Homing runs on the control-loop thread so it never
    races send_action."""
    if controls.paused:
        # An earlier home timed out mid-way and left us halted. Manual 'h' is
        # disabled here, so resume rather than refusing forever.
        print("[vlm] client still paused from a previous home; resuming", flush=True)
        controls.toggle_home()
        return _wait_until(stop, lambda: not controls.paused, timeout=timeout)
    controls.toggle_home()                                          # halt + home
    if not _wait_until(stop, lambda: controls.paused, timeout=5.0):  # halt picked up
        return False
    # The resume toggle is only consumed after the (blocking) home() finishes on
    # the control-loop thread, so waiting for paused to clear confirms the home
    # actually landed before we continue.
    controls.toggle_home()                                          # resume
    return _wait_until(stop, lambda: not controls.paused, timeout=timeout)


def vlm_controller(client, cfg: VlmConfig, stop: threading.Event) -> None:
    """Every cfg.vlm_interval seconds, pick the task from the head cam. A pick
    that differs from the running task for cfg.switch_streak consecutive cycles
    ends the episode: home, then run the new pick. Re-home every cfg.home_every
    seconds regardless."""
    from bbos import Reader

    from live_controls import LiveControls

    controls = LiveControls(client)  # sole driver: owns the hook, no keyboard

    try:
        client_vlm = make_vlm_client(cfg)
        cam = Reader(CAMERA_TOPIC, keeptime=False)
        cam.__enter__()
    except Exception as e:  # noqa: BLE001 — a dead selector thread must be loud
        print(f"[vlm] FATAL: controller could not start: {e!r}", flush=True)
        print("[vlm] no task selector -> halting the policy", flush=True)
        _halt_everything(client, stop)
        return
    time.sleep(0.5)
    last_home = time.monotonic()
    current_task = None
    votes = 0                # consecutive usable picks that left the current task
    start_needs_home = True  # boot: arms are wherever they were; a completion homes first
    try:
        while not stop.is_set():
            t0 = time.monotonic()
            since_home = t0 - last_home
            task = loc = conf = latency = None
            reason = ""
            holding = False
            try:
                jpeg = grab_right_eye(cam)
                tc = time.monotonic()
                task, loc, conf, reason, holding = select_task(cfg, client_vlm, jpeg)
                latency = time.monotonic() - tc
            except Exception as e:  # noqa: BLE001 — one bad cycle must not kill the loop
                print(f"[vlm] cycle error (skipping): {e}", flush=True)

            # Debounce: an episode ends after cfg.switch_streak consecutive
            # cycles whose pick is any task other than the running one
            if (task is None or task == current_task or holding
                    or since_home < cfg.episode_floor):
                votes = 0
            else:
                votes += 1

            if task is None:
                action = "no decision (bad response); retrying next cycle"
                print(f"[vlm] {loc} -- {reason or 'no reason given'}", flush=True)
                # Still honor the safety re-home: a run of bad cycles must not
                # leave the arms dwelling in an out-of-distribution pose.
                if current_task is not None and since_home >= cfg.home_every:
                    action += "; safety re-home"
                    print(f"[vlm] {since_home:.0f}s since home with no usable "
                          f"decision; re-homing anyway", flush=True)
                    if home_and_continue(controls, stop, cfg.home_timeout):
                        last_home = time.monotonic()
            elif current_task is None:
                # Boot, or the cycle after an episode ended: run the fresh pick.
                # Home first unless the episode-done branch homed moments ago.
                action = "START -> " + ("run from home" if not start_needs_home
                                        else "home then run")
                print(f"[vlm] start: state={loc} conf={conf} -> {task!r}", flush=True)
                controls.set_task(task)
                current_task = task
                votes = 0
                if start_needs_home:
                    if home_and_continue(controls, stop, cfg.home_timeout):
                        last_home = time.monotonic()
                start_needs_home = True
            elif since_home < cfg.episode_floor:
                action = f"hold (floor {since_home:.0f}/{cfg.episode_floor:.0f}s)"
                print(f"[vlm] {action} (state={loc})", flush=True)
            elif holding and task != current_task:
                action = f"hold (bowl in gripper; pick {loc} ignored)"
                print(f"[vlm] gripper has the bowl; ignoring pick {loc} until "
                      f"it is free", flush=True)
            elif votes >= cfg.switch_streak:
                action = (f"episode done ({votes} picks left the task, last {loc}) "
                          f"-> home + re-pick")
                print(f"[vlm] scene left the running task ({votes}x, last pick "
                      f"{loc}); homing, picking fresh next cycle", flush=True)
                current_task = None
                votes = 0
                if home_and_continue(controls, stop, cfg.home_timeout):
                    last_home = time.monotonic()
                    start_needs_home = False
            elif since_home >= cfg.home_every:
                action = f"re-home (>= {cfg.home_every:.0f}s, task unchanged)"
                print(f"[vlm] {since_home:.0f}s since home >= {cfg.home_every:.0f}s; "
                      f"re-homing (task unchanged: {loc})", flush=True)
                if home_and_continue(controls, stop, cfg.home_timeout):
                    last_home = time.monotonic()
                votes = 0   # the view changes over a home; start over
            elif votes:
                action = f"pending episode end ({votes}/{cfg.switch_streak}: {loc})"
                print(f"[vlm] pick {loc} != current ({votes}/{cfg.switch_streak}); "
                      f"ending episode if it holds", flush=True)
            else:
                action = "unchanged"
                print(f"[vlm] state={loc} conf={conf} task unchanged", flush=True)

            write_latest(
                OUT_FILE,
                time=time.strftime("%Y-%m-%d %H:%M:%S"),
                provider=cfg.provider,
                model=cfg.model,
                state=loc,
                confidence=conf,
                vlm_latency_s=f"{latency:.2f}" if latency is not None else "n/a",
                since_home_s=f"{since_home:.1f}",
                current_task=current_task,
                holding=holding,
                pending_switch=f"{loc} {votes}/{cfg.switch_streak}" if votes else "none",
                reason=reason or "n/a",
                action=action,
            )

            # Pace to the requested interval; VLM latency may exceed it, in which
            # case the next cycle starts immediately. stop.wait doubles as exit.
            if stop.wait(max(0.0, cfg.vlm_interval - (time.monotonic() - t0))):
                break
    except Exception as e:  # noqa: BLE001 — never die silently mid-run
        print(f"[vlm] FATAL: controller thread died: {e!r} -> halting the policy", flush=True)
        _halt_everything(client, stop)
    finally:
        cam.__exit__(None, None, None)


def run_client(config, robot, cfg: VlmConfig) -> None:
    """Drop-in for robot_client.run_client: same wiring, minus the key listener,
    plus the VLM selector thread."""
    from policy_client.async_inference.robot_client import RobotClient

    client = RobotClient(config, robot)
    if not client.start():
        return

    stop = threading.Event()
    receiver = threading.Thread(target=client.receive_actions, daemon=True)
    receiver.start()

    # No _key_listener: manual 'h'/'t' are disabled — the VLM is the sole driver.
    threading.Thread(target=vlm_controller, args=(client, cfg, stop), daemon=True).start()
    client.logger.info(
        f"VLM controller running (every {cfg.vlm_interval}s, re-home every "
        f"{cfg.home_every}s, model={cfg.model}); manual 'h'/'t' disabled")

    try:
        client.control_loop()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        stop.set()
        client.stop()
        receiver.join()
        client.logger.info("Client stopped")

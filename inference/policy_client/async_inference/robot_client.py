# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Standalone async-inference robot client.

Two cooperating threads share an action queue:

1. The main thread runs ``control_loop``: at every 1/fps tick it pops one
   action from the queue (if any), sends it to the robot, then optionally
   captures and ships a fresh observation to the policy server.

2. A daemon receiver thread runs ``receive_actions``: it blocks on
   ``stub.GetActions()``, deserializes the returned chunk of TimedActions,
   and merges them into the queue (replacing any overlapping timesteps with
   the configured aggregate function).

Pacing is governed by ``chunk_size_threshold``: a new observation is only
sent when the queue is at most that fraction full. The ``must_go`` event is
the safety net — if the queue ever drains completely, the next observation
is flagged ``must_go=True`` so the server bypasses its dedup/similarity
filters and produces a chunk immediately.

The Robot interface is duck-typed: any object exposing the six members
listed in the ``Robot`` Protocol below works. The wire format is unchanged
from upstream lerobot, so this client talks to an unmodified
``lerobot.async_inference.policy_server``.
"""

import logging
import pickle  # nosec
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Queue
from typing import Any, Protocol, runtime_checkable

import grpc
import torch

from policy_client.transport import (
    services_pb2,  # type: ignore
    services_pb2_grpc,  # type: ignore
)
from policy_client.transport.utils import grpc_channel_options, send_bytes_in_chunks

from .helpers import (
    Action,
    FPSTracker,
    Observation,
    RawObservation,
    RemotePolicyConfig,
    TimedAction,
    TimedObservation,
    get_logger,
    map_robot_keys_to_lerobot_features,
    visualize_action_queue_size,
)


# ── Configuration ───────────────────────────────────────────────────────────
# Inlined from the deleted configs.py.

DEFAULT_FPS = 30

# Action-chunk merge functions. The receiver thread uses one of these to
# blend overlapping timesteps when a new chunk arrives. ``weighted_average``
# is the upstream default and biases toward the most recent prediction.
AGGREGATE_FUNCTIONS: dict[str, Callable[[torch.Tensor, torch.Tensor], torch.Tensor]] = {
    "weighted_average": lambda old, new: 0.3 * old + 0.7 * new,
    "latest_only": lambda old, new: new,
    "average": lambda old, new: 0.5 * old + 0.5 * new,
    "conservative": lambda old, new: 0.7 * old + 0.3 * new,
}


# ---------------------------------------------------------------------------
# Debug visualization — flip this constant to False to silence. Per action chunk it prints:
#   1. the state we sent           (model input)
#   2. the actions we received     (model output, executed slice)
#   3. each action as it executes  (post-aggregation, what hits the motors)
# Every vector is 16 joint values — left j0..j7 then right j0..j7 — in the
# normalized [-100,100] / [0,100] space.
# ---------------------------------------------------------------------------
DEBUG_PRINT = True

_DBG_HDR = " " * 11 + "".join(f"{f'j{i}':>7}" for i in range(8))

# Set while something else owns the terminal, so _dbg() can't shred the input line.
dbg_quiet = threading.Event()


def _dbg(title, rows):
    """Print a titled debug block: a header row, then an L and R line for each
    (label, 16-D joint vec) in ``rows`` (normalized joint values). Emitted as a
    single print() so the receiver and control-loop threads never interleave a
    block's lines."""
    if dbg_quiet.is_set():
        return
    lines = [title, _DBG_HDR]
    for label, vec in rows:
        v = [float(x) for x in vec]
        lines.append(f"{label + ' L':>9} | " + "".join(f"{x:7.1f}" for x in v[:8]))
        lines.append(f"{label + ' R':>9} | " + "".join(f"{x:7.1f}" for x in v[8:]))
    print("\n".join(lines), flush=True)


@dataclass
class RobotClientConfig:
    """All client-side parameters. Plain dataclass — instantiate in Python.

    Required fields are at the top; everything else has sensible defaults.
    The robot is NOT a field — pass a Robot instance to ``RobotClient`` directly.
    """

    policy_type: str
    pretrained_name_or_path: str
    actions_per_chunk: int

    task: str = ""
    server_address: str = "localhost:8080"
    rename_map: dict[str, str] = field(default_factory=dict)
    policy_device: str = "cpu"
    chunk_size_threshold: float = 0.5
    fps: int = DEFAULT_FPS
    aggregate_fn_name: str = "weighted_average"
    debug_visualize_queue_size: bool = False

    @property
    def environment_dt(self) -> float:
        return 1 / self.fps

    def __post_init__(self):
        if self.aggregate_fn_name not in AGGREGATE_FUNCTIONS:
            raise ValueError(
                f"Unknown aggregate_fn_name {self.aggregate_fn_name!r}. "
                f"Available: {list(AGGREGATE_FUNCTIONS)}"
            )
        if not (0 <= self.chunk_size_threshold <= 1):
            raise ValueError(f"chunk_size_threshold must be in [0, 1], got {self.chunk_size_threshold}")
        if self.fps <= 0 or self.actions_per_chunk <= 0:
            raise ValueError("fps and actions_per_chunk must be positive")
        self.aggregate_fn = AGGREGATE_FUNCTIONS[self.aggregate_fn_name]


# ── Robot Protocol ──────────────────────────────────────────────────────────
# Inlined from the deleted robot.py. Any object exposing these six members
# can be passed to RobotClient.


@runtime_checkable
class Robot(Protocol):
    """Minimal interface RobotClient relies on.

    The order of keys in ``action_features`` MUST match the order the policy
    was trained with — the client zips the policy's flat output tensor with
    these keys to build the dict it sends to ``send_action``.
    """

    @property
    def observation_features(self) -> dict[str, type | tuple]:
        """``{name: float | (h, w, c)}``: float for joints, tuple for cameras."""
        ...

    @property
    def action_features(self) -> dict[str, type]:
        """``{name: float}``: ordered list of action keys."""
        ...

    def connect(self, calibrate: bool = True) -> None: ...
    def disconnect(self) -> None: ...
    def get_observation(self) -> dict[str, Any]: ...
    def send_action(self, action: dict[str, float]) -> dict[str, float]: ...


# ── Client ──────────────────────────────────────────────────────────────────


class RobotClient:
    prefix = "robot_client"
    logger = get_logger(prefix)

    def __init__(self, config: RobotClientConfig, robot: Robot):
        """Initialize RobotClient with configuration and a robot instance.

        ``robot`` is connected immediately and disconnected in ``stop()``.
        """
        self.config = config
        self.robot = robot
        self.robot.connect()

        lerobot_features = map_robot_keys_to_lerobot_features(self.robot)
        self.server_address = config.server_address

        self.policy_config = RemotePolicyConfig(
            config.policy_type,
            config.pretrained_name_or_path,
            lerobot_features,
            config.actions_per_chunk,
            config.policy_device,
            rename_map=config.rename_map,
        )
        self.channel = grpc.insecure_channel(
            self.server_address, grpc_channel_options(initial_backoff=f"{config.environment_dt:.4f}s")
        )
        self.stub = services_pb2_grpc.AsyncInferenceStub(self.channel)
        self.logger.info(f"Initializing client to connect to server at {self.server_address}")

        self.shutdown_event = threading.Event()

        # Action queue + pacing state.
        self.latest_action_lock = threading.Lock()
        self.latest_action = -1
        self.action_chunk_size = -1
        self._chunk_size_threshold = config.chunk_size_threshold

        self.action_queue: Queue[TimedAction] = Queue()
        self.action_queue_lock = threading.Lock()
        self.action_queue_size: list[int] = []
        self.start_barrier = threading.Barrier(2)  # action receiver + control loop

        self.fps_tracker = FPSTracker(target_fps=self.config.fps)

        # must_go: re-armed by receive_actions, cleared each time a starving
        # observation is sent. Initially set so the very first obs goes out.
        self.must_go = threading.Event()
        self.must_go.set()

        self._rtc_logged = False  # one-shot notice that the server is sending RTC chunks

        # Hooks for an external control layer (see bbapps live_controls).
        self.task = config.task
        self.paused = False
        self.on_tick: Callable[[], None] | None = None

        # DEBUG_PRINT visualization state (see _dbg()).
        self._dbg_chunk_id = 0
        self._dbg_exec_idx = 0
        self._dbg_sent_state = None

        self.logger.info("Robot connected and ready")

    @property
    def running(self):
        return not self.shutdown_event.is_set()

    def start(self) -> bool:
        """Handshake with the policy server and ship the policy config."""
        try:
            self.stub.Ready(services_pb2.Empty())

            policy_config_bytes = pickle.dumps(self.policy_config)
            self.stub.SendPolicyInstructions(services_pb2.PolicySetup(data=policy_config_bytes))
            self.logger.info(
                f"Policy config sent: {self.policy_config.policy_type} | "
                f"{self.policy_config.pretrained_name_or_path} | device={self.policy_config.device}"
            )

            self.shutdown_event.clear()
            return True

        except grpc.RpcError as e:
            self.logger.error(f"Failed to connect to policy server: {e}")
            return False

    def stop(self):
        """Tear down the receiver thread and close the channel."""
        self.shutdown_event.set()
        self.robot.disconnect()
        self.channel.close()
        self.logger.debug("Client stopped, channel closed")

    def send_observation(self, obs: TimedObservation) -> bool:
        """Pickle the observation and stream it to the server."""
        if not self.running:
            raise RuntimeError("Client not running. Call RobotClient.start() first.")
        if not isinstance(obs, TimedObservation):
            raise ValueError("Input observation needs to be a TimedObservation")

        observation_bytes = pickle.dumps(obs)
        try:
            observation_iterator = send_bytes_in_chunks(
                observation_bytes,
                services_pb2.Observation,
                log_prefix="[CLIENT] Observation",
                silent=True,
            )
            # Robot clock only; server spans aren't comparable.
            t_send = time.perf_counter()
            self.stub.SendObservations(observation_iterator)
            send_ms = (time.perf_counter() - t_send) * 1000
            self.logger.info(
                f"obs #{obs.get_timestep()} sent | capture {getattr(obs, 'client_capture_ms', float('nan')):6.2f} | "
                f"send_rpc {send_ms:7.2f} | q {self.action_queue.qsize():3d}"
            )
            return True

        except grpc.RpcError as e:
            self.logger.error(f"Error sending observation #{obs.get_timestep()}: {e}")
            return False

    def _aggregate_action_queues(
        self,
        incoming_actions: list[TimedAction],
        aggregate_fn: Callable[[torch.Tensor, torch.Tensor], torch.Tensor] | None = None,
    ):
        """Merge a fresh chunk of actions into the existing queue.

        For each incoming action:
        - older than ``latest_action`` → drop (already executed)
        - timestep not in queue        → put as-is
        - timestep already in queue    → blend old + new with ``aggregate_fn``

        The whole queue is then replaced with the result, so anything in the
        old queue that the new chunk did NOT cover is silently dropped.
        """
        if aggregate_fn is None:
            def aggregate_fn(_old, new):  # noqa: E306
                return new

        future_action_queue: Queue[TimedAction] = Queue()
        with self.action_queue_lock:
            internal_queue = list(self.action_queue.queue)

        current = {a.get_timestep(): a.get_action() for a in internal_queue}

        with self.latest_action_lock:
            latest_action = self.latest_action

        for new_action in incoming_actions:
            ts = new_action.get_timestep()
            if ts <= latest_action:
                continue  # already executed
            if ts not in current:
                future_action_queue.put(new_action)
                continue
            if (getattr(new_action, "profile", None) or {}).get("rtc"):
                # Server already made this chunk continuous; blending would undo that.
                if not self._rtc_logged:
                    self._rtc_logged = True
                    self.logger.info("RTC chunks detected — replacing overlaps instead of blending")
                future_action_queue.put(new_action)
                continue
            # Same timestep already queued — blend.
            future_action_queue.put(
                TimedAction(
                    timestamp=new_action.get_timestamp(),
                    timestep=ts,
                    action=aggregate_fn(current[ts], new_action.get_action()),
                )
            )

        with self.action_queue_lock:
            self.action_queue = future_action_queue

    def clear_action_queue(self) -> None:
        """Drop every queued action. Safe from any thread."""
        with self.action_queue_lock:
            self.action_queue = Queue()

    def receive_actions(self):
        """Receiver thread: block on GetActions, merge chunks into the queue."""
        self.start_barrier.wait()
        self.logger.info("Action receiving thread starting")

        while self.running:
            try:
                # Single clock, spans both network legs and all server-side work.
                t_rpc = time.perf_counter()
                actions_chunk = self.stub.GetActions(services_pb2.Empty())
                rpc_ms = (time.perf_counter() - t_rpc) * 1000
                if len(actions_chunk.data) == 0:
                    continue  # server returned Empty, retry

                timed_actions: list[TimedAction] = pickle.loads(actions_chunk.data)  # nosec
                if not timed_actions:
                    continue

                if self.paused:
                    continue  # so a resume starts from a fresh obs

                self.action_chunk_size = max(self.action_chunk_size, len(timed_actions))
                self.logger.info(
                    f"chunk #{timed_actions[0].get_timestep()} recv | rtt {rpc_ms:7.2f} | "
                    f"q_before {self.action_queue.qsize():3d}"
                )

                if DEBUG_PRINT:
                    self._dbg_chunk_id += 1
                    self._dbg_exec_idx = 0
                    # ~(1 - threshold) of the chunk runs before a fresh obs/chunk arrives.
                    n_exec = max(1, round(len(timed_actions) * (1 - self._chunk_size_threshold)))
                    if self._dbg_sent_state is not None:
                        _dbg(f"[chunk {self._dbg_chunk_id}] state sent",
                             [("state", self._dbg_sent_state)])
                    _dbg(f"[chunk {self._dbg_chunk_id}] actions received → {n_exec} executed",
                         [(f"a{i:02d}", a.get_action().tolist())
                          for i, a in enumerate(timed_actions[:n_exec])])

                try:
                    self._aggregate_action_queues(timed_actions, self.config.aggregate_fn)
                finally:
                    # Re-armed even if the merge raises, or the client can never flag
                    # starvation again. After the swap, so a refilled queue isn't seen empty.
                    self.must_go.set()

            except grpc.RpcError as e:
                self.logger.error(f"Error receiving actions: {e}")
            except Exception:
                # If this thread dies the control loop ticks against a queue nothing refills.
                self.logger.exception("Unexpected error handling action chunk; continuing")

    def control_loop_action(self) -> dict[str, Any]:
        """Pop one action from the queue and execute it on the robot."""
        with self.action_queue_lock:
            self.action_queue_size.append(self.action_queue.qsize())
            timed_action = self.action_queue.get_nowait()

        action_tensor = timed_action.get_action()
        if DEBUG_PRINT:
            _dbg(f"[exec {self._dbg_chunk_id}:{self._dbg_exec_idx:02d}]",
                 [("act", action_tensor.tolist())])
            self._dbg_exec_idx += 1
        action_dict = {key: action_tensor[i].item() for i, key in enumerate(self.robot.action_features)}
        performed_action = self.robot.send_action(action_dict)

        with self.latest_action_lock:
            self.latest_action = timed_action.get_timestep()

        return performed_action

    def control_loop_observation(self, task: str) -> RawObservation:
        """Capture an observation, stamp it, and ship it to the server."""
        try:
            # Between the world changing and the timestamp stamped below: uncounted staleness.
            t_capture = time.perf_counter()
            raw_observation: RawObservation = self.robot.get_observation()
            capture_ms = (time.perf_counter() - t_capture) * 1000
            raw_observation["task"] = task

            if DEBUG_PRINT:
                # Stash the exact joint state we're shipping, in action_features
                # order (left j0..j7 then right j0..j7), for the chunk printout.
                try:
                    self._dbg_sent_state = [float(raw_observation[k]) for k in self.robot.action_features]
                except (KeyError, TypeError):
                    self._dbg_sent_state = None

            with self.latest_action_lock:
                latest_action = self.latest_action

            observation = TimedObservation(
                timestamp=time.time(),  # cross-machine comparable
                observation=raw_observation,
                timestep=max(latest_action, 0),
            )
            observation.client_capture_ms = capture_ms

            with self.action_queue_lock:
                observation.must_go = self.must_go.is_set() and self.action_queue.empty()

            self.send_observation(observation)

            if observation.must_go:
                # The next must-go is re-armed by receive_actions on chunk receipt.
                self.must_go.clear()

            return raw_observation

        except Exception as e:
            self.logger.error(f"Error in observation sender: {e}")

    def control_loop(self) -> tuple[Observation, Action]:
        """Main thread: tick at 1/fps, executing one action and maybe shipping
        one observation per tick."""
        self.start_barrier.wait()
        self.logger.info("Control loop thread starting")

        performed_action = None
        captured_observation = None

        while self.running:
            tick_start = time.perf_counter()

            # On this thread, so a hook can touch the robot without racing send_action.
            if self.on_tick is not None:
                self.on_tick()

            if not self.paused:
                # (1) Execute one queued action if available.
                with self.action_queue_lock:
                    has_action = not self.action_queue.empty()
                if has_action:
                    performed_action = self.control_loop_action()

                # (2) Maybe ship a new observation. The pacing rule is "queue is
                # at most chunk_size_threshold full" — when chunks start arriving,
                # action_chunk_size becomes positive and the predicate kicks in.
                # Before the first chunk it's -1, so any qsize/-1 ratio is <= 0.5
                # and the very first observation always goes out.
                with self.action_queue_lock:
                    ratio = self.action_queue.qsize() / self.action_chunk_size
                if ratio <= self._chunk_size_threshold:
                    captured_observation = self.control_loop_observation(self.task)

            # Pad to maintain the desired control frequency.
            elapsed = time.perf_counter() - tick_start
            time.sleep(max(0, self.config.environment_dt - elapsed))

        return captured_observation, performed_action


def run_client(config: RobotClientConfig, robot: Robot) -> None:
    """Convenience entry point: wire up the receiver thread + main loop.

    Blocks until the policy server hangs up or the caller hits Ctrl+C.
    """
    logging.info(f"Starting RobotClient with config: {config}")

    client = RobotClient(config, robot)
    if not client.start():
        return

    client.logger.info("Starting action receiver thread...")
    action_receiver_thread = threading.Thread(target=client.receive_actions, daemon=True)
    action_receiver_thread.start()

    try:
        client.control_loop()
    finally:
        client.stop()
        action_receiver_thread.join()
        if config.debug_visualize_queue_size:
            visualize_action_queue_size(client.action_queue_size)
        client.logger.info("Client stopped")

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

"""Wire-format dataclasses + a couple of small client-side helpers.

This module is the wire-format anchor: ``TimedData``, ``TimedAction``,
``TimedObservation``, and ``RemotePolicyConfig`` MUST live at the
fully-qualified path ``lerobot.async_inference.helpers.<Class>`` because the
upstream policy server pickles them with that exact module path. Renaming or
moving this module will break ``pickle.loads`` on the other side.

A second pickle wire-format anchor — ``PolicyFeature`` and ``FeatureType``
which the upstream lives at ``lerobot.configs.types`` — is also defined
here. We avoid shipping a real ``lerobot/configs/types.py`` file by
synthesizing the module in ``sys.modules`` at import time and mangling the
classes' ``__module__`` attribute so pickle still records and resolves the
canonical path. See the small block right after the imports.
"""

import logging
import os
import sys
import time
import types as _stdtypes
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import torch

# ── Wire-format anchor #2: PolicyFeature / FeatureType ──────────────────────
# These two classes' canonical pickle path is ``lerobot.configs.types``.
# We define them here in helpers.py and synthesize the parent module so we
# don't need a separate ``lerobot/configs/`` directory on disk. Pickle's
# find_class("lerobot.configs.types", "PolicyFeature") consults sys.modules
# first, so as long as helpers.py has been imported (which always happens
# before any pickle.dumps/loads on the client), the path resolves.


class FeatureType(str, Enum):
    STATE = "STATE"
    VISUAL = "VISUAL"
    ENV = "ENV"
    ACTION = "ACTION"
    REWARD = "REWARD"
    LANGUAGE = "LANGUAGE"


@dataclass
class PolicyFeature:
    type: FeatureType
    shape: tuple[int, ...]


# Make pickle record/resolve the canonical upstream path.
FeatureType.__module__ = "lerobot.configs.types"
PolicyFeature.__module__ = "lerobot.configs.types"

# Synthesize lerobot.configs and lerobot.configs.types so the import system
# can satisfy `from lerobot.configs.types import PolicyFeature` without an
# on-disk module. setdefault: if the upstream lerobot is somehow on the path
# alongside us, do not clobber its real module.
#
# This package is named ``policy_client`` on disk, so ``lerobot`` is no longer
# a real importable package. We must stub the top-level ``lerobot`` module too,
# otherwise ``__import__("lerobot.configs.types")`` fails resolving the parent.
if "lerobot" not in sys.modules:
    _le = _stdtypes.ModuleType("lerobot")
    _le.__path__ = []  # mark as a package so submodules resolve
    sys.modules["lerobot"] = _le
if "lerobot.configs" not in sys.modules:
    sys.modules["lerobot.configs"] = _stdtypes.ModuleType("lerobot.configs")
if "lerobot.configs.types" not in sys.modules:
    _ct = _stdtypes.ModuleType("lerobot.configs.types")
    _ct.FeatureType = FeatureType
    _ct.PolicyFeature = PolicyFeature
    sys.modules["lerobot.configs.types"] = _ct

# Type aliases used as annotations in robot_client.py
Action = torch.Tensor
RawObservation = dict[str, Any]
Observation = dict[str, torch.Tensor]


# ── Wire-format dataclasses (DO NOT MOVE / RENAME) ──────────────────────────
# These four classes' fully-qualified module path is part of the wire format.


@dataclass
class TimedData:
    timestamp: float
    timestep: int

    def get_timestamp(self):
        return self.timestamp

    def get_timestep(self):
        return self.timestep


@dataclass
class TimedAction(TimedData):
    action: Action
    profile: dict[str, float | int] = field(default_factory=dict)

    def get_action(self):
        return self.action


@dataclass
class TimedObservation(TimedData):
    observation: RawObservation
    must_go: bool = False


@dataclass
class RemotePolicyConfig:
    policy_type: str
    pretrained_name_or_path: str
    lerobot_features: dict[str, PolicyFeature]
    actions_per_chunk: int
    device: str = "cpu"
    rename_map: dict[str, str] = field(default_factory=dict)


# Pin these four classes to the canonical upstream pickle path. When this file
# lived at ``lerobot/async_inference/helpers.py`` they got this __module__ for
# free; now that the package is ``policy_client`` we must set it explicitly so
# the unmodified upstream policy server can ``pickle.loads`` them. We also stub
# ``lerobot.async_inference[.helpers]`` in sys.modules so the path resolves.
if "lerobot.async_inference" not in sys.modules:
    _ai = _stdtypes.ModuleType("lerobot.async_inference")
    _ai.__path__ = []  # mark as a package so submodules resolve
    sys.modules["lerobot.async_inference"] = _ai
sys.modules["lerobot.async_inference.helpers"] = sys.modules[__name__]
for _wire_cls in (TimedData, TimedAction, TimedObservation, RemotePolicyConfig):
    _wire_cls.__module__ = "lerobot.async_inference.helpers"


# ── FPS tracker ─────────────────────────────────────────────────────────────


@dataclass
class FPSTracker:
    """Utility to track the average observation rate the client is sending."""

    target_fps: float
    first_timestamp: float = None
    total_obs_count: int = 0

    def calculate_fps_metrics(self, current_timestamp: float) -> dict[str, float]:
        self.total_obs_count += 1
        if self.first_timestamp is None:
            self.first_timestamp = current_timestamp
        total_duration = current_timestamp - self.first_timestamp
        avg_fps = (self.total_obs_count - 1) / total_duration if total_duration > 1e-6 else 0.0
        return {"avg_fps": avg_fps, "target_fps": self.target_fps}


# ── Feature spec builder ────────────────────────────────────────────────────
# Inlined collapse of three upstream functions
# (hw_to_dataset_features + map_robot_keys_to_lerobot_features +
# _validate_feature_names) into a single specialised builder. The upstream
# generic version handled both ACTION and OBSERVATION prefixes; we only ever
# need OBSERVATION because that's all the client sends to the policy server.


def map_robot_keys_to_lerobot_features(robot) -> dict[str, dict]:
    """Build the ``lerobot_features`` dict the policy server consumes during
    ``SendPolicyInstructions``, from a Robot's ``observation_features``.

    Joint/scalar entries collapse into one ``observation.state`` feature with
    a stable name ordering. Tuple entries (camera frame shapes) become one
    ``observation.images.<name>`` feature each.
    """
    hw_features = robot.observation_features

    joint_fts = {
        key: ftype
        for key, ftype in hw_features.items()
        if ftype is float or (isinstance(ftype, PolicyFeature) and ftype.type != FeatureType.VISUAL)
    }
    cam_fts = {key: shape for key, shape in hw_features.items() if isinstance(shape, tuple)}

    features: dict[str, dict] = {}
    if joint_fts:
        features["observation.state"] = {
            "dtype": "float32",
            "shape": (len(joint_fts),),
            "names": list(joint_fts),
        }

    for key, shape in cam_fts.items():
        full_key = f"observation.images.{key}"
        if "/" in full_key:
            raise ValueError(f"Feature names should not contain '/': {full_key!r}")
        features[full_key] = {
            "dtype": "image",
            "shape": shape,
            "names": ["height", "width", "channels"],
        }

    return features


# ── Logging ─────────────────────────────────────────────────────────────────


def get_logger(name: str, log_to_file: bool = True) -> logging.Logger:
    """Stdlib-only logger setup. Idempotent: configures the root logger once
    per process; subsequent calls just return the named logger."""
    root = logging.getLogger()
    if not root.handlers:
        formatter = logging.Formatter(
            "%(levelname)s %(asctime)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler = logging.StreamHandler()
        handler.setFormatter(formatter)
        root.addHandler(handler)
        root.setLevel(logging.INFO)

        if log_to_file:
            os.makedirs("logs", exist_ok=True)
            file_handler = logging.FileHandler(f"logs/{name}_{int(time.time())}.log")
            file_handler.setFormatter(formatter)
            file_handler.setLevel(logging.DEBUG)
            root.addHandler(file_handler)

    return logging.getLogger(name)


# ── Optional debug helper ───────────────────────────────────────────────────


def visualize_action_queue_size(action_queue_size: list[int]) -> None:
    """Plot queue depth over time. matplotlib is imported lazily."""
    import matplotlib.pyplot as plt

    _, ax = plt.subplots()
    ax.set_title("Action Queue Size Over Time")
    ax.set_xlabel("Environment steps")
    ax.set_ylabel("Action Queue Size")
    if action_queue_size:
        ax.set_ylim(0, max(action_queue_size) * 1.1)
    ax.grid(True, alpha=0.3)
    ax.plot(range(len(action_queue_size)), action_queue_size)
    plt.show()

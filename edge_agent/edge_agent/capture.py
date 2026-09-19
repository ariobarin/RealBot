"""Capture-time synchronization for RGB, depth, and map pose."""

from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any, Generic, TypeVar


T = TypeVar("T")


@dataclass(frozen=True)
class TimedSample(Generic[T]):
    timestamp_ns: int
    value: T


@dataclass(frozen=True)
class CaptureBundle:
    capture_id: str
    captured_monotonic_ns: int
    rgb_timestamp_ns: int
    depth_timestamp_ns: int
    pose_timestamp_ns: int
    rgb: bytes
    depth: Any
    map_pose: dict[str, float]
    map_id: str
    map_revision: int
    calibration_revision: str


class CaptureSyncError(RuntimeError):
    pass


class CaptureSynchronizer:
    def __init__(
        self,
        *,
        max_samples: int = 90,
        max_rgb_depth_skew_ms: float = 50,
        max_rgb_pose_skew_ms: float = 100,
        max_age_ms: float = 750,
    ) -> None:
        self._rgb: deque[TimedSample[bytes]] = deque(maxlen=max_samples)
        self._depth: deque[TimedSample[Any]] = deque(maxlen=max_samples)
        self._pose: deque[TimedSample[dict[str, float]]] = deque(maxlen=max_samples)
        self._depth_skew_ns = int(max_rgb_depth_skew_ms * 1_000_000)
        self._pose_skew_ns = int(max_rgb_pose_skew_ms * 1_000_000)
        self._max_age_ns = int(max_age_ms * 1_000_000)

    def add_rgb(self, timestamp_ns: int, value: bytes) -> None:
        self._rgb.append(TimedSample(timestamp_ns, value))

    def add_depth(self, timestamp_ns: int, value: Any) -> None:
        self._depth.append(TimedSample(timestamp_ns, value))

    def add_pose(self, timestamp_ns: int, value: dict[str, float]) -> None:
        self._pose.append(TimedSample(timestamp_ns, value))

    def bundle(
        self,
        *,
        map_id: str,
        map_revision: int,
        calibration_revision: str,
        now_ns: int | None = None,
    ) -> CaptureBundle:
        if not self._rgb or not self._depth or not self._pose:
            raise CaptureSyncError("missing RGB, depth, or pose sample")
        now_ns = time.monotonic_ns() if now_ns is None else now_ns
        rgb = self._rgb[-1]
        depth = min(self._depth, key=lambda sample: abs(sample.timestamp_ns - rgb.timestamp_ns))
        pose = min(self._pose, key=lambda sample: abs(sample.timestamp_ns - rgb.timestamp_ns))
        if now_ns - rgb.timestamp_ns > self._max_age_ns:
            raise CaptureSyncError("RGB sample is stale")
        if abs(depth.timestamp_ns - rgb.timestamp_ns) > self._depth_skew_ns:
            raise CaptureSyncError("RGB/depth skew exceeds limit")
        if abs(pose.timestamp_ns - rgb.timestamp_ns) > self._pose_skew_ns:
            raise CaptureSyncError("RGB/pose skew exceeds limit")
        return CaptureBundle(
            capture_id=f"cap_{uuid.uuid4().hex}",
            captured_monotonic_ns=now_ns,
            rgb_timestamp_ns=rgb.timestamp_ns,
            depth_timestamp_ns=depth.timestamp_ns,
            pose_timestamp_ns=pose.timestamp_ns,
            rgb=rgb.value,
            depth=depth.value,
            map_pose=pose.value,
            map_id=map_id,
            map_revision=map_revision,
            calibration_revision=calibration_revision,
        )

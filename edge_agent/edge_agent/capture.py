"""Validation and packaging for samples already aligned by bbOS IPC."""

from __future__ import annotations

import time
import uuid
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


class AlignedCaptureBuilder:
    """Validate a bbOS-aligned RGB/depth/pose set and create evidence.

    bbOS owns buffering and source-time alignment through ``Reader(aligned_to=...)``.
    This class deliberately does not maintain a second set of sensor queues.
    """

    def __init__(
        self,
        *,
        max_rgb_depth_skew_ms: float = 50,
        max_rgb_pose_skew_ms: float = 100,
        max_age_ms: float = 750,
    ) -> None:
        self._depth_skew_ns = int(max_rgb_depth_skew_ms * 1_000_000)
        self._pose_skew_ns = int(max_rgb_pose_skew_ms * 1_000_000)
        self._max_age_ns = int(max_age_ms * 1_000_000)

    def bundle(
        self,
        *,
        rgb: TimedSample[bytes],
        depth: TimedSample[Any],
        pose: TimedSample[dict[str, float]],
        map_id: str,
        map_revision: int,
        calibration_revision: str,
        now_timestamp_ns: int | None = None,
        now_monotonic_ns: int | None = None,
    ) -> CaptureBundle:
        # bbOS source timestamps use wall-clock nanoseconds. Keep monotonic time
        # only for local evidence; comparing the two clocks would be invalid.
        now_timestamp_ns = time.time_ns() if now_timestamp_ns is None else now_timestamp_ns
        now_monotonic_ns = (
            time.monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
        )
        if now_timestamp_ns - rgb.timestamp_ns > self._max_age_ns:
            raise CaptureSyncError("RGB sample is stale")
        if rgb.timestamp_ns > now_timestamp_ns + self._max_age_ns:
            raise CaptureSyncError("RGB timestamp is in the future")
        if abs(depth.timestamp_ns - rgb.timestamp_ns) > self._depth_skew_ns:
            raise CaptureSyncError("RGB/depth skew exceeds limit")
        if abs(pose.timestamp_ns - rgb.timestamp_ns) > self._pose_skew_ns:
            raise CaptureSyncError("RGB/pose skew exceeds limit")
        return CaptureBundle(
            capture_id=f"cap_{uuid.uuid4().hex}",
            captured_monotonic_ns=now_monotonic_ns,
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

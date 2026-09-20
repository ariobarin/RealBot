"""Read-only bbOS telemetry, suitable for the existing remote_session room.

No writers, motion commands, token requests, or camera consumers are opened.
"""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import json
import math
import time
from typing import Any


TOPIC = "realbot.telemetry"
TOPICS = ("slam.pose", "slam.health", "nav.state")


def fresh(record: Any, now_ns: int, max_age_ms: int) -> bool:
    if record is None:
        return False
    age = now_ns - int(record["timestamp"])
    return 0 <= age <= max_age_ms * 1_000_000


def text(value: Any) -> str:
    if hasattr(value, "tobytes"):
        value = value.tobytes()
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return str(value)


def snapshot(records: dict[str, Any], *, now_ns: int, map_id: str | None = None) -> dict:
    pose = records.get("slam.pose")
    health = records.get("slam.health")
    nav = records.get("nav.state")
    pose_fresh = fresh(pose, now_ns, 300)
    health_fresh = fresh(health, now_ns, 1_000)
    nav_fresh = fresh(nav, now_ns, 2_000)
    position = None
    if pose_fresh:
        x, y, z = (float(v) for v in pose["pos"])
        # Same planar quaternion convention used by the deployed nav daemon.
        qz, qw = float(pose["quat"][2]), float(pose["quat"][3])
        if all(math.isfinite(v) for v in (x, y, z, qz, qw)) and math.hypot(qz, qw) > 1e-6:
            position = {"x": x, "y": y, "z": z, "heading": 2 * math.atan2(qz, qw)}
    flags = {key: bool(health[key]) if health_fresh else None
             for key in ("localized", "degraded", "stalled", "vo_lost", "relocalized")}
    ready = position is not None and health_fresh and flags["localized"] and not any(
        flags[key] for key in ("degraded", "stalled", "vo_lost"))
    return {
        "version": 1,
        "type": "telemetry",
        "at": now_ns // 1_000_000,
        "mapId": map_id,
        "ready": bool(ready),
        "pose": position,
        "slam": {"fresh": health_fresh, "poseFresh": position is not None, **flags},
        "navigation": {
            "fresh": nav_fresh,
            "state": text(nav["state"]) if nav_fresh else "unavailable",
            "reason": text(nav["reason"])[:512] if nav_fresh else "Navigation telemetry missing or stale",
            "waypointIndex": int(nav["waypoint_index"]) if nav_fresh else None,
        },
    }


class BbosTelemetrySource:
    def __init__(self, *, reader_factory=None, map_id: str | None = None):
        if reader_factory is None:
            from bbos import Reader
            reader_factory = Reader
        self.reader_factory = reader_factory
        self.map_id = map_id
        self.stack = ExitStack()
        self.readers: dict[str, Any] = {}

    def __enter__(self):
        try:
            for topic in TOPICS:
                self.readers[topic] = self.stack.enter_context(self.reader_factory(topic, keeptime=False))
        except BaseException:
            self.stack.close()
            raise
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    def read(self) -> dict:
        records = {}
        for topic, reader in self.readers.items():
            reader.ready()  # Also detects a dead/recreated bbOS writer.
            if reader.readable and reader.data is not None:
                records[topic] = reader.data.copy()
        return snapshot(records, now_ns=time.time_ns(), map_id=self.map_id)


async def publish_telemetry(room, *, source_factory=BbosTelemetrySource) -> None:
    """Add this coroutine to remote_session's existing task/cancellation scope.

    Small full snapshots at 5 Hz also initialize late joiners. No incoming data
    handler is necessary. A slow or broken publish fails within two seconds;
    the host's normal session teardown owns reconnect and task cleanup.
    """
    with source_factory() as source:
        while True:
            payload = json.dumps(source.read(), allow_nan=False, separators=(",", ":")).encode()
            await asyncio.wait_for(room.local_participant.publish_data(
                payload, reliable=True, topic=TOPIC), timeout=2.0)
            await asyncio.sleep(0.2)


if __name__ == "__main__":
    # Read-only smoke check: no LiveKit session or hardware writer is started.
    with BbosTelemetrySource() as source:
        for _ in range(3):
            print(json.dumps(source.read(), allow_nan=False), flush=True)
            time.sleep(0.2)

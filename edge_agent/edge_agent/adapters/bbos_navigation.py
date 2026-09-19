"""Navigation adapter for bbOS ``nav.command`` and ``nav.state`` topics."""

from __future__ import annotations

import asyncio
import math
from typing import Any

from ..motion import MotionInterrupted


def navigation_target(payload: dict[str, Any]) -> tuple[float, float, float]:
    """Convert an already map-resolved command into one bbOS waypoint."""
    if "x" not in payload or "y" not in payload:
        raise ValueError("navigation requires map-frame x and y")
    x, y = payload["x"], payload["y"]
    heading = payload.get("heading", math.nan)
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, (int, float))
        or not isinstance(y, (int, float))
        or not math.isfinite(float(x))
        or not math.isfinite(float(y))
    ):
        raise ValueError("navigation x and y must be finite numbers")
    if isinstance(heading, bool) or not isinstance(heading, (int, float)):
        raise ValueError("navigation heading must be a number")
    heading = float(heading)
    if math.isinf(heading):
        raise ValueError("navigation heading cannot be infinite")
    return float(x), float(y), heading


class BbosNavigationAdapter:
    """Keep the command writer alive and observe bbOS until the route ends.

    bbOS retains ownership of planning, ``drive.ctrl``, stale-pose handling,
    command-writer disconnect handling, and arrival/failure decisions.
    """

    def __init__(
        self,
        *,
        poll_interval_s: float = 0.05,
        completion_timeout_s: float = 300.0,
    ) -> None:
        self._poll_interval_s = poll_interval_s
        self._completion_timeout_s = completion_timeout_s
        self._writer: Any | None = None
        self._reader: Any | None = None
        self._stopped = asyncio.Event()

    async def start(self, command_id: str, payload: dict[str, Any]) -> None:
        del command_id  # command IDs remain in the RealBot envelope, not bbOS topics.
        if self._writer is not None:
            raise RuntimeError("navigation adapter is already active")
        x, y, heading = navigation_target(payload)

        # Import lazily so protocol/state-machine tests do not need a robot
        # installation. The real process must run inside a bbOS environment.
        from bbos import Reader, Type, Writer

        writer = Writer("nav.command", Type("nav_command"), keeptime=False)
        reader = Reader("nav.state", keeptime=False)
        self._writer = writer.__enter__()
        self._reader = reader.__enter__()
        self._stopped.clear()
        with self._writer.buf() as command:
            command["enabled"] = True
            command["num_waypoints"] = 1
            command["waypoints"].fill(0)
            command["waypoints"][0] = (x, y, heading)
            command["loop"] = False
            command["global_goal"] = bool(payload.get("globalGoal", False))

    async def wait(self) -> None:
        reader = self._reader
        if reader is None:
            raise RuntimeError("navigation is not active")
        accepted = False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._completion_timeout_s
        while not self._stopped.is_set():
            if loop.time() >= deadline:
                raise TimeoutError("bbOS navigation did not reach a terminal state")
            reader.ready()
            if reader.readable and reader.data is not None:
                state = _text(reader.data["state"])
                reason = _text(reader.data["reason"])
                if state in {"navigating", "waiting_for_drive"}:
                    accepted = True
                elif state == "reached":
                    return
                elif state == "failed":
                    raise RuntimeError(reason or "bbOS navigation failed")
                elif state == "idle" and accepted:
                    raise RuntimeError(reason or "bbOS navigation stopped before arrival")
            await asyncio.sleep(self._poll_interval_s)
        raise MotionInterrupted("navigation was stopped")

    async def update(self, payload: dict[str, Any]) -> None:
        del payload
        raise RuntimeError("bbOS navigation does not accept live route updates")

    async def stop(self, reason: str) -> None:
        del reason
        self._stopped.set()
        writer, reader = self._writer, self._reader
        self._writer = self._reader = None
        try:
            if writer is not None:
                try:
                    with writer.buf() as command:
                        command["enabled"] = False
                        command["num_waypoints"] = 0
                        command["loop"] = False
                        command["global_goal"] = False
                finally:
                    writer.__exit__(None, None, None)
        finally:
            if reader is not None:
                reader.__exit__(None, None, None)


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0].decode("utf-8", errors="replace")
    if hasattr(value, "tobytes"):
        return value.tobytes().split(b"\0", 1)[0].decode("utf-8", errors="replace")
    return str(value)

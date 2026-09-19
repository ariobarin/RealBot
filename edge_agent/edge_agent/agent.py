"""Command orchestration that remains authoritative on the robot."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any

from .motion import MotionCoordinator, MotionMode
from .protocol import Command, CommandError, parse_command
from .watchdog import ControlLeaseWatchdog


Publish = Callable[[dict[str, Any]], Awaitable[None]]


class EdgeAgent:
    def __init__(
        self,
        motion: MotionCoordinator,
        publish: Publish,
        *,
        journal: dict[str, dict[str, Any]] | None = None,
        lease_timeout_s: float = 3.0,
        journal_capacity: int = 256,
    ) -> None:
        self.motion = motion
        self._publish = publish
        self._journal = journal if journal is not None else {}
        self._journal_capacity = journal_capacity
        self._watchdog = ControlLeaseWatchdog(self._lease_expired, timeout_s=lease_timeout_s)
        self.last_stop_reason: str | None = None

    async def start(self) -> None:
        await self._watchdog.start()

    async def close(self) -> None:
        await self._watchdog.close()
        await self.motion.stop_all("edge_shutdown")

    def heartbeat(self) -> None:
        self._watchdog.beat()

    async def control_lost(self, reason: str = "control_disconnected") -> None:
        await self._watchdog.revoke(reason)

    async def handle_command(self, message: dict[str, Any]) -> None:
        command_id = str(message.get("commandId", ""))
        if command_id in self._journal:
            await self._publish(self._journal[command_id])
            return
        try:
            command = parse_command(message)
        except CommandError as exc:
            await self._status(command_id, "rejected", str(exc))
            return

        await self._status(command.command_id, "delivered")
        await self._status(command.command_id, "accepted")
        await self._status(command.command_id, "executing")
        try:
            await self._execute(command)
        except Exception as exc:
            await self._status(command.command_id, "failed", str(exc))
            return
        await self._status(command.command_id, "succeeded")

    async def _execute(self, command: Command) -> None:
        action = command.action
        if action == "stop":
            self.last_stop_reason = "operator_stop"
            await self.motion.stop_all(self.last_stop_reason)
            return
        if action in {"move_to", "move_to_view"}:
            await self._run_to_completion(MotionMode.NAVIGATING, command)
            return
        if action == "follow_start":
            await self.motion.transition(MotionMode.FOLLOWING, command.command_id, command.payload)
            return
        if action == "follow_stop":
            if self.motion.mode == MotionMode.FOLLOWING:
                await self.motion.stop_all("follow_stopped")
            return
        if action == "free_cam_start":
            await self.motion.transition(MotionMode.FREE_CAM, command.command_id, command.payload)
            return
        if action == "free_cam_pose":
            await self.motion.update(MotionMode.FREE_CAM, command.payload)
            return
        if action == "free_cam_stop":
            if self.motion.mode == MotionMode.FREE_CAM:
                await self.motion.stop_all("free_cam_stopped")
            return
        if action == "use_action":
            await self._run_to_completion(MotionMode.INTERACTABLE_TEST, command)
            return
        raise RuntimeError("unsupported action")

    async def _run_to_completion(self, mode: MotionMode, command: Command) -> None:
        await self.motion.transition(mode, command.command_id, command.payload)
        await self.motion.wait_for_completion(mode, command.command_id)
        await self.motion.complete(mode, command.command_id, "completed")

    async def _lease_expired(self, reason: str) -> None:
        self.last_stop_reason = reason
        await self.motion.stop_all(reason)

    async def _status(
        self,
        command_id: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        message: dict[str, Any] = {
            "type": "command_status",
            "commandId": command_id,
            "status": status,
        }
        if detail:
            message["detail"] = detail
        if command_id:
            self._journal[command_id] = message
            while len(self._journal) > self._journal_capacity:
                del self._journal[next(iter(self._journal))]
        await self._publish(message)

    def state(self) -> dict[str, Any]:
        return {
            "type": "robot_state",
            "at": int(time.time() * 1000),
            **self.motion.snapshot(),
            "controlLease": (
                "expired"
                if self._watchdog.expired
                else "active"
                if self._watchdog.active
                else "inactive"
            ),
            "lastStopReason": self.last_stop_reason,
        }

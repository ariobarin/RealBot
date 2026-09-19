"""Exclusive robot motion ownership and priority Stop handling."""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Any, Protocol


class MotionMode(StrEnum):
    IDLE = "idle"
    NAVIGATING = "navigating"
    FOLLOWING = "following"
    FREE_CAM = "free_cam"
    INTERACTABLE_TEST = "interactable_test"
    FAULTED = "faulted"


class MotionInterrupted(RuntimeError):
    """The active motion was stopped or superseded before normal completion."""


class MotionAdapter(Protocol):
    async def start(self, command_id: str, payload: dict[str, Any]) -> None: ...

    async def update(self, payload: dict[str, Any]) -> None: ...

    async def stop(self, reason: str) -> None: ...


class MotionCoordinator:
    """Owns the single active motion mode.

    Normal transitions are serialized. ``stop_all`` deliberately does not wait
    for that transition lock, allowing it to interrupt an adapter that is still
    starting or executing.
    """

    def __init__(self, adapters: dict[MotionMode, MotionAdapter], timeout_s: float = 2.0) -> None:
        self._adapters = adapters
        self._timeout_s = timeout_s
        self._transition_lock = asyncio.Lock()
        self._stop_lock = asyncio.Lock()
        self._epoch = 0
        self.mode = MotionMode.IDLE
        self.owner_command_id: str | None = None
        self.fault: str | None = None

    async def transition(
        self,
        target: MotionMode,
        command_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        if target in {MotionMode.IDLE, MotionMode.FAULTED}:
            raise ValueError("use stop_all to leave an active mode")

        async with self._transition_lock:
            if self.mode == target and self.owner_command_id == command_id:
                return
            self._epoch += 1
            transition_epoch = self._epoch
            await self._stop_active("mode_transition")

            adapter = self._adapters[target]
            self.mode = target
            self.owner_command_id = command_id
            self.fault = None
            try:
                await asyncio.wait_for(adapter.start(command_id, payload or {}), self._timeout_s)
            except Exception as exc:
                await self._fault(adapter, f"{target.value} start failed: {exc}")
                raise

            if transition_epoch != self._epoch:
                await self._safe_stop(adapter, "superseded")
                raise MotionInterrupted(f"{target.value} was interrupted")

    async def update(self, expected: MotionMode, payload: dict[str, Any]) -> None:
        if self.mode != expected:
            raise RuntimeError(f"{expected.value} is not active")
        await asyncio.wait_for(self._adapters[expected].update(payload), self._timeout_s)

    async def complete(
        self,
        expected_mode: MotionMode,
        expected_command_id: str,
        reason: str = "completed",
    ) -> None:
        async with self._transition_lock:
            if self.mode != expected_mode or self.owner_command_id != expected_command_id:
                raise MotionInterrupted(f"{expected_mode.value} no longer owns motion")
            self._epoch += 1
            await self._stop_active(reason)

    async def stop_all(self, reason: str) -> None:
        async with self._stop_lock:
            self._epoch += 1
            await self._stop_active(reason)

    async def _stop_active(self, reason: str) -> None:
        mode = self.mode
        adapter = self._adapters.get(mode)
        self.mode = MotionMode.IDLE
        self.owner_command_id = None
        if adapter is None:
            return
        try:
            await asyncio.wait_for(adapter.stop(reason), self._timeout_s)
        except Exception as exc:
            self.mode = MotionMode.FAULTED
            self.fault = f"{mode.value} stop failed: {exc}"
            raise

    async def _safe_stop(self, adapter: MotionAdapter, reason: str) -> None:
        try:
            await asyncio.wait_for(adapter.stop(reason), self._timeout_s)
        except Exception as exc:
            self.mode = MotionMode.FAULTED
            self.fault = f"superseded stop failed: {exc}"

    async def _fault(self, adapter: MotionAdapter, detail: str) -> None:
        self.mode = MotionMode.FAULTED
        self.owner_command_id = None
        self.fault = detail
        await self._safe_stop(adapter, "start_failed")

    def snapshot(self) -> dict[str, Any]:
        return {
            "motionMode": self.mode.value,
            "motionOwnerCommandId": self.owner_command_id,
            "motionFault": self.fault,
        }

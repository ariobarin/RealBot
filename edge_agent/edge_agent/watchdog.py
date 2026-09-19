"""Robot-local controller heartbeat watchdog."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable


ExpiryCallback = Callable[[str], Awaitable[None] | None]


class ControlLeaseWatchdog:
    def __init__(
        self,
        on_expire: ExpiryCallback,
        timeout_s: float = 3.0,
        check_interval_s: float = 0.1,
    ) -> None:
        self._on_expire = on_expire
        self._timeout_s = timeout_s
        self._check_interval_s = check_interval_s
        self._last_beat: float | None = None
        self._task: asyncio.Task[None] | None = None
        self.expired = False
        self.active = False

    def beat(self) -> None:
        self._last_beat = time.monotonic()
        self.expired = False
        self.active = True

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def revoke(self, reason: str = "control_disconnected") -> None:
        self._last_beat = None
        self.active = False
        await self._expire(reason)

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._check_interval_s)
            if self._last_beat is None or self.expired:
                continue
            if time.monotonic() - self._last_beat >= self._timeout_s:
                self._last_beat = None
                await self._expire("heartbeat_expired")

    async def _expire(self, reason: str) -> None:
        if self.expired:
            return
        self.expired = True
        self.active = False
        result = self._on_expire(reason)
        if inspect.isawaitable(result):
            await result

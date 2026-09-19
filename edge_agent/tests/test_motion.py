import asyncio
from typing import Any

from edge_agent.motion import MotionCoordinator, MotionInterrupted, MotionMode


class FakeAdapter:
    def __init__(self, block: bool = False) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.updated: list[dict[str, Any]] = []
        self.started_event = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()

    async def start(self, command_id: str, payload: dict[str, Any]) -> None:
        self.started.append(command_id)
        self.started_event.set()
        await self.release.wait()

    async def update(self, payload: dict[str, Any]) -> None:
        self.updated.append(payload)

    async def stop(self, reason: str) -> None:
        self.stopped.append(reason)
        self.release.set()


def test_transition_stops_previous_owner() -> None:
    async def scenario() -> None:
        follow = FakeAdapter()
        nav = FakeAdapter()
        motion = MotionCoordinator(
            {MotionMode.FOLLOWING: follow, MotionMode.NAVIGATING: nav}, timeout_s=0.5
        )
        await motion.transition(MotionMode.FOLLOWING, "follow-1")
        await motion.transition(MotionMode.NAVIGATING, "nav-1")
        assert follow.stopped == ["mode_transition"]
        assert motion.mode == MotionMode.NAVIGATING
        assert motion.owner_command_id == "nav-1"

    asyncio.run(scenario())


def test_stop_preempts_start_in_progress() -> None:
    async def scenario() -> None:
        free_cam = FakeAdapter(block=True)
        motion = MotionCoordinator({MotionMode.FREE_CAM: free_cam}, timeout_s=0.5)
        transition = asyncio.create_task(
            motion.transition(MotionMode.FREE_CAM, "cam-1", {"sessionId": "session"})
        )
        await free_cam.started_event.wait()
        await motion.stop_all("operator_stop")
        try:
            await transition
        except MotionInterrupted:
            pass
        else:
            raise AssertionError("interrupted transition must not report success")
        assert "operator_stop" in free_cam.stopped
        assert motion.mode == MotionMode.IDLE
        assert motion.owner_command_id is None

    asyncio.run(scenario())


def test_duplicate_transition_is_idempotent() -> None:
    async def scenario() -> None:
        follow = FakeAdapter()
        motion = MotionCoordinator({MotionMode.FOLLOWING: follow})
        await motion.transition(MotionMode.FOLLOWING, "follow-1")
        await motion.transition(MotionMode.FOLLOWING, "follow-1")
        assert follow.started == ["follow-1"]

    asyncio.run(scenario())

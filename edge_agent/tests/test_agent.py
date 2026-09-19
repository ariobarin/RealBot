import asyncio
import time
from typing import Any

from edge_agent.agent import EdgeAgent
from edge_agent.motion import MotionCoordinator, MotionMode


class Adapter:
    def __init__(self) -> None:
        self.starts = 0
        self.stops: list[str] = []
        self.completed = asyncio.Event()
        self.completed.set()

    async def start(self, command_id: str, payload: dict[str, Any]) -> None:
        self.starts += 1

    async def update(self, payload: dict[str, Any]) -> None:
        pass

    async def wait(self) -> None:
        await self.completed.wait()

    async def stop(self, reason: str) -> None:
        self.stops.append(reason)
        self.completed.set()


class SlowAdapter(Adapter):
    def __init__(self) -> None:
        super().__init__()
        self.started_event = asyncio.Event()
        self.completed.clear()

    async def start(self, command_id: str, payload: dict[str, Any]) -> None:
        self.starts += 1
        self.started_event.set()

    async def wait(self) -> None:
        await self.completed.wait()


def command(command_id: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    now = int(time.time() * 1000)
    return {
        "type": "command",
        "commandId": command_id,
        "createdAt": now,
        "expiresAt": now + 1_000,
        "action": action,
        "payload": payload or {},
    }


def test_duplicate_command_does_not_repeat_motion() -> None:
    async def scenario() -> None:
        adapter = Adapter()
        published: list[dict[str, Any]] = []

        async def publish(message: dict[str, Any]) -> None:
            published.append(message)

        agent = EdgeAgent(
            MotionCoordinator({MotionMode.NAVIGATING: adapter}),
            publish,
        )
        message = command("nav-1", "move_to", {"x": 1, "y": 2})
        await agent.handle_command(message)
        await agent.handle_command(message)
        assert adapter.starts == 1
        assert published[-1]["status"] == "succeeded"

    asyncio.run(scenario())


def test_control_loss_stops_persistent_mode_without_resume() -> None:
    async def scenario() -> None:
        follow = Adapter()

        async def publish(message: dict[str, Any]) -> None:
            pass

        agent = EdgeAgent(
            MotionCoordinator({MotionMode.FOLLOWING: follow}),
            publish,
        )
        await agent.handle_command(command("follow-1", "follow_start"))
        assert agent.motion.mode == MotionMode.FOLLOWING
        await agent.control_lost("controller_disconnected")
        assert agent.motion.mode == MotionMode.IDLE
        assert agent.last_stop_reason == "controller_disconnected"
        agent.heartbeat()
        assert agent.motion.mode == MotionMode.IDLE

    asyncio.run(scenario())


def test_retry_while_command_is_running_does_not_start_twice() -> None:
    async def scenario() -> None:
        adapter = SlowAdapter()
        published: list[dict[str, Any]] = []

        async def publish(message: dict[str, Any]) -> None:
            published.append(message)

        agent = EdgeAgent(MotionCoordinator({MotionMode.NAVIGATING: adapter}), publish)
        message = command("nav-running", "move_to", {"x": 1, "y": 2})
        first = asyncio.create_task(agent.handle_command(message))
        await adapter.started_event.wait()
        await agent.handle_command(message)
        adapter.completed.set()
        await first
        assert adapter.starts == 1
        assert any(item["status"] == "executing" for item in published)

    asyncio.run(scenario())


def test_stop_makes_interrupted_command_fail_not_succeed() -> None:
    async def scenario() -> None:
        adapter = SlowAdapter()
        published: list[dict[str, Any]] = []

        async def publish(message: dict[str, Any]) -> None:
            published.append(message)

        agent = EdgeAgent(MotionCoordinator({MotionMode.NAVIGATING: adapter}), publish)
        moving = asyncio.create_task(
            agent.handle_command(command("nav-interrupted", "move_to", {"x": 1, "y": 2}))
        )
        await adapter.started_event.wait()
        await agent.handle_command(command("stop-1", "stop"))
        await moving
        nav_results = [
            item["status"]
            for item in published
            if item.get("commandId") == "nav-interrupted"
        ]
        assert nav_results[-1] == "failed"
        assert "succeeded" not in nav_results

    asyncio.run(scenario())

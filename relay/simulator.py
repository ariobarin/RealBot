"""Simulated robot that exercises the same edge runtime as hardware adapters."""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import time
from typing import Any

from edge_agent import EdgeAgent, MotionCoordinator, MotionMode
from PIL import Image, ImageDraw
from websockets.asyncio.client import connect

RELAY_WS_URL = os.getenv("RELAY_WS_URL", "ws://127.0.0.1:8000").rstrip("/")
ROOM_ID = os.getenv("ROOM_ID", "demo-bot")
ROBOT_TOKEN = os.getenv("ROBOT_TOKEN")


async def authenticate(socket: Any) -> None:
    if ROBOT_TOKEN:
        await socket.send(json.dumps({"type": "auth", "token": ROBOT_TOKEN}))


class SimulatedMotionAdapter:
    """Fake one hardware owner while preserving real coordinator semantics."""

    def __init__(self, simulator: "Simulator", mode: MotionMode) -> None:
        self.simulator = simulator
        self.mode = mode
        self.highest_sequence = -1
        self.session_id: str | None = None
        self.payload: dict[str, Any] = {}
        self.stopped = False

    async def start(self, command_id: str, payload: dict[str, Any]) -> None:
        del command_id
        self.simulator.status = self.mode.value
        self.payload = payload
        self.stopped = False
        if self.mode == MotionMode.FREE_CAM:
            session_id = payload.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise ValueError("free cam requires sessionId")
            self.session_id = session_id
            self.highest_sequence = -1
            return
        if self.mode == MotionMode.FOLLOWING:
            return

    async def wait(self) -> None:
        if self.mode not in {MotionMode.NAVIGATING, MotionMode.INTERACTABLE_TEST}:
            raise RuntimeError(f"{self.mode.value} is a persistent mode")
        await asyncio.sleep(0.15)
        if self.stopped:
            raise RuntimeError(f"{self.mode.value} was stopped")
        payload = self.payload
        if self.mode == MotionMode.NAVIGATING:
            if "x" in payload and "y" in payload:
                self.simulator.x = float(payload["x"])
                self.simulator.y = float(payload["y"])
            elif "u" in payload and "v" in payload:
                u = min(max(float(payload["u"]), 0.0), 1.0)
                v = min(max(float(payload["v"]), 0.0), 1.0)
                self.simulator.x += 0.4 + (1.0 - v) * 2.0
                self.simulator.y += (u - 0.5) * 1.5
        elif self.mode == MotionMode.INTERACTABLE_TEST:
            self.simulator.status = f"tested {payload.get('name', 'demo_action')}"

    async def update(self, payload: dict[str, Any]) -> None:
        if self.mode != MotionMode.FREE_CAM:
            raise RuntimeError("only Free Cam accepts live updates")
        if payload.get("sessionId") != self.session_id:
            raise ValueError("stale Free Cam session")
        sequence = payload.get("sequence")
        if not isinstance(sequence, int) or sequence <= self.highest_sequence:
            raise ValueError("stale Free Cam sequence")
        self.highest_sequence = sequence
        pan = float(payload.get("panDeg", 0))
        tilt = float(payload.get("tiltDeg", 0))
        if not -60 <= pan <= 60 or not -35 <= tilt <= 45:
            raise ValueError("Free Cam pose outside bounds")

    async def stop(self, reason: str) -> None:
        self.stopped = True
        self.session_id = None
        self.simulator.status = "stopped" if reason != "completed" else "ready"


class Simulator:
    def __init__(self) -> None:
        self.x = 0.0
        self.y = 0.0
        self.heading = 0.0
        self.status = "ready"
        self.results: dict[str, dict[str, Any]] = {}

    async def emit(self, socket: Any, message: dict[str, Any]) -> None:
        await socket.send(json.dumps(message))

    def motion(self) -> MotionCoordinator:
        adapters = {
            mode: SimulatedMotionAdapter(self, mode)
            for mode in (
                MotionMode.NAVIGATING,
                MotionMode.FOLLOWING,
                MotionMode.FREE_CAM,
                MotionMode.INTERACTABLE_TEST,
            )
        }
        return MotionCoordinator(adapters)

    async def control_session(self) -> None:
        uri = f"{RELAY_WS_URL}/ws/robot/{ROOM_ID}"
        async with connect(uri, ping_interval=10, ping_timeout=20) as socket:
            await authenticate(socket)
            print(f"[sim] control connected: {uri}", flush=True)
            agent = EdgeAgent(self.motion(), lambda message: self.emit(socket, message), journal=self.results)
            await agent.start()

            async def states() -> None:
                while True:
                    state = agent.state()
                    state.update(
                        {
                            "ready": True,
                            "status": self.status,
                            "pose": {"x": self.x, "y": self.y, "heading": self.heading},
                        }
                    )
                    await self.emit(socket, state)
                    await asyncio.sleep(0.25)

            async def commands() -> None:
                async for raw in socket:
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if message.get("type") == "heartbeat":
                        agent.heartbeat()
                    elif message.get("type") == "control_lost":
                        await agent.control_lost(str(message.get("reason", "control_disconnected")))
                    elif message.get("type") == "command":
                        asyncio.create_task(agent.handle_command(message))

            try:
                await asyncio.gather(states(), commands())
            finally:
                await agent.control_lost("relay_disconnected")
                await agent.close()

    def frame(self, tick: int) -> bytes:
        width, height = 640, 360
        image = Image.new("RGB", (width, height), "#20242b")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 250, width, height), fill="#736d63")
        for offset in range(0, width, 80):
            x = (offset - tick * 5) % (width + 80) - 40
            draw.line((x, 250, x + 90, height), fill="#969086", width=2)
        cx = int(width / 2 + math.sin(tick / 12) * 120)
        draw.ellipse((cx - 24, 165, cx + 24, 213), fill="#ff385c")
        draw.text((20, 20), f"BracketBot camera · {ROOM_ID}", fill="white")
        draw.text((20, 44), time.strftime("%H:%M:%S"), fill="#d7d7d7")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=68, optimize=True)
        return output.getvalue()

    async def video_session(self) -> None:
        uri = f"{RELAY_WS_URL}/ws/robot/{ROOM_ID}/video"
        async with connect(uri, ping_interval=10, ping_timeout=20, max_size=None) as socket:
            await authenticate(socket)
            print(f"[sim] video connected: {uri}", flush=True)
            tick = 0
            while True:
                await socket.send(self.frame(tick))
                tick += 1
                await asyncio.sleep(0.2)


async def reconnecting(label: str, run: Any) -> None:
    delay = 0.5
    while True:
        try:
            await run()
            delay = 0.5
        except Exception as exc:
            print(f"[sim] {label} disconnected: {exc}; retrying in {delay:.1f}s", flush=True)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8.0)


async def main() -> None:
    simulator = Simulator()
    await asyncio.gather(
        reconnecting("control", simulator.control_session),
        reconnecting("video", simulator.video_session),
    )


if __name__ == "__main__":
    asyncio.run(main())

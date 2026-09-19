"""Simulated robot for exercising the hackathon relay without hardware."""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import time
from typing import Any

from PIL import Image, ImageDraw
from websockets.asyncio.client import connect

RELAY_WS_URL = os.getenv("RELAY_WS_URL", "ws://127.0.0.1:8000").rstrip("/")
ROOM_ID = os.getenv("ROOM_ID", "demo-bot")


class Simulator:
    def __init__(self) -> None:
        self.x = 0.0
        self.y = 0.0
        self.heading = 0.0
        self.status = "ready"
        self.results: dict[str, dict[str, Any]] = {}

    async def emit(self, socket: Any, message: dict[str, Any]) -> None:
        await socket.send(json.dumps(message))

    async def command_status(
        self, socket: Any, command_id: str, status: str, detail: str | None = None
    ) -> None:
        message: dict[str, Any] = {
            "type": "command_status",
            "commandId": command_id,
            "status": status,
        }
        if detail:
            message["detail"] = detail
        self.results[command_id] = message
        await self.emit(socket, message)

    async def execute(self, socket: Any, command: dict[str, Any]) -> None:
        command_id = str(command.get("commandId", ""))
        if not command_id:
            return
        if command_id in self.results:
            await self.emit(socket, self.results[command_id])
            return

        action = command.get("action")
        payload = command.get("payload") or {}
        drop_first = bool(payload.get("dropFirstAck"))

        # Reserve the ID before any await so retries can never execute twice.
        self.results[command_id] = {
            "type": "command_status",
            "commandId": command_id,
            "status": "executing",
        }
        if not drop_first:
            await self.command_status(socket, command_id, "delivered")
            await self.command_status(socket, command_id, "accepted")
            await self.command_status(socket, command_id, "executing")

        self.status = str(action or "working")
        await asyncio.sleep(0.65)
        if action == "move_to":
            self.x = float(payload.get("x", self.x))
            self.y = float(payload.get("y", self.y))
        elif action == "move_to_view":
            # Demo-only image-to-ground approximation. The hardware adapter will
            # replace this with depth/calibration plus navigation planning.
            u = min(max(float(payload.get("u", 0.5)), 0.0), 1.0)
            v = min(max(float(payload.get("v", 0.75)), 0.0), 1.0)
            forward = 0.4 + (1.0 - v) * 2.0
            lateral = (u - 0.5) * 1.5
            self.x += forward
            self.y += lateral
        elif action == "stop":
            self.status = "stopped"
        elif action == "use_action":
            self.status = f"used {payload.get('name', 'demo_action')}"
        self.status = "ready" if action != "stop" else "stopped"
        result = {
            "type": "command_status",
            "commandId": command_id,
            "status": "succeeded",
        }
        self.results[command_id] = result
        await self.emit(socket, result)

    async def control_session(self) -> None:
        uri = f"{RELAY_WS_URL}/ws/robot/{ROOM_ID}"
        async with connect(uri, ping_interval=10, ping_timeout=20) as socket:
            print(f"[sim] control connected: {uri}", flush=True)

            async def states() -> None:
                while True:
                    await self.emit(
                        socket,
                        {
                            "type": "robot_state",
                            "at": int(time.time() * 1000),
                            "ready": True,
                            "status": self.status,
                            "pose": {"x": self.x, "y": self.y, "heading": self.heading},
                        },
                    )
                    await asyncio.sleep(0.5)

            async def commands() -> None:
                async for raw in socket:
                    try:
                        message = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if message.get("type") == "command":
                        asyncio.create_task(self.execute(socket, message))

            await asyncio.gather(states(), commands())

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

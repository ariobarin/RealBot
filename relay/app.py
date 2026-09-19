"""In-memory hackathon relay for one robot and one browser per room."""

from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


@dataclass
class Room:
    robot: WebSocket | None = None
    client: WebSocket | None = None
    robot_video: WebSocket | None = None
    client_video: WebSocket | None = None
    last_state: str | None = None


app = FastAPI(title="RealBot Hackathon Relay", version="0.1.0")
rooms: dict[str, Room] = {}


def room_for(room_id: str) -> Room:
    return rooms.setdefault(room_id, Room())


async def send_text(socket: WebSocket | None, message: str) -> bool:
    if socket is None:
        return False
    try:
        await socket.send_text(message)
        return True
    except Exception:
        return False


async def presence(room: Room, online: bool) -> None:
    await send_text(room.client, json.dumps({"type": "presence", "online": online}))


async def replace(room: Room, slot: str, socket: WebSocket) -> None:
    previous = getattr(room, slot)
    setattr(room, slot, socket)
    if previous is not None and previous is not socket:
        try:
            await previous.close(code=1012, reason="connection replaced")
        except Exception:
            pass


def prune(room_id: str, room: Room) -> None:
    if not any((room.robot, room.client, room.robot_video, room.client_video)):
        rooms.pop(room_id, None)


@app.get("/health")
async def health() -> dict[str, int | str]:
    return {"status": "ok", "rooms": len(rooms)}


@app.websocket("/ws/robot/{room_id}")
async def robot_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    room = room_for(room_id)
    await replace(room, "robot", socket)
    await presence(room, True)
    try:
        while True:
            message = await socket.receive_text()
            try:
                parsed = json.loads(message)
                if parsed.get("type") == "robot_state":
                    room.last_state = message
            except (json.JSONDecodeError, AttributeError):
                pass
            await send_text(room.client, message)
    except WebSocketDisconnect:
        pass
    finally:
        if room.robot is socket:
            room.robot = None
            await presence(room, False)
        prune(room_id, room)


@app.websocket("/ws/client/{room_id}")
async def client_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    room = room_for(room_id)
    await replace(room, "client", socket)
    await presence(room, room.robot is not None)
    if room.last_state:
        await send_text(socket, room.last_state)
    try:
        while True:
            message = await socket.receive_text()
            if await send_text(room.robot, message):
                continue
            try:
                command_id = json.loads(message).get("commandId")
            except (json.JSONDecodeError, AttributeError):
                command_id = None
            await socket.send_text(
                json.dumps(
                    {
                        "type": "command_status",
                        "commandId": command_id,
                        "status": "failed",
                        "detail": "robot offline",
                    }
                )
            )
    except WebSocketDisconnect:
        pass
    finally:
        if room.client is socket:
            room.client = None
        prune(room_id, room)


@app.websocket("/ws/robot/{room_id}/video")
async def robot_video_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    room = room_for(room_id)
    await replace(room, "robot_video", socket)
    try:
        while True:
            frame = await socket.receive_bytes()
            client = room.client_video
            if client is not None:
                try:
                    await client.send_bytes(frame)
                except Exception:
                    if room.client_video is client:
                        room.client_video = None
    except WebSocketDisconnect:
        pass
    finally:
        if room.robot_video is socket:
            room.robot_video = None
        prune(room_id, room)


@app.websocket("/ws/client/{room_id}/video")
async def client_video_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    room = room_for(room_id)
    await replace(room, "client_video", socket)
    try:
        while True:
            # Browsers do not send video data; receiving keeps disconnect detection immediate.
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        if room.client_video is socket:
            room.client_video = None
        prune(room_id, room)

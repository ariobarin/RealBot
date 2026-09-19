"""In-memory hackathon relay for one robot and one browser per room."""

from __future__ import annotations

import asyncio
import hmac
import json
import os
from dataclasses import dataclass, field

from fastapi import FastAPI, WebSocket, WebSocketDisconnect


@dataclass
class Room:
    robot: WebSocket | None = None
    robot_video: WebSocket | None = None
    clients: list[WebSocket] = field(default_factory=list)
    client_videos: list[WebSocket] = field(default_factory=list)
    client_can_control: dict[int, bool] = field(default_factory=dict)
    controller: WebSocket | None = None
    last_state: str | None = None


app = FastAPI(title="RealBot Hackathon Relay", version="0.1.0")
rooms: dict[str, Room] = {}


def room_for(room_id: str) -> Room:
    return rooms.setdefault(room_id, Room())


def token_matches(provided: str | None, expected: str) -> bool:
    return provided is not None and hmac.compare_digest(provided, expected)


async def receive_auth_token(socket: WebSocket) -> str | None:
    try:
        raw = await asyncio.wait_for(socket.receive_text(), timeout=5.0)
        message = json.loads(raw)
    except (TimeoutError, json.JSONDecodeError, AttributeError):
        return None
    if message.get("type") != "auth" or not isinstance(message.get("token"), str):
        return None
    return message["token"]


async def authenticate_robot(socket: WebSocket) -> bool:
    expected = os.getenv("ROBOT_TOKEN")
    if expected is None:
        return True
    return token_matches(await receive_auth_token(socket), expected)


async def client_access(socket: WebSocket) -> tuple[bool, bool]:
    control_secret = os.getenv("CONTROL_TOKEN")
    view_secret = os.getenv("VIEW_TOKEN")
    if control_secret is None and view_secret is None:
        return True, True
    provided = await receive_auth_token(socket)
    if control_secret is not None and token_matches(provided, control_secret):
        return True, True
    if view_secret is not None and token_matches(provided, view_secret):
        return True, False
    return False, False


async def reject_unauthorized(socket: WebSocket) -> None:
    await socket.close(code=1008, reason="unauthorized")


async def send_text(socket: WebSocket | None, message: str) -> bool:
    if socket is None:
        return False
    try:
        await socket.send_text(message)
        return True
    except Exception:
        return False


async def presence(room: Room, online: bool) -> None:
    await broadcast_text(room.clients, json.dumps({"type": "presence", "online": online}))


async def send_lease(socket: WebSocket, acquired: bool) -> None:
    await send_text(socket, json.dumps({"type": "control_lease", "acquired": acquired}))


async def announce_leases(room: Room) -> None:
    for client in list(room.clients):
        if not await send_text(
            client,
            json.dumps({"type": "control_lease", "acquired": client is room.controller}),
        ) and client in room.clients:
            room.clients.remove(client)


async def broadcast_text(sockets: list[WebSocket], message: str) -> None:
    for socket in list(sockets):
        if not await send_text(socket, message) and socket in sockets:
            sockets.remove(socket)


async def replace(room: Room, slot: str, socket: WebSocket) -> None:
    previous = getattr(room, slot)
    setattr(room, slot, socket)
    if previous is not None and previous is not socket:
        try:
            await previous.close(code=1012, reason="connection replaced")
        except Exception:
            pass


def prune(room_id: str, room: Room) -> None:
    if not any((room.robot, room.clients, room.robot_video, room.client_videos)):
        rooms.pop(room_id, None)


@app.get("/health")
async def health() -> dict[str, int | str]:
    return {"status": "ok", "rooms": len(rooms)}


@app.websocket("/ws/robot/{room_id}")
async def robot_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    if not await authenticate_robot(socket):
        await reject_unauthorized(socket)
        return
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
            await broadcast_text(room.clients, message)
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
    authorized, can_control = await client_access(socket)
    if not authorized:
        await reject_unauthorized(socket)
        return
    room = room_for(room_id)
    room.clients.append(socket)
    room.client_can_control[id(socket)] = can_control
    if room.controller is None and can_control:
        room.controller = socket
    await send_text(socket, json.dumps({"type": "presence", "online": room.robot is not None}))
    await send_lease(socket, socket is room.controller)
    if room.last_state:
        await send_text(socket, room.last_state)
    try:
        while True:
            message = await socket.receive_text()
            try:
                parsed = json.loads(message)
            except (json.JSONDecodeError, AttributeError):
                parsed = {}
            message_type = parsed.get("type")
            action = parsed.get("action")
            may_control = socket is room.controller or (
                message_type == "command" and action == "stop"
            )
            if message_type in {"heartbeat", "command"} and not may_control:
                if message_type == "command":
                    await socket.send_text(
                        json.dumps(
                            {
                                "type": "command_status",
                                "commandId": parsed.get("commandId"),
                                "status": "rejected",
                                "detail": "control lease not held",
                            }
                        )
                    )
                continue
            if await send_text(room.robot, message):
                continue
            if message_type != "command":
                continue
            await socket.send_text(
                json.dumps(
                    {
                        "type": "command_status",
                        "commandId": parsed.get("commandId"),
                        "status": "failed",
                        "detail": "robot offline",
                    }
                )
            )
    except WebSocketDisconnect:
        pass
    finally:
        if socket in room.clients:
            room.clients.remove(socket)
        room.client_can_control.pop(id(socket), None)
        if room.controller is socket:
            room.controller = None
            await send_text(
                room.robot,
                json.dumps({"type": "control_lost", "reason": "controller_disconnected"}),
            )
            room.controller = next(
                (client for client in room.clients if room.client_can_control.get(id(client))),
                None,
            )
            await announce_leases(room)
        prune(room_id, room)


@app.websocket("/ws/robot/{room_id}/video")
async def robot_video_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    if not await authenticate_robot(socket):
        await reject_unauthorized(socket)
        return
    room = room_for(room_id)
    await replace(room, "robot_video", socket)
    try:
        while True:
            frame = await socket.receive_bytes()
            for client in list(room.client_videos):
                try:
                    await client.send_bytes(frame)
                except Exception:
                    if client in room.client_videos:
                        room.client_videos.remove(client)
    except WebSocketDisconnect:
        pass
    finally:
        if room.robot_video is socket:
            room.robot_video = None
        prune(room_id, room)


@app.websocket("/ws/client/{room_id}/video")
async def client_video_socket(socket: WebSocket, room_id: str) -> None:
    await socket.accept()
    authorized, _ = await client_access(socket)
    if not authorized:
        await reject_unauthorized(socket)
        return
    room = room_for(room_id)
    room.client_videos.append(socket)
    try:
        while True:
            # Browsers do not send video data; receiving keeps disconnect detection immediate.
            message = await socket.receive()
            if message["type"] == "websocket.disconnect":
                break
    except WebSocketDisconnect:
        pass
    finally:
        if socket in room.client_videos:
            room.client_videos.remove(socket)
        prune(room_id, room)

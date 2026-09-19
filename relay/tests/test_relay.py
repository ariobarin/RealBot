import json

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import app, rooms


def connect_client(client: TestClient, room: str):
    socket = client.websocket_connect(f"/ws/client/{room}")
    return socket


def test_control_presence_lease_and_forwarding() -> None:
    rooms.clear()
    with TestClient(app) as client:
        with connect_client(client, "demo") as browser:
            assert browser.receive_json() == {"type": "presence", "online": False}
            assert browser.receive_json() == {"type": "control_lease", "acquired": True}
            with client.websocket_connect("/ws/robot/demo") as robot:
                assert browser.receive_json() == {"type": "presence", "online": True}
                heartbeat = {"type": "heartbeat", "at": 123}
                browser.send_json(heartbeat)
                assert robot.receive_json() == heartbeat

                command = {
                    "type": "command",
                    "commandId": "cmd-1",
                    "action": "move_to_view",
                    "payload": {"u": 0.25, "v": 0.7, "coordinateSpace": "normalized_camera"},
                }
                browser.send_json(command)
                assert robot.receive_json() == command

                status = {
                    "type": "command_status",
                    "commandId": "cmd-1",
                    "status": "delivered",
                }
                robot.send_json(status)
                assert browser.receive_json() == status


def test_offline_command_fails_immediately() -> None:
    rooms.clear()
    with TestClient(app) as client:
        with connect_client(client, "offline") as browser:
            browser.receive_json()
            browser.receive_json()
            browser.send_text(json.dumps({"type": "command", "commandId": "missing"}))
            assert browser.receive_json() == {
                "type": "command_status",
                "commandId": "missing",
                "status": "failed",
                "detail": "robot offline",
            }


def test_video_frames_are_forwarded() -> None:
    rooms.clear()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/client/camera/video") as browser_video:
            with client.websocket_connect("/ws/robot/camera/video") as robot_video:
                robot_video.send_bytes(b"jpeg-frame")
                assert browser_video.receive_bytes() == b"jpeg-frame"


def test_multiple_viewers_receive_state_but_only_controller_commands() -> None:
    rooms.clear()
    with TestClient(app) as client:
        with connect_client(client, "shared") as controller:
            assert controller.receive_json() == {"type": "presence", "online": False}
            assert controller.receive_json() == {"type": "control_lease", "acquired": True}
            with connect_client(client, "shared") as viewer:
                assert viewer.receive_json() == {"type": "presence", "online": False}
                assert viewer.receive_json() == {"type": "control_lease", "acquired": False}
                with client.websocket_connect("/ws/robot/shared") as robot:
                    assert controller.receive_json() == {"type": "presence", "online": True}
                    assert viewer.receive_json() == {"type": "presence", "online": True}
                    state = {
                        "type": "robot_state",
                        "ready": True,
                        "status": "ready",
                        "pose": {"x": 0, "y": 0, "heading": 0},
                    }
                    robot.send_json(state)
                    assert controller.receive_json() == state
                    assert viewer.receive_json() == state

                    viewer.send_json(
                        {"type": "command", "commandId": "blocked", "action": "move_to"}
                    )
                    assert viewer.receive_json() == {
                        "type": "command_status",
                        "commandId": "blocked",
                        "status": "rejected",
                        "detail": "control lease not held",
                    }

                    stop = {"type": "command", "commandId": "stop-anywhere", "action": "stop"}
                    viewer.send_json(stop)
                    assert robot.receive_json() == stop


def test_controller_disconnect_notifies_robot_and_promotes_viewer() -> None:
    rooms.clear()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/robot/handoff") as robot:
            with connect_client(client, "handoff") as viewer:
                assert viewer.receive_json() == {"type": "presence", "online": True}
                assert viewer.receive_json() == {"type": "control_lease", "acquired": True}
                with connect_client(client, "handoff") as next_viewer:
                    assert next_viewer.receive_json() == {"type": "presence", "online": True}
                    assert next_viewer.receive_json() == {"type": "control_lease", "acquired": False}
                    viewer.close()
                    assert robot.receive_json() == {
                        "type": "control_lost",
                        "reason": "controller_disconnected",
                    }
                    assert next_viewer.receive_json() == {
                        "type": "control_lease",
                        "acquired": True,
                    }


def test_configured_tokens_separate_control_and_view_access(monkeypatch: pytest.MonkeyPatch) -> None:
    rooms.clear()
    monkeypatch.setenv("CONTROL_TOKEN", "control-secret")
    monkeypatch.setenv("VIEW_TOKEN", "view-secret")
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/client/secure") as unauthorized:
                unauthorized.send_json({"type": "auth", "token": "wrong"})
                unauthorized.receive_json()
        with client.websocket_connect("/ws/client/secure") as viewer:
            viewer.send_json({"type": "auth", "token": "view-secret"})
            assert viewer.receive_json() == {"type": "presence", "online": False}
            assert viewer.receive_json() == {"type": "control_lease", "acquired": False}
        with client.websocket_connect("/ws/client/secure") as controller:
            controller.send_json({"type": "auth", "token": "control-secret"})
            assert controller.receive_json() == {"type": "presence", "online": False}
            assert controller.receive_json() == {"type": "control_lease", "acquired": True}

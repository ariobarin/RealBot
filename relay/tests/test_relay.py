import json

from fastapi.testclient import TestClient

from app import app, rooms


def test_control_presence_and_forwarding() -> None:
    rooms.clear()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/client/demo") as browser:
            assert browser.receive_json() == {"type": "presence", "online": False}
            with client.websocket_connect("/ws/robot/demo") as robot:
                assert browser.receive_json() == {"type": "presence", "online": True}
                command = {
                    "type": "command",
                    "commandId": "cmd-1",
                    "action": "move_to",
                    "payload": {"x": 1, "y": 2},
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
        with client.websocket_connect("/ws/client/offline") as browser:
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


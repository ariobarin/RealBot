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


def test_multiple_viewers_receive_robot_state() -> None:
    rooms.clear()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/client/shared") as realtor:
            assert realtor.receive_json()["online"] is False
            with client.websocket_connect("/ws/client/shared") as user:
                assert user.receive_json()["online"] is False
                with client.websocket_connect("/ws/robot/shared") as robot:
                    assert realtor.receive_json() == {"type": "presence", "online": True}
                    assert user.receive_json() == {"type": "presence", "online": True}
                    state = {
                        "type": "robot_state",
                        "ready": True,
                        "status": "ready",
                        "pose": {"x": 0, "y": 0, "heading": 0},
                    }
                    robot.send_json(state)
                    assert realtor.receive_json() == state
                    assert user.receive_json() == state

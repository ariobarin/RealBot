import asyncio
import queue
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from edge_agent.demo_session import DemoConfig, serve, legacy_guard, head_frames


class Room:
    def __init__(self):
        self.handlers = {}
        self.events = []
        self.name = "demo"
        self.local_participant = SimpleNamespace(identity="robot")
        self.remote_participants = {}
    def on(self, event, fn): self.handlers[event] = fn
    def off(self, event, fn): self.handlers.pop(event, None)
    def emit(self, event, *args): self.handlers[event](*args)
    async def connect(self, url, token, options): self.events.append("connect")
    async def disconnect(self): self.events.append("disconnect")


def config(mode="camera"):
    return DemoConfig("wss://test.invalid", "demo", "robot", "controller", "test-token", int(time.time())+60, mode)


@pytest.mark.parametrize("event", ["participant_disconnected", "disconnected", "reconnecting", "shutdown", "failure", "guard"])
def test_camera_and_driving_start_together_and_stop_before_disconnect(event):
    async def scenario():
        room, shutdown = Room(), asyncio.Event()
        started = asyncio.Event()
        guard_ok = True
        async def camera(room):
            room.events.append("camera:cam-wrist")
            try:
                await started.wait()
                if event == "failure": raise RuntimeError("Camera failed")
                await asyncio.Event().wait()
            finally: room.events.append("camera_closed")
        async def telemetry(room): await asyncio.Event().wait()
        async def driving(room, **kwargs):
            assert kwargs == dict(controller_identity="controller", legacy_teleop_disabled=True)
            room.events.append("driving")
            started.set()
            try: await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                room.events.append("nav_stopped")
        def guard():
            if not guard_ok: raise RuntimeError("Legacy daemon enabled")
        task = asyncio.create_task(serve(config("drive"), room, options=None, camera=camera,
                    telemetry=telemetry, driving=driving, guard=guard, shutdown=shutdown, enable_driving=True))
        await asyncio.wait_for(started.wait(), 1)
        if event == "shutdown": shutdown.set()
        elif event == "guard": guard_ok = False
        elif event == "participant_disconnected": room.emit(event, SimpleNamespace(identity="controller"))
        elif event != "failure": room.emit(event)
        result = await asyncio.gather(asyncio.wait_for(task, 1), return_exceptions=True)
        if event in {"failure", "guard"}: assert isinstance(result[0], RuntimeError)
        else: assert result == [None]
        assert room.events.index("nav_stopped") < room.events.index("disconnect")
        assert room.events.index("camera_closed") < room.events.index("disconnect")
        assert not room.handlers
    asyncio.run(scenario())


def test_camera_only_never_installs_driving_and_expiry_ends_session():
    async def scenario():
        room = Room()
        async def camera(room):
            room.events.append("camera")
            await asyncio.Event().wait()
        async def telemetry(room): await asyncio.Event().wait()
        async def forbidden(*args, **kwargs): raise AssertionError("No driving in camera-only mode")
        cfg = config()
        with patch("edge_agent.demo_session.time.time", return_value=cfg.expires-.05):
            await asyncio.wait_for(serve(cfg, room, options=None, camera=camera, telemetry=telemetry,
                                   driving=forbidden, guard=lambda: None, shutdown=asyncio.Event(),
                                   enable_driving=False), 1)
        assert room.events == ["connect", "camera", "disconnect"]
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,enabled", [("camera", True), ("drive", False)])
def test_driving_needs_config_and_operator_opt_in(mode, enabled):
    async def scenario():
        room = Room()
        with pytest.raises(ValueError):
            await serve(config(mode), room, options=None, camera=None, telemetry=None, driving=None,
                        guard=lambda: None, shutdown=asyncio.Event(), enable_driving=enabled)
        assert not room.events
    asyncio.run(scenario())


def test_guard_requires_stopped_marker_and_no_remaining_process(tmp_path):
    daemon, proc = tmp_path / "remote_session", tmp_path / "proc"
    daemon.mkdir(); proc.mkdir()
    with pytest.raises(RuntimeError, match="explicitly stopped"): legacy_guard(daemon, proc)
    (daemon / ".stopped").touch()
    legacy_guard(daemon, proc)
    (proc / "123").mkdir()
    (proc / "123/cmdline").write_bytes(b"python\0" + str(daemon / "daemon.py").encode() + b"\0")
    with pytest.raises(RuntimeError, match="still running"): legacy_guard(daemon, proc)


def test_wrong_livekit_identity_never_starts_camera_or_driving():
    async def scenario():
        room = Room()
        room.local_participant.identity = "wrong"
        async def forbidden(*args, **kwargs): raise AssertionError("Must not start tasks")
        with pytest.raises(RuntimeError, match="identity"):
            await serve(config(), room, options=None, camera=forbidden, telemetry=forbidden,
                        driving=forbidden, guard=lambda: None, shutdown=asyncio.Event(), enable_driving=False)
        assert room.events == ["connect", "disconnect"]
        assert not room.handlers
    asyncio.run(scenario())


def test_robot_launcher_uses_cached_libraries_without_importing_cached_secrets(tmp_path):
    import importlib.util
    import json
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("robot_launcher", Path(__file__).parents[1] / "run_robot_demo.py")
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    daemon = tmp_path / "bbos/daemons/remote_session"
    (daemon / ".venv/bin").mkdir(parents=True)
    (daemon / ".devenv").mkdir()
    (daemon / ".venv/bin/python").touch()
    (daemon / ".devenv/bbos-env.json").write_text(json.dumps({"LD_LIBRARY_PATH": "/private-libs",
                                                            "UNRELATED_CACHED_SECRET": "do-not-copy"}))
    python, env = launcher.runtime(tmp_path)
    assert python == daemon / ".venv/bin/python"
    assert env["LD_LIBRARY_PATH"] == "/private-libs"
    assert "UNRELATED_CACHED_SECRET" not in env
    assert str(tmp_path) in env["PYTHONPATH"]


def test_head_reader_splits_real_topic_without_opening_hardware_writers():
    stop, frames = threading.Event(), queue.Queue(maxsize=1)
    class Reader:
        readable = True
        data = {"timestamp": time.time_ns(), "jpeg_len": 4, "jpeg": np.ones(8, dtype=np.uint8)}
        closed = False
        def __enter__(self): return self
        def __exit__(self, *args): self.closed = True
        def ready(self): return True
    reader = Reader()
    def factory(topic, **kwargs):
        assert topic == "camera.head.jpeg" and kwargs == {"keeptime": False}
        return reader
    def push(q, image): q.put_nowait(image); stop.set()
    rgb = np.arange(24, dtype=np.uint8).reshape(2,4,3)
    cv = SimpleNamespace(IMREAD_COLOR=1, COLOR_BGR2RGB=1, imdecode=lambda *a: rgb,
                         cvtColor=lambda img, _: img)
    camera = SimpleNamespace(width=4, height=2, split=lambda img: (img[:,:2], img[:,2:]))
    head_frames(frames, stop, push=push, reader_factory=factory, config=camera, cv=cv)
    assert np.array_equal(frames.get_nowait(), rgb[:,:2])
    assert reader.closed


def test_session_dispatches_capture_and_move_to_navigation_then_stops_on_leave(monkeypatch):
    from edge_agent.driving import run_driving
    from edge_agent.adapters import bbos_navigation, bbos_view
    from test_driving import Source, Navigation

    class View(Source):
        def __enter__(self): return self
        def __exit__(self, *args): pass
    nav = Navigation()
    monkeypatch.setattr(bbos_view, "BbosFloorView", View)
    monkeypatch.setattr(bbos_navigation, "BbosNavigationAdapter", lambda: nav)

    async def scenario():
        import json
        room, packets = Room(), []
        async def publish(raw, **kwargs):
            assert kwargs["destination_identities"] == ["controller"]
            packets.append(json.loads(raw))
        room.local_participant.publish_data = publish
        async def idle(room): await asyncio.Event().wait()
        task = asyncio.create_task(serve(config("drive"), room, options=None, camera=idle,
                    telemetry=idle, driving=run_driving, guard=lambda: None,
                    shutdown=asyncio.Event(), enable_driving=True))
        async def wait_for(predicate):
            async with asyncio.timeout(1):
                while not predicate(): await asyncio.sleep(.001)
        await wait_for(lambda: len(packets) > 0)
        session_id = packets[0]["sessionId"]
        def send(**fields):
            raw = json.dumps(dict(sessionId=session_id, expiresAt=int(time.time()*1000)+2000, **fields)).encode()
            room.emit("data_received", SimpleNamespace(topic="realbot.drive_command", data=raw,
                                                       participant=SimpleNamespace(identity="controller")))
        send(type="heartbeat", sequence=1)
        send(type="command", commandId="capture", action="capture")
        await wait_for(lambda: any(p.get("commandId") == "capture" and p.get("status") == "succeeded" for p in packets))
        await wait_for(lambda: any(p.get("type") == "state" and p.get("canCapture") for p in packets))
        send(type="command", commandId="move", action="move", captureId="capture-1", u=.5, v=.5)
        await asyncio.wait_for(nav.started.wait(), 1)
        room.emit("participant_disconnected", SimpleNamespace(identity="controller"))
        await asyncio.wait_for(task, 2)
        assert nav.starts == 1
        assert "session_closed" in nav.stops
        assert room.events[-1] == "disconnect"
    asyncio.run(scenario())

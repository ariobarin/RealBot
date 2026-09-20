"""Manual single-user session, replacing (never accompanying) legacy remote_session.

Reuses bbOS's installed LiveKit camera publisher. No polling of bb-cloud, no
arm/drive.ctrl writers, no automatic daemon startup. Default mode is camera-only.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import signal
import threading
import time

from .demo_tokens import validate_url


@dataclass(frozen=True)
class DemoConfig:
    url: str
    room: str
    robot: str
    controller: str
    token: str = field(repr=False)
    expires: int
    mode: str

    @classmethod
    def load(cls, path: Path):
        if path.stat().st_size > 16_384:
            raise ValueError("Session configuration is too large")
        if os.name == "posix" and path.stat().st_mode & 0o077:
            raise ValueError("Robot credential file must be private: chmod 600 <file>")
        return cls.parse(json.loads(path.read_text(encoding="utf-8")))

    @classmethod
    def parse(cls, data):
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("Unsupported robot session configuration")
        for key in ("url", "roomId", "robotIdentity", "controllerIdentity", "token"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                raise ValueError(f"Missing session field: {key}")
        validate_url(data["url"])
        if data.get("mode") not in {"camera", "drive"} or data["robotIdentity"] == data["controllerIdentity"]:
            raise ValueError("Invalid mode or participant identities")
        expires = data.get("expiresAt")
        if type(expires) is not int or not time.time() < expires <= time.time() + 3600:
            raise ValueError("Session must expire within the next hour")
        try:
            encoded = data["token"].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            grant = claims["video"]
            valid = (claims["sub"] == data["robotIdentity"] and grant["room"] == data["roomId"]
                     and grant.get("roomJoin") is True and grant.get("canPublish") is True
                     and grant.get("canPublishData") is True and grant.get("canSubscribe") is True
                     and grant.get("canPublishSources") == ["camera"]
                     and not any(grant.get(key) for key in ("roomAdmin", "roomCreate", "roomList", "roomRecord", "ingressAdmin"))
                     and claims["exp"] >= expires)
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Token does not match the restricted robot session")
        # Decoding checks operator mistakes only. LiveKit verifies the signature
        # during connect. No signing secret belongs on the robot.
        return cls(data["url"], data["roomId"], data["robotIdentity"], data["controllerIdentity"],
                   data["token"], expires, data["mode"])


def legacy_guard(daemon_dir: Path, proc_dir: Path = Path("/proc")):
    if not (daemon_dir / ".stopped").is_file():
        raise RuntimeError("Legacy remote_session must remain explicitly stopped")
    if not proc_dir.is_dir():
        raise RuntimeError("Cannot check for a competing legacy daemon")
    target = str(daemon_dir / "daemon.py").encode()
    for process in proc_dir.iterdir():
        if not process.name.isdigit():
            continue
        try:
            command = (process / "cmdline").read_bytes()
        except FileNotFoundError:
            continue
        if target in command or b"bbos.daemons.remote_session.daemon" in command:
            raise RuntimeError("Legacy remote_session process is still running")


@contextmanager
def single_session():
    import fcntl  # Robot is Linux; fail closed elsewhere.
    directory = Path.home() / ".local/state/realbot"
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd = os.open(directory / "demo.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another RealBot demo session is already running") from None
        yield
    finally:
        os.close(fd)


def head_frames(frames: queue.Queue, stop: threading.Event, *, push, reader_factory=None,
                config=None, cv=None):
    """Read only camera.head.jpeg; follow the installed daemon's left-eye split."""
    if reader_factory is None:
        from bbos import Reader, Config
        import cv2
        reader_factory, config, cv = Reader, Config("cam_head"), cv2
    reader = reader_factory("camera.head.jpeg", keeptime=False).__enter__()
    last_frame, last_timestamp = time.monotonic(), -1
    try:
        while not stop.is_set():
            if reader.ready() and reader.readable and reader.data is not None:
                data = reader.data
                value = data["timestamp"]
                timestamp = int(value.astype("int64")) if hasattr(value, "astype") else int(value)
                length = int(data["jpeg_len"])
                if (timestamp > last_timestamp and 0 <= time.time_ns()-timestamp <= 500_000_000
                        and 0 < length <= len(data["jpeg"])):
                    bgr = cv.imdecode(data["jpeg"][:length].copy(), cv.IMREAD_COLOR)
                    if bgr is not None and bgr.shape[:2] == (config.height, config.width):
                        rgb = cv.cvtColor(bgr, cv.COLOR_BGR2RGB)
                        left, _ = config.split(rgb)
                        push(frames, left.copy())
                        last_frame, last_timestamp = time.monotonic(), timestamp
            if time.monotonic() - last_frame > 3:
                raise RuntimeError("Head camera is absent or stale; ending session")
            stop.wait(.01)
    finally:
        # bbOS Reader.__exit__ suppresses exceptions when passed exc_info.
        # Preserve failures so the session supervisor can stop navigation.
        reader.__exit__(None, None, None)


async def serve(config: DemoConfig, room, *, options, camera, telemetry, driving,
                guard, shutdown: asyncio.Event, enable_driving: bool):
    """Testable lifecycle: all task cleanup completes before room disconnect."""
    if (config.mode == "drive") != enable_driving:
        raise ValueError("Driving requires both a drive credential file and --enable-driving")
    guard()
    ending = asyncio.Event()
    def disconnected(*_): ending.set()
    def participant_left(participant):
        if participant.identity == config.controller:
            ending.set()
    room.on("participant_disconnected", participant_left)
    room.on("disconnected", disconnected)
    room.on("reconnecting", disconnected)  # Never resume movement after reconnection.
    tasks = []

    async def monitor():
        deadline = time.monotonic() + max(0, config.expires - time.time())
        while not ending.is_set() and not shutdown.is_set():
            guard()
            if time.monotonic() >= deadline or time.time() >= config.expires:
                return
            await asyncio.sleep(.1)

    try:
        await asyncio.wait_for(room.connect(config.url, config.token, options=options), 15)
        if room.name != config.room or room.local_participant.identity != config.robot:
            raise RuntimeError("Connected room or robot identity does not match configuration")
        if ending.is_set() or shutdown.is_set() or time.time() >= config.expires:
            return
        guard()
        tasks = [asyncio.create_task(camera(room)), asyncio.create_task(telemetry(room)),
                 asyncio.create_task(monitor())]
        if enable_driving:
            tasks.append(asyncio.create_task(driving(room, controller_identity=config.controller,
                                                     legacy_teleop_disabled=True)))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        # Cancel in parallel but await Stop/writer cleanup before leaving LiveKit.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        room.off("participant_disconnected", participant_left)
        room.off("disconnected", disconnected)
        room.off("reconnecting", disconnected)
        await asyncio.wait_for(room.disconnect(), 5)


async def camera_task(room, media):
    frames = queue.Queue(maxsize=1)
    stop = threading.Event()
    reader = asyncio.create_task(asyncio.to_thread(head_frames, frames, stop, push=media.push_queue))
    publisher = asyncio.create_task(media.publish_feed(room, frames, "cam-wrist"))
    try:
        done, _ = await asyncio.wait([reader, publisher], return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        stop.set()
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
        await asyncio.wait_for(asyncio.shield(reader), 5)


async def run(config, enable_driving):
    from livekit import rtc
    # Import only transport helpers, NOT daemon.py or its actuator-owning thread.
    from bbos.daemons.remote_session import session as media
    from .telemetry import publish_telemetry
    from .driving import run_driving

    daemon_dir = Path(media.__file__).parent
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for sig in signals:
        loop.add_signal_handler(sig, shutdown.set)
    try:
        with single_session():
            await serve(config, rtc.Room(), options=rtc.RoomOptions(auto_subscribe=False),
                        camera=lambda room: camera_task(room, media), telemetry=publish_telemetry,
                        driving=run_driving, guard=lambda: legacy_guard(daemon_dir),
                        shutdown=shutdown, enable_driving=enable_driving)
    finally:
        for sig in signals:
            loop.remove_signal_handler(sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Private robot.session.json")
    parser.add_argument("--enable-driving", action="store_true", help="Requires supervised motion clearance")
    parser.add_argument("--check", action="store_true", help="Check imports and legacy-daemon guard; no connection or writers")
    args = parser.parse_args()
    try:
        if args.check:
            from bbos.daemons.remote_session import session as media
            if args.enable_driving:
                from .adapters.bbos_view import BbosFloorView  # dependencies only; no readers opened
                import yaml
            legacy_guard(Path(media.__file__).parent)
            print("Requested runtime dependencies import successfully; legacy daemon is stopped.")
            print("No LiveKit room joined. No sensor readers, hardware writers, or services started.")
            return
        if args.config is None:
            parser.error("--config is required unless using --check")
        config = DemoConfig.load(args.config)
        asyncio.run(run(config, args.enable_driving))
    except KeyboardInterrupt:
        pass
    except Exception:
        # SDK connection exceptions can contain credentials. Do not log them.
        parser.exit(1, "Session ended or failed. Check private config, stopped legacy daemon, camera freshness and installed dependencies. No automatic restart.\n")


if __name__ == "__main__":
    main()

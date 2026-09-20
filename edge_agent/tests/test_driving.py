import asyncio
import json
import time
from types import SimpleNamespace

from edge_agent.driving import DrivingSession


class Source:
    healthy = True
    def ready(self): return self.healthy
    async def capture(self):
        return SimpleNamespace(capture_id="capture-1", image=b"jpeg")
    def resolve(self, capture, u, v):
        assert capture.capture_id == "capture-1"
        if u == 0: raise ValueError("Not floor")
        return {"x": 1., "y": 2.}


class Navigation:
    def __init__(self):
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.starts = 0
        self.stops = []
    async def start(self, command_id, payload):
        self.starts += 1
        self.started.set()
    async def wait(self): await self.finish.wait()
    async def stop(self, reason): self.stops.append(reason)


def setup():
    packets = []
    async def publish(packet): packets.append(packet)
    source, nav = Source(), Navigation()
    session = DrivingSession(controller_identity="operator", source=source, navigation=nav, publish=publish)
    return session, source, nav, packets


def send(s, action, id="command", sender="operator", **extra):
    message = {"sessionId": s.session_id, "expiresAt": int(time.time()*1000)+2000,
               "type": "command", "commandId": id, "action": action, **extra}
    s.receive(sender, json.dumps(message).encode())


async def beat_and_capture(s):
    send(s, "", type="heartbeat", sequence=1)
    send(s, "capture", id="capture-request")
    await s.task


def test_capture_move_duplicate_and_arrival():
    async def scenario():
        s, _, nav, packets = setup()
        await beat_and_capture(s)
        assert any(p["type"] == "capture" for p in packets)
        send(s, "move", captureId="capture-1", u=.5, v=.5)
        await nav.started.wait()
        send(s, "move", captureId="capture-1", u=.5, v=.5)
        await asyncio.sleep(0)
        nav.finish.set()
        await s.task
        assert nav.starts == 1
        assert s.results["command"]["status"] == "succeeded"
        assert "arrived" in nav.stops
        await s.close()
    asyncio.run(scenario())


def test_stop_interrupts_route_and_new_route_requires_capture():
    async def scenario():
        s, _, nav, _ = setup()
        await beat_and_capture(s)
        send(s, "move", captureId="capture-1", u=.5, v=.5)
        await nav.started.wait()
        send(s, "stop", id="stop")
        await s.stop_task
        assert s.results["command"]["status"] == "stopped"
        assert s.results["stop"]["status"] == "succeeded"
        assert "operator_stop" in nav.stops
        send(s, "move", id="new", captureId="capture-1", u=.5, v=.5)
        await s.task
        assert s.results["new"]["status"] == "failed"
        assert nav.starts == 1
        await s.close()
    asyncio.run(scenario())


def test_unauthorized_expired_and_wrong_session_do_not_start_work():
    async def scenario():
        s, _, nav, _ = setup()
        send(s, "capture", sender="viewer")
        send(s, "capture", sessionId="old-session")
        send(s, "capture", expiresAt=0)
        assert s.task is None
        send(s, "capture")  # Missing heartbeat.
        await asyncio.sleep(.01)
        assert s.results["command"]["status"] == "rejected"
        assert nav.starts == 0
        await s.close()
    asyncio.run(scenario())


def test_route_failure_releases_writer():
    async def scenario():
        s, _, nav, _ = setup()
        async def failure(): raise RuntimeError("No route")
        nav.wait = failure
        await beat_and_capture(s)
        send(s, "move", captureId="capture-1", u=.5, v=.5)
        await s.task
        assert s.results["command"]["status"] == "failed"
        assert "driving_failed" in nav.stops
        await s.close()
    asyncio.run(scenario())


def test_localization_loss_and_expired_heartbeat_stop_motion():
    async def scenario(localization):
        s, source, nav, _ = setup()
        await beat_and_capture(s)
        send(s, "move", captureId="capture-1", u=.5, v=.5)
        await nav.started.wait()
        if localization: source.healthy = False
        else: s.last_heartbeat = time.monotonic()-4
        runner = asyncio.create_task(s.run())
        await asyncio.sleep(.03)
        assert "controller_or_localization_lost" in nav.stops
        assert s.capture is None
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
        assert s.closed
    asyncio.run(scenario(True))
    asyncio.run(scenario(False))


def test_session_exits_even_if_transport_swallows_cancellation():
    async def scenario():
        s, _, _, _ = setup()
        publishing = asyncio.Event()
        async def send(message):
            publishing.set()
            try: await asyncio.Event().wait()
            except asyncio.CancelledError: pass  # Emulate a wait_for completion race.
        s.send = send
        runner = asyncio.create_task(s.run())
        await publishing.wait()
        runner.cancel()
        async with asyncio.timeout(1):
            await asyncio.gather(runner, return_exceptions=True)
        assert s.closed
    asyncio.run(scenario())

"""Bounded visitor driving protocol for the existing bbOS LiveKit participant.

The host supplies the authorized controller identity from its trusted session
record and must disable legacy teleoperation for the entire driving session.
"""
from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
import json
import time
import uuid

from .motion import MotionCoordinator, MotionInterrupted, MotionMode

TOPIC = "realbot.driving"
COMMAND_TOPIC = "realbot.drive_command"


class DrivingSession:
    def __init__(self, *, controller_identity, source, navigation, publish, preview_only=False):
        if not controller_identity:
            raise ValueError("A trusted controller identity is required")
        self.controller = controller_identity
        self.source = source
        self.preview_only = preview_only
        self.motion = MotionCoordinator({} if preview_only else {MotionMode.NAVIGATING: navigation})
        self.publish = publish
        self.session_id = uuid.uuid4().hex
        self.capture = None
        self.task = None
        self.stop_task = None
        self.last_heartbeat = None
        self.heartbeat_sequence = -1
        self.results = OrderedDict()
        self.closed = False
        self._background = set()

    @property
    def lease_active(self):
        return self.last_heartbeat is not None and time.monotonic() - self.last_heartbeat < 3

    @property
    def busy(self):
        return any(task is not None and not task.done() for task in (self.task, self.stop_task))

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._background.add(task)
        def finished(t):
            self._background.discard(t)
            if not t.cancelled():
                t.exception()  # run()/watchdog owns transport-loss cleanup.
        task.add_done_callback(finished)
        return task

    async def send(self, message):
        await asyncio.wait_for(self.publish({"version": 1, "sessionId": self.session_id,
                                            "at": int(time.time()*1000), **message}), 1)

    async def status(self, command_id, status, detail=""):
        message = {"type": "result", "commandId": command_id, "status": status, "detail": detail[:300]}
        self.results[command_id] = message
        while len(self.results) > 128:
            self.results.popitem(last=False)
        await self.send(message)

    def receive(self, sender: str, raw: bytes):
        """Called by data_received. Reserve work before yielding; never queue motion."""
        if self.closed or sender != self.controller or len(raw) > 2048:
            return
        try:
            message = json.loads(raw)
            if not isinstance(message, dict) or message.get("sessionId") != self.session_id:
                return
            expires = message.get("expiresAt")
            now = int(time.time()*1000)
            if type(expires) is not int or not now < expires <= now + 5_000:
                return
            if message.get("type") == "heartbeat":
                sequence = message.get("sequence")
                if type(sequence) is int and self.heartbeat_sequence < sequence <= 2**53 - 1:
                    self.heartbeat_sequence = sequence
                    self.last_heartbeat = time.monotonic()
                return
            command_id = message.get("commandId")
            action = message.get("action")
            if not isinstance(command_id, str) or not 1 <= len(command_id) <= 128 or action not in {"move_to_view", "capture", "move", "preview", "stop"}:
                return
            # Bound response work as well as physical operations under packet floods.
            if len(self._background) >= 8 and action != "stop":
                return
            if command_id in self.results:
                if len(self._background) < 8:
                    self._spawn(self.send(self.results[command_id]))
                return
            if action == "stop":
                if self.stop_task is None or self.stop_task.done():
                    self.results[command_id] = {"type": "result", "commandId": command_id, "status": "accepted", "detail": ""}
                    self.capture = None
                    self.stop_task = self._spawn(self._stop_command(command_id))
                return
            if not self.lease_active or self.busy or self.motion.mode != MotionMode.IDLE:
                self._spawn(self.status(command_id, "rejected", "Control unavailable or robot busy. Stop or wait before trying again."))
                return
            self.results[command_id] = {"type": "result", "commandId": command_id, "status": "accepted", "detail": ""}
            self.task = self._spawn(self._execute(command_id, action, message))
        except (ValueError, TypeError, OverflowError):
            return

    async def _execute(self, command_id, action, message):
        try:
            await self.status(command_id, "accepted")
            if action == "move" and self.preview_only:
                raise ValueError("Preview-only session: movement is disabled.")
            if action == "preview" and not self.preview_only:
                raise ValueError("Preview requires a preview-only session.")
            if not self.source.ready() or not self.lease_active:
                raise ValueError("Robot localization, sensors, or controller are unavailable.")
            if action == "move_to_view":
                if message.get("coordinateSpace") != "normalized_camera":
                    raise ValueError("Unsupported camera coordinate space.")
                capture = await self.source.capture()
                if not self.lease_active or not self.source.ready():
                    raise ValueError("Click interrupted. Wait for the robot to become ready and try again.")
                target = self.source.resolve(capture, message.get("u"), message.get("v"))
                if not self.lease_active:
                    raise ValueError("Controller heartbeat expired.")
                if self.preview_only:
                    await self.status(command_id, "succeeded",
                                      f"Preview accepted: map x={target['x']:.2f} m, y={target['y']:.2f} m. No movement sent.")
                    return
                await self.motion.transition(MotionMode.NAVIGATING, command_id, target)
                await self.status(command_id, "moving")
                await self.motion.wait_for_completion(MotionMode.NAVIGATING, command_id)
                await self.motion.complete(MotionMode.NAVIGATING, command_id, "arrived")
                await self.status(command_id, "succeeded", "Arrived.")
            elif action == "capture":
                self.capture = None
                capture = await self.source.capture()
                if not self.lease_active or not self.source.ready():
                    raise ValueError("Capture interrupted. Reconnect and choose a fresh view.")
                self.capture = capture
                await self.send({"type": "capture", "commandId": command_id,
                                 "captureId": capture.capture_id, "validForMs": 10_000,
                                 "image": base64.b64encode(capture.image).decode()})
                await self.status(command_id, "succeeded", "Click a clear floor location in the captured view.")
            else:
                capture = self.capture
                if capture is None or message.get("captureId") != capture.capture_id:
                    raise ValueError("Choose a fresh destination view first.")
                self.capture = None  # A capture is one-use, even if the target is rejected.
                target = self.source.resolve(capture, message.get("u"), message.get("v"))
                if not self.lease_active:
                    raise ValueError("Controller heartbeat expired.")
                if self.preview_only:
                    await self.status(command_id, "succeeded",
                                      f"Preview accepted: map x={target['x']:.2f} m, y={target['y']:.2f} m. No movement sent.")
                    return
                await self.motion.transition(MotionMode.NAVIGATING, command_id, target)
                await self.status(command_id, "moving")
                await self.motion.wait_for_completion(MotionMode.NAVIGATING, command_id)
                await self.motion.complete(MotionMode.NAVIGATING, command_id, "arrived")
                await self.status(command_id, "succeeded", "Arrived.")
        except asyncio.CancelledError:
            await self.status(command_id, "stopped", "Operation stopped.")
            raise
        except MotionInterrupted:
            # Stop can win while adapter.start is completing. The coordinator
            # has already revoked ownership; this is not a navigation failure.
            await self.status(command_id, "stopped", "Operation stopped.")
        except Exception as exc:
            await self.motion.stop_all("driving_failed")
            await self.status(command_id, "failed", str(exc))

    async def stop(self, reason):
        self.capture = None
        # Cancel pending capture/start before releasing writers. No new work is
        # accepted while task/stop_task is pending.
        task = self.task
        if task is not None and not task.done():
            task.cancel()
        try:
            await self.motion.stop_all(reason)
        finally:
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)

    async def _stop_command(self, command_id):
        try:
            await self.stop("operator_stop")
            await self.status(command_id, "succeeded", "Stopped.")
        except Exception as exc:
            await self.status(command_id, "failed", str(exc))

    async def run(self):
        try:
            while not self.closed:
                # Python 3.11 wait_for can race with a completed publish during
                # cancellation. Never let a consumed cancellation resume control.
                if asyncio.current_task().cancelling():
                    raise asyncio.CancelledError
                ready = self.source.ready()
                if (not self.lease_active or not ready) and (self.busy or self.capture is not None):
                    await self.stop("controller_or_localization_lost")
                await self.send({"type": "state", "ready": ready, "lease": self.lease_active,
                                 "previewOnly": self.preview_only,
                                 "busy": self.busy, "fault": self.motion.fault,
                                 "canClick": ready and self.lease_active and not self.busy and self.motion.mode == MotionMode.IDLE,
                                 "canCapture": ready and self.lease_active and not self.busy and self.motion.mode == MotionMode.IDLE})
                await asyncio.sleep(.2)
        finally:
            await self.close()

    async def close(self):
        self.closed = True
        self.last_heartbeat = None
        await self.stop("session_closed")
        for task in list(self._background):
            task.cancel()
        await asyncio.gather(*list(self._background), return_exceptions=True)


async def run_driving(room, *, controller_identity: str, legacy_teleop_disabled: bool, preview_only: bool = False):
    """Host hook; controller identity comes from trusted server/operator config.

    Call only in a dedicated driving session whose bbos_thread does not acquire
    drive/arm writers and whose movement/manipulation/quest handlers are disabled.
    This flag is an explicit integration precondition, not automatic arbitration.
    """
    if not legacy_teleop_disabled:
        raise RuntimeError("Release legacy teleop ownership before enabling visitor driving")
    from .adapters.bbos_view import BbosFloorView
    navigation = None
    if not preview_only:
        from .adapters.bbos_navigation import BbosNavigationAdapter
        navigation = BbosNavigationAdapter()

    async def publish(message):
        payload = json.dumps(message, allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > 15_000:
            raise ValueError("Driving packet exceeds LiveKit data size limit")
        await room.local_participant.publish_data(payload, reliable=True, topic=TOPIC,
                                                  destination_identities=[controller_identity])

    with BbosFloorView() as source:
        session = DrivingSession(controller_identity=controller_identity, source=source,
                                 navigation=navigation, publish=publish, preview_only=preview_only)
        def on_data(packet):
            if packet.topic == COMMAND_TOPIC and packet.participant is not None:
                session.receive(packet.participant.identity, packet.data)
        room.on("data_received", on_data)
        try:
            await session.run()
        finally:
            room.off("data_received", on_data)

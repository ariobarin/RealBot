"""LiveKit transport for the existing annotated camera, map, and keyboard endpoint."""
import argparse
import asyncio
import gzip
import json
from pathlib import Path
import queue
import signal
import time

import cv2
import httpx
import numpy as np
from websockets.asyncio.client import connect

from edge_agent.demo_session import DemoConfig, legacy_guard, single_session
from visitor_freecam import FreeCamRelay
from visitor_actions import ActionRelay


class KeyboardRelay:
    def __init__(self, controller):
        self.controller = controller
        self.lock = asyncio.Lock()
        self.nonce = ''
        self.sequence = -1
        self.task = None
        self.commands = asyncio.Queue(maxsize=1)

    async def start(self, caller, payload):
        if caller != self.controller:
            raise ValueError('Unauthorized controller')
        message = json.loads(payload)
        nonce = message.get('nonce')
        if not isinstance(nonce, str) or not 1 <= len(nonce) <= 64:
            raise ValueError('Invalid drive session')
        async with self.lock:
            if self.task and not self.task.done():
                raise ValueError('Drive already enabled')
            socket = await connect('ws://127.0.0.1:8006/drive', origin='http://127.0.0.1:5178')
            try:
                if json.loads(await asyncio.wait_for(socket.recv(), 1)) != {'ready': True}:
                    raise ValueError('Drive unavailable')
            except BaseException:
                await socket.close()
                raise
            self.nonce, self.sequence = nonce, -1
            self.commands = asyncio.Queue(maxsize=1)
            self.task = asyncio.create_task(self.forward(socket, nonce))
            return json.dumps({'ready': True, 'nonce': nonce, 'at': int(time.time() * 1000)})

    async def forward(self, socket, nonce):
        try:
            async with socket:
                while True:
                    command = await asyncio.wait_for(self.commands.get(), .25)
                    await socket.send(json.dumps(command))
        except Exception:
            pass  # Closing the local socket stops and releases drive.ctrl.
        finally:
            if self.nonce == nonce:
                self.nonce = ''

    def receive(self, caller, payload):
        if caller != self.controller or len(payload) > 512 or not self.nonce:
            return
        try:
            message = json.loads(payload)
            if not isinstance(message, dict):
                return
            sequence, expiry = message['sequence'], message['expiresAt']
            if (message.get('nonce') != self.nonce or type(sequence) is not int
                    or sequence <= self.sequence or type(expiry) is not int
                    or not 0 < expiry - time.time() * 1000 <= 750):
                return
            self.sequence = sequence
            if self.commands.full():
                self.commands.get_nowait()
            self.commands.put_nowait({'keys': message['keys'], 'shift': message['shift']})
        except (ValueError, KeyError, TypeError):
            return

    async def stop(self, nonce=None):
        async with self.lock:
            if nonce is not None and nonce != self.nonce:
                return
            self.nonce = ''
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
                self.task = None


async def camera(room, http, media, relay, freecam=None, actions=None, controller=None):
    frames = queue.Queue(maxsize=1)
    async def read():
        size = None
        while True:
            if freecam and freecam.nonce:
                response = await freecam.http.get('/camera')
                response.raise_for_status()
                frame = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
                if size is None:
                    size = (frame.shape[1], frame.shape[0])
                media.push_queue(frames, cv2.cvtColor(cv2.resize(frame, size), cv2.COLOR_BGR2RGB))
                await asyncio.sleep(.1)
                continue
            health = (await http.get('/health')).json()
            if (health.get('status') != 'running'
                    or not 0 <= time.time_ns() - health.get('source_timestamp_ns', 0) < 1_000_000_000):
                await relay.stop()
                if actions:
                    actions.stop()
                if time.time_ns() - health.get('source_timestamp_ns', 0) > 5_000_000_000:
                    raise RuntimeError('Camera stale')
                await asyncio.sleep(.1)
                continue
            if actions:
                await room.local_participant.publish_data(json.dumps({
                    'points': health.get('visible_actions', []), 'state': actions.snapshot(),
                }), reliable=False, topic='realbot.actions', destination_identities=[controller])
            response = await http.get('/frame.jpg')
            response.raise_for_status()
            frame = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
            left = frame[:, :frame.shape[1] // 2]
            if size is None:
                size = (left.shape[1], left.shape[0])
            media.push_queue(frames, cv2.cvtColor(cv2.resize(left, size), cv2.COLOR_BGR2RGB))
            await asyncio.sleep(.1)
    tasks = [asyncio.create_task(read()), asyncio.create_task(media.publish_feed(room, frames, 'cam-wrist'))]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def maps(room, http, controller):
    while True:
        if controller in room.remote_participants:
            response = await http.get('/slam-map')
            if response.is_success:
                payload = await asyncio.to_thread(gzip.compress, response.content)
                writer = await room.local_participant.stream_bytes(
                    'slam-map.json.gz', topic='realbot.slam_map', total_size=len(payload),
                    mime_type='application/gzip', destination_identities=[controller])
                await writer.write(payload)
                await writer.aclose()
        await asyncio.sleep(1)


async def run(config, freecam_path=None):
    from livekit import rtc
    from bbos.daemons.remote_session import session as media

    daemon = Path(media.__file__).parent
    legacy_guard(daemon)
    relay = KeyboardRelay(config.controller)
    freecam = FreeCamRelay.load(config.controller, freecam_path) if freecam_path else None
    actions = ActionRelay(config.controller)
    actions.enabled = actions.enabled and config.mode == 'drive'
    control_lock = asyncio.Lock()
    room = rtc.Room()
    shutdown = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, shutdown.set)
    room.on('disconnected', lambda *_: shutdown.set())
    room.on('reconnecting', lambda *_: shutdown.set())
    async def stop_controls():
        actions.stop()
        await relay.stop()
        if freecam:
            try:
                await freecam.stop()
            except (httpx.HTTPError, ValueError):
                pass  # The local controller independently expires its heartbeat.

    room.on('participant_disconnected', lambda participant:
            asyncio.create_task(stop_controls()) if participant.identity == config.controller else None)

    def received(packet):
        if not packet.participant:
            return
        if packet.topic == 'realbot.keyboard':
            relay.receive(packet.participant.identity, packet.data)
        elif packet.topic == 'realbot.freecam' and freecam:
            freecam.receive(packet.participant.identity, packet.data)
        elif packet.topic == 'realbot.action_pulse':
            actions.receive(packet.participant.identity, packet.data)
    room.on('data_received', received)

    async def start(data):
        if config.mode != 'drive':
            raise ValueError('Driving is disabled for this session')
        try:
            async with control_lock:
                if actions.owned:
                    raise ValueError('Finish the arm policy session before driving')
                if freecam and (freecam.nonce or freecam.state.get('phase') != 'idle'):
                    raise ValueError('Release Free Cam control before driving')
                return await relay.start(data.caller_identity, data.payload)
        except Exception as error:
            print('Drive handshake:', type(error).__name__, str(error)[:200], flush=True)
            raise

    async def stop(data):
        if data.caller_identity != config.controller:
            raise ValueError('Unauthorized controller')
        await relay.stop(json.loads(data.payload).get('nonce'))
        return '{}'

    async def freecam_command(data):
        if not freecam or config.mode != 'drive':
            raise ValueError('Free Cam unavailable for this session')
        async with control_lock:
            if actions.owned:
                raise ValueError('Finish the arm policy session before Free Cam')
            return await freecam.command(data.caller_identity, data.payload, relay)

    async def action_command(data):
        try:
            async with control_lock:
                message = actions.validate(data.caller_identity, data.payload)
                async with httpx.AsyncClient(base_url='http://127.0.0.1:8006', timeout=.5) as http:
                    health = (await http.get('/health')).json() if message.get('action') == 'start' else {}
                await actions.command(data.caller_identity, data.payload, health, relay, freecam)
                return json.dumps(actions.snapshot())
        except ValueError as error:
            raise rtc.RpcError(2001, str(error)) from None

    async def state():
        while not shutdown.is_set() and time.time() < config.expires:
            await actions.tick()
            legacy_guard(daemon)
            await room.local_participant.publish_data(
                json.dumps({'at': int(time.time() * 1000), 'nonce': relay.nonce}),
                reliable=False, topic='realbot.keyboard_state', destination_identities=[config.controller])
            if freecam:
                await room.local_participant.publish_data(
                    json.dumps(await freecam.tick()), reliable=False, topic='realbot.freecam_state',
                    destination_identities=[config.controller])
            await asyncio.sleep(.1)

    tasks = []
    with single_session():
        try:
            await room.connect(config.url, config.token, options=rtc.RoomOptions(auto_subscribe=False))
            room.local_participant.register_rpc_method('realbot.keyboard.start', start)
            room.local_participant.register_rpc_method('realbot.keyboard.stop', stop)
            room.local_participant.register_rpc_method('realbot.freecam.command', freecam_command)
            room.local_participant.register_rpc_method('realbot.action.command', action_command)
            print('LiveKit connected; publishing annotated left camera and map', flush=True)
            async with httpx.AsyncClient(base_url='http://127.0.0.1:8006', timeout=5) as http:
                tasks = [asyncio.create_task(camera(room, http, media, relay, freecam, actions, config.controller)),
                         asyncio.create_task(maps(room, http, config.controller)),
                         asyncio.create_task(state()), asyncio.create_task(shutdown.wait())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            await stop_controls()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await room.disconnect()
            if freecam:
                await freecam.http.aclose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--free-cam', type=Path, help='Private local Free Cam connection JSON')
    args = parser.parse_args()
    try:
        asyncio.run(run(DemoConfig.load(args.config), args.free_cam))
    except Exception as error:
        print(f'LiveKit session ended: {type(error).__name__}', flush=True)
        raise SystemExit(1) from None

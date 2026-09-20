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


async def camera(room, http, media, relay):
    frames = queue.Queue(maxsize=1)
    async def read():
        while True:
            health = (await http.get('/health')).json()
            if (health.get('status') != 'running'
                    or not 0 <= time.time_ns() - health.get('source_timestamp_ns', 0) < 1_000_000_000):
                await relay.stop()
                if time.time_ns() - health.get('source_timestamp_ns', 0) > 5_000_000_000:
                    raise RuntimeError('Camera stale')
                await asyncio.sleep(.1)
                continue
            response = await http.get('/frame.jpg')
            response.raise_for_status()
            frame = cv2.imdecode(np.frombuffer(response.content, np.uint8), cv2.IMREAD_COLOR)
            left = frame[:, :frame.shape[1] // 2]
            media.push_queue(frames, cv2.cvtColor(left, cv2.COLOR_BGR2RGB))
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


async def run(config):
    from livekit import rtc
    from bbos.daemons.remote_session import session as media

    daemon = Path(media.__file__).parent
    legacy_guard(daemon)
    relay = KeyboardRelay(config.controller)
    room = rtc.Room()
    shutdown = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, shutdown.set)
    room.on('disconnected', lambda *_: shutdown.set())
    room.on('reconnecting', lambda *_: shutdown.set())
    room.on('participant_disconnected', lambda participant:
            asyncio.create_task(relay.stop()) if participant.identity == config.controller else None)
    room.on('data_received', lambda packet:
            relay.receive(packet.participant.identity, packet.data)
            if packet.topic == 'realbot.keyboard' and packet.participant else None)

    async def start(data):
        if config.mode != 'drive':
            raise ValueError('Driving is disabled for this session')
        try:
            return await relay.start(data.caller_identity, data.payload)
        except Exception as error:
            print('Drive handshake:', type(error).__name__, str(error)[:200], flush=True)
            raise

    async def stop(data):
        if data.caller_identity != config.controller:
            raise ValueError('Unauthorized controller')
        await relay.stop(json.loads(data.payload).get('nonce'))
        return '{}'

    async def state():
        while not shutdown.is_set() and time.time() < config.expires:
            legacy_guard(daemon)
            await room.local_participant.publish_data(
                json.dumps({'at': int(time.time() * 1000), 'nonce': relay.nonce}),
                reliable=False, topic='realbot.keyboard_state', destination_identities=[config.controller])
            await asyncio.sleep(.1)

    tasks = []
    with single_session():
        try:
            await room.connect(config.url, config.token, options=rtc.RoomOptions(auto_subscribe=False))
            room.local_participant.register_rpc_method('realbot.keyboard.start', start)
            room.local_participant.register_rpc_method('realbot.keyboard.stop', stop)
            print('LiveKit connected; publishing annotated left camera and map', flush=True)
            async with httpx.AsyncClient(base_url='http://127.0.0.1:8006', timeout=5) as http:
                tasks = [asyncio.create_task(camera(room, http, media, relay)),
                         asyncio.create_task(maps(room, http, config.controller)),
                         asyncio.create_task(state()), asyncio.create_task(shutdown.wait())]
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
        finally:
            await relay.stop()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await room.disconnect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        asyncio.run(run(DemoConfig.load(args.config)))
    except Exception as error:
        print(f'LiveKit session ended: {type(error).__name__}', flush=True)
        raise SystemExit(1) from None

"""Room-scoped transport for the existing local right-arm controller."""
import asyncio
import json
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit

import httpx


class FreeCamRelay:
    def __init__(self, controller, http):
        self.controller, self.http = controller, http
        self.lock = asyncio.Lock()
        self.nonce = self.session = ''
        self.sequence = self.motor_sequence = -1
        self.pending = None
        self.beat = 0
        self.owned = False
        self.direction = (0, 0)
        self.state = {'available': False, 'phase': 'idle'}

    @classmethod
    def load(cls, controller, path: Path):
        if path.stat().st_mode & 0o077:
            raise ValueError('Free Cam connection file must be private')
        data = json.loads(path.read_text())
        url = urlsplit(data['url'])
        if (data['robot'] != socket.gethostname() or url.scheme != 'http'
                or url.netloc != '127.0.0.1:8012' or url.path != '/' or not url.fragment):
            raise ValueError('Free Cam must target this robot on loopback')
        return cls(controller, httpx.AsyncClient(base_url='http://127.0.0.1:8012', timeout=.4,
                                                headers={'Authorization': 'Bearer ' + url.fragment}))

    def validate(self, caller, payload):
        if caller != self.controller or len(payload) > 1024:
            raise ValueError('Unauthorized Free Cam command')
        message = json.loads(payload)
        expiry = message.get('expiresAt')
        if type(expiry) is not int or not 0 < expiry - time.time() * 1000 <= 750:
            raise ValueError('Free Cam command expired')
        return message

    async def post(self, action, **values):
        self.motor_sequence += 1
        response = await self.http.post('/command', json=dict(
            action=action, session=self.session, sequence=self.motor_sequence,
            expires=time.time() + .5, **values))
        data = response.json()
        if not response.is_success:
            raise ValueError(data.get('error', 'Free Cam unavailable'))
        return data

    async def command(self, caller, payload, keyboard):
        message = self.validate(caller, payload)
        async with self.lock:
            self.validate(caller, payload)  # An RPC may have waited behind a network request.
            action, nonce = message.get('action'), message.get('nonce')
            if action == 'open':
                if self.nonce or not isinstance(nonce, str) or not 1 <= len(nonce) <= 64:
                    raise ValueError('Free Cam is already open')
                response = await self.http.get('/state')
                response.raise_for_status()
                await keyboard.stop()
                self.state = dict(response.json(), available=True)
                self.nonce, self.sequence, self.beat = nonce, -1, time.monotonic()
            else:
                if not self.nonce or nonce != self.nonce:
                    raise ValueError('Stale Free Cam session')
                if action == 'start':
                    result = await self.post('start')
                    self.session = result.pop('session')
                    self.owned = True
                    self.state = dict(result, available=True)
                elif action in ('stop', 'close'):
                    await self.hold()
                    if action == 'close':
                        self.nonce = ''
                elif action in ('pose', 'open_gripper', 'release'):
                    if not (self.owned if action == 'release' else self.session):
                        raise ValueError('Start Free Cam first')
                    values = ({'pan': 0, 'tilt': 0} if action == 'pose' else
                              {'supported': message.get('supported') is True} if action == 'release' else {})
                    self.state = dict(await self.post(action, **values), available=True)
                    self.direction = (0, 0)
                    if action == 'release':
                        self.session = ''
                        self.owned = False
                else:
                    raise ValueError('Unknown Free Cam command')
            return json.dumps(dict(self.state, viewing=bool(self.nonce)))

    def receive(self, caller, payload):
        try:
            message = self.validate(caller, payload)
            sequence = message.get('sequence')
            if (not self.nonce or message.get('nonce') != self.nonce
                    or type(sequence) is not int or sequence <= self.sequence
                    or any(type(message.get(k)) is not int or message[k] not in (-1, 0, 1) for k in ('pan', 'tilt'))):
                return
            self.sequence, self.pending = sequence, message
        except (ValueError, TypeError, AttributeError):
            pass

    async def hold(self):
        if self.session:
            try:
                self.state = dict(await self.post('stop'), available=True)
            finally:
                self.session = ''
        self.pending = None
        self.direction = (0, 0)

    async def stop(self):
        async with self.lock:
            self.nonce = ''
            await self.hold()

    async def tick(self):
        async with self.lock:
            try:
                message, self.pending = self.pending, None
                if (self.nonce and message and message['nonce'] == self.nonce
                        and message['expiresAt'] > time.time() * 1000):
                    self.beat = time.monotonic()
                    if self.session:
                        await self.post('heartbeat')
                        if self.state.get('phase') == 'active' and self.state.get('ready'):
                            direction = (message['pan'], message['tilt'])
                            if any(direction) or any(self.direction):
                                await self.post('jog', pan=direction[0], tilt=direction[1])
                            self.direction = direction
                if self.nonce and time.monotonic() - self.beat > .6:
                    self.nonce = ''
                    await self.hold()
                response = await self.http.get('/state')
                response.raise_for_status()
                self.state = dict(response.json(), available=True)
            except (httpx.HTTPError, ValueError):
                self.nonce = ''
                try:
                    await self.hold()
                except (httpx.HTTPError, ValueError):
                    pass
                self.state = {'available': False, 'phase': 'held', 'reason': 'Free Cam connection interrupted'}
            return dict(self.state, viewing=bool(self.nonce))

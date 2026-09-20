"""Remote run/pause for the existing, operator-loaded ACT tmux session."""
import asyncio
import json
from pathlib import Path
import time


class ActRelay:
    def __init__(self, controller, root=None, proc=Path('/proc')):
        self.controller = controller
        self.root = root or Path.home() / 'act-local'
        self.proc = proc
        self.lock = asyncio.Lock()
        self.nonce = ''
        self.beat = 0
        self.sequence = -1

    def state(self):
        policy = None
        for path in self.proc.glob('[0-9]*/cmdline'):
            try:
                args = path.read_bytes().split(b'\0')
                if any(arg.endswith(b'/live2.py') or arg == b'live2.py' for arg in args) and (path.parent / 'cwd').resolve() == self.root.resolve():
                    policy = args
                    break
            except (OSError, RuntimeError):
                continue
        if policy is None:
            return {'phase': 'offline', 'reason': 'Load the ACT model in the terminal first', 'nonce': self.nonce}
        phase = 'loading'
        try:
            with (self.root / 'live2.log').open('rb') as stream:
                stream.seek(0, 2)
                stream.seek(max(0, stream.tell() - 12000))
                lines = stream.read().decode(errors='replace').splitlines()
            for line in lines:
                if line.startswith('=== demo.sh') or line.startswith('Loading weights'):
                    phase = 'loading'
                elif line.startswith('MODEL READY'): phase = 'ready'
                elif line.startswith(('RUNNING', 'RESUMED', '{"second":')): phase = 'running'
                elif line.startswith('PAUSED'):
                    phase = 'paused' if 'R resumes' in line else 'error'
                elif line.startswith(('RAMPING', 'NEW ATTEMPT')): phase = 'positioning'
                elif line.startswith(('Traceback', 'RuntimeError')): phase = 'error'
        except OSError:
            pass
        return {'phase': phase, 'inPlace': b'--hold-start' in policy, 'nonce': self.nonce}

    async def key(self, *keys):
        process = await asyncio.create_subprocess_exec('tmux', 'send-keys', '-t', 'act-v3', *keys,
                                                       stdout=asyncio.subprocess.DEVNULL,
                                                       stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(process.wait(), 1)
        if process.returncode:
            raise ValueError('ACT terminal is unavailable')

    async def command(self, caller, payload):
        if caller != self.controller or len(payload) > 512:
            raise ValueError('Unauthorized ACT controller')
        message = json.loads(payload)
        async with self.lock:
            expiry = message.get('expiresAt')
            if type(expiry) is not int or not 0 < expiry - time.time()*1000 <= 2000:
                raise ValueError('ACT command expired')
            if message.get('action') == 'stop':
                await self.pause()
            elif message.get('action') == 'run':
                nonce = message.get('nonce')
                if not isinstance(nonce, str) or not 1 <= len(nonce) <= 64 or nonce == self.nonce:
                    raise ValueError('Invalid ACT attempt')
                state = self.state()
                if state['phase'] == 'ready' and state.get('inPlace'):
                    await self.key('Enter')
                elif state['phase'] in ('running', 'paused'):
                    await self.key('Space', 'r')
                else:
                    raise ValueError('Load the model with demo.sh guided and finish positioning first')
                self.nonce, self.sequence, self.beat = nonce, -1, time.monotonic()
            else:
                raise ValueError('Unknown ACT command')
            return json.dumps(self.state())

    def receive(self, caller, payload):
        if caller != self.controller or len(payload) > 512 or not self.nonce:
            return
        try:
            message = json.loads(payload)
            sequence, expiry = message.get('sequence'), message.get('expiresAt')
            if (message.get('nonce') == self.nonce and type(sequence) is int and sequence > self.sequence
                    and type(expiry) is int and 0 < expiry-time.time()*1000 <= 2000):
                self.sequence, self.beat = sequence, time.monotonic()
        except (ValueError, AttributeError):
            pass

    async def pause(self):
        self.nonce = ''
        if self.state()['phase'] not in ('offline', 'loading'):
            await self.key('Space')

    async def stop(self):
        async with self.lock:
            if self.nonce:
                await self.pause()

    async def tick(self):
        async with self.lock:
            if self.nonce and time.monotonic()-self.beat > 3:
                await self.pause()
            return self.state()

"""Allowlisted electrical-box action, scoped to the current visitor and robot 0188."""
import asyncio
import json
from pathlib import Path
import socket
import time

from act_local.visitor_control import write_json


class ActionRelay:
    def __init__(self, controller, root=Path('/home/bracketbot/act-local')):
        self.controller, self.root = controller, root
        self.path = root / 'visitor-command.json'
        self.enabled = (socket.gethostname() == 'bracketbot-0188'
                        and (root / 'run-v3/best/model.safetensors').is_file())
        self.attempt = ''
        self.used = set()
        self.beat = 0
        self.sequence = -1
        self.owned = False
        self.checked = 0
        self.state = {'phase': 'idle'}
        if self.enabled:
            try:
                state = json.loads(self.path.with_suffix('.state.json').read_text())
                command = Path(f'/proc/{int(state["pid"])}/cmdline').read_bytes().split(b'\0')
                script = str(Path(__file__).parent / 'act_local/live2.py').encode()
                if script in command and b'--visitor-control' in command:
                    self.owned = True
                    write_json(self.path, {'attempt': '', 'until': 0})
                    self.state = {'phase': 'held', 'reason': 'Policy ready; click the electrical-box circle'}
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def validate(self, caller, payload):
        if caller != self.controller or len(payload) > 1024:
            raise ValueError('Unauthorized action command')
        message = json.loads(payload)
        expiry = message.get('expiresAt')
        if type(expiry) is not int or not 0 < expiry - time.time() * 1000 <= 750:
            raise ValueError('Action command expired')
        return message

    async def tmux(self, *args):
        process = await asyncio.create_subprocess_exec('tmux', *args,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        return await process.wait()

    async def command(self, caller, payload, health, keyboard, freecam):
        message = self.validate(caller, payload)
        if message.get('action') == 'stop':
            if message.get('attempt') == self.attempt:
                self.stop()
            return
        if message.get('action') != 'start' or not self.enabled:
            raise ValueError('Electrical-box policy is available only on 0188')
        attempt = message.get('attempt')
        if not isinstance(attempt, str) or not 1 <= len(attempt) <= 64:
            raise ValueError('Invalid attempt')
        if attempt in self.used:
            return  # RPC retries must never restart motion.
        if self.attempt:
            raise ValueError('An action is already running')
        if self.owned and self.state.get('phase') == 'error':
            raise ValueError('Policy needs an operator restart after an error')
        if (health.get('status') != 'running'
                or not 0 <= time.time_ns() - health.get('source_timestamp_ns', 0) < 750_000_000):
            raise ValueError('Camera or action locations are stale')
        point = next((p for p in health.get('visible_actions', []) if p['id'] == message.get('id')), None)
        if not point or point['action_id'] != 'electric_box':
            raise ValueError('This visible location has no trained policy')
        if freecam and (freecam.nonce or freecam.state.get('phase') != 'idle'):
            raise ValueError('Release Free Cam before running an action')
        if not self.owned:
            # Never take over or park somebody else's policy/teleop session.
            for session in ('act-v3', 'quest-teleop', 'visitor-act'):
                if await self.tmux('has-session', '-t', session) == 0:
                    raise ValueError(f'{session} is already using the arms; finish that session first')
        self.validate(caller, payload)
        await keyboard.stop()
        self.used.add(attempt)
        self.attempt, self.sequence, self.beat = attempt, -1, time.monotonic()
        write_json(self.path, {'attempt': attempt, 'until': self.beat + 1})
        if not self.owned:
            self.path.with_suffix('.state.json').unlink(missing_ok=True)
            result = await self.tmux('new-session', '-d', '-s', 'visitor-act', '-c', str(self.root),
                                    str(self.root / '.venv/bin/python'), '-u', str(Path(__file__).parent / 'act_local/live2.py'),
                                    '--hold-start', '--yes', '--visitor-control', str(self.path))
            if result:
                self.stop()
                raise ValueError('Could not start the policy')
            self.owned = True
        self.state = {'phase': 'loading', 'reason': 'Preparing electrical-box policy'}

    def receive(self, caller, payload):
        try:
            message = self.validate(caller, payload)
            sequence = message.get('sequence')
            if (self.attempt and message.get('attempt') == self.attempt
                    and type(sequence) is int and sequence > self.sequence):
                self.sequence, self.beat = sequence, time.monotonic()
                write_json(self.path, {'attempt': self.attempt, 'until': self.beat + 1})
        except (ValueError, TypeError, AttributeError):
            pass

    def stop(self):
        if self.attempt:
            write_json(self.path, {'attempt': self.attempt, 'until': 0})
            self.state = {'phase': 'held', 'attempt': self.attempt, 'reason': 'Stopped; arms held'}
            self.attempt = ''

    async def tick(self):
        self.snapshot()
        if self.owned and time.monotonic() - self.checked > 1:
            self.checked = time.monotonic()
            if await self.tmux('has-session', '-t', 'visitor-act') != 0:
                self.stop()
                self.owned = False
                self.state.update(phase='error', reason='Policy exited; check visitor-act log before retrying')

    def snapshot(self):
        if self.attempt and time.monotonic() - self.beat > 1:
            self.stop()
        if self.owned:
            try:
                state = json.loads(self.path.with_suffix('.state.json').read_text())
                if state.get('attempt') == (self.attempt or self.state.get('attempt')) and time.monotonic() - state['at'] < 2:
                    self.state = state
                    if state['phase'] in ('held', 'error'):
                        self.stop()
                        self.state = state
            except (OSError, ValueError, KeyError):
                pass
        return dict(self.state, available=self.enabled, owned=self.owned,
                    attempt=self.attempt or self.state.get('attempt', ''))

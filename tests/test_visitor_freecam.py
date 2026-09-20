import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx
import pytest

from visitor_freecam import FreeCamRelay


def packet(action='open', **values):
    return json.dumps(dict(action=action, nonce='view', expiresAt=int(time.time()*1000)+700, **values))


def setup():
    commands = []
    state = dict(phase='idle', ready=True)

    def handle(request):
        if request.method == 'POST':
            command = json.loads(request.content)
            commands.append(command)
            action = command['action']
            if action == 'start':
                state['phase'] = 'active'
                return httpx.Response(200, json=dict(state, session='arm-session'))
            if action in ('stop', 'release'):
                state['phase'] = 'held' if action == 'stop' else 'idle'
        return httpx.Response(200, json=state)

    relay = FreeCamRelay('controller', httpx.AsyncClient(transport=httpx.MockTransport(handle), base_url='http://local'))
    return relay, commands, AsyncMock()


def test_auth_expiry_and_session_gate_all_robot_commands():
    async def run():
        relay, commands, keyboard = setup()
        with pytest.raises(ValueError):
            await relay.command('stranger', packet(), keyboard)
        with pytest.raises(ValueError):
            await relay.command('controller', packet('start'), keyboard)
        assert not commands
        await relay.command('controller', packet(), keyboard)
        keyboard.stop.assert_awaited_once()
        await relay.command('controller', packet('start'), keyboard)
        for caller, values in [('stranger', {}), ('controller', {'nonce': 'old'}),
                               ('controller', {'expiresAt': 1}), ('controller', {'pan': 8})]:
            message = dict(nonce='view', expiresAt=int(time.time()*1000)+400, sequence=1, pan=1, tilt=0)
            message.update(values)
            relay.receive(caller, json.dumps(message))
        assert relay.pending is None
        assert [c['action'] for c in commands] == ['start']
    asyncio.run(run())


def test_latest_input_only_then_loss_holds_without_releasing_or_resuming():
    async def run():
        relay, commands, keyboard = setup()
        await relay.command('controller', packet(), keyboard)
        await relay.command('controller', packet('start'), keyboard)
        for sequence in range(10):
            relay.receive('controller', packet(sequence=sequence, pan=1, tilt=0))
        await relay.tick()
        assert [c['action'] for c in commands] == ['start', 'heartbeat', 'jog']
        assert commands[-1]['session'] == 'arm-session'
        relay.receive('controller', packet(sequence=8, pan=-1, tilt=0))
        assert relay.pending is None
        relay.beat = time.monotonic()-1
        state = await relay.tick()
        assert state['phase'] == 'held' and not state['viewing']
        relay.receive('controller', packet(sequence=11, pan=1, tilt=0))
        await relay.tick()
        assert [c['action'] for c in commands].count('stop') == 1
        assert commands[-1]['action'] == 'stop'
        with pytest.raises(ValueError):
            await relay.command('controller', packet('start'), keyboard)
    asyncio.run(run())


def test_zero_heartbeat_preserves_pose_and_gripper_motion_then_close_holds():
    async def run():
        relay, commands, keyboard = setup()
        await relay.command('controller', packet(), keyboard)
        await relay.command('controller', packet('start'), keyboard)
        await relay.command('controller', packet('open_gripper'), keyboard)
        relay.receive('controller', packet(sequence=1, pan=0, tilt=0))
        await relay.tick()
        assert commands[-1]['action'] == 'heartbeat'
        await relay.command('controller', packet('close'), keyboard)
        assert commands[-1]['action'] == 'stop'
        assert not relay.nonce and not relay.session
        assert not any(c['action'] == 'release' for c in commands)
    asyncio.run(run())


def test_rpc_expiring_while_queued_cannot_start():
    async def run():
        relay, commands, keyboard = setup()
        await relay.command('controller', packet(), keyboard)
        await relay.lock.acquire()
        payload = json.dumps(dict(action='start', nonce='view', expiresAt=int(time.time()*1000)+20))
        task = asyncio.create_task(relay.command('controller', payload, keyboard))
        await asyncio.sleep(.04)
        relay.lock.release()
        with pytest.raises(ValueError):
            await task
        assert not commands
    asyncio.run(run())

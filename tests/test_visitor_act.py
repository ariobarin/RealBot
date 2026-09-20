import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from visitor_act import ActRelay


def message(action='run', **values):
    return json.dumps(dict(action=action, nonce='attempt', expiresAt=int(time.time()*1000)+1000) | values)


def test_connecting_and_disconnect_do_not_touch_an_operator_owned_policy():
    async def run():
        relay = ActRelay('controller')
        relay.state = lambda: {'phase': 'running'}
        relay.key = AsyncMock()
        await relay.tick()
        await relay.stop()
        relay.key.assert_not_called()
        for caller, payload in [('other', message()), ('controller', message(expiresAt=1))]:
            with pytest.raises(ValueError):
                await relay.command(caller, payload)
        relay.key.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('phase,in_place,keys', [('ready', True, ('Enter',)), ('paused', False, ('Space', 'r')), ('running', False, ('Space', 'r'))])
def test_run_reuses_loaded_policy_and_missing_heartbeat_pauses(phase, in_place, keys):
    async def run():
        relay = ActRelay('controller')
        relay.state = lambda: dict(phase=phase, inPlace=in_place)
        relay.key = AsyncMock()
        await relay.command('controller', message())
        relay.key.assert_awaited_once_with(*keys)
        relay.beat = time.monotonic()-4
        stale = relay.beat
        relay.receive('other', message(sequence=1))
        relay.receive('controller', message(sequence=1, expiresAt=1))
        assert relay.beat == stale
        await relay.tick()
        assert relay.key.await_args.args == ('Space',)
        assert not relay.nonce
        count = relay.key.await_count
        relay.receive('controller', message(sequence=2))
        await relay.tick()
        assert relay.key.await_count == count
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['offline', 'loading', 'positioning', 'error', 'ready'])
def test_run_never_cold_starts_or_homes_a_policy(phase):
    async def run():
        relay = ActRelay('controller')
        relay.state = lambda: dict(phase=phase, inPlace=False)
        relay.key = AsyncMock()
        with pytest.raises(ValueError):
            await relay.command('controller', message())
        relay.key.assert_not_called()
    asyncio.run(run())


@pytest.mark.parametrize('line,phase', [('MODEL READY.', 'ready'), ('{"second": 30, "steps": 300}', 'running'),
                                      ('PAUSED after 3 actions. Holding pose; R resumes.', 'paused'),
                                      ('PAUSED after 3 actions. Holding current pose. Q parks, E cuts torque.', 'error')])
def test_status_matches_live_driver_log_including_non_resumable_fault(tmp_path, line, phase):
    proc = tmp_path/'proc'
    root = proc/'123'/'cwd'
    root.mkdir(parents=True)
    (root.parent/'cmdline').write_bytes(b'python\0live2.py\0--hold-start\0')
    (root/'live2.log').write_text(line)
    state = ActRelay('controller', root, proc).state()
    assert state['phase'] == phase and state['inPlace']

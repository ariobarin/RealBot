import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from act_local.visitor_control import VisitorControl, write_json
from visitor_actions import ActionRelay


def message(**values):
    return json.dumps(dict(action='start', id='box', attempt='one',
                          expiresAt=int(time.time() * 1000) + 700, **values))


def test_expired_or_stopped_attempt_never_resumes_from_heartbeats(tmp_path):
    path = tmp_path / 'command.json'
    control = VisitorControl(path)
    write_json(path, {'attempt': 'one', 'until': time.monotonic() + 1})
    assert control.tick() == (True, True)
    write_json(path, {'attempt': 'one', 'until': 0})
    assert control.tick() == (False, False)
    write_json(path, {'attempt': 'one', 'until': time.monotonic() + 1})
    assert control.tick() == (False, False)
    write_json(path, {'attempt': 'two', 'until': time.monotonic() + 1})
    assert control.tick() == (True, True)
    control.hold()
    assert control.tick() == (False, False)


def test_only_visible_allowlisted_action_and_controller_can_launch(tmp_path):
    async def run():
        relay = ActionRelay('visitor', tmp_path)
        relay.enabled = True
        relay.tmux = AsyncMock(return_value=1)
        keyboard = type('Keyboard', (), {'stop': AsyncMock()})()
        health = {'status': 'running', 'source_timestamp_ns': time.time_ns(),
                  'visible_actions': [{'id': 'box', 'action_id': 'light_switch'}]}
        for caller, data in [('stranger', message()), ('visitor', message())]:
            with pytest.raises(ValueError):
                await relay.command(caller, data, health, keyboard, None)
        assert not relay.tmux.called
        health['visible_actions'][0]['action_id'] = 'electric_box'
        relay.tmux.return_value = 0
        with pytest.raises(ValueError, match='already using'):
            await relay.command('visitor', message(), health, keyboard, None)
        keyboard.stop.assert_not_called()
        relay.tmux = AsyncMock(side_effect=[1, 1, 1, 0])
        await relay.command('visitor', message(), health, keyboard, None)
        assert relay.owned
        keyboard.stop.assert_awaited_once()
        calls = relay.tmux.call_count
        await relay.command('visitor', message(), health, keyboard, None)
        assert relay.tmux.call_count == calls
        relay.stop()
        await relay.command('visitor', message(), health, keyboard, None)
        assert relay.attempt == ''  # An old RPC cannot restart a stopped attempt.
        assert VisitorControl(relay.path).read() == ''
    asyncio.run(run())


def test_wrong_robot_stale_location_and_freecam_reject_before_start(tmp_path):
    async def run():
        relay = ActionRelay('visitor', tmp_path)
        relay.tmux = AsyncMock()
        keyboard = type('Keyboard', (), {'stop': AsyncMock()})()
        with pytest.raises(ValueError, match='0188'):
            await relay.command('visitor', message(), {}, keyboard, None)
        relay.enabled = True
        with pytest.raises(ValueError, match='stale'):
            await relay.command('visitor', message(), {}, keyboard, None)
        health = {'status': 'running', 'source_timestamp_ns': time.time_ns(),
                  'visible_actions': [{'id': 'box', 'action_id': 'electric_box'}]}
        freecam = type('FreeCam', (), {'nonce': 'active'})()
        with pytest.raises(ValueError, match='Free Cam'):
            await relay.command('visitor', message(), health, keyboard, freecam)
        relay.tmux.assert_not_called()
    asyncio.run(run())


def test_missing_heartbeat_expires_local_lease(tmp_path):
    relay = ActionRelay('visitor', tmp_path)
    relay.attempt, relay.beat = 'one', time.monotonic() - 2
    write_json(relay.path, {'attempt': 'one', 'until': time.monotonic() + 1})
    assert relay.snapshot()['phase'] == 'held'
    assert relay.attempt == ''
    assert VisitorControl(relay.path).read() == ''
    relay.receive('visitor', json.dumps({'attempt': 'one', 'sequence': 1,
                  'expiresAt': int(time.time() * 1000) + 500}))
    assert VisitorControl(relay.path).read() == ''


def test_explicit_run_button_uses_same_policy_without_a_visible_marker(tmp_path):
    async def run():
        relay = ActionRelay('visitor', tmp_path)
        relay.enabled = relay.owned = True
        keyboard = type('Keyboard', (), {'stop': AsyncMock()})()
        data = json.loads(message())
        data['id'] = 'policy:electric_box'
        health = {'status': 'running', 'source_timestamp_ns': time.time_ns(), 'visible_actions': []}
        await relay.command('visitor', json.dumps(data), health, keyboard, None)
        assert VisitorControl(relay.path).read() == 'one'
        relay.stop()
        assert VisitorControl(relay.path).read() == ''
    asyncio.run(run())

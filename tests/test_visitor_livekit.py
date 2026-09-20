import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / 'edge_agent'))
from visitor_livekit import KeyboardRelay


def packet(sequence=1, nonce='current', expires=None):
    return json.dumps(dict(keys='w', shift=False, nonce=nonce, sequence=sequence,
                           expiresAt=expires or int(time.time() * 1000) + 400)).encode()


def test_only_current_controller_session_and_fresh_sequence_are_forwarded():
    relay = KeyboardRelay('controller')
    relay.nonce = 'current'
    relay.receive('stranger', packet())
    relay.receive('controller', packet(nonce='old'))
    relay.receive('controller', packet(expires=int(time.time() * 1000) - 1))
    relay.receive('controller', packet(expires=int(time.time() * 1000) + 5000))
    assert relay.commands.empty()
    relay.receive('controller', packet(sequence=2))
    assert relay.commands.get_nowait() == {'keys': 'w', 'shift': False}
    relay.receive('controller', packet(sequence=1))
    relay.receive('controller', packet(sequence=2))
    assert relay.commands.empty()
    for seq in range(3, 100):
        relay.receive('controller', packet(sequence=seq))
    assert relay.commands.qsize() == 1


def test_unauthorized_start_does_not_open_socket():
    with pytest.raises(ValueError, match='Unauthorized'):
        asyncio.run(KeyboardRelay('controller').start('stranger', '{}'))


@pytest.mark.parametrize('timeout', [True, False])
def test_timeout_or_stop_closes_local_control_and_invalidates_old_packets(timeout):
    class Socket:
        closed = False
        messages = []
        async def __aenter__(self): return self
        async def __aexit__(self, *_): self.closed = True
        async def send(self, message): self.messages.append(json.loads(message))

    async def run():
        relay, socket = KeyboardRelay('controller'), Socket()
        relay.nonce = 'current'
        relay.task = asyncio.create_task(relay.forward(socket, relay.nonce))
        relay.receive('controller', packet())
        await asyncio.sleep(.01)
        if timeout:
            await asyncio.wait_for(relay.task, .5)
        else:
            await relay.stop('current')
        assert socket.closed
        assert socket.messages == [{'keys': 'w', 'shift': False}]
        assert relay.nonce == ''
        relay.receive('controller', packet(sequence=2))
        assert relay.commands.empty()
    asyncio.run(run())

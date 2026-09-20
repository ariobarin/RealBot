import asyncio
import json
from types import SimpleNamespace

import pytest

from edge_agent.telemetry import BbosTelemetrySource, TOPIC, publish_telemetry, snapshot

NOW = 10_000_000_000


def records():
    return {
        'slam.pose': dict(timestamp=NOW, pos=[1., 2., 0.], quat=[0., 0., 0., 1.]),
        'slam.health': dict(timestamp=NOW, localized=True, degraded=False, stalled=False, vo_lost=False, relocalized=False),
        'nav.state': dict(timestamp=NOW, state=b'navigating\0', reason=b'', waypoint_index=0),
    }


def test_real_pose_and_health_are_required_for_ready():
    result = snapshot(records(), now_ns=NOW)
    assert result['ready']
    assert result['pose'] == dict(x=1., y=2., z=0., heading=0.)
    assert result['mapId'] is None
    assert result['navigation']['state'] == 'navigating'
    missing = snapshot({}, now_ns=NOW)
    assert missing['pose'] is None and not missing['ready']
    assert missing['slam']['localized'] is None


@pytest.mark.parametrize('flag', ['degraded', 'stalled', 'vo_lost'])
def test_unhealthy_slam_never_reports_ready(flag):
    data = records()
    data['slam.health'][flag] = True
    assert not snapshot(data, now_ns=NOW)['ready']


def test_stale_future_and_nonfinite_pose_are_unavailable():
    for stamp in (NOW - 301_000_000, NOW + 1):
        data = records()
        data['slam.pose']['timestamp'] = stamp
        assert snapshot(data, now_ns=NOW)['pose'] is None
    data = records()
    data['slam.pose']['pos'][0] = float('nan')
    result = snapshot(data, now_ns=NOW)
    assert result['pose'] is None
    json.dumps(result, allow_nan=False)


def test_reader_drops_disappeared_writer_and_closes_every_reader():
    readers = []
    class Reader:
        def __init__(self, topic, **kwargs):
            self.data = records()[topic]
            self.readable = True
            self.closed = False
            readers.append(self)
        def __enter__(self): return self
        def __exit__(self, *args): self.closed = True
        def ready(self): return False  # A repeated sample is still readable.
    with BbosTelemetrySource(reader_factory=Reader) as source:
        for reader in readers:
            reader.readable = False
        assert source.read()['pose'] is None
    assert all(reader.closed for reader in readers)


def test_publisher_sends_full_snapshots_and_closes_on_cancellation():
    async def run():
        closed = []
        sent = asyncio.Event()
        class Source:
            def __enter__(self): return self
            def __exit__(self, *args): closed.append(True)
            def read(self): return snapshot({}, now_ns=NOW)
        async def publish(payload, **kwargs):
            assert kwargs == dict(reliable=True, topic=TOPIC)
            assert json.loads(payload)['pose'] is None
            sent.set()
        room = SimpleNamespace(local_participant=SimpleNamespace(publish_data=publish))
        task = asyncio.create_task(publish_telemetry(room, source_factory=Source))
        await asyncio.wait_for(sent.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert closed
    asyncio.run(run())

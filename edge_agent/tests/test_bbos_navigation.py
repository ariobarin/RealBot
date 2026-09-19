import asyncio
import contextlib
import math
import sys
from types import SimpleNamespace
from unittest.mock import patch

from edge_agent.adapters.bbos_navigation import BbosNavigationAdapter, navigation_target


def test_navigation_target_uses_map_coordinates() -> None:
    x, y, heading = navigation_target({"x": 1.25, "y": -0.5})
    assert (x, y) == (1.25, -0.5)
    assert math.isnan(heading)


def test_navigation_target_rejects_unresolved_camera_coordinates() -> None:
    try:
        navigation_target({"u": 0.25, "v": 0.75})
    except ValueError as exc:
        assert "map-frame" in str(exc)
    else:
        raise AssertionError("camera coordinates must be projected before bbOS navigation")


class Waypoints:
    def __init__(self) -> None:
        self.first = None

    def fill(self, value: int) -> None:
        self.first = value

    def __setitem__(self, index: int, value: tuple[float, float, float]) -> None:
        assert index == 0
        self.first = value


class FakeWriter:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.command = {"waypoints": Waypoints()}
        self.closed = False

    def __enter__(self) -> "FakeWriter":
        return self

    @contextlib.contextmanager
    def buf(self):
        yield self.command

    def __exit__(self, *args: object) -> None:
        self.closed = True


class FakeReader:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.readable = True
        self.data = {"state": b"navigating", "reason": b""}
        self.reads = 0
        self.closed = False

    def __enter__(self) -> "FakeReader":
        return self

    def ready(self) -> bool:
        self.reads += 1
        if self.reads > 1:
            self.data = {"state": b"reached", "reason": b""}
        return True

    def __exit__(self, *args: object) -> None:
        self.closed = True


def test_adapter_waits_for_bbos_reached_and_releases_writer() -> None:
    async def scenario() -> None:
        writers: list[FakeWriter] = []
        readers: list[FakeReader] = []

        def writer(*args: object, **kwargs: object) -> FakeWriter:
            writers.append(FakeWriter(*args, **kwargs))
            return writers[-1]

        def reader(*args: object, **kwargs: object) -> FakeReader:
            readers.append(FakeReader(*args, **kwargs))
            return readers[-1]

        fake_bbos = SimpleNamespace(Reader=reader, Writer=writer, Type=lambda name: name)
        with patch.dict(sys.modules, {"bbos": fake_bbos}):
            adapter = BbosNavigationAdapter(poll_interval_s=0)
            await adapter.start("nav-1", {"x": 1.0, "y": 2.0})
            assert writers[0].command["enabled"] is True
            await adapter.wait()
            await adapter.stop("completed")

        assert writers[0].command["enabled"] is False
        assert writers[0].closed
        assert readers[0].closed

    asyncio.run(scenario())

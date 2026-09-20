from contextlib import contextmanager

import pytest
from fastapi import FastAPI, WebSocketDisconnect
from fastapi.testclient import TestClient

import visitor_drive


@pytest.fixture
def drive(monkeypatch):
    class Writer:
        commands = []
        closed = False

        def __setitem__(self, name, value):
            assert name == 'twist'
            self.commands.append(value)

        def __exit__(self, *_):
            self.closed = True

    writer = Writer()
    monkeypatch.setattr(visitor_drive, 'open_drive', lambda: (writer, 0.5))
    app = FastAPI()
    app.include_router(visitor_drive.router)
    with TestClient(app) as client:
        yield client, writer


@contextmanager
def connect(client):
    with client.websocket_connect('/drive', headers={'origin': 'http://127.0.0.1:5178'}) as ws:
        assert ws.receive_json() == {'ready': True}
        yield ws


@pytest.mark.parametrize(('keys', 'shift', 'expected'), [
    ('w', False, (0.1, 0.0)), ('s', True, (-0.2, 0.0)),
    ('a', False, (0.0, 0.3)), ('d', False, (0.0, -0.3)),
    ('wa', True, (0.165, 0.46)), ('', True, (0.0, 0.0)),
])
def test_commands_and_disconnect_stop(drive, keys, shift, expected):
    client, writer = drive
    with connect(client) as ws:
        ws.send_json({'keys': keys, 'shift': shift})
        ws.send_json({'keys': '', 'shift': False})
    assert writer.commands[1] == pytest.approx(expected)
    assert writer.commands[-1] == (0.0, 0.0)
    assert writer.closed


@pytest.mark.parametrize('message', [None, {'keys': 'w', 'shift': 'yes'}, {'keys': ['w'], 'shift': False}])
def test_timeout_and_bad_commands_release_drive(drive, message):
    client, writer = drive
    with connect(client) as ws:
        ws.send_json({'keys': 'w', 'shift': False})
        if message is not None:
            ws.send_json(message)
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1008
    assert writer.commands[-1] == (0.0, 0.0)
    assert writer.closed


def test_other_origins_cannot_acquire_drive(drive):
    client, writer = drive
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/drive', headers={'origin': 'https://other.example'}):
            pass
    assert writer.commands == []


def test_busy_drive_is_not_taken_over(drive, monkeypatch):
    client, writer = drive
    def busy():
        raise RuntimeError('Writer for drive.ctrl already exists')
    monkeypatch.setattr(visitor_drive, 'open_drive', busy)
    with client.websocket_connect('/drive', headers={'origin': 'http://127.0.0.1:5178'}) as ws:
        with pytest.raises(WebSocketDisconnect) as error:
            ws.receive_json()
        assert error.value.code == 1013
    assert writer.commands == []

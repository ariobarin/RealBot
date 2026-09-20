import numpy as np
import base64
import io
import json
import pytest
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

from action_landmarking import ActionLandmarkRecorder, DepthRayIntersector, KeywordListener, action_definition, camera_to_slam, slam_to_camera, pointing_ray, record_timestamp, classify_audio


@pytest.mark.parametrize('amplitude', [500, 90])
def test_microphone_keeps_capturing_during_recognition(monkeypatch, amplitude):
    started, finish = threading.Event(), threading.Event()
    reads = []
    class Microphone:
        def __init__(self, *args, **kwargs): self.due = 0.
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def ready(self):
            if time.monotonic() < self.due: return False
            self.due = time.monotonic()+.02
            self.data = {'timestamp': np.int64(time.time_ns()), 'audio': np.full((320, 1), amplitude, np.int16)}
            reads.append(self.due)
            return True
    def recognize(*args):
        started.set()
        finish.wait(5)
        return 'light_switch'
    monkeypatch.setitem(sys.modules, 'bbos', SimpleNamespace(Reader=Microphone, Config=lambda _: SimpleNamespace(sample_rate=16000, channels=1)))
    monkeypatch.setitem(sys.modules, 'dotenv', SimpleNamespace(load_dotenv=lambda _: None))
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-key')
    monkeypatch.setattr('action_landmarking.classify_audio', recognize)
    listener = KeywordListener(Mock())
    listener.listen_after()
    listener.start()
    try:
        assert started.wait(4), 'Audible quiet speech was never submitted'
        count = len(reads)
        time.sleep(.15)
        assert len(reads) >= count+4, 'Microphone paused while recognition was running'
    finally:
        finish.set()
        listener.close()


@pytest.mark.parametrize('answer,expected', [('light_switch', 'light_switch'), (' electric_box\n', 'electric_box'),
                                           ('none', 'none'), ('not light_switch', 'none'),
                                           ('light_switch electric_box', 'none')])
def test_audio_request_and_strict_labels(monkeypatch, answer, expected):
    def respond(request, timeout):
        assert request.full_url == 'https://openrouter.ai/api/v1/chat/completions'
        assert request.get_header('Authorization') == 'Bearer test-key'
        assert timeout == 8
        payload = json.loads(request.data)
        audio = payload['messages'][0]['content'][1]['input_audio']
        assert audio['format'] == 'wav'
        assert base64.b64decode(audio['data']) == b'test-wav'
        return io.BytesIO(json.dumps({'choices': [{'message': {'content': answer}}]}).encode())
    monkeypatch.setattr('action_landmarking.urllib.request.urlopen', respond)
    assert classify_audio(b'test-wav', 'test-key') == expected


def test_native_bbos_timestamp():
    value = 1789893000000000000
    assert record_timestamp({'timestamp': np.datetime64(value, 'ns')}) == value


def test_held_thumb_starts_once_and_times_out_without_saving(tmp_path, monkeypatch):
    clock = [10.]
    monkeypatch.setattr('action_landmarking.time.monotonic', lambda: clock[0])
    recorder = ActionLandmarkRecorder(np.eye(3, 4), (640, 480), np.eye(4), 'cal',
                                     tmp_path/'landmarks.json', tmp_path)
    recorder.narrator = Mock()
    recorder.keywords = Mock()
    args = dict(stereo={'matches': [{'thumbs_up': True, 'distance_to_camera_pair_m': .6}]},
                rgb_timestamp_ns=None, depth_record=None, pose_record=None, health_record=None, pitch_rad=0.)
    for seconds in (10., 10.2, 10.6, 11.):
        clock[0] = seconds
        recorder.observe(**args)
    assert recorder.snapshot()['state'] == 'recording'
    recorder.narrator.say.assert_called_once_with('starting action location recording')
    clock[0] = 27.
    recorder.observe(**args)
    assert recorder.snapshot()['state'] == 'cooldown'
    assert recorder.records() == []
    assert not (tmp_path/'landmarks.json').exists()


def test_action_ids_have_stable_display_definitions() -> None:
    light_switch = action_definition("light_switch")
    electric_box = action_definition("electric_box")
    assert light_switch["display_name"] == "Light switch"
    assert electric_box["display_name"] == "Electric box"
    assert light_switch["id"] != electric_box["id"]


def _landmark(point: tuple[float, float, float] | None) -> dict[str, object]:
    if point is None:
        return {"position_left_camera_m": None}
    return {
        "position_left_camera_m": {"x": point[0], "y": point[1], "z": point[2]}
    }


def test_index_only_hand_produces_pointing_ray() -> None:
    points: list[tuple[float, float, float] | None] = [None] * 21
    points[5:9] = [(0.0, 0.0, 0.50), (0.0, 0.0, 0.55), (0.0, 0.0, 0.60), (0.0, 0.0, 0.65)]
    for indices, x in (((9, 10, 11, 12), 0.03), ((13, 14, 15, 16), 0.06), ((17, 18, 19, 20), 0.09)):
        a, b, c, d = indices
        points[a], points[b], points[c], points[d] = (
            (x, 0.0, 0.50),
            (x, -0.03, 0.50),
            (x, -0.03, 0.47),
            (x, 0.0, 0.49),
        )
    ray = pointing_ray({"landmarks": [_landmark(point) for point in points]})
    assert ray is not None
    origin, direction, metrics = ray
    assert np.allclose(origin, [0.0, 0.0, 0.65])
    assert np.allclose(direction, [0.0, 0.0, 1.0])
    assert metrics["folded_fingers"] == 3


def test_depth_ray_hits_front_plane() -> None:
    projection = np.asarray(
        [[500.0, 0.0, 320.0, 0.0], [0.0, 500.0, 240.0, 0.0], [0.0, 0.0, 1.0, 0.0]]
    )
    depth = np.full((480, 640), 2000, dtype=np.uint16)
    hit = DepthRayIntersector(projection, (640, 480)).intersect(
        depth,
        np.asarray([0.0, 0.0, 0.5]),
        np.asarray([0.0, 0.0, 1.0]),
    )
    assert hit is not None
    assert np.allclose(hit.point_camera_m, [0.0, 0.0, 2.0], atol=0.01)
    assert hit.ray_error_m < 0.01


def test_camera_to_slam_matches_nav_axis_convention() -> None:
    pose = {
        "pos": np.asarray([10.0, 20.0, 0.0]),
        "quat": np.asarray([0.0, 0.0, 0.0, 1.0]),
    }
    point_base, point_map = camera_to_slam(
        np.asarray([1.0, 2.0, 3.0]),
        np.eye(4),
        pose,
        0.0,
    )
    assert np.allclose(point_base, [1.0, 2.0, 3.0])
    # At yaw zero base X is map X and base Y is map Y.
    assert np.allclose(point_map, [11.0, 22.0, 3.0])


@pytest.mark.parametrize('yaw,pitch', [(0., 0.), (1.2, .3), (-2.4, -.2)])
def test_saved_map_point_returns_to_camera_after_rotation(yaw, pitch):
    pose = {'pos': np.array([3., -2., 0.]),
            'quat': np.array([0., 0., np.sin(yaw/2), np.cos(yaw/2)])}
    mount = np.array([[1., 0., 0., .1], [0., 0., 1., .2],
                      [0., -1., 0., 1.5], [0., 0., 0., 1.]])
    point = np.array([.2, -.1, 1.4])
    world = camera_to_slam(point, mount, pose, pitch)[1]
    assert np.allclose(slam_to_camera(world, mount, pose, pitch), point)

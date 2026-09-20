import time
from types import SimpleNamespace
from contextlib import nullcontext
import numpy as np
import pytest

from edge_agent.free_cam import CameraMotion, camera_rotation
from edge_agent.wrist_demo import WristSession
from edge_agent.adapters.bbos_free_cam import BbosWrist, fresh


def motion():
    def solve(angles, current):
        q = np.zeros(8)
        q[[3, 5]] = np.asarray(angles)/360
        q[7] = current[7]
        return q
    m = CameraMotion(np.zeros(8), np.zeros(8), -np.ones(8), np.ones(8), solve)
    m.ready = True
    return m


@pytest.mark.parametrize('pan,tilt', [(-110, 85), (90, 0), (0, -90), (80, -90), (0, 85)])
def test_camera_basis_stays_level_and_defined_at_floor(pan, tilt):
    r = camera_rotation(pan, tilt)
    np.testing.assert_allclose(r.T@r, np.eye(3), atol=1e-14)
    assert np.linalg.det(r) == pytest.approx(1)
    assert r[2, 0] == 0
    assert r[2, 2] == pytest.approx(np.sin(np.radians(tilt)))


def test_jog_is_continuous_rate_limited_and_release_or_lease_loss_freezes():
    m = motion()
    positions = []
    for i in range(200):
        if i % 5 == 0: m.jog(1, -1, i, i*.02)
        positions.append(m.tick(i*.02))
    assert m.angles[0] > 60 and m.angles[1] < -60
    assert np.max(np.abs(np.diff(positions, axis=0))) <= 20/360*.02+1e-12
    np.testing.assert_array_equal(np.array(positions)[:, 7], 0)
    m.jog(0, 0, 201, 4)
    held = m.position.copy()
    np.testing.assert_array_equal(m.tick(4.1), held)
    m.jog(-1, 0, 202, 4.2)
    m.tick(4.3)
    held = m.position.copy()
    np.testing.assert_array_equal(m.tick(5), held)
    m.jog(1, 0, 201, 5)
    np.testing.assert_array_equal(m.tick(5.1), held)


def test_unreachable_or_discontinuous_solution_holds_last_valid_pose():
    m = motion()
    for solve in (lambda a, q: None, lambda a, q: np.array([0, .1, 0, 0, 0, 0, 0, 0])):
        m.solve = solve
        m.jog(1, 0, m.sequence+1, 0)
        m.tick(.02)
        np.testing.assert_array_equal(m.position, 0)
        np.testing.assert_array_equal(m.angles, 0)
        assert 'Reach limit' in m.notice


def test_preparation_is_paced_preserves_grip_and_resume_finishes():
    m = motion()
    m.ready = False
    m.neutral[3] = .25
    m.tick(0)
    before = m.position.copy()
    m.tick(.02)
    assert 0 < m.position[3]-before[3] <= 10/360*.02
    with pytest.raises(ValueError, match='viewing pose'): m.jog(1, 0, 1, .02)
    m.stop()
    held = m.position.copy()
    m.tick(100)
    np.testing.assert_array_equal(m.position, held)
    for i in range(600): m.tick(100+i*.02)
    assert m.ready and m.position[3] == .25


@pytest.mark.parametrize('pan,tilt,seq', [(111, 0, 1), (0, -91, 1), (0, 86, 1), (0, float('nan'), 1), (True, 0, 1), (0, 0, True), (0, 0, -1)])
def test_invalid_commands_do_not_change_pose_or_sequence(pan, tilt, seq):
    m = motion()
    with pytest.raises(ValueError): m.pose(pan, tilt, seq, 0)
    assert m.sequence == -1 and m.move is None


def test_recenter_and_wide_request_respect_limits():
    m = motion()
    m.pose(110, -90, 1, 0)
    for i in range(800): m.tick(i*.02)
    np.testing.assert_allclose(m.angles, [110, -90], atol=.05)
    m.pose(0, 0, 2, 20)
    for i in range(800): m.tick(20+i*.02)
    np.testing.assert_allclose(m.position, 0, atol=.05/360)


class Robot:
    def __init__(self):
        self.motion = None
        self.writes = self.releases = 0
        self.failure = False
    def health(self, **kwargs):
        if self.failure: raise RuntimeError('Camera or feedback lost')
        return {'pos': self.motion.position if self.motion else np.arange(8)/100}
    def acquire(self): self.motion = motion()
    def write(self): self.writes += 1
    def release(self): self.releases += 1; self.motion = None


def command(session, action, now=0., **kwargs):
    session.command(dict(action=action, session=session.session, expires=time.time()+1, **kwargs), now)


def active():
    r = Robot()
    s = WristSession(r, True)
    command(s, 'start')
    command(s, 'heartbeat', .8)
    command(s, 'heartbeat', 1.5)
    s.tick(1.6)
    assert s.phase == 'active'
    return r, s


def test_jog_release_preserves_session_but_heartbeat_expiry_stops_it():
    r, s = active()
    command(s, 'jog', 1.7, pan=1, tilt=0, sequence=1)
    s.tick(1.75)
    held = r.motion.position.copy()
    command(s, 'jog', 1.8, pan=0, tilt=0, sequence=2)
    s.tick(1.85)
    assert s.phase == 'active' and s.session
    np.testing.assert_array_equal(r.motion.position, held)
    command(s, 'jog', 1.9, pan=-1, tilt=0, sequence=3)
    s.tick(3)
    assert s.phase == 'held' and s.session is None
    np.testing.assert_array_equal(r.motion.position, held)


@pytest.mark.parametrize('cause', ['stop', 'heartbeat', 'sensor'])
def test_stop_disconnect_and_sensor_fault_freeze_without_releasing_torque(cause):
    r, s = active()
    command(s, 'pose', 1.7, pan=10, tilt=0, sequence=1)
    s.tick(1.8)
    held = r.motion.position.copy()
    if cause == 'stop': command(s, 'stop', 1.8)
    elif cause == 'sensor': r.failure = True
    s.tick(3.)
    np.testing.assert_array_equal(r.motion.position, held)
    assert s.phase == 'held' and s.session is None and r.releases == 0
    assert r.writes > 0
    with pytest.raises(ValueError): command(s, 'pose', 3.1, pan=0, tilt=0, sequence=2)


def test_readonly_stale_commands_release_confirmation_and_stop_during_start():
    r, s = Robot(), WristSession(Robot(), False)
    with pytest.raises(ValueError, match='Read-only'): command(s, 'start')
    s = WristSession(r, True)
    with pytest.raises(ValueError, match='expired'):
        s.command({'action': 'start', 'expires': 0}, 0)
    command(s, 'start')
    command(s, 'stop', .1)
    s.tick(2.)
    assert s.phase == 'held'
    with pytest.raises(ValueError): command(s, 'release')
    assert r.releases == 0
    command(s, 'release', supported=True)
    assert r.releases == 1 and s.phase == 'idle'


def test_stale_robot_state_is_rejected():
    data = np.zeros((), dtype=[('timestamp', 'datetime64[ns]')])
    data['timestamp'] = np.datetime64(time.time_ns()-1_000_000_000, 'ns')
    with pytest.raises(RuntimeError, match='stale'):
        fresh(SimpleNamespace(ready=lambda: None, readable=True, data=data))


@pytest.mark.parametrize('mode', [0, 1, 2])
def test_zero_drive_preserves_configured_base_mode(mode):
    robot = object.__new__(BbosWrist)
    robot.base = SimpleNamespace(lean_angle_deg=4)
    robot.base_mode, robot.motion = mode, motion()
    buffers = []
    for name in ('drive_writer', 'mode', 'ctrl', 'torque'):
        buffer = {}
        buffers.append(buffer)
        setattr(robot, name, SimpleNamespace(buf=lambda b=buffer: nullcontext(b)))
    robot.write()
    assert buffers[0] == dict(twist=0, twist_torque=0)
    assert buffers[1] == dict(mode=mode, lean_angle_deg=4)
    np.testing.assert_array_equal(buffers[2]['pos'], robot.motion.neutral)


def test_balance_corrections_allowed_but_excess_speed_or_tilt_stops(monkeypatch):
    robot = object.__new__(BbosWrist)
    robot.base = SimpleNamespace(MODE_TWIST=2, wheel_diam=.165)
    robot.base_mode = 0
    robot.fast_since = None
    robot.drive = {'vel': np.array([.16, .08])}
    robot.orientation = {'rpy': np.array([0., 1.6, 20.])}
    robot.jpeg = lambda: b''
    monkeypatch.setattr('edge_agent.adapters.bbos_free_cam.fresh', lambda reader: reader)
    now = [0.]
    monkeypatch.setattr('edge_agent.adapters.bbos_free_cam.time.monotonic', lambda: now[0])
    robot.health(arm=False)
    robot.base_mode = 2
    with pytest.raises(RuntimeError, match='speed'): robot.health(arm=False)
    robot.base_mode = 0
    robot.drive['vel'][0] = .25
    robot.health(arm=False)
    now[0] = .3
    robot.health(arm=False)
    robot.drive['vel'][:] = 0
    robot.health(arm=False)
    robot.drive['vel'][0] = .25
    robot.health(arm=False)
    now[0] = .81
    with pytest.raises(RuntimeError, match='speed'): robot.health(arm=False)
    robot.drive['vel'][:] = 0
    robot.health(arm=False)
    robot.drive['vel'][0] = .4
    with pytest.raises(RuntimeError, match='speed'): robot.health(arm=False)
    robot.drive['vel'][:] = 0
    robot.orientation['rpy'][1] = 11
    with pytest.raises(RuntimeError, match='upright'): robot.health(arm=False)



def test_open_claw_holds_camera_and_stop_freezes_gripper():
    m = motion()
    m.open_gripper(-.19, 0)
    for i in range(25): m.tick((i+1)*.02)
    np.testing.assert_array_equal(m.position[:7], 0)
    assert m.position[7] == pytest.approx(-.04)
    m.stop()
    held = m.position.copy()
    np.testing.assert_array_equal(m.tick(1), held)
    m.open_gripper(-.19, 1)
    for i in range(150): m.tick(1+(i+1)*.02)
    assert m.position[7] == pytest.approx(-.19)
    assert m.neutral[7] == m.position[7] and m.gripper_target is None
    m.jog(1, 0, 1, 4.1)
    m.tick(4.2)
    assert m.position[7] == pytest.approx(-.19) and m.angles[0] > 0
    with pytest.raises(ValueError): m.open_gripper(-2, 5)


def test_free_cam_owns_only_right_arm_and_uses_right_camera(monkeypatch, tmp_path):
    import json
    import sys
    configs, readers, writers = [], [], []
    arm = SimpleNamespace(name='arm_right')
    base = SimpleNamespace(default_mode=0, MODE_BALANCE=0, MODE_LEAN=1, MODE_TWIST=2, lean_angle_deg=4)
    def config(name):
        configs.append(name)
        return arm if name == 'arm_right' else base
    def reader(topic, **kwargs):
        readers.append(topic)
        return nullcontext(object())
    def writer(topic, kind, **kwargs):
        writers.append(topic)
        return nullcontext(SimpleNamespace(buf=lambda: nullcontext({})))
    monkeypatch.setitem(sys.modules, 'bbos', SimpleNamespace(Config=config, Reader=reader, Writer=writer, Type=lambda x:x))
    def ik(cfg, pos, lower, upper):
        assert cfg is arm
        return SimpleNamespace(neutral=pos.copy(), lower=lower, upper=upper, solve=lambda angles, q:q)
    monkeypatch.setattr('edge_agent.adapters.bbos_free_cam.CameraIK', ik)
    ranges = tmp_path/'bbos/daemons/arm_right/ranges.calibration.json'
    ranges.parent.mkdir(parents=True)
    ranges.write_text(json.dumps(dict(cal_min=[-1]*8, cal_max=[1]*8)))
    robot = BbosWrist(tmp_path)
    monkeypatch.setattr(robot, 'health', lambda:dict(pos=np.zeros(8), vel=np.zeros(8)))
    try:
        robot.acquire()
        assert configs == ['arm_right', 'base']
        assert 'camera.right.jpeg' in readers and 'arm_right.state' in readers
        assert set(writers) == {'nav.command','drive.ctrl','base.mode','arm_right.ctrl','arm_right.torque'}
        assert not any('left' in topic for topic in readers+writers)
    finally:
        robot.close()

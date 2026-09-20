"""Native bbOS IK and exclusive right-arm ownership for Free Cam."""
from contextlib import ExitStack
import json
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import numpy as np

from ..free_cam import CameraMotion, camera_rotation, vector


class CameraIK:
    # Optical right = EEF Y, down = -EEF X, forward = EEF Z on 0187.
    camera_in_eef = np.array([[0., -1., 0.], [1., 0., 0.], [0., 0., 1.]])

    def __init__(self, cfg, position, lower, upper):
        from bbos.tf import rmat_to_quat, pose_to_matrix
        self.cfg, self.quat, self.matrix = cfg, rmat_to_quat, pose_to_matrix
        self.lower, self.upper = lower.copy(), upper.copy()
        self.lower[0] = max(lower[0], position[0]-.08/(.0465*2*np.pi))
        self.upper[0] = min(upper[0], position[0]+.08/(.0465*2*np.pi))
        home = cfg.home.astype(float).copy()
        home[[0, 7]] = position[[0, 7]]
        self.nominal = cfg.q2urdf(home)[:7]
        old = cfg.ik
        self.anchor = np.asarray(old.fk(self.nominal.tolist())[0])
        self.anchor[1] = -.20
        model = ET.fromstring(old._urdf_content)
        a, b = cfg.q2urdf(self.lower), cfg.q2urdf(self.upper)
        for i, name in enumerate(cfg.joint_names[:7]):
            limit = model.find(f".//joint[@name='{name}']/limit")
            lo, hi = sorted((a[i], b[i]))
            limit.set('lower', str(lo))
            limit.set('upper', str(hi))
        self.ik = type(old)(old._solver_name, ET.tostring(model, encoding='unicode'),
                            old._base_link, old._ee_link, self.nominal.tolist(),
                            old.tolerances, self.nominal.tolist(), old._joint_centering_weight,
                            old._rik_max_iterations, old._centering_weights)
        self.neutral = self.solve([0, 0], home, iterations=30)
        if self.neutral is None:
            raise RuntimeError('No level forward camera pose at this lift height')

    def solve(self, angles, position, iterations=4):
        camera = camera_rotation(*angles)
        rotation = camera @ self.camera_in_eef.T
        target = self.anchor+.12*(camera[:, 2]-[1, 0, 0])
        seed = self.cfg.q2urdf(position)
        self.ik.reset(seed[:7].tolist())
        self.ik.set_nominal(self.nominal.tolist())
        quaternion = self.quat(rotation).tolist()
        for _ in range(iterations):
            solution = np.asarray(self.ik.solve(target.tolist(), quaternion))
            if solution.shape != (7,) or not np.isfinite(solution).all():
                return None
        p, q = self.ik.fk(solution.tolist())
        actual = self.matrix([0, 0, 0], q)[:3, :3]
        error = np.degrees(np.arccos(np.clip((np.trace(rotation.T@actual)-1)/2, -1, 1)))
        seed[:7] = solution
        motor = self.cfg.urdf2q(seed)
        motor[7] = position[7]
        if (np.linalg.norm(np.asarray(p)-target) > .025 or error > 3
                or np.any(motor[:7] < self.lower[:7]) or np.any(motor[:7] > self.upper[:7])):
            return None
        return motor


def fresh(reader, age=.3):
    reader.ready()
    if not reader.readable or reader.data is None:
        raise RuntimeError('Robot stream unavailable')
    data = reader.data.copy()
    if not 0 <= time.time_ns()-int(data['timestamp'].astype('int64')) <= age*1e9:
        raise RuntimeError('Robot stream stale')
    return data


class BbosWrist:
    def __init__(self, root):
        from bbos import Config, Reader, Writer, Type
        self.Reader, self.Writer, self.Type = Reader, Writer, Type
        self.cfg, self.base = Config('arm_right'), Config('base')
        self.base_mode = self.base.default_mode
        self.fast_since = None
        if self.base_mode not in (self.base.MODE_BALANCE, self.base.MODE_LEAN, self.base.MODE_TWIST):
            raise RuntimeError('Unsupported configured base mode')
        ranges = json.loads((Path(root)/'bbos/daemons/arm_right/ranges.calibration.json').read_text())
        a, b = vector(ranges['cal_min']), vector(ranges['cal_max'])
        self.lower, self.upper = np.minimum(a, b), np.maximum(a, b)
        self.readers, self.writers = ExitStack(), None
        self.motion = None
        try:
            self.arm = self.readers.enter_context(Reader('arm_right.state', keeptime=False))
            self.drive = self.readers.enter_context(Reader('drive.state', keeptime=False))
            self.camera = self.readers.enter_context(Reader('camera.right.jpeg', keeptime=False))
            self.orientation = self.readers.enter_context(Reader('imu.orientation', keeptime=False))
        except BaseException:
            self.readers.close()
            raise

    def health(self, *, arm=True):
        wheels = np.asarray(fresh(self.drive)['vel'])
        if not np.isfinite(wheels).all():
            raise RuntimeError('Invalid base wheel feedback')
        speed = float(np.max(np.abs(wheels)))
        if self.base_mode == self.base.MODE_TWIST:
            excessive = speed > .02
        else:
            # Allow brief balance corrections, not sustained travel or large spikes.
            speed *= np.pi*self.base.wheel_diam
            now = time.monotonic()
            self.fast_since = (now if self.fast_since is None else self.fast_since) if speed > .1 else None
            excessive = speed > .2 or (self.fast_since is not None and now-self.fast_since >= .5)
        if excessive:
            raise RuntimeError('Base wheel speed exceeds Free Cam limit')
        rpy = np.asarray(fresh(self.orientation)['rpy'])  # bbOS publishes degrees.
        if not np.isfinite(rpy).all() or np.max(np.abs(rpy[:2])) > 10:
            raise RuntimeError('Base is not upright')
        self.jpeg()
        if arm:
            data = fresh(self.arm)
            for field in ('pos', 'vel', 'current', 'temp'):
                vector(data[field])
            if np.any(data['temp'] >= self.cfg.software_temp_limit) or np.any(np.abs(data['current']) >= self.cfg.hard_current_limit*.9):
                raise RuntimeError('Arm load or temperature limit; hold stopped')
            return data

    def jpeg(self):
        data = fresh(self.camera, .5)
        length = int(data['jpeg_len'])
        if not 0 < length <= len(data['jpeg']):
            raise RuntimeError('Invalid wrist camera frame')
        return data['jpeg'][:length].tobytes()

    def acquire(self):
        if self.writers is not None:
            raise RuntimeError('Arm is already held')
        data = self.health()
        if np.max(np.abs(data['vel'])) > .02:
            raise RuntimeError('Wait until the right arm is still')
        ik = CameraIK(self.cfg, data['pos'], self.lower, self.upper)
        motion = CameraMotion(data['pos'], ik.neutral, ik.lower, ik.upper, ik.solve)
        stack = ExitStack()
        try:
            def writer(topic, kind):
                return stack.enter_context(self.Writer(topic, self.Type(kind), keeptime=False))
            # IPC refuses competing owners. Never kill Quest, inference or navigation.
            nav = writer('nav.command', 'nav_command')
            self.drive_writer = writer('drive.ctrl', 'drive_ctrl')
            self.mode = writer('base.mode', 'base_mode')
            self.ctrl = writer('arm_right.ctrl', 'arm_ctrl')
            self.torque = writer('arm_right.torque', 'arm_torque')
            with nav.buf() as command:
                command['enabled'] = False
                command['num_waypoints'] = 0
                command['loop'] = command['global_goal'] = False
            self.motion = motion
            self.write(enable=False)
            self.write(enable=True)
            self.writers = stack
        except BaseException:
            stack.close()
            self.motion = None
            raise

    def write(self, enable=True):
        with self.drive_writer.buf() as data:
            data['twist'] = 0
            data['twist_torque'] = 0
        with self.mode.buf() as data:
            data['mode'] = self.base_mode
            data['lean_angle_deg'] = self.base.lean_angle_deg
        with self.ctrl.buf() as data:
            data['pos'] = self.motion.position
            data['vel'] = data['tau'] = 0
            data['alpha'] = 0
        with self.torque.buf() as data:
            data['enable'] = enable
            for field in ('tau_mode', 'compliance_mode', 'axis_aligned', 'force_only', 'j0_homing', 'calibrating'):
                data[field] = False

    def release(self):
        if self.writers is not None:
            self.motion.stop()
            self.write(enable=False)
            self.writers.close()
            self.writers = None
            self.motion = None

    def open_gripper(self, now):
        target = self.cfg.q2urdf(self.motion.position)
        target[7] = 1.2  # bbOS remote_session GRIPPER_OPEN_POS_WIDE, in URDF radians.
        self.motion.open_gripper(float(self.cfg.urdf2q(target)[7]), now)

    def close(self):
        self.release()
        self.readers.close()

"""Continuous, level-roll camera targets with bounded arm motion."""
import math
import numpy as np


def vector(value):
    result = np.asarray(value, dtype=float)
    if result.shape != (8,) or not np.isfinite(result).all():
        raise ValueError('Expected eight finite motor positions')
    return result.copy()


def camera_rotation(pan, tilt):
    yaw, pitch = np.radians([-pan, tilt])
    forward = np.array([np.cos(pitch)*np.cos(yaw), np.cos(pitch)*np.sin(yaw), np.sin(pitch)])
    right = np.array([np.sin(yaw), -np.cos(yaw), 0.])
    return np.column_stack((right, np.cross(forward, right), forward))


class CameraMotion:
    limits = np.array([[-110., -90.], [110., 85.]])
    speeds = np.array([.1, *([20/360]*6), 0.])  # Motor turns/s; J0 is the lift.

    def __init__(self, position, neutral, lower, upper, solve):
        self.position, self.neutral = vector(position), vector(neutral)
        self.lower, self.upper = vector(lower), vector(upper)
        self.solve = solve
        self.check(self.position)
        self.check(self.neutral)
        if self.neutral[7] != self.position[7]:
            raise ValueError('Free Cam must preserve the gripper')
        self.angles, self.direction, self.velocity = np.zeros((3, 2))
        self.ready = False
        self.notice = 'Preparing camera pose'
        self.move = None
        self.gripper_target = None
        self.sequence = -1
        self.jog_until = self.last_tick = None

    def check(self, target):
        if np.any(target < self.lower) or np.any(target > self.upper):
            raise ValueError('Camera pose exceeds calibrated joint ranges')

    def validate(self, pan, tilt, sequence, limits):
        if type(sequence) is not int or not 0 <= sequence <= 2**53-1:
            raise ValueError('Invalid pose sequence')
        if any(type(v) not in (int, float) or not math.isfinite(v) or not lo <= v <= hi
               for v, lo, hi in zip((pan, tilt), *limits)):
            raise ValueError('Camera command is outside its range')
        if not self.ready:
            raise ValueError('Wait for the camera to reach its viewing pose')
        return sequence > self.sequence

    def pose(self, pan, tilt, sequence, now):
        if not self.validate(pan, tilt, sequence, self.limits):
            return
        self.stop()
        self.move = np.array([pan, tilt], dtype=float)
        self.sequence, self.last_tick = sequence, now

    def jog(self, pan, tilt, sequence, now):
        if not self.validate(pan, tilt, sequence, [[-1, -1], [1, 1]]):
            return
        if not np.any(self.direction):
            self.last_tick = now
        self.move = None
        self.direction = np.array([pan, tilt], dtype=float)
        self.jog_until, self.sequence = now+.35, sequence
        if not np.any(self.direction):
            self.stop()

    def open_gripper(self, target, now):
        if not self.ready or not math.isfinite(target) or not self.lower[7] <= target <= self.upper[7]:
            raise ValueError('Gripper target unavailable or outside calibrated range')
        self.stop()
        self.gripper_target, self.last_tick = target, now

    def tick(self, now):
        dt = min(.05, max(0., now-self.last_tick)) if self.last_tick is not None else 0.
        self.last_tick = now
        if not dt:
            return self.position.copy()
        if self.gripper_target is not None:
            self.position[7] += np.clip(self.gripper_target-self.position[7], -.08*dt, .08*dt)
            self.neutral[7] = self.position[7]
            if abs(self.gripper_target-self.position[7]) < 1e-8:
                self.gripper_target = None
            return self.position.copy()
        if not self.ready:
            delta = self.neutral-self.position
            self.position += np.clip(delta, -self.speeds*dt*.5, self.speeds*dt*.5)
            self.ready = bool(np.max(np.abs(delta)) < .0001)
            if self.ready:
                self.notice = ''
            return self.position.copy()
        if np.any(self.direction) and now >= self.jog_until:
            self.stop()
        if self.move is None and not np.any(self.direction):
            return self.position.copy()
        desired = self.direction*20 if self.move is None else np.clip((self.move-self.angles)/dt, -20, 20)
        self.velocity += np.clip(desired-self.velocity, -60*dt, 60*dt)
        step = self.velocity*dt
        if self.move is not None:
            remaining = self.move-self.angles
            step = np.sign(remaining)*np.minimum(np.abs(step), np.abs(remaining))
        angles = np.clip(self.angles+step, *self.limits)
        target = self.solve(angles, self.position)
        if target is None:
            self.notice = 'Reach limit: change direction'
            self.velocity[:] = 0
            self.move = None
            return self.position.copy()
        target = vector(target)
        self.check(target)
        if target[7] != self.neutral[7] or np.max(np.abs(target[1:7]-self.position[1:7])) > .04:
            self.notice = 'Reach limit: no continuous arm solution'
            self.velocity[:] = 0
            self.move = None
            return self.position.copy()
        delta = target-self.position
        scale = min(1., float(np.min(self.speeds[:7]*dt/np.maximum(np.abs(delta[:7]), 1e-12))))
        self.position += delta*scale
        self.angles += (angles-self.angles)*scale
        self.notice = 'View angle limit' if np.any(angles == self.limits) else ''
        if self.move is not None and np.max(np.abs(self.move-self.angles)) < .05:
            self.stop()
        return self.position.copy()

    def stop(self):
        self.move = None
        self.gripper_target = None
        self.direction[:] = 0
        self.velocity[:] = 0
        self.last_tick = None

    def offsets(self):
        return self.angles.tolist()

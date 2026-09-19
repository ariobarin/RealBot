"""Quaternion helpers, shared by the homing and teleop paths."""
import numpy as np


def quat_mul(a, b):
    """Hamilton product a∘b of xyzw quaternions (apply b's rotation, then a's)."""
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ])


def quat_forward_z(quat):
    """Forward (gripper) axis = body Z-axis of an xyzw quaternion, in the IK target frame."""
    x, y, z, w = quat
    return np.array([
        2 * (x * z + y * w),
        2 * (y * z - x * w),
        1 - 2 * (x * x + y * y),
    ])


def slerp(q0, q1, s):
    """Spherical interpolation between xyzw quaternions (shortest arc)."""
    q0 = q0 / np.linalg.norm(q0)
    q1 = q1 / np.linalg.norm(q1)
    d = float(np.dot(q0, q1))
    if d < 0.0:
        q1, d = -q1, -d
    if d > 0.9995:
        out = q0 + s * (q1 - q0)
        return out / np.linalg.norm(out)
    th = np.arccos(np.clip(d, -1.0, 1.0))
    return (np.sin((1.0 - s) * th) * q0 + np.sin(s * th) * q1) / np.sin(th)

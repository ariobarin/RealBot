"""Wheel speeds shared by keyboard teleop and the visitor camera."""

WHEEL_VEL_COMBOS = {
    'w': (0.20, 0.20),
    's': (-0.20, -0.20),
    'a': (-0.15, 0.15),
    'd': (0.15, -0.15),
    'wa': (0.05, 0.28),
    'wd': (0.28, 0.05),
    'sa': (-0.05, -0.28),
    'sd': (-0.28, -0.05),
    '': (0.0, 0.0),
}


def wheel_vels_to_twist(v_left, v_right, robot_width):
    return (v_left + v_right) / 2.0, (v_right - v_left) / robot_width

import math
import time

import numpy as np
import pytest

from edge_agent.projection import FloorCapture, floor_goal


def capture():
    # Camera at 1 m: optical +z is base +y, optical +y is base -z.
    return FloorCapture("capture", time.monotonic(), b"jpeg", np.full((61, 61), 1000),
                        (20., 20., 30., 10.), np.array([[1,0,0,0], [0,0,1,0], [0,-1,0,1]]),
                        (0., 0., 0.), "revision")


def resolve(c, u=.5, v=.5, **changes):
    args = dict(current_pose=c.pose, revision="revision", grid=np.ones((100,100)),
                origin=(-5., -5.), resolution=.1, clearance_m=.25)
    args.update(changes)
    return floor_goal(c, u, v, **args)


def test_floor_projection_and_rotated_map_frame():
    c = capture()
    assert resolve(c) == pytest.approx({"x": 0., "y": 1.})
    c.pose = (2., 1., math.pi/2)
    assert resolve(c) == pytest.approx({"x": 1., "y": 1.})


@pytest.mark.parametrize("changes,match", [
    ({"revision": "new-map"}, "Map changed"),
    ({"current_pose": (.04, 0., 0.)}, "Robot moved"),
    ({"current_pose": (0., 0., .05)}, "Robot moved"),
    ({"now": time.monotonic()+30}, "expired"),
    ({"grid": np.zeros((100,100))}, "unknown floor"),
    ({"grid": np.ones((3,3))}, "outside"),
])
def test_rejects_invalid_evidence(changes, match):
    with pytest.raises(ValueError, match=match):
        resolve(capture(), **changes)


def test_rejects_wall_missing_depth_edges_and_bad_clicks():
    c = capture()
    with pytest.raises(ValueError, match="not on the floor"):
        resolve(c, v=1/6)
    c.depth[30,30] = 0
    with pytest.raises(ValueError, match="No reliable depth"):
        resolve(c)
    c.depth[30,30] = 1500
    with pytest.raises(ValueError, match="Depth edge"):
        resolve(c)
    for u in (True, -1, float("nan"), None):
        with pytest.raises(ValueError, match="outside"):
            resolve(c, u=u)


def test_obstacle_in_robot_footprint_rejects_target():
    grid = np.ones((100,100))
    grid[51,60] = 2
    with pytest.raises(ValueError, match="obstacle"):
        resolve(capture(), grid=grid)

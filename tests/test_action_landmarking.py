import numpy as np

from action_landmarking import DepthRayIntersector, action_definition, camera_to_slam, pointing_ray


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

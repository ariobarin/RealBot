from edge_agent.capture import CaptureSyncError, CaptureSynchronizer


def test_builds_bundle_from_nearest_samples() -> None:
    sync = CaptureSynchronizer()
    now = 10_000_000_000
    sync.add_depth(now - 5_000_000, [[1000]])
    sync.add_pose(now + 4_000_000, {"x": 1.0, "y": 2.0, "heading": 0.5})
    sync.add_rgb(now, b"jpeg")
    bundle = sync.bundle(
        map_id="space-1",
        map_revision=2,
        calibration_revision="cal-1",
        now_ns=now + 10_000_000,
    )
    assert bundle.capture_id.startswith("cap_")
    assert bundle.depth == [[1000]]
    assert bundle.map_pose["x"] == 1.0


def test_rejects_stale_or_skewed_capture() -> None:
    sync = CaptureSynchronizer(max_rgb_depth_skew_ms=10, max_age_ms=20)
    sync.add_rgb(1_000_000_000, b"jpeg")
    sync.add_depth(1_050_000_000, [[1000]])
    sync.add_pose(1_000_000_000, {"x": 0.0})
    try:
        sync.bundle(
            map_id="space-1",
            map_revision=1,
            calibration_revision="cal-1",
            now_ns=1_005_000_000,
        )
    except CaptureSyncError as exc:
        assert "depth skew" in str(exc)
    else:
        raise AssertionError("expected capture rejection")

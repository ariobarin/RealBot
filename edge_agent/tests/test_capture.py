from edge_agent.capture import AlignedCaptureBuilder, CaptureSyncError, TimedSample


def test_builds_bundle_from_nearest_samples() -> None:
    builder = AlignedCaptureBuilder()
    now = 10_000_000_000
    bundle = builder.bundle(
        rgb=TimedSample(now, b"jpeg"),
        depth=TimedSample(now - 5_000_000, [[1000]]),
        pose=TimedSample(now + 4_000_000, {"x": 1.0, "y": 2.0, "heading": 0.5}),
        map_id="space-1",
        map_revision=2,
        calibration_revision="cal-1",
        now_timestamp_ns=now + 10_000_000,
        now_monotonic_ns=123,
    )
    assert bundle.capture_id.startswith("cap_")
    assert bundle.depth == [[1000]]
    assert bundle.map_pose["x"] == 1.0


def test_rejects_stale_or_skewed_capture() -> None:
    builder = AlignedCaptureBuilder(max_rgb_depth_skew_ms=10, max_age_ms=20)
    try:
        builder.bundle(
            rgb=TimedSample(1_000_000_000, b"jpeg"),
            depth=TimedSample(1_050_000_000, [[1000]]),
            pose=TimedSample(1_000_000_000, {"x": 0.0}),
            map_id="space-1",
            map_revision=1,
            calibration_revision="cal-1",
            now_timestamp_ns=1_005_000_000,
        )
    except CaptureSyncError as exc:
        assert "depth skew" in str(exc)
    else:
        raise AssertionError("expected capture rejection")

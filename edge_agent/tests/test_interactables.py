import asyncio
from typing import Any

from edge_agent.capture import CaptureBundle
from edge_agent.interactables import InteractableSetup, SetupState
from edge_agent.motion import MotionCoordinator, MotionMode


class Adapter:
    async def start(self, command_id: str, payload: dict[str, Any]) -> None:
        pass

    async def update(self, payload: dict[str, Any]) -> None:
        pass

    async def stop(self, reason: str) -> None:
        pass


def bundle() -> CaptureBundle:
    return CaptureBundle(
        capture_id="cap-1",
        captured_monotonic_ns=10,
        rgb_timestamp_ns=10,
        depth_timestamp_ns=10,
        pose_timestamp_ns=10,
        rgb=b"jpeg",
        depth=[[1000]],
        map_pose={"x": 0.0},
        map_id="space-1",
        map_revision=1,
        calibration_revision="cal-1",
    )


def test_two_gate_success_is_the_only_save_path() -> None:
    async def scenario() -> None:
        phrases: list[str] = []
        saved: list[dict[str, Any]] = []
        adapter = Adapter()
        setup = InteractableSetup(
            MotionCoordinator(
                {
                    MotionMode.FOLLOWING: adapter,
                    MotionMode.INTERACTABLE_TEST: adapter,
                }
            ),
            phrases.append,
            saved.append,
        )
        await setup.start(slam_ready=True)
        await setup.open_palm("follow-1")
        await setup.capture_point(bundle(), {"x": 1.0, "y": 2.0, "z": 0.8})
        await setup.label("cabinet", {"opens": "left"})
        await setup.confirm_and_test("test-1", confirmed=True)
        assert setup.state == SetupState.AWAITING_TEST_RESULT
        assert saved == []
        record = await setup.confirm_test_result(passed=True, confirmed_by="realtor-1")
        assert record is not None
        assert record["status"] == "verified"
        assert len(saved) == 1
        assert "saved this cabinet" in phrases[-1]

    asyncio.run(scenario())


def test_failed_realtor_check_does_not_save_or_allow_end() -> None:
    async def scenario() -> None:
        saved: list[dict[str, Any]] = []
        adapter = Adapter()
        setup = InteractableSetup(
            MotionCoordinator(
                {
                    MotionMode.FOLLOWING: adapter,
                    MotionMode.INTERACTABLE_TEST: adapter,
                }
            ),
            lambda phrase: None,
            saved.append,
        )
        await setup.start(slam_ready=True)
        await setup.open_palm("follow-1")
        await setup.capture_point(bundle(), {"x": 1.0, "y": 2.0, "z": 0.8})
        await setup.label("cabinet", {"opens": "right"})
        await setup.confirm_and_test("test-1", confirmed=True)
        assert await setup.confirm_test_result(passed=False, confirmed_by="realtor-1") is None
        assert saved == []
        try:
            await setup.end()
        except RuntimeError as exc:
            assert "cancel or verify" in str(exc)
        else:
            raise AssertionError("unverified draft must block setup end")
        await setup.cancel_current()
        await setup.end()
        assert setup.state == SetupState.IDLE

    asyncio.run(scenario())


def test_type_registry_requires_action_parameters() -> None:
    async def scenario() -> None:
        adapter = Adapter()
        setup = InteractableSetup(
            MotionCoordinator(
                {
                    MotionMode.FOLLOWING: adapter,
                    MotionMode.INTERACTABLE_TEST: adapter,
                }
            ),
            lambda phrase: None,
            lambda record: None,
        )
        await setup.start(slam_ready=True)
        await setup.open_palm("follow-1")
        await setup.capture_point(bundle(), {"x": 1.0, "y": 2.0, "z": 0.8})
        try:
            await setup.label("cabinet", {})
        except ValueError as exc:
            assert "opens" in str(exc)
        else:
            raise AssertionError("cabinet direction must be required")

    asyncio.run(scenario())

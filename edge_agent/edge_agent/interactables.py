"""Realtor-guided interactable setup and two-gate MVP verification."""

from __future__ import annotations

import inspect
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from .capture import CaptureBundle
from .motion import MotionCoordinator, MotionMode


class SetupState(StrEnum):
    IDLE = "idle"
    READY = "ready"
    FOLLOWING = "following"
    AWAITING_LABEL = "awaiting_label"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    TESTING = "testing"
    AWAITING_TEST_RESULT = "awaiting_test_result"
    RETRY_REQUIRED = "retry_required"
    PAUSED_SLAM = "paused_slam"


@dataclass
class InteractableDraft:
    draft_id: str
    capture_id: str
    map_id: str
    map_revision: int
    point: dict[str, float]
    interaction_type: str | None = None
    parameters: dict[str, Any] | None = None
    execution_result: str | None = None


Narrate = Callable[[str], Awaitable[None] | None]
SaveInteractable = Callable[[dict[str, Any]], Awaitable[None] | None]

INTERACTION_RULES: dict[str, dict[str, set[str]]] = {
    "cabinet": {"opens": {"left", "right"}},
    "drawer": {},
    "light_switch": {"desiredState": {"on", "off"}},
    "fridge": {"opens": {"left", "right"}},
}


class InteractableSetup:
    """Owns setup state; only explicit two-gate success invokes ``save``."""

    def __init__(
        self,
        motion: MotionCoordinator,
        narrate: Narrate,
        save: SaveInteractable,
    ) -> None:
        self.motion = motion
        self._narrate = narrate
        self._save = save
        self.state = SetupState.IDLE
        self.draft: InteractableDraft | None = None

    async def start(self, *, slam_ready: bool) -> None:
        if not slam_ready:
            raise RuntimeError("SLAM must be localized before setup")
        self.state = SetupState.READY
        await self._say("Interactables setup is ready. Show me an open hand when you want me to follow.")

    async def open_palm(self, command_id: str) -> None:
        if self.state not in {SetupState.READY, SetupState.FOLLOWING}:
            raise RuntimeError("open palm is not valid in the current setup state")
        await self._say("Open hand detected. I'll follow you.")
        await self.motion.transition(MotionMode.FOLLOWING, command_id)
        self.state = SetupState.FOLLOWING

    async def capture_point(
        self,
        bundle: CaptureBundle,
        point: dict[str, float],
    ) -> InteractableDraft:
        if self.state != SetupState.FOLLOWING:
            raise RuntimeError("point capture requires following mode")
        if set(point) != {"x", "y", "z"} or not all(
            isinstance(value, (int, float)) and math.isfinite(float(value))
            for value in point.values()
        ):
            raise ValueError("point must contain finite x, y, and z coordinates")
        await self._say("Pointing detected. I'm stopping to capture the location.")
        await self.motion.stop_all("point_capture")
        self.draft = InteractableDraft(
            draft_id=f"draft_{uuid.uuid4().hex}",
            capture_id=bundle.capture_id,
            map_id=bundle.map_id,
            map_revision=bundle.map_revision,
            point=point,
        )
        self.state = SetupState.AWAITING_LABEL
        await self._say("I captured the point. What type of interaction is it?")
        return self.draft

    async def label(self, interaction_type: str, parameters: dict[str, Any]) -> None:
        if self.state != SetupState.AWAITING_LABEL or self.draft is None:
            raise RuntimeError("there is no point awaiting a label")
        rules = INTERACTION_RULES.get(interaction_type)
        if rules is None:
            raise ValueError("unsupported interaction type")
        for name, allowed in rules.items():
            if parameters.get(name) not in allowed:
                choices = ", ".join(sorted(allowed))
                raise ValueError(f"{interaction_type} requires {name}: {choices}")
        self.draft.interaction_type = interaction_type
        self.draft.parameters = parameters
        self.state = SetupState.AWAITING_CONFIRMATION
        detail = ", ".join(f"{key} {value}" for key, value in parameters.items())
        suffix = f", {detail}" if detail else ""
        await self._say(f"I heard {interaction_type}{suffix}. Should I test it?")

    async def confirm_and_test(self, command_id: str, *, confirmed: bool) -> None:
        if self.state != SetupState.AWAITING_CONFIRMATION or self.draft is None:
            raise RuntimeError("there is no labeled point awaiting confirmation")
        if not confirmed:
            self.state = SetupState.AWAITING_LABEL
            await self._say("I did not start the test. Please label the point again or cancel it.")
            return
        self.state = SetupState.TESTING
        await self._say(f"I'm testing the {self.draft.interaction_type} now.")
        try:
            await self.motion.transition(
                MotionMode.INTERACTABLE_TEST,
                command_id,
                {
                    "name": self.draft.interaction_type,
                    **(self.draft.parameters or {}),
                    "point": self.draft.point,
                },
            )
            await self.motion.complete(
                MotionMode.INTERACTABLE_TEST,
                command_id,
                "test_motion_completed",
            )
        except Exception:
            self.draft.execution_result = "failed"
            self.state = SetupState.RETRY_REQUIRED
            await self._say("The test motion failed. I did not save this interaction.")
            raise
        self.draft.execution_result = "succeeded"
        self.state = SetupState.AWAITING_TEST_RESULT
        await self._say(
            f"The motion completed. Did the {self.draft.interaction_type} work correctly?"
        )

    async def confirm_test_result(self, *, passed: bool, confirmed_by: str) -> dict[str, Any] | None:
        if self.state != SetupState.AWAITING_TEST_RESULT or self.draft is None:
            raise RuntimeError("there is no completed test awaiting a result")
        if not passed:
            self.state = SetupState.RETRY_REQUIRED
            await self._say("The test did not pass. I did not save this interaction.")
            return None
        record = {
            "id": f"int_{uuid.uuid4().hex}",
            **asdict(self.draft),
            "status": "verified",
            "verification": {
                "method": "realtor_confirmation",
                "executionResult": self.draft.execution_result,
                "confirmed": True,
                "confirmedBy": confirmed_by,
                "confirmedAt": int(time.time() * 1000),
            },
        }
        await self._call(self._save, record)
        await self._say(f"The test passed. I saved this {self.draft.interaction_type}.")
        self.draft = None
        self.state = SetupState.READY
        return record

    async def cancel_current(self) -> None:
        await self.motion.stop_all("setup_cancelled")
        self.draft = None
        self.state = SetupState.READY
        await self._say("I cancelled this interaction point.")

    async def slam_lost(self) -> None:
        await self.motion.stop_all("slam_lost")
        self.state = SetupState.PAUSED_SLAM
        await self._say("I lost localization, so I stopped.")

    async def end(self) -> None:
        if self.draft is not None:
            raise RuntimeError("cancel or verify the current interaction before ending setup")
        await self.motion.stop_all("setup_ended")
        self.state = SetupState.IDLE
        await self._say("Interactables setup is complete.")

    async def _say(self, phrase: str) -> None:
        await self._call(self._narrate, phrase)

    @staticmethod
    async def _call(callback: Callable[..., Any], *args: Any) -> None:
        result = callback(*args)
        if inspect.isawaitable(result):
            await result

"""Small, strict protocol model shared by the real edge agent and simulator."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any


ALLOWED_ACTIONS = frozenset(
    {
        "move_to",
        "move_to_view",
        "stop",
        "use_action",
        "follow_start",
        "follow_stop",
        "free_cam_start",
        "free_cam_pose",
        "free_cam_stop",
    }
)


class CommandError(ValueError):
    """A command is malformed, unsupported, or unsafe to execute."""


@dataclass(frozen=True)
class Command:
    command_id: str
    action: str
    created_at_ms: int
    expires_at_ms: int
    payload: dict[str, Any]


def _finite_numbers(value: Any) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, dict):
        return all(_finite_numbers(item) for item in value.values())
    if isinstance(value, list):
        return all(_finite_numbers(item) for item in value)
    return True


def parse_command(message: dict[str, Any], now_ms: int | None = None) -> Command:
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    command_id = message.get("commandId")
    action = message.get("action")
    payload = message.get("payload", {})
    created_at = message.get("createdAt", now_ms)
    expires_at = message.get("expiresAt", now_ms + 6_000)

    if not isinstance(command_id, str) or not command_id or len(command_id) > 128:
        raise CommandError("invalid commandId")
    if action not in ALLOWED_ACTIONS:
        raise CommandError("unsupported action")
    if not isinstance(payload, dict) or not _finite_numbers(payload):
        raise CommandError("invalid payload")
    if not isinstance(created_at, int) or not isinstance(expires_at, int):
        raise CommandError("invalid command timestamps")
    if expires_at <= now_ms:
        raise CommandError("command expired")
    if expires_at - created_at > 30_000:
        raise CommandError("command lifetime too long")

    return Command(command_id, action, created_at, expires_at, payload)

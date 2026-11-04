"""Stable, user-facing error codes (the subset of AXPilot's that a mobile server raises).

Every failure that reaches an MCP client is a :class:`MobilePilotError` with a code from
:class:`ErrorCode`. Agents branch on codes, so never rename or reuse one.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    NO_DEVICE = "NO_DEVICE"
    ENGINE_UNAVAILABLE = "ENGINE_UNAVAILABLE"
    APP_NOT_FOUND = "APP_NOT_FOUND"
    SNAPSHOT_NOT_FOUND = "SNAPSHOT_NOT_FOUND"
    STALE_REF = "STALE_REF"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    ELEMENT_NOT_ACTIONABLE = "ELEMENT_NOT_ACTIONABLE"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    TIMEOUT = "TIMEOUT"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    NEEDS_USER = "NEEDS_USER"
    COMMAND_FAILED = "COMMAND_FAILED"
    INTERNAL = "INTERNAL"


_DEFAULT_HINTS: dict[ErrorCode, str] = {
    ErrorCode.NO_DEVICE: (
        "No emulator, simulator or device is connected. Start one (or plug a phone in with USB "
        "debugging on), then call mobile_devices. `mobilepilot doctor` says what is missing."
    ),
    ErrorCode.STALE_REF: "The screen changed since that snapshot. Call mobile_snapshot again and use the new refs.",
    ErrorCode.SNAPSHOT_NOT_FOUND: "Snapshots expire. Call mobile_snapshot again.",
    ErrorCode.ELEMENT_NOT_FOUND: "Nothing on screen matches. Scroll (mobile_find scroll=true) or check the screen.",
}


class MobilePilotError(Exception):
    """An error with a stable :class:`ErrorCode`, a human message and an optional hint."""

    def __init__(
        self, code: ErrorCode, message: str, *, hint: str | None = None, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint if hint is not None else _DEFAULT_HINTS.get(code)
        self.details = details or {}

    def __str__(self) -> str:
        return f"{self.code.value}: {self.message}"

    def to_text(self) -> str:
        text = f"{self.code.value}: {self.message}"
        if self.hint:
            text += f"\nHint: {self.hint}"
        return text

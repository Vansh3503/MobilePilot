"""The engine interface: what a platform backend must do for the server.

Trimmed from AXPilot's ``Engine`` to what a phone needs. Every method blocks; the server runs
them in worker threads with deadlines. Coordinates are in the space ``capture_tree`` reports
frames in (Android pixels, iOS points), so a tap at an element's centre needs no conversion.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

from mobilepilot.errors import ErrorCode, MobilePilotError
from mobilepilot.models import RawTree

Platform = Literal["android", "ios"]
AppAction = Literal["launch", "stop", "clear", "install", "grant", "current", "open"]

KEYS: tuple[str, ...] = ("back", "home", "enter", "delete", "tab", "app_switch", "volume_up", "volume_down", "search",
                         "notifications", "quick_settings")
"""Key names ``press_key`` accepts on every engine (an engine may refuse one it cannot press)."""


@dataclass(slots=True)
class Device:
    serial: str
    """adb serial (``emulator-5554``, a USB serial, ``host:port`` for wireless) or simulator UDID."""
    platform: Platform
    kind: Literal["emulator", "simulator", "device"]
    state: str
    """``device`` (Android, ready), ``unauthorized``/``offline`` (Android), ``Booted`` (iOS)."""
    name: str = ""
    os_version: str = ""

    @property
    def ready(self) -> bool:
        return self.state in ("device", "Booted")


class Engine(ABC):
    """One connected device."""

    platform: Platform
    last_fingerprint: int | None = None
    """:meth:`signature` of the screen the last :meth:`capture_tree` read (None: unknown)."""

    @abstractmethod
    def capture_tree(self) -> RawTree:
        """The foreground app's accessibility tree (system bars and the keyboard left out)."""

    @abstractmethod
    def signature(self) -> int:
        """A cheap fingerprint of the screen: equal twice in a row means it has settled."""

    @abstractmethod
    def tap(self, x: float, y: float, *, hold_s: float = 0.0) -> None:
        """Tap (``hold_s`` > 0: long-press for that long)."""

    @abstractmethod
    def type_text(self, text: str, *, clear: bool = False) -> None:
        """Type into the focused field, replacing its content first when ``clear``."""

    @abstractmethod
    def swipe(self, x1: float, y1: float, x2: float, y2: float, *, duration_s: float = 0.3) -> None: ...

    def double_tap(self, x: float, y: float) -> None:
        self.tap(x, y)
        self.tap(x, y)

    def drag(self, x1: float, y1: float, x2: float, y2: float, *, hold_s: float = 0.6,
             duration_s: float = 0.8) -> None:
        """Press, hold, move and release: drag and drop, reordering, sliders."""
        raise MobilePilotError(ErrorCode.NOT_SUPPORTED, "This device cannot drag yet.")

    def pinch(self, cx: float, cy: float, spread: float, *, zoom: Literal["in", "out"]) -> None:
        """Two fingers around (cx, cy): zoom "in" spreads them apart, "out" brings them together."""
        raise MobilePilotError(ErrorCode.NOT_SUPPORTED, "This device cannot pinch yet.")

    def rotate(self, orientation: Literal["portrait", "landscape", "auto"]) -> None:
        raise MobilePilotError(ErrorCode.NOT_SUPPORTED, "This device cannot be rotated from here.")

    @abstractmethod
    def press_key(self, key: str) -> None:
        """One of :data:`KEYS`."""

    @abstractmethod
    def app(self, action: AppAction, *, app: str | None = None, path: str | None = None,
            permission: str | None = None, url: str | None = None) -> str:
        """App lifecycle, and "open" for a deep link; returns a one-line summary of what happened."""

    def ensure_awake(self) -> str | None:
        """Make the screen usable: wake it, dismiss a keyguard with no PIN. Returns what it did,
        or None. A PIN, pattern or password raises NEEDS_USER: never type one."""
        return None

    @abstractmethod
    def screenshot(self, path: str, *, max_side: int = 1280) -> tuple[int, int]:
        """Write a PNG to ``path`` (longest side at most ``max_side``); returns its size."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release the device connection."""

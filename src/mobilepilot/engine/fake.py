"""A scripted engine over recorded Android dumps, for tests (AXPilot's ``engine/fake.py`` idea,
far smaller): screens are fixture XML files, and a tap on an element with a scripted label
moves to another screen."""

from __future__ import annotations

from pathlib import Path

from mobilepilot.engine.android import parse_hierarchy
from mobilepilot.engine.base import AppAction, Engine
from mobilepilot.errors import ErrorCode, MobilePilotError
from mobilepilot.models import RawNode, RawTree


class FakeEngine(Engine):
    platform = "android"

    def __init__(self, screens: dict[str, str | Path], start: str, *,
                 taps: dict[tuple[str, str], str] | None = None, back: dict[str, str] | None = None,
                 activities: dict[str, str] | None = None, package: str = "com.android.settings") -> None:
        self.screens = {name: Path(p).read_text() if isinstance(p, Path) else p for name, p in screens.items()}
        self.current = start
        self.taps = taps or {}
        """``(screen, label) → next screen``; label is an element's text, content-desc or resource-id."""
        self.back = back or {}
        self.activities = activities or {}
        """Screen → activity name (default: the screen name). Screens of one activity share refs."""
        self.package = package
        self.log: list[str] = []
        self.shot_size = (320, 640)
        """Size of the PNG ``screenshot`` writes (smaller than the screen to test scaling)."""
        self.typed: list[str] = []

    def capture_tree(self) -> RawTree:
        return parse_hierarchy(self.screens[self.current], package=self.package,
                               activity=self.activities.get(self.current, self.current),
                               pid=4242, size=(320, 640))

    def signature(self) -> int:
        return hash(self.current)

    def _labels(self, node: RawNode) -> set[str]:
        """A node's own labels and its descendants' (a row is tapped through its title text)."""
        return {label for n in node.walk() for label in (n.title, n.value, n.description, n.identifier)
                if isinstance(label, str) and label}

    def tap(self, x: float, y: float, *, hold_s: float = 0.0) -> None:
        self.log.append(f"tap {x:g},{y:g}")
        # the innermost node under the finger, then its own labels and its children's (a row is
        # tapped through its title text)
        under = [n for n in self.capture_tree().root.walk()
                 if n.frame is not None and n.frame.x <= x < n.frame.x + n.frame.w and n.frame.y <= y < n.frame.y + n.frame.h]
        for node in sorted(under, key=lambda n: n.frame.area):
            hits = [self.taps[(self.current, label)] for label in self._labels(node) if (self.current, label) in self.taps]
            if hits:
                self.current = hits[0]
                return
            if node.actions:  # a tappable element with no script: the tap is its own, nothing moves
                return

    def type_text(self, text: str, *, clear: bool = False) -> None:
        self.typed.append(text)
        self.log.append(f"type {'(clear) ' if clear else ''}{text}")

    def double_tap(self, x: float, y: float) -> None:
        self.log.append(f"double-tap {x:g},{y:g}")

    def drag(self, x1: float, y1: float, x2: float, y2: float, *, hold_s: float = 0.6,
             duration_s: float = 0.8) -> None:
        self.log.append(f"drag {x1:g},{y1:g}->{x2:g},{y2:g} hold {hold_s:g}s")

    def pinch(self, cx: float, cy: float, spread: float, *, zoom: str) -> None:
        self.log.append(f"pinch {zoom} at {cx:g},{cy:g} spread {spread:g}")

    def rotate(self, orientation: str) -> None:
        self.log.append(f"rotate {orientation}")

    def swipe(self, x1: float, y1: float, x2: float, y2: float, *, duration_s: float = 0.3) -> None:
        self.log.append(f"swipe {x1:g},{y1:g}->{x2:g},{y2:g} in {duration_s:g}s")

    def press_key(self, key: str) -> None:
        self.log.append(f"key {key}")
        if key == "back" and self.current in self.back:
            self.current = self.back[self.current]

    def app(self, action: AppAction, *, app: str | None = None, path: str | None = None,
            permission: str | None = None, url: str | None = None) -> str:
        self.log.append(f"app {action} {app or path or url or ''}".strip())
        if action == "clear" and not app:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "clear needs app.")
        return f"{action} {app or path or ''}".strip()

    def screenshot(self, path: str, *, max_side: int = 1280) -> tuple[int, int]:
        from PIL import Image

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", self.shot_size, "white").save(path, format="PNG")
        return self.shot_size

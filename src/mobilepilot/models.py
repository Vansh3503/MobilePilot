"""Plain data types shared by the engines, perception and server layers.

Copied from AXPilot's ``axpilot.models`` (same authors, Apache-2.0), trimmed to what a mobile
engine produces. Coordinates are device **pixels** on Android and **points** on iOS, origin at
the top-left of the screen; taps use the same space, so nothing converts between them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float

    def to_list(self) -> list[float]:
        return [round(self.x, 1), round(self.y, 1)]


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def center(self) -> Point:
        return Point(self.x + self.w / 2, self.y + self.h / 2)

    @property
    def area(self) -> float:
        return max(self.w, 0.0) * max(self.h, 0.0)

    def contains(self, p: Point) -> bool:
        return self.x <= p.x < self.x + self.w and self.y <= p.y < self.y + self.h

    def intersects(self, other: Rect) -> bool:
        return not (
            other.x >= self.x + self.w
            or other.x + other.w <= self.x
            or other.y >= self.y + self.h
            or other.y + other.h <= self.y
        )

    def intersection(self, other: Rect) -> Rect | None:
        x1, y1 = max(self.x, other.x), max(self.y, other.y)
        x2, y2 = min(self.x + self.w, other.x + other.w), min(self.y + self.h, other.y + other.h)
        if x2 <= x1 or y2 <= y1:
            return None
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def union(self, other: Rect) -> Rect:
        """The smallest rectangle containing both."""
        x1, y1 = min(self.x, other.x), min(self.y, other.y)
        x2, y2 = max(self.x + self.w, other.x + other.w), max(self.y + self.h, other.y + other.h)
        return Rect(x1, y1, x2 - x1, y2 - y1)

    def to_list(self) -> list[float]:
        return [round(self.x, 1), round(self.y, 1), round(self.w, 1), round(self.h, 1)]


@dataclass(slots=True)
class AppInfo:
    name: str
    pid: int
    bundle_id: str | None = None
    path: str | None = None
    frontmost: bool = False
    hidden: bool = False

    def matches(self, query: str) -> bool:
        """True if ``query`` equals the name or bundle id (case-insensitive) or the pid."""
        q = query.strip().casefold()
        if not q:
            return False
        if q == str(self.pid):
            return True
        if self.bundle_id and q == self.bundle_id.casefold():
            return True
        return q == self.name.casefold() or q.removesuffix(".app") == self.name.casefold()


@dataclass(slots=True)
class WindowInfo:
    window_id: int
    pid: int
    app: str
    title: str
    frame: Rect
    bundle_id: str | None = None
    display_id: int | None = None
    focused: bool = False
    main: bool = False
    minimized: bool = False
    fullscreen: bool = False


@dataclass(slots=True, eq=False)
class RawNode:
    """One accessibility element as captured by an engine.

    ``value`` is never populated for secure (password) fields: engines must leave it
    ``None`` when ``secure`` is true. ``role``/``subrole`` are AX-style names (``AXButton``,
    ``AXCheckBox``/``AXSwitch``) so the perception layer copied from AXPilot works unchanged.
    ``handle`` is a value, not a live object: what the node shows and where (see
    ``engine.android.handle_of``); actions target the element's frame.
    """

    role: str
    subrole: str | None = None
    title: str | None = None
    description: str | None = None
    value: Any = None
    help: str | None = None
    identifier: str | None = None
    placeholder: str | None = None
    label: str | None = None
    url: str | None = None
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    expanded: bool | None = None
    secure: bool = False
    visible: bool = True
    frame: Rect | None = None
    actions: tuple[str, ...] = ()
    child_count: int | None = None
    """Total number of children when ``children`` was truncated by capture budgets."""
    children: list[RawNode] = field(default_factory=list)
    handle: Any = field(default=None, repr=False)
    native_class: str | None = None
    a11y_id: str | None = None
    """What Appium's accessibility-id strategy matches: ``content-desc`` on Android,
    ``accessibilityIdentifier`` on iOS."""
    """The platform class (``android.widget.Switch``, ``XCUIElementTypeSwitch``): what an Appium
    class-name or XPath locator names. ``role`` is the AX-style role it maps to."""

    def walk(self):
        """Pre-order traversal of this node and its descendants."""
        stack: list[RawNode] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.children))


@dataclass(slots=True)
class RawTree:
    """The accessibility tree of one app (usually scoped to one window)."""

    app: AppInfo
    window: WindowInfo | None
    root: RawNode
    menu_bar: RawNode | None = None
    node_count: int = 0
    truncated: bool = False
    """True when the engine stopped early because of ``max_nodes``/``max_depth``/timeouts."""
    elapsed_ms: float = 0.0
    keyboard: bool = False
    """The soft keyboard is open (its keys are left out of the tree)."""
    keyboard_frame: Rect | None = None
    """Where the keyboard covers the screen, when the engine knows."""
    overlays: list[Overlay] = field(default_factory=list)
    empty_web: bool = False
    """A web view is on screen with no content in the tree (Chrome drops it after a scroll)."""
    """Windows drawn over the app's own: menus, drop-downs, dialogs, sheets, system prompts."""


@dataclass(slots=True)
class Overlay:
    """A window over the app's main one (Android: a top-level window in the dump)."""

    kind: str
    """``popup menu``, ``drop-down list``, ``dialog``, ``bottom sheet``, ``system dialog``,
    ``permission dialog`` or ``crash/ANR dialog``."""
    package: str
    frame: Rect | None
    handles: frozenset = frozenset()
    """Handles of its nodes, to pick its elements out of a snapshot."""

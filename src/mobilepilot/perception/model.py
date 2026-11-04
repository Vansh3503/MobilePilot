"""Perception data model: snapshots of the UI with short element refs.

A :class:`Snapshot` is what the ``snapshot`` tool returns: a pruned, numbered view of one
app's accessibility tree. Every element gets a ref (``e12``; OCR text ``o3``) that tools
accept instead of coordinates. A plain :func:`~mobilepilot.perception.snapshot.build_snapshot`
numbers ``e1…eN`` in pre-order; the server then gives every element a *session ref* with
:func:`~mobilepilot.perception.refs.carry_refs`: unique per session, never reused for another
element, and kept by an element across snapshots of its window. :class:`Locator` lets the
server re-find an element after its engine handle went stale.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from mobilepilot.models import AppInfo, Point, Rect, WindowInfo

SnapshotMode = Literal["interactive", "full", "text"]
SNAPSHOT_MODES: tuple[SnapshotMode, ...] = ("interactive", "full", "text")

TOGGLE_ROLES: frozenset[str] = frozenset({"checkbox", "toggle", "switch", "radio", "tab"})
"""Short roles whose value is an on/off state (rendered as ``checked`` rather than a value)."""


@dataclass(slots=True, eq=False)
class Locator:
    """Enough information to find an element again in a fresh capture of the same window."""

    pid: int
    window_id: int | None
    path: tuple[tuple[str, int], ...]
    """``(ax_role, index among same-role siblings)`` for each step from the root."""
    ax_role: str
    identifier: str | None
    name: str | None


@dataclass(slots=True, eq=False)
class Element:
    """One element of a snapshot, as shown to the model."""

    ref: str
    """``"e12"`` (``"o3"`` for OCR text): unique within its snapshot. Session refs (see
    :mod:`mobilepilot.perception.refs`) are unique per session and stay with the element across
    snapshots; a plain ``build_snapshot`` numbers ``e1…eN`` in pre-order."""
    role: str
    """Short role, see :func:`mobilepilot.perception.roles.short_role`."""
    ax_role: str
    subrole: str | None
    name: str | None
    """Best label: title > label > description > placeholder (stripped, truncated to 120)."""
    value: str | None
    """Stringified value, truncated to ``value_max_chars``; always ``None`` for secure fields."""
    frame: Rect | None
    depth: int
    enabled: bool
    focused: bool
    selected: bool
    expanded: bool | None
    secure: bool
    interactive: bool
    actions: tuple[str, ...]
    extra: dict[str, Any]
    """Role-specific facts, e.g. ``{"rows": 482}``, ``{"scrollable": True}``, ``{"url": "…"}``."""
    locator: Locator
    handle: Any = field(repr=False)
    """Engine handle used to act on the element. Never serialized."""
    parent_ref: str | None
    value_digest: str | None = field(default=None, repr=False)
    """Digest of the whole value when ``value`` was cut, so a diff sees edits past the cut (a long
    text area). Never serialized."""

    @property
    def center(self) -> Point | None:
        """Center of the frame in screen points (``None`` without a frame)."""
        return self.frame.center if self.frame is not None else None

    @property
    def identifier(self) -> str | None:
        """The element's ``AXIdentifier``, if the app set one."""
        return self.locator.identifier

    @property
    def checked(self) -> bool | None:
        """On/off state of checkboxes, switches, radio buttons and tabs (``None`` otherwise).

        A mixed checkbox (value ``2``) counts as checked."""
        if self.role not in TOGGLE_ROLES or self.value is None:
            return None
        return self.value.strip().casefold() not in {"0", "false", "", "off", "no"}

    @property
    def states(self) -> tuple[str, ...]:
        """State words in display order: disabled, selected, focused, expanded/collapsed,
        secure, checked/mixed."""
        words: list[str] = []
        if not self.enabled:
            words.append("disabled")
        if self.selected:
            words.append("selected")
        if self.focused:
            words.append("focused")
        if self.expanded is True:
            words.append("expanded")
        elif self.expanded is False:
            words.append("collapsed")
        if self.secure:
            words.append("secure")
        if self.checked:
            words.append("mixed" if self.value == "2" else "checked")
        return tuple(words)


@dataclass(slots=True)
class Snapshot:
    """A numbered, pruned view of one app (usually one window)."""

    snapshot_id: str
    """``"s_"`` followed by 4-6 lowercase alphanumeric characters."""
    app: AppInfo
    window: WindowInfo | None
    mode: SnapshotMode
    elements: list[Element]
    """The page returned to the model (after ``offset``/``max_elements``)."""
    all_elements: list[Element]
    """Every candidate element; refs resolve against this list."""
    focused_ref: str | None
    total_nodes: int
    """Nodes in the raw tree."""
    offset: int
    truncated: bool
    """True when the engine stopped capturing early (node/depth budgets or timeouts)."""
    menus: list[str]
    """Top-level menu titles (without the Apple menu) if the menu bar was captured."""
    created_at: float
    """``time.monotonic()`` at creation."""
    ref_session: str | None = None
    """The :class:`~mobilepilot.perception.refs.RefCounter` session that numbered the refs
    (``None`` for a plain ``build_snapshot``). Equal refs of two snapshots with the same
    session name the same element."""
    _index: tuple[list[Element], int, dict[str, Element]] | None = field(
        default=None, init=False, repr=False, compare=False
    )
    """Lazily built ``ref → element`` map, with the ``all_elements`` list (and its length) it
    was built for, so replacing or extending the list rebuilds it."""

    @property
    def total(self) -> int:
        """Number of candidate elements across all pages."""
        return len(self.all_elements)

    @property
    def next_offset(self) -> int | None:
        """Offset of the next page, or ``None`` if this page reaches the end."""
        end = self.offset + len(self.elements)
        return end if end < len(self.all_elements) else None

    def element(self, ref: str) -> Element | None:
        """The element with ``ref`` (any page), or ``None``. O(1): refs are not positional."""
        elements = self.all_elements
        index = self._index
        if index is None or index[0] is not elements or index[1] != len(elements):
            index = (elements, len(elements), {el.ref: el for el in elements})
            self._index = index
        return index[2].get(ref)

    @property
    def focused_element(self) -> Element | None:
        """The element that has keyboard focus, if it is part of the snapshot."""
        return self.element(self.focused_ref) if self.focused_ref else None


@dataclass(slots=True)
class SnapshotDiff:
    """What changed between two snapshots of the same app."""

    before_id: str
    after_id: str
    app_changed: bool
    window_changed: bool
    focus_before: str | None
    """Label (name, or short role if unnamed) of the focused element before (a label, so it
    reads the same whether or not the two snapshots share refs)."""
    focus_after: str | None
    added: list[Element]
    """Elements of ``after`` with no match in ``before``."""
    removed: list[Element]
    """Elements of ``before`` with no match in ``after`` (their refs no longer name anything on screen)."""
    changed: list[tuple[Element, Element]]
    """Matched ``(before, after)`` pairs whose name, value or state changed."""
    app_before: str | None = None
    app_after: str | None = None
    window_before: str | None = None
    """Title of the window before (``None`` if there was none)."""
    window_after: str | None = None
    focus_after_ref: str | None = None
    """Ref (in ``after``) of the newly focused element."""

    def is_empty(self) -> bool:
        """True if nothing observable changed."""
        return not (
            self.app_changed
            or self.window_changed
            or self.added
            or self.removed
            or self.changed
            or self.focus_before != self.focus_after
        )

"""Turn a raw accessibility tree into a pruned, numbered :class:`Snapshot`.

Pruning keeps what an agent needs to act and drops the rest:

* ``visible=False`` nodes are skipped together with their subtrees. Zero-size nodes and
  nodes entirely outside the window frame are skipped themselves, but their children are
  still examined one by one (web content often nests visible nodes in 0×0 wrappers).
  Nodes inside an open menu are exempt from the window-frame check because menus float
  outside the window.
* Mode ``interactive`` (the default) keeps interactive elements plus *context* elements:
  named lists/tables/outlines/scroll areas/web areas/tab groups/toolbars/headings/groups,
  and sheets, popovers and dialogs (even unnamed, since a modal changes what the agent can
  do). Static text inside a sheet or dialog is kept too, because that is where alert
  messages live. Everything else is collapsed: its children are promoted to its level.
  The window itself is described by the header, and the window-frame buttons (close,
  minimize, zoom) are left to the ``window`` tool, so neither is listed.
* Mode ``full`` keeps every visible node. Mode ``text`` keeps static text and headings only
  (a reading view).

Refs (``e1``, ``e2``, …) are assigned in pre-order over the kept elements, so the same tree
always yields the same refs. These are *local* refs: the server renumbers every snapshot with
:func:`mobilepilot.perception.refs.carry_refs`, which makes refs unique per session and keeps them
stable across snapshots of a window.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from mobilepilot.errors import MobilePilotError, ErrorCode
from mobilepilot.models import RawNode, RawTree, Rect
from mobilepilot.perception.model import SNAPSHOT_MODES, Element, Locator, Snapshot, SnapshotMode
from mobilepilot.perception.roles import DIALOG_SUBROLES, TEXT_INPUT_ROLES, is_interactive, short_role

NAME_MAX_CHARS = 120
ELLIPSIS = "…"

Path = tuple[tuple[str, int], ...]

COLLECTION_ROLES: frozenset[str] = frozenset({"AXList", "AXTable", "AXOutline", "AXBrowser", "AXGrid"})
"""Containers whose row count is reported as ``extra["rows"]``."""

CONTEXT_ROLES: frozenset[str] = COLLECTION_ROLES | frozenset(
    {
        "AXScrollArea",
        "AXWebArea",
        "AXTabGroup",
        "AXToolbar",
        "AXHeading",
        "AXGroup",
        "AXRadioGroup",
        "AXSheet",
        "AXPopover",
    }
)
"""Non-interactive roles kept (when named) to give the model structure."""

MODAL_ROLES: frozenset[str] = frozenset({"AXSheet", "AXPopover", "AXDialog"})
"""Roles kept even without a name: they change what the agent can interact with."""

WINDOW_FRAME_SUBROLES: frozenset[str] = frozenset(
    {"AXCloseButton", "AXMinimizeButton", "AXZoomButton", "AXFullScreenButton"}
)
"""The traffic-light buttons; the ``window`` tool covers them."""

READING_ROLES: frozenset[str] = frozenset({"AXStaticText", "AXHeading"})

SCROLL_CONTENT_ROLES: frozenset[str] = COLLECTION_ROLES | frozenset({"AXTextArea", "AXWebArea", "AXGroup"})
"""Roles that inherit ``scrollable`` from an unnamed (collapsed) parent scroll area."""

_SUBROLE_LABELS: dict[str, str] = {
    "AXCloseButton": "Close",
    "AXMinimizeButton": "Minimize",
    "AXZoomButton": "Zoom",
    "AXFullScreenButton": "Full Screen",
}

_CONTENT_LABEL_MAX_PARTS = 2
_CONTENT_LABEL_MAX_NODES = 60


# --------------------------------------------------------------------------- text helpers
def truncate(text: str, limit: int) -> str:
    """Cut ``text`` to at most ``limit`` characters, ending with ``…`` when shortened."""
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ELLIPSIS[:limit]
    return text[: limit - 1].rstrip() + ELLIPSIS


def clean_text(text: str | None) -> str | None:
    """Collapse whitespace (including newlines) to single spaces; ``None`` if empty."""
    if not text:
        return None
    cleaned = " ".join(str(text).split())
    return cleaned or None


def stringify_value(value: Any, limit: int) -> str | None:
    """Render an ``AXValue`` as text (booleans as ``true``/``false``), truncated to ``limit``."""
    text = value_text(value)
    return truncate(text, limit) if text is not None else None


def value_text(value: Any) -> str | None:
    """An ``AXValue`` as text, whole (booleans as ``true``/``false``)."""
    if value is None:
        return None
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, int):
        text = str(value)
    elif isinstance(value, float):
        text = format(value, ".6g") if math.isfinite(value) else str(value)
    elif isinstance(value, str):
        text = value
    elif isinstance(value, list | tuple):
        text = ", ".join(str(item) for item in value)
    elif isinstance(value, dict):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    else:
        text = str(value)
    return text


def _own_label(node: RawNode) -> str | None:
    for candidate in (node.title, node.label, node.description, node.placeholder):
        text = clean_text(candidate)
        if text:
            return text
    return None


def _content_label(node: RawNode) -> str | None:
    """Label an unnamed row/button/link/heading by the static text inside it.

    Rows also use the values of their text fields (Finder's Name column is an editable field), so
    a table view that recycles a row for another item gives it another name; never a secure one."""
    fields = node.role == "AXRow"
    parts: list[str] = []
    visited = 0
    stack = list(reversed(node.children))
    while stack and len(parts) < _CONTENT_LABEL_MAX_PARTS and visited < _CONTENT_LABEL_MAX_NODES:
        child = stack.pop()
        visited += 1
        if not child.visible:
            continue
        if child.role == "AXStaticText":
            text = _own_label(child) or clean_text(stringify_value(child.value, NAME_MAX_CHARS * 2))
            if text:
                parts.append(text)
            continue
        if fields and child.role == "AXTextField":
            text = None if child.secure else clean_text(stringify_value(child.value, NAME_MAX_CHARS * 2))
            if text:
                parts.append(text)
            continue
        stack.extend(reversed(child.children))
    return " — ".join(parts) if parts else None


def node_name(node: RawNode) -> str | None:
    """Best human label for a node, truncated to :data:`NAME_MAX_CHARS`.

    Order: title > label > description > placeholder. Static text falls back to its value;
    unnamed interactive elements and headings fall back to the static text they contain
    (``"Shopping list — 10:42"`` for a list row); window-frame buttons get a fixed label.
    """
    text = _own_label(node)
    if text is None and node.role == "AXStaticText":
        text = clean_text(stringify_value(node.value, NAME_MAX_CHARS * 2))
    if (
        text is None
        and node.role not in TEXT_INPUT_ROLES
        and (node.role == "AXHeading" or is_interactive(node))
    ):
        text = _content_label(node)
    if text is None and node.subrole:
        text = _SUBROLE_LABELS.get(node.subrole)
    return truncate(text, NAME_MAX_CHARS) if text else None


# --------------------------------------------------------------------------- tree helpers
def child_paths(node: RawNode, path: Path) -> Iterator[tuple[RawNode, Path]]:
    """Children of ``node`` with their locator paths (role + index among same-role siblings)."""
    seen: dict[str, int] = {}
    for child in node.children:
        index = seen.get(child.role, 0)
        seen[child.role] = index + 1
        yield child, (*path, (child.role, index))


def iter_with_paths(root: RawNode) -> Iterator[tuple[RawNode, Path]]:
    """Pre-order traversal yielding every node with its locator path (the root has ``()``)."""
    stack: list[tuple[RawNode, Path]] = [(root, ())]
    while stack:
        node, path = stack.pop()
        yield node, path
        stack.extend(reversed(list(child_paths(node, path))))


def node_at_path(root: RawNode, path: Path) -> RawNode | None:
    """Follow a locator path from ``root``; ``None`` if the structure no longer matches."""
    node = root
    for role, index in path:
        same_role = [child for child in node.children if child.role == role]
        if index >= len(same_role):
            return None
        node = same_role[index]
    return node


def is_modal(node: RawNode) -> bool:
    """Sheets, popovers and dialog windows."""
    return node.role in MODAL_ROLES or (node.subrole is not None and node.subrole in DIALOG_SUBROLES)


def is_scrollable(node: RawNode) -> bool:
    """A scroll area that shows a scroll bar or whose content overflows it."""
    if "scroll" in node.actions:  # mobile engines say so directly (Android scrollable="true")
        return True
    if node.role != "AXScrollArea":
        return False
    for child in node.children:
        if child.role == "AXScrollBar" and child.visible:
            return True
        if node.frame is not None and child.frame is not None and _overflows(child.frame, node.frame):
            return True
    return False


def _overflows(inner: Rect, outer: Rect) -> bool:
    return (
        inner.x < outer.x - 0.5
        or inner.y < outer.y - 0.5
        or inner.x + inner.w > outer.x + outer.w + 0.5
        or inner.y + inner.h > outer.y + outer.h + 0.5
    )


def collection_rows(node: RawNode) -> int:
    """Visible rows of a list/table/outline, plus children cut off by capture budgets."""
    rows = sum(1 for child in node.children if child.role == "AXRow" and child.visible)
    if rows == 0 and node.role == "AXList":
        rows = sum(1 for child in node.children if child.visible and child.role != "AXScrollBar")
    if node.child_count is not None and node.child_count > len(node.children):
        rows += node.child_count - len(node.children)
    return rows


def menu_titles(menu_bar: RawNode | None) -> list[str]:
    """Top-level menu titles, without the Apple menu."""
    if menu_bar is None:
        return []
    titles: list[str] = []
    for index, item in enumerate(menu_bar.children):
        title = clean_text(item.title)
        if not title or (index == 0 and title == "Apple"):
            continue
        titles.append(title)
    return titles


# --------------------------------------------------------------------------- builder
def _child_viewport(node: RawNode, element: Element | None, scope: _Scope) -> Rect | None:
    """The viewport children inherit: a collapsed scroll area narrows it; a listed element
    resets it (its own frame and ``parent_ref`` describe the nesting from there on)."""
    if element is not None:
        return None
    if node.role == "AXScrollArea" and node.frame is not None:
        if scope.viewport is None:
            return node.frame
        return scope.viewport.intersection(node.frame) or Rect(node.frame.x, node.frame.y, 0.0, 0.0)
    return scope.viewport


@dataclass(frozen=True, slots=True)
class _Scope:
    depth: int
    parent_ref: str | None
    in_modal: bool
    in_menu: bool
    scroll_hint: bool
    viewport: Rect | None = None
    """Frame of the nearest scroll area that was collapsed away (not listed as an element)."""


class _Builder:
    def __init__(self, tree: RawTree, mode: SnapshotMode, value_max_chars: int) -> None:
        self._tree = tree
        self._mode = mode
        self._value_max = value_max_chars
        self._window_id = tree.window.window_id if tree.window else None
        if tree.window is not None:
            self._bounds: Rect | None = tree.window.frame
        elif tree.root.role == "AXWindow":
            self._bounds = tree.root.frame
        else:
            self._bounds = None
        self.elements: list[Element] = []

    def run(self) -> list[Element]:
        root_scope = _Scope(depth=0, parent_ref=None, in_modal=False, in_menu=False, scroll_hint=False)
        self._visit(self._tree.root, (), root_scope, is_root=True)
        return self.elements

    def _visit(self, node: RawNode, path: Path, scope: _Scope, *, is_root: bool = False) -> None:
        if not node.visible or (node.role == "AXMenuBar" and self._mode != "full"):
            return
        element: Element | None = None
        if self._on_screen(node, scope) and self._wanted(node, scope, is_root=is_root):
            element = self._make_element(node, path, scope)
            self.elements.append(element)
        scrollable = is_scrollable(node)
        if element is not None:
            if scrollable or (scope.scroll_hint and node.role in SCROLL_CONTENT_ROLES):
                element.extra["scrollable"] = True
            if scope.viewport is not None and node.role in SCROLL_CONTENT_ROLES:
                # The scroll area itself is not listed, so remember the part of the screen it
                # shows: a table's own frame spans its whole content, most of it off-screen.
                element.extra["viewport"] = scope.viewport.to_list()
            if self._mode == "text" and node.role == "AXHeading":
                return  # the heading's text is its content; don't repeat it
        child_scope = _Scope(
            depth=scope.depth + 1 if element is not None else scope.depth,
            parent_ref=element.ref if element is not None else scope.parent_ref,
            in_modal=scope.in_modal or is_modal(node),
            in_menu=scope.in_menu or node.role == "AXMenu",
            scroll_hint=element is None and (scrollable or scope.scroll_hint),
            viewport=_child_viewport(node, element, scope),
        )
        for child, child_path in child_paths(node, path):
            self._visit(child, child_path, child_scope)

    def _on_screen(self, node: RawNode, scope: _Scope) -> bool:
        frame = node.frame
        if frame is None:
            return True
        if frame.w <= 0 or frame.h <= 0:
            return False
        return self._bounds is None or scope.in_menu or frame.intersects(self._bounds)

    def _wanted(self, node: RawNode, scope: _Scope, *, is_root: bool) -> bool:
        if self._mode == "full":
            return True
        if self._mode == "text":
            return node.role in READING_ROLES and node_name(node) is not None
        if is_root and node.role in ("AXWindow", "AXApplication"):
            return False
        if node.subrole in WINDOW_FRAME_SUBROLES:
            return False
        if is_interactive(node) or is_modal(node) or "scroll" in node.actions:
            return True  # a mobile scroller is kept even unnamed: it is what a swipe or find targets
        if node.role in CONTEXT_ROLES:
            return node_name(node) is not None
        return node.role == "AXStaticText" and scope.in_modal and node_name(node) is not None

    def _make_element(self, node: RawNode, path: Path, scope: _Scope) -> Element:
        secure = node.secure or node.subrole == "AXSecureTextField"
        name = node_name(node)
        whole = None if secure else value_text(node.value)
        value = truncate(whole, self._value_max) if whole is not None else None
        if node.role == "AXStaticText" and _own_label(node) is None:
            whole = value = None  # static text without a title: its value already is the name
        digest = None
        if whole is not None and value != whole:
            digest = hashlib.blake2b(whole.encode("utf-8", "surrogatepass"), digest_size=8).hexdigest()
        interactive = is_interactive(node)
        extra = self._extra(node, name)
        if node.role == "AXButton" and name is None and node.frame is not None:
            # MobilePilot change: an unlabeled icon (back arrow, ⋮, ×) gets where it is, so the
            # model can tell "top-left button" from "top-right button" without a screenshot.
            screen = self._tree.window.frame if self._tree.window is not None else self._tree.root.frame
            if screen is not None and screen.w and screen.h:
                extra["where"] = _where(node.frame.center, screen)
        return Element(
            ref=f"e{len(self.elements) + 1}",
            role=short_role(node.role, node.subrole),
            ax_role=node.role,
            subrole=node.subrole,
            name=name,
            value=value,
            frame=node.frame,
            depth=scope.depth,
            enabled=node.enabled,
            focused=node.focused,
            selected=node.selected,
            expanded=node.expanded,
            secure=secure,
            interactive=interactive,
            actions=tuple(node.actions),
            extra=extra,
            locator=Locator(
                pid=self._tree.app.pid,
                window_id=self._window_id,
                path=path,
                ax_role=node.role,
                identifier=node.identifier,
                name=name,
            ),
            handle=node.handle,
            parent_ref=scope.parent_ref,
            value_digest=digest,
        )

    @staticmethod
    def _extra(node: RawNode, name: str | None) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if node.identifier:
            extra["identifier"] = node.identifier
        if node.a11y_id:
            extra["a11y_id"] = node.a11y_id
        if node.native_class:
            extra["class"] = node.native_class
        text = node.title or node.label or (value_text(node.value) if node.role == "AXStaticText" else None)
        if text and not node.secure:
            extra["text"] = text  # the raw text attribute a text locator matches (not a composed row name)
        description = clean_text(node.description)
        if description and description != name:
            extra["description"] = truncate(description, NAME_MAX_CHARS)
        if node.url:
            extra["url"] = node.url
        if node.role in COLLECTION_ROLES:
            extra["rows"] = collection_rows(node)
        return extra


def _count_nodes(root: RawNode) -> int:
    return sum(1 for _ in root.walk())


def _where(c, screen) -> str:
    """``top-left`` … ``bottom-right`` by screen thirds (rows: top 15 %, bottom 15 %)."""
    fy, fx = (c.y - screen.y) / screen.h, (c.x - screen.x) / screen.w
    row = "top" if fy < 0.15 else "bottom" if fy > 0.85 else "middle"
    col = "left" if fx < 1 / 3 else "right" if fx > 2 / 3 else "centre"
    return f"{row}-{col}"


def build_snapshot(
    tree: RawTree,
    *,
    snapshot_id: str,
    mode: SnapshotMode = "interactive",
    max_elements: int = 150,
    offset: int = 0,
    value_max_chars: int = 200,
) -> Snapshot:
    """Prune and number ``tree`` (see the module docstring for the rules).

    ``elements`` is the page ``all_elements[offset:offset + max_elements]``; refs always
    resolve against ``all_elements``. Refs are local (``e1…eN`` in pre-order); pass the result
    through :func:`~mobilepilot.perception.refs.carry_refs` for session refs. Raises
    ``INVALID_ARGUMENT`` for a bad mode or limits.
    """
    if mode not in SNAPSHOT_MODES:
        raise MobilePilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"Unknown snapshot mode {mode!r}.",
            hint="Use one of: " + ", ".join(SNAPSHOT_MODES) + ".",
        )
    if max_elements < 1:
        raise MobilePilotError(
            ErrorCode.INVALID_ARGUMENT, f"max_elements must be at least 1, got {max_elements}."
        )
    if offset < 0:
        raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"offset must be 0 or more, got {offset}.")
    if value_max_chars < 1:
        raise MobilePilotError(
            ErrorCode.INVALID_ARGUMENT, f"value_max_chars must be at least 1, got {value_max_chars}."
        )
    all_elements = _Builder(tree, mode, value_max_chars).run()
    return Snapshot(
        snapshot_id=snapshot_id,
        app=tree.app,
        window=tree.window,
        mode=mode,
        elements=all_elements[offset : offset + max_elements],
        all_elements=all_elements,
        focused_ref=next((el.ref for el in all_elements if el.focused), None),
        total_nodes=tree.node_count or _count_nodes(tree.root),
        offset=offset,
        truncated=tree.truncated,
        menus=menu_titles(tree.menu_bar),
        created_at=time.monotonic(),
    )

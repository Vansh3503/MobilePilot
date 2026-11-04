"""Render snapshots and diffs as compact text (for ``content``) and JSON (for
``structuredContent``).

The text format is designed to be cheap in tokens and easy for a model to scan::

    app Notes (com.apple.Notes, pid 812) · window "Notes" [0,25 1440×875] · snapshot s_7f3a
    focused: e6 textarea "Note body"
    e1  button   "New Note"                (1180,52)
    e2  search   "Search"                  value=""  (1300,52)
    e3  list     "Notes"                   2 rows, scrollable
    e4    row    "Shopping list — 10:42"   selected  (344,133)
    e5    row    "Trip ideas — Yesterday"  (344,197)
    e6  textarea "Note body"               value="Milk, eggs, bread…"  focused  (971,488)
    menus: Notes · File · Edit · Format · View · Window · Help   (use select_menu)
    6 of 6 elements shown (interactive).

Mobile rows also carry the element's locators inline: ``aid`` (accessibility id), ``id=`` (Android
resource-id). Columns: ref, role (indented two spaces per level below the shallowest element on the page,
at most six levels), quoted name, then details: ``value=…``, facts such as ``12 rows`` or
``scrollable``, state words, and for interactive elements the center in screen points.
Paged output ends with ``More: offset=N.``. Values are cut to :data:`LINE_VALUE_MAX`
characters in text (the JSON payload keeps the snapshot's full value).
"""

from __future__ import annotations

import json
from typing import Any

from mobilepilot.models import Point, Rect
from mobilepilot.perception.diff import element_changes
from mobilepilot.perception.model import Element, Snapshot, SnapshotDiff
from mobilepilot.perception.snapshot import truncate

LINE_VALUE_MAX = 80
"""Values longer than this are cut with ``…`` in text output (JSON keeps the full value)."""
NAME_COLUMN_MAX = 28
"""Names are padded up to this width so the detail columns line up."""
MAX_INDENT_LEVELS = 6
DIFF_TEXT_MAX = 60


# --------------------------------------------------------------------------- primitives
def quote(text: str, limit: int | None = None) -> str:
    """Double-quote ``text`` with JSON escaping (newlines become ``\\n``), optionally truncated."""
    if limit is not None:
        text = truncate(text, limit)
    return json.dumps(text, ensure_ascii=False)


def _num(value: float) -> str:
    rounded = round(value)
    return "0" if rounded == 0 else str(rounded)


def format_point(point: Point) -> str:
    """``(x,y)`` rounded to whole points."""
    return f"({_num(point.x)},{_num(point.y)})"


def format_rect(rect: Rect) -> str:
    """``[x,y w×h]`` rounded to whole points."""
    return f"[{_num(rect.x)},{_num(rect.y)} {_num(rect.w)}×{_num(rect.h)}]"


def _details(el: Element) -> list[str]:
    parts: list[str] = []
    if el.value is not None and el.checked is None and el.value != el.name:
        parts.append(f"value={quote(el.value, LINE_VALUE_MAX)}")
    facts: list[str] = []
    rows = el.extra.get("rows")
    if isinstance(rows, int):
        facts.append(f"{rows} row" if rows == 1 else f"{rows} rows")
    if el.extra.get("scrollable"):
        facts.append("scrollable")
    if facts:
        parts.append(", ".join(facts))
    if el.states:
        parts.append(", ".join(el.states))
    where = el.extra.get("where")
    if where:
        parts.append(f"unlabeled, {where}")
    parts.extend(locator_hints(el))
    center = el.center
    if el.interactive and center is not None:
        parts.append(format_point(center))
    return parts


def locator_hints(el: Element) -> list[str]:
    """The element's best locators, inline, so no separate locator call is needed: ``aid`` is the
    accessibility id (bare when it equals the shown name), ``id=`` the Android resource-id after
    ``:id/`` (``mobile_locate`` gives the full one and grades it)."""
    hints: list[str] = []
    a11y = el.extra.get("a11y_id")
    if isinstance(a11y, str) and a11y:
        hints.append("aid" if a11y == el.name else f"aid={quote(a11y, LINE_VALUE_MAX)}")
    ident = el.extra.get("identifier")
    if isinstance(ident, str) and ident and ident != a11y:
        hints.append("id=" + ident.rpartition(":id/")[2])
    return hints


def _label(el: Element, *, with_ref: bool = True) -> str:
    head = f"{el.ref} {el.role}" if with_ref else el.role
    return f"{head} {quote(el.name)}" if el.name else head


def format_element_line(el: Element) -> str:
    """One element on one line, without column padding (for ``find`` results and diffs)."""
    return "  ".join([_label(el), *_details(el)])


# --------------------------------------------------------------------------- snapshots
def _header(snap: Snapshot) -> str:
    app = snap.app
    ident = f"{app.bundle_id}, pid {app.pid}" if app.bundle_id else f"pid {app.pid}"
    parts = [f"app {app.name} ({ident})"]
    if snap.window is not None:
        parts.append(f"screen {quote(snap.window.title)} {format_rect(snap.window.frame)}")
    else:
        parts.append("no screen")
    parts.append(f"snapshot {snap.snapshot_id}")
    return " · ".join(parts)


def _element_lines(elements: list[Element]) -> list[str]:
    if not elements:
        return []
    base = min(el.depth for el in elements)
    indents = ["  " * min(el.depth - base, MAX_INDENT_LEVELS) for el in elements]
    names = [quote(el.name) if el.name else "" for el in elements]
    ref_width = max(len(el.ref) for el in elements) + 2
    role_width = max(len(indent) + len(el.role) for el, indent in zip(elements, indents, strict=True)) + 1
    name_width = min(max(len(name) for name in names), NAME_COLUMN_MAX)
    lines: list[str] = []
    for el, indent, name in zip(elements, indents, names, strict=True):
        line = el.ref.ljust(ref_width) + (indent + el.role).ljust(role_width) + name
        details = _details(el)
        if details:
            line = line.ljust(ref_width + role_width + name_width) + "  " + "  ".join(details)
        lines.append(line.rstrip())
    return lines


def _footer(snap: Snapshot) -> str:
    text = f"{len(snap.elements)} of {snap.total} elements shown ({snap.mode}"
    text += f", offset {snap.offset})." if snap.offset else ")."
    if snap.next_offset is not None:
        text += f" More: offset={snap.next_offset}."
    if snap.mode == "interactive" and (snap.total == 0 or (
            snap.total <= 2 and not any(el.name for el in snap.all_elements))):  # a dialog's named buttons are fine
        text += (' Little or nothing in the tree: if the screen shows more, it is drawn without accessibility (a game, Flutter, a canvas'
                 ' or a thin web view). Try mode="full"; else mobile_screenshot(annotate=true, show=true) and tap'
                 ' its pixels with coordinate_space="screenshot".')
    if snap.truncated:
        text += " The screen was too large to capture completely; narrow it with filter or use mobile_find."
    return text


def format_snapshot(snap: Snapshot) -> str:
    """The compact text view of a snapshot (header, focus, one line per element, footer)."""
    lines = [_header(snap)]
    focused = snap.focused_element
    if focused is not None:
        lines.append(f"focused: {_label(focused)}")
    lines.extend(_element_lines(snap.elements))
    lines.append(_footer(snap))
    return "\n".join(lines)


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_json_safe(item) for item in value]
    return str(value)


def element_payload(el: Element) -> dict[str, Any]:
    """JSON-safe dict for one element (the handle is never included)."""
    center = el.center
    return {
        "ref": el.ref,
        "role": el.role,
        "name": el.name,
        "value": el.value,
        "frame": el.frame.to_list() if el.frame is not None else None,
        "center": center.to_list() if center is not None else None,
        "states": list(el.states),
        "actions": list(el.actions),
        "depth": el.depth,
        "parent_ref": el.parent_ref,
        "extra": _json_safe(el.extra),
    }


def snapshot_payload(snap: Snapshot) -> dict[str, Any]:
    """JSON-safe dict for ``structuredContent`` describing the current page of ``snap``."""
    window = snap.window
    return {
        "snapshot_id": snap.snapshot_id,
        "app": {"name": snap.app.name, "bundle_id": snap.app.bundle_id, "pid": snap.app.pid},
        "window": (
            {"window_id": window.window_id, "title": window.title, "frame": window.frame.to_list()}
            if window is not None
            else None
        ),
        "mode": snap.mode,
        "focused_ref": snap.focused_ref,
        "elements": [element_payload(el) for el in snap.elements],
        "shown": len(snap.elements),
        "total": snap.total,
        "offset": snap.offset,
        "next_offset": snap.next_offset,
        "truncated": snap.truncated,
        "menus": list(snap.menus),
    }


# --------------------------------------------------------------------------- diffs
def _quote_or_none(text: str | None) -> str:
    return quote(text, DIFF_TEXT_MAX) if text is not None else "none"


def _describe_change(after: Element, field: str, old: Any, new: Any) -> str:
    if field == "name":
        return f"name: {_quote_or_none(old)} → {_quote_or_none(new)}"
    if field == "value":
        if after.checked is not None:
            return "checked" if after.checked else "unchecked"
        if old == new:
            return "value changed past the part shown (read shows all of it)"
        return f"value: {_quote_or_none(old)} → {_quote_or_none(new)}"
    if field == "enabled":
        return "enabled" if new else "disabled"
    if field == "selected":
        return "selected" if new else "unselected"
    if field == "focused":
        return "focused" if new else "unfocused"
    if field == "expanded":
        return {True: "expanded", False: "collapsed"}.get(new, "no longer expandable")
    return f"{field}: {old!r} → {new!r}"


def _change_lines(diff: SnapshotDiff, *, hide_focus: bool) -> list[str]:
    lines: list[str] = []
    for before, after in diff.changed:
        changes = element_changes(before, after)
        if hide_focus:
            changes.pop("focused", None)
        if not changes:
            continue
        words = [_describe_change(after, field, old, new) for field, (old, new) in changes.items()]
        name = f" {quote(after.name, DIFF_TEXT_MAX)}" if after.name else ""
        lines.append(f"~ {after.ref} {after.role}{name} " + ", ".join(words))
    return lines


def format_diff(diff: SnapshotDiff, max_items: int = 30) -> str:
    """Concise text description of a diff, at most ``max_items`` element lines."""
    if diff.is_empty():
        return f"No changes since {diff.before_id} (now {diff.after_id})."
    focus_moved = diff.focus_before != diff.focus_after
    added = ["+ " + format_element_line(el) for el in diff.added]
    if (diff.app_changed or diff.window_changed) and diff.removed:
        count = len(diff.removed)
        where = "app" if diff.app_changed else "window"
        removed = [f"- {count} element{'s' if count != 1 else ''} of the previous {where}"]
    else:
        removed = ["- " + _label(el, with_ref=False) for el in diff.removed]
    changed = _change_lines(diff, hide_focus=focus_moved)
    # only counts that are not zero: "0 added, 0 removed, 0 changed" after a focus move says nothing
    counts = ", ".join(f"{n} {what}" for n, what in ((len(added), "added"), (len(diff.removed), "removed"),
                                                      (len(changed), "changed")) if n)
    lines = [f"Changes since {diff.before_id} (now {diff.after_id})" + (f": {counts}" if counts else "")]
    if diff.app_changed:
        lines.append(f"app: {diff.app_before or '?'} → {diff.app_after or '?'}")
    if diff.window_changed:
        lines.append(f"screen: {_quote_or_none(diff.window_before)} → {_quote_or_none(diff.window_after)}")
    if focus_moved:
        target = f"{diff.focus_after_ref} " if diff.focus_after_ref and diff.focus_after else ""
        lines.append(
            f"focus: {_quote_or_none(diff.focus_before)} → {target}{_quote_or_none(diff.focus_after)}"
        )
    items = added + removed + changed
    lines.extend(items[: max(max_items, 0)])
    hidden = len(items) - max(max_items, 0)
    if hidden > 0:
        lines.append(f"… {hidden} more change{'s' if hidden != 1 else ''} (call mobile_snapshot for the full view).")
    return "\n".join(lines)


def _diff_element(el: Element) -> dict[str, Any]:
    center = el.center
    return {
        "ref": el.ref,
        "role": el.role,
        "name": el.name,
        "value": el.value,
        "center": center.to_list() if center is not None else None,
        "states": list(el.states),
    }


def diff_payload(diff: SnapshotDiff, max_items: int = 50) -> dict[str, Any]:
    """JSON-safe dict describing ``diff``. Each list holds at most ``max_items`` entries;
    ``counts`` has the full sizes. Refs of ``removed`` belong to the ``before_id`` snapshot."""
    limit = max(max_items, 0)
    changed = [
        {
            "ref": after.ref,
            "role": after.role,
            "name": after.name,
            "changes": {
                field: [_json_safe(old), _json_safe(new)]
                for field, (old, new) in element_changes(before, after).items()
            },
        }
        for before, after in diff.changed
    ]
    return {
        "before_id": diff.before_id,
        "after_id": diff.after_id,
        "empty": diff.is_empty(),
        "app_changed": diff.app_changed,
        "app_before": diff.app_before,
        "app_after": diff.app_after,
        "window_changed": diff.window_changed,
        "window_before": diff.window_before,
        "window_after": diff.window_after,
        "focus_before": diff.focus_before,
        "focus_after": diff.focus_after,
        "focus_after_ref": diff.focus_after_ref,
        "added": [_diff_element(el) for el in diff.added[:limit]],
        "removed": [_diff_element(el) for el in diff.removed[:limit]],
        "changed": changed[:limit],
        "counts": {"added": len(diff.added), "removed": len(diff.removed), "changed": len(diff.changed)},
        "truncated": max(len(diff.added), len(diff.removed), len(diff.changed)) > limit,
    }

"""Compare two snapshots of the same app ("what did my action change?").

When both snapshots carry session refs from the same counter (see
:mod:`mobilepilot.perception.refs`), elements with the **same ref** are paired first: refs are
carried from snapshot to snapshot, so an equal ref means the same element. The remaining
elements are matched in passes, strongest evidence first; each element is matched at most once:

1. same ``AXIdentifier`` (and AX role);
2. same locator path *and* same name;
3. same AX role, name and frame (rounded to whole points);
4. same AX role and name, when that pair is unique among the unmatched elements on both sides
   (handles elements that moved because something was inserted above them);
5. same locator path alone, when the frames overlap (a label whose text changed, or a
   Play button that became Pause). With shared refs this pass only pairs *unnamed*
   elements: ref carrying already kept the ref of a named element that is still there (a
   renamed button keeps its engine handle), so a named element with another ref at the same
   place is another element (a list row that now shows other content), never a rename.

Unmatched elements of ``after`` are *added*, unmatched elements of ``before`` are *removed*,
and matched pairs whose name, value or state differ are *changed*. When the app or window
differs between the snapshots nothing is matched: every element of ``after`` is new to the
agent, and pairing look-alike controls of two different windows would only mislead it.

The passes run on the matching engine shared with ref carrying
(:class:`~mobilepilot.perception.refs.ElementMatcher`).
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from mobilepilot.perception.model import Element, Snapshot, SnapshotDiff
from mobilepilot.perception.refs import ElementMatcher, MatchPass, rounded_frame

_COMPARED_FIELDS: tuple[str, ...] = ("name", "value", "enabled", "selected", "focused", "expanded")


def element_changes(before: Element, after: Element) -> dict[str, tuple[Any, Any]]:
    """Fields (name, value and states) that differ, as ``{field: (before, after)}`` in a fixed order.
    A value whose shown part is the same but whose whole text differs is changed too (both sides
    then read the same)."""
    changes: dict[str, tuple[Any, Any]] = {}
    for field in _COMPARED_FIELDS:
        old, new = getattr(before, field), getattr(after, field)
        if old != new or (field == "value" and before.value_digest != after.value_digest):
            changes[field] = (old, new)
    return changes


def _by_identifier(el: Element) -> Hashable | None:
    return (el.ax_role, el.identifier) if el.identifier else None


def _by_path_and_name(el: Element) -> Hashable | None:
    return (el.locator.window_id, el.locator.path, el.name)


def _by_role_name_frame(el: Element) -> Hashable | None:
    frame = rounded_frame(el)
    return (el.ax_role, el.name, frame) if frame is not None else None


def _by_role_and_name(el: Element) -> Hashable | None:
    return (el.ax_role, el.name) if el.name else None


def _by_path(el: Element) -> Hashable | None:
    return (el.locator.window_id, el.locator.path)


def _frames_overlap(before: Element, after: Element) -> bool:
    if before.frame is None or after.frame is None:
        return True
    return before.frame.intersects(after.frame)


DIFF_PASSES: tuple[MatchPass, ...] = (
    MatchPass("identifier", key=_by_identifier),
    MatchPass("path+name", key=_by_path_and_name),
    MatchPass("role+name+frame", key=_by_role_name_frame),
    MatchPass("role+name", key=_by_role_and_name, unique=True),
    MatchPass("path", key=_by_path, accept=_frames_overlap),
)
"""Passes 1-5 of the module docstring."""


SHARED_REF_PASSES: tuple[MatchPass, ...] = ()
"""MobilePilot change: no passes after pairing equal refs. Ref carrying already decided identity
with stricter evidence (unique ids, a row's own ref for what is inside it). Android reuses
resource-ids across rows (``android:id/switch_widget`` in every toggle row), so AXPilot's extra
passes paired the Airplane-mode switch with the Wi-Fi switch and reported it "changed"."""


def shares_refs(before: Snapshot, after: Snapshot) -> bool:
    """True if both snapshots were numbered by the same ref counter (equal ref, same element)."""
    return before.ref_session is not None and before.ref_session == after.ref_session


def match_snapshots(before: Snapshot, after: Snapshot) -> dict[int, int]:
    """Indices of ``after.all_elements`` → matched indices of ``before.all_elements``."""
    matcher = ElementMatcher(before.all_elements, after.all_elements)
    if not shares_refs(before, after):
        return matcher.run(DIFF_PASSES)
    index_of = {el.ref: j for j, el in enumerate(before.all_elements)}
    for i, el in enumerate(after.all_elements):
        j = index_of.get(el.ref)
        if j is not None:
            matcher.pair(i, j)
    return matcher.run(SHARED_REF_PASSES)


def _focus_label(el: Element | None) -> str | None:
    if el is None:
        return None
    return el.name or el.role


def diff_snapshots(before: Snapshot, after: Snapshot) -> SnapshotDiff:
    """Compute what changed from ``before`` to ``after`` (see the module docstring)."""
    window_before = before.window.window_id if before.window else None
    window_after = after.window.window_id if after.window else None
    app_changed = (before.app.pid, before.app.bundle_id) != (after.app.pid, after.app.bundle_id)
    window_changed = window_before != window_after
    same_view = not (app_changed or window_changed)
    pairs = match_snapshots(before, after) if same_view else {}
    matched_before = set(pairs.values())
    added = [el for i, el in enumerate(after.all_elements) if i not in pairs]
    removed = [el for j, el in enumerate(before.all_elements) if j not in matched_before]
    changed = [
        (before.all_elements[pairs[i]], after.all_elements[i])
        for i in sorted(pairs)
        if element_changes(before.all_elements[pairs[i]], after.all_elements[i])
    ]
    focus_before, focus_after = before.focused_element, after.focused_element
    return SnapshotDiff(
        before_id=before.snapshot_id,
        after_id=after.snapshot_id,
        app_changed=app_changed,
        window_changed=window_changed,
        focus_before=_focus_label(focus_before),
        focus_after=_focus_label(focus_after),
        added=added,
        removed=removed,
        changed=changed,
        app_before=before.app.name,
        app_after=after.app.name,
        window_before=before.window.title if before.window else None,
        window_after=after.window.title if after.window else None,
        focus_after_ref=focus_after.ref if focus_after else None,
    )

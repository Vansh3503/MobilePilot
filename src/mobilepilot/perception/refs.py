"""Element refs that are unique per session and stay valid across snapshots.

Every element an agent sees gets a short ref: ``e12`` for accessibility elements, ``o3`` for
OCR text (see :mod:`mobilepilot.perception.vision`). AXPilot keeps three promises about them:

1. **Unique per session.** Numbers come from one :class:`RefCounter` per server context (it
   lives in the :class:`~mobilepilot.perception.store.SnapshotStore`). A number is issued once and
   is never reused for another element: not after that element disappears, not in another
   window, not in another app.
2. **Stable.** When an app window is captured again, :func:`carry_refs` gives every element
   that is still there the ref it had, so the refs an agent holds keep working after its own
   actions re-snapshot the window.
3. **Never ambiguous.** A ref is carried only to an element that matches its earlier holder
   by the evidence below; everything else gets a fresh number. A ref whose element is gone
   therefore cannot silently hit another element: resolving it gives ``STALE_REF``.

Matching, strongest evidence first (each earlier element is used at most once):

1. the same engine handle (``==``; for the macOS engine ``CFEqual`` on the ``AXUIElement``)
   and the same AX role;
2. the same ``AXIdentifier`` and AX role, when unique among the unmatched elements on both sides;
3. the same AX role, locator path and name (named elements only);
4. the same AX role, name and frame (rounded to whole points);
5. the same AX role and name, when that pair is unique among the unmatched elements on both sides;
6. *position*, for **unnamed** elements only, under a parent that kept its ref: the same AX
   role at the same locator path with overlapping frames, or else the same AX role with frames
   overlapping by at least :data:`OVERLAP_MIN_IOU` (intersection over union). A named element
   is never matched by position alone.

List rows get extra care because table views recycle them for other content: rows are
matched first; a row whose name (its content) changed never keeps its ref, even with the same
handle; and an element inside a row only keeps its ref when its row kept the row's ref.

The previous snapshot of the same app window gets every pass. Older snapshots of that window
(``history``) are searched by handle only, so an element that was briefly scrolled out of view,
or left out of a ``text``-mode snapshot, gets its old ref back. Refs never cross apps or windows.

The matching engine (:class:`ElementMatcher` with :class:`MatchPass` rules) is shared with
:func:`mobilepilot.perception.diff.diff_snapshots`, which uses its own passes.
"""

from __future__ import annotations

import bisect
import dataclasses
import functools
import logging
import re
import secrets
import threading
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from mobilepilot.models import Rect
from mobilepilot.perception.model import Element, Snapshot
from mobilepilot.perception.vision import is_vision_element

__all__ = [
    "AX_PREFIX",
    "CARRY_PASSES",
    "HISTORY_LIMIT",
    "OCR_PREFIX",
    "OVERLAP_MIN_IOU",
    "ROW_AX_ROLES",
    "ElementMatcher",
    "HandleIndex",
    "MatchPass",
    "RefCounter",
    "carry_refs",
    "iou",
    "rounded_frame",
    "same_handle",
    "split_ref",
    "view_of",
]

_log = logging.getLogger(__name__)

AX_PREFIX = "e"
"""Prefix of accessibility element refs."""
OCR_PREFIX = "o"
"""Prefix of OCR text refs."""
ROW_AX_ROLES: frozenset[str] = frozenset({"AXRow"})
"""Roles that table/outline views recycle for other content (see the module docstring)."""
OVERLAP_MIN_IOU = 0.5
"""Unnamed elements at another path still match when their frames overlap at least this much."""
HISTORY_LIMIT = 8
"""How many older snapshots of a window :class:`~mobilepilot.server.Session` searches by handle."""

_REF = re.compile(r"^([a-z])([1-9][0-9]*)$")
_PREFIX = re.compile(r"^[a-z]$")

View = tuple[int, int | None]
"""``(pid, window_id)``: refs are carried only between snapshots of the same view."""


def split_ref(ref: str) -> tuple[str, int] | None:
    """``("e", 12)`` for ``"e12"``; ``None`` for anything that is not a plain ref."""
    match = _REF.match(ref) if isinstance(ref, str) else None
    return (match.group(1), int(match.group(2))) if match else None


def view_of(snap: Snapshot) -> View:
    """The app (pid) and window a snapshot shows."""
    return (snap.app.pid, snap.window.window_id if snap.window is not None else None)


# --------------------------------------------------------------------------- the counter
class RefCounter:
    """Thread-safe source of session-unique refs: ``e1``, ``e2``, … and ``o1``, ``o2``, …

    Each prefix has its own sequence. :attr:`session` identifies the counter, so code that
    compares two snapshots can tell whether their refs come from the same sequence."""

    def __init__(self) -> None:
        self.session = secrets.token_hex(6)
        self._issued: dict[str, int] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _check(prefix: str) -> None:
        if not _PREFIX.match(prefix):
            raise ValueError(f"A ref prefix is one lowercase letter, got {prefix!r}.")

    def next(self, prefix: str = AX_PREFIX) -> str:
        """A ref that was never issued before (``"e57"``)."""
        self._check(prefix)
        with self._lock:
            number = self._issued.get(prefix, 0) + 1
            self._issued[prefix] = number
        return f"{prefix}{number}"

    def allocator(self, prefix: str = AX_PREFIX) -> Callable[[], str]:
        """A zero-argument function issuing refs with ``prefix`` (e.g. for ``merge_vision``)."""
        self._check(prefix)
        return functools.partial(self.next, prefix)

    def issued(self, prefix: str = AX_PREFIX) -> int:
        """The highest number issued (or reserved) for ``prefix`` so far; ``0`` if none."""
        with self._lock:
            return self._issued.get(prefix, 0)

    def reserve(self, refs: Iterable[str]) -> None:
        """Never issue the numbers used by ``refs`` (refs numbered elsewhere, e.g. by a plain
        :func:`~mobilepilot.perception.snapshot.build_snapshot`)."""
        highest: dict[str, int] = {}
        for ref in refs:
            parts = split_ref(ref)
            if parts is not None:
                prefix, number = parts
                highest[prefix] = max(number, highest.get(prefix, 0))
        if not highest:
            return
        with self._lock:
            for prefix, number in highest.items():
                if number > self._issued.get(prefix, 0):
                    self._issued[prefix] = number


# --------------------------------------------------------------------------- handles
def same_handle(a: Any, b: Any) -> bool:
    """True if two engine handles denote the same UI element (``None`` never matches).

    A handle whose ``==`` raises is treated as different (and logged at debug level)."""
    if a is None or b is None:
        return False
    if a is b:
        return True
    try:
        return bool(a == b)
    except Exception as exc:  # an engine handle we cannot compare is "not the same", never a crash
        _log.debug("Cannot compare element handles (%s, %s): %s", type(a).__name__, type(b).__name__, exc)
        return False


class HandleIndex:
    """Looks up positions by engine handle: hashed when the handle is hashable, a linear scan
    otherwise (PyObjC wrappers compare with ``CFEqual``). Hashing and comparing are guarded:
    a handle that cannot be hashed (``TypeError``) or whose ``==`` fails is compared one by one
    with :func:`same_handle` instead."""

    def __init__(self, entries: Iterable[tuple[Any, int]]) -> None:
        self._hashed: dict[Any, list[int]] = {}
        self._unhashed: list[tuple[Any, int]] = []
        for handle, index in entries:
            if handle is None:
                continue
            try:
                self._hashed.setdefault(handle, []).append(index)
            except TypeError:
                self._unhashed.append((handle, index))
            except Exception as exc:  # == raised while probing a hash collision: keep it out of the dict
                _log.debug("Element handle %s cannot be indexed: %s", type(handle).__name__, exc)
                self._unhashed.append((handle, index))

    def find(self, handle: Any) -> list[int]:
        """Positions whose handle equals ``handle``, in ascending order."""
        if handle is None:
            return []
        try:
            found = list(self._hashed.get(handle, ()))
        except Exception as exc:  # unhashable key, or == raised: compare one by one
            if not isinstance(exc, TypeError):
                _log.debug("Element handle %s cannot be looked up: %s", type(handle).__name__, exc)
            found = [i for key, indices in self._hashed.items() if same_handle(key, handle) for i in indices]
        found.extend(i for key, i in self._unhashed if same_handle(key, handle))
        return sorted(found)


# --------------------------------------------------------------------------- matching engine
PassKind = Literal["handle", "key", "overlap"]
KeyFn = Callable[[Element], Hashable | None]


@dataclass(frozen=True, slots=True)
class MatchPass:
    """One rule of an :class:`ElementMatcher`.

    ``kind="handle"`` pairs equal engine handles; ``"key"`` pairs equal (non-``None``) keys;
    ``"overlap"`` pairs elements with equal keys whose frames overlap by at least
    :data:`OVERLAP_MIN_IOU`, best overlap first."""

    name: str
    kind: PassKind = "key"
    key: KeyFn | None = None
    unique: bool = False
    """``key`` passes: only match when the key occurs once among the unmatched elements on each side."""
    accept: Callable[[Element, Element], bool] | None = None
    """Extra condition on a ``(before, after)`` pair."""
    structural: bool = False
    """Position-like evidence: the matcher's ``allowed`` hook may require a consistent parent."""


Allowed = Callable[[MatchPass, int, int], bool]
"""``(pass, before_index, after_index) -> bool``: a veto the caller can put on any pair."""


def rounded_frame(el: Element) -> tuple[int, int, int, int] | None:
    """The element's frame rounded to whole points (``None`` without a frame)."""
    if el.frame is None:
        return None
    return (round(el.frame.x), round(el.frame.y), round(el.frame.w), round(el.frame.h))


def iou(a: Rect, b: Rect) -> float:
    """Intersection over union of two rectangles (``0.0`` when they do not overlap)."""
    inter = a.intersection(b)
    if inter is None:
        return 0.0
    union = a.area + b.area - inter.area
    return inter.area / union if union > 0 else 0.0


class ElementMatcher:
    """Greedy multi-pass matching of ``after`` elements onto ``before`` elements.

    Passes run in order; each element is matched at most once; within a pass ``after``
    elements are visited in document order and take the first acceptable candidate (for
    ``overlap`` passes, the best one). :attr:`pairs` maps ``after`` indices to ``before`` indices.
    """

    def __init__(
        self,
        before: Sequence[Element],
        after: Sequence[Element],
        *,
        allowed: Allowed | None = None,
        on_match: Callable[[int, int], None] | None = None,
    ) -> None:
        self.before = before
        self.after = after
        self.pairs: dict[int, int] = {}
        self._taken: set[int] = set()
        self._allowed = allowed
        self._on_match = on_match

    def pair(self, after_index: int, before_index: int) -> None:
        """Record a match decided outside the passes (e.g. equal refs)."""
        self.pairs[after_index] = before_index
        self._taken.add(before_index)
        if self._on_match is not None:
            self._on_match(before_index, after_index)

    def run(
        self,
        passes: Sequence[MatchPass],
        *,
        before_indices: Iterable[int] | None = None,
        after_indices: Iterable[int] | None = None,
    ) -> dict[int, int]:
        """Apply ``passes`` to the given subsets (default: everything); returns :attr:`pairs`."""
        before_ids = list(range(len(self.before)) if before_indices is None else before_indices)
        after_ids = list(range(len(self.after)) if after_indices is None else after_indices)
        for match_pass in passes:
            free_before = [j for j in before_ids if j not in self._taken]
            free_after = [i for i in after_ids if i not in self.pairs]
            if not free_before or not free_after:
                break
            if match_pass.kind == "handle":
                self._handle_pass(match_pass, free_before, free_after)
            elif match_pass.kind == "overlap":
                self._overlap_pass(match_pass, free_before, free_after)
            else:
                self._key_pass(match_pass, free_before, free_after)
        return self.pairs

    def _acceptable(self, match_pass: MatchPass, j: int, i: int) -> bool:
        if match_pass.accept is not None and not match_pass.accept(self.before[j], self.after[i]):
            return False
        return self._allowed is None or self._allowed(match_pass, j, i)

    def _handle_pass(self, match_pass: MatchPass, free_before: list[int], free_after: list[int]) -> None:
        index = HandleIndex((self.before[j].handle, j) for j in free_before)
        for i in free_after:
            for j in index.find(self.after[i].handle):
                if j not in self._taken and self._acceptable(match_pass, j, i):
                    self.pair(i, j)
                    break

    def _key_pass(self, match_pass: MatchPass, free_before: list[int], free_after: list[int]) -> None:
        key_fn = match_pass.key
        if key_fn is None:
            raise ValueError(f"Match pass {match_pass.name!r} needs a key function.")
        candidates: dict[Hashable, list[int]] = {}
        for j in free_before:
            key = key_fn(self.before[j])
            if key is not None:
                candidates.setdefault(key, []).append(j)
        after_keys = {i: key_fn(self.after[i]) for i in free_after}
        after_counts = Counter(k for k in after_keys.values() if k is not None) if match_pass.unique else None
        for i in free_after:
            key = after_keys[i]
            found = candidates.get(key) if key is not None else None
            if not found:
                continue
            if after_counts is not None and (len(found) != 1 or after_counts[key] != 1):
                continue
            for j in found:
                if self._acceptable(match_pass, j, i):
                    found.remove(j)
                    self.pair(i, j)
                    break

    def _overlap_pass(self, match_pass: MatchPass, free_before: list[int], free_after: list[int]) -> None:
        """Best overlap per ``after`` element. Two boxes with IoU ≥ 0.5 contain each other's
        centers, so candidates are looked up by center (sorted by y, bisected), not compared all
        against all."""
        key_fn = match_pass.key
        if key_fn is None:
            raise ValueError(f"Match pass {match_pass.name!r} needs a key function.")
        buckets: dict[Hashable, list[tuple[float, float, int]]] = {}
        for j in free_before:
            frame = self.before[j].frame
            key = key_fn(self.before[j])
            if key is not None and frame is not None:
                center = frame.center
                buckets.setdefault(key, []).append((center.y, center.x, j))
        for bucket in buckets.values():
            bucket.sort()
        ys = {key: [y for y, _, _ in bucket] for key, bucket in buckets.items()}
        for i in free_after:
            frame = self.after[i].frame
            key = key_fn(self.after[i])
            bucket = buckets.get(key) if key is not None and frame is not None else None
            if not bucket or frame is None or key is None:
                continue
            start = bisect.bisect_left(ys[key], frame.y)
            stop = bisect.bisect_right(ys[key], frame.y + frame.h)
            best: int | None = None
            best_score = 0.0
            for _, x, j in bucket[start:stop]:
                before_frame = self.before[j].frame
                if j in self._taken or before_frame is None or not frame.x <= x <= frame.x + frame.w:
                    continue
                score = iou(before_frame, frame)
                if score >= OVERLAP_MIN_IOU and score > best_score and self._acceptable(match_pass, j, i):
                    best, best_score = j, score
            if best is not None:
                self.pair(i, best)


# --------------------------------------------------------------------------- carry passes
def _identifier(el: Element) -> Hashable | None:
    return (el.ax_role, el.identifier) if el.identifier else None


def _path_and_name(el: Element) -> Hashable | None:
    return (el.ax_role, el.locator.path, el.name) if el.name else None


def _role_name_frame(el: Element) -> Hashable | None:
    frame = rounded_frame(el)
    return (el.ax_role, el.name, frame) if el.name and frame is not None else None


def _role_and_name(el: Element) -> Hashable | None:
    return (el.ax_role, el.name) if el.name else None


def _unnamed_path(el: Element) -> Hashable | None:
    return None if el.name else (el.ax_role, el.locator.path)


def _unnamed_role(el: Element) -> Hashable | None:
    return None if el.name else el.ax_role


def _same_role(before: Element, after: Element) -> bool:
    return before.ax_role == after.ax_role


def _same_row_content(before: Element, after: Element) -> bool:
    """A recycled row shows other content under the same handle: its name tells."""
    return before.ax_role == after.ax_role and before.name == after.name


def _frames_overlap(before: Element, after: Element) -> bool:
    """Position evidence needs positions: both frames present and overlapping."""
    if before.frame is None or after.frame is None:
        return False
    return before.frame.intersects(after.frame)


def _passes(handle_accept: Callable[[Element, Element], bool]) -> tuple[MatchPass, ...]:
    return (
        MatchPass("handle", kind="handle", accept=handle_accept),
        MatchPass("identifier", key=_identifier, unique=True),
        MatchPass("path+name", key=_path_and_name),
        MatchPass("role+name+frame", key=_role_name_frame),
        MatchPass("role+name", key=_role_and_name, unique=True),
        MatchPass("position", key=_unnamed_path, accept=_frames_overlap, structural=True),
        MatchPass("overlap", kind="overlap", key=_unnamed_role, structural=True),
    )


CARRY_PASSES: tuple[MatchPass, ...] = _passes(_same_role)
"""Every pass, for elements outside list rows (and the elements inside rows)."""
_ROW_PASSES: tuple[MatchPass, ...] = _passes(_same_row_content)
_HISTORY_PASSES: tuple[MatchPass, ...] = (CARRY_PASSES[0],)
_ROW_HISTORY_PASSES: tuple[MatchPass, ...] = (_ROW_PASSES[0],)


# --------------------------------------------------------------------------- carrying
def _parent_indices(elements: Sequence[Element]) -> list[int | None]:
    index_of = {el.ref: k for k, el in enumerate(elements)}
    return [index_of.get(el.parent_ref) if el.parent_ref else None for el in elements]


def _nearest_row(elements: Sequence[Element], parents: Sequence[int | None], k: int) -> int | None:
    seen = {k}
    parent = parents[k]
    while parent is not None and parent not in seen:
        if elements[parent].ax_role in ROW_AX_ROLES:
            return parent
        seen.add(parent)
        parent = parents[parent]
    return None


def _row_indices(elements: Sequence[Element], parents: Sequence[int | None]) -> list[int | None]:
    """Index of the nearest *ancestor* row of each element (``None`` outside rows)."""
    return [_nearest_row(elements, parents, k) for k in range(len(elements))]


def _consistent(before_ref: str | None, after_index: int | None, assigned: dict[int, str]) -> bool:
    """Both sides have no such ancestor, or the ``after`` ancestor kept the ``before`` one's ref."""
    if before_ref is None or after_index is None:
        return before_ref is None and after_index is None
    return assigned.get(after_index) == before_ref


@dataclass
class _Carry:
    """State of one :func:`carry_refs` call: what the new snapshot's elements have inherited."""

    after: Sequence[Element]
    parents: list[int | None]
    rows: list[int | None]
    assigned: dict[int, str] = dataclasses.field(default_factory=dict)
    used: set[str] = dataclasses.field(default_factory=set)

    def inherit(self, old: Snapshot, *, full: bool) -> None:
        """Match the still-unassigned elements against ``old`` (every pass, or handles only)."""
        before = old.all_elements
        free_after = [
            i for i, el in enumerate(self.after) if i not in self.assigned and not is_vision_element(el)
        ]
        free_before = [
            j for j, el in enumerate(before) if el.ref not in self.used and not is_vision_element(el)
        ]
        if not free_after or not free_before:
            return
        before_rows = _row_indices(before, _parent_indices(before))
        row_refs = [before[row].ref if row is not None else None for row in before_rows]

        def allowed(match_pass: MatchPass, j: int, i: int) -> bool:
            if self.after[i].ax_role not in ROW_AX_ROLES and not _consistent(
                row_refs[j], self.rows[i], self.assigned
            ):
                return False
            return not match_pass.structural or _consistent(
                before[j].parent_ref, self.parents[i], self.assigned
            )

        def on_match(j: int, i: int) -> None:
            self.assigned[i] = before[j].ref
            self.used.add(before[j].ref)

        matcher = ElementMatcher(before, self.after, allowed=allowed, on_match=on_match)
        rows_before = [j for j in free_before if before[j].ax_role in ROW_AX_ROLES]
        rows_after = [i for i in free_after if self.after[i].ax_role in ROW_AX_ROLES]
        matcher.run(
            _ROW_PASSES if full else _ROW_HISTORY_PASSES, before_indices=rows_before, after_indices=rows_after
        )
        matcher.run(
            CARRY_PASSES if full else _HISTORY_PASSES,
            before_indices=[j for j in free_before if before[j].ax_role not in ROW_AX_ROLES],
            after_indices=[i for i in free_after if self.after[i].ax_role not in ROW_AX_ROLES],
        )


def _renumber(snap: Snapshot, assigned: dict[int, str], counter: RefCounter) -> Snapshot:
    mapping: dict[str, str] = {}
    for i, el in enumerate(snap.all_elements):
        if i in assigned:
            mapping[el.ref] = assigned[i]
        else:
            mapping[el.ref] = counter.next(OCR_PREFIX if is_vision_element(el) else AX_PREFIX)
    renamed = [
        dataclasses.replace(
            el,
            ref=mapping[el.ref],
            parent_ref=mapping.get(el.parent_ref) if el.parent_ref else None,
            extra=dict(el.extra),
        )
        for el in snap.all_elements
    ]
    by_old_ref = {old.ref: new for old, new in zip(snap.all_elements, renamed, strict=True)}
    page = [by_old_ref[el.ref] for el in snap.elements if el.ref in by_old_ref]
    return dataclasses.replace(
        snap,
        elements=page,
        all_elements=renamed,
        focused_ref=mapping.get(snap.focused_ref) if snap.focused_ref else None,
        ref_session=counter.session,
    )


def carry_refs(
    snap: Snapshot,
    previous: Snapshot | None,
    counter: RefCounter,
    *,
    history: Sequence[Snapshot] = (),
) -> Snapshot:
    """A copy of ``snap`` whose refs are session refs from ``counter``.

    Elements that match an element of ``previous`` (every pass of the module docstring) or of a
    ``history`` snapshot (same handle only; newest first) keep that element's ref; the others
    get fresh numbers, in document order (``e…`` for AX elements, ``o…`` for OCR text, which is
    never carried). Only snapshots of the same app window (:func:`view_of`) that ``counter``
    numbered (same ``ref_session``) are used; when ``previous`` does not qualify, the first
    qualifying ``history`` snapshot takes its place. ``parent_ref``, ``focused_ref`` and the
    page (``elements``) are remapped consistently, and the copy's ``ref_session`` is
    ``counter.session``. ``snap`` is not modified.
    """
    view = view_of(snap)
    candidates: list[Snapshot] = []
    for old in (previous, *history):
        if (
            old is None
            or old is snap
            or old.ref_session != counter.session
            or view_of(old) != view
            or any(old is c for c in candidates)
        ):
            continue
        candidates.append(old)
    parents = _parent_indices(snap.all_elements)
    carry = _Carry(after=snap.all_elements, parents=parents, rows=_row_indices(snap.all_elements, parents))
    for position, old in enumerate(candidates):
        carry.inherit(old, full=position == 0)
    return _renumber(snap, carry.assigned, counter)

"""Thread-safe LRU stores for snapshots and screenshots, addressed by short ids.

Ids look random (``s_7f3a``, ``c_k2p9``) but come from a counter mixed with a per-process
random salt: unique within a process (for 36⁴ ids, far beyond the store capacities) and
different across restarts, so an id held over from a previous server process is reported
as expired instead of silently resolving to an unrelated snapshot.

The :class:`SnapshotStore` also owns the session's :class:`~mobilepilot.perception.refs.RefCounter`:
element refs are numbered from it (see :mod:`mobilepilot.perception.refs`), so a ref names one
element for the whole session and :meth:`SnapshotStore.resolve` can look a bare ref up in every
stored snapshot, newest first.
"""

from __future__ import annotations

import itertools
import math
import re
import secrets
import threading
from collections import OrderedDict
from collections.abc import Callable

from mobilepilot.errors import MobilePilotError, ErrorCode
from mobilepilot.perception.model import Element, Snapshot
from mobilepilot.perception.refs import RefCounter, same_handle, split_ref, view_of
from mobilepilot.perception.vision import is_vision_element

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
_REF_PATTERN = re.compile(r"^(?:(?P<sid>s_[0-9a-z]{4,6}):)?(?P<ref>[eo][1-9][0-9]*)$")
"""Element refs: ``e12`` (accessibility elements) or ``o3`` (OCR text, see ``perception.vision``)."""


def _base36(value: int, width: int) -> str:
    digits: list[str] = []
    while value:
        value, digit = divmod(value, 36)
        digits.append(_ALPHABET[digit])
    return "".join(reversed(digits)).rjust(width, "0")


class IdFactory:
    """Produces ``<prefix><4 base-36 chars>`` ids: a salted permutation of a counter."""

    def __init__(self, prefix: str, width: int = 4) -> None:
        self._prefix = prefix
        self._width = width
        self._modulus = 36**width
        self._salt = secrets.randbelow(self._modulus)
        self._stride = self._pick_stride()
        self._counter = itertools.count()
        self._lock = threading.Lock()

    def _pick_stride(self) -> int:
        """A random stride coprime to the modulus (so the sequence is a full-period permutation),
        taken from the middle third of the range so consecutive ids never look sequential."""
        third = self._modulus // 3
        while True:
            stride = third + secrets.randbelow(third)
            if math.gcd(stride, self._modulus) == 1:
                return stride

    def next(self, taken: Callable[[str], bool] | None = None) -> str:
        """A fresh id; ``taken`` can veto ids still in use (only possible after wrap-around)."""
        with self._lock:
            for _ in range(self._modulus):
                n = next(self._counter)
                ident = self._prefix + _base36((self._salt + n * self._stride) % self._modulus, self._width)
                if taken is None or not taken(ident):
                    return ident
        raise MobilePilotError(ErrorCode.INTERNAL, f"Ran out of {self._prefix!r} ids.")  # pragma: no cover


def parse_ref(ref: str) -> tuple[str | None, str]:
    """Split ``"e12"``, ``"o3"`` or ``"s_ab12:e12"`` into ``(snapshot_id | None, "e12")``.

    Raises ``INVALID_ARGUMENT`` for anything else."""
    match = _REF_PATTERN.match(ref.strip().lower()) if isinstance(ref, str) else None
    if match is None:
        raise MobilePilotError(
            ErrorCode.INVALID_ARGUMENT,
            f"{ref!r} is not an element ref.",
            hint='Refs look like "e12" or "o3" (or "s_ab12:e12"); take them from the latest snapshot.',
        )
    return match.group("sid"), match.group("ref")


_SUMMARY_MAX_RUNS = 6


def describe_refs(snap: Snapshot) -> str:
    """The refs a snapshot lists, as compact runs: ``"e1–e12, e30, e41–e44, o7–o9"``."""
    numbers: dict[str, list[int]] = {}
    for el in snap.all_elements:
        parts = split_ref(el.ref)
        if parts is not None:
            numbers.setdefault(parts[0], []).append(parts[1])
    runs: list[str] = []
    for prefix in sorted(numbers, key=lambda p: (p != "e", p)):
        values = sorted(set(numbers[prefix]))
        start = previous = values[0]
        for value in [*values[1:], None]:
            if value is not None and value == previous + 1:
                previous = value
                continue
            runs.append(f"{prefix}{start}" if start == previous else f"{prefix}{start}–{prefix}{previous}")
            if value is not None:
                start = previous = value
    if not runs:
        return "no elements"
    shown = ", ".join(runs[:_SUMMARY_MAX_RUNS])
    return shown + (", …" if len(runs) > _SUMMARY_MAX_RUNS else "")


def _label(el: Element) -> str:
    return f'{el.role} "{el.name}"' if el.name else el.role


class SnapshotStore:
    """Keeps the most recently used snapshots so refs can be resolved by later tool calls.

    :attr:`refs` is the session's ref counter; snapshots put here should be numbered from it
    (``ServerContext.build_snapshot`` does that, through
    :func:`~mobilepilot.perception.refs.carry_refs`). A plain ``build_snapshot`` result (local refs,
    no ``ref_session``) is accepted for tools and tests that do not need session refs: its
    numbers are reserved so the counter never issues them afterwards."""

    def __init__(self, capacity: int = 32, *, refs: RefCounter | None = None) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._capacity = capacity
        self._items: OrderedDict[str, Snapshot] = OrderedDict()
        self._created: dict[str, int] = {}
        self._sequence = itertools.count()
        self._ids = IdFactory("s_")
        self._lock = threading.Lock()
        self.refs = refs if refs is not None else RefCounter()
        """Source of the session's element refs (``e…``) and OCR refs (``o…``)."""

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def __contains__(self, snapshot_id: object) -> bool:
        with self._lock:
            return snapshot_id in self._items

    def new_id(self) -> str:
        """A new, unused snapshot id (``"s_"`` + 4 lowercase alphanumerics)."""
        return self._ids.next(taken=self.__contains__)

    def put(self, snap: Snapshot) -> None:
        """Store ``snap`` (replacing one with the same id), evicting the least recently used.

        Raises ``ValueError`` for a snapshot numbered by another store's counter: its refs
        would collide with this session's."""
        if snap.ref_session is not None and snap.ref_session != self.refs.session:
            raise ValueError(f"Snapshot {snap.snapshot_id} was numbered by another session's ref counter.")
        self.refs.reserve(el.ref for el in snap.all_elements)
        with self._lock:
            self._items[snap.snapshot_id] = snap
            self._items.move_to_end(snap.snapshot_id)
            self._created[snap.snapshot_id] = next(self._sequence)
            while len(self._items) > self._capacity:
                evicted, _ = self._items.popitem(last=False)
                self._created.pop(evicted, None)

    def get(self, snapshot_id: str) -> Snapshot:
        """The snapshot with this id (marks it recently used). Raises ``SNAPSHOT_NOT_FOUND``."""
        key = snapshot_id.strip().lower() if isinstance(snapshot_id, str) else snapshot_id
        with self._lock:
            snap = self._items.get(key)
            if snap is None:
                raise MobilePilotError(
                    ErrorCode.SNAPSHOT_NOT_FOUND,
                    f"Snapshot {snapshot_id!r} does not exist or has expired.",
                )
            self._items.move_to_end(key)
            return snap

    def latest(self, pid: int | None = None) -> Snapshot | None:
        """The most recently *created* snapshot (optionally of the app with ``pid``)."""
        with self._lock:
            candidates = [
                (self._created[sid], snap)
                for sid, snap in self._items.items()
                if pid is None or snap.app.pid == pid
            ]
        return max(candidates, key=lambda pair: pair[0])[1] if candidates else None

    def _newest_first(self) -> list[Snapshot]:
        with self._lock:
            ordered = sorted(self._items.items(), key=lambda item: self._created[item[0]], reverse=True)
        return [snap for _, snap in ordered]

    def snapshots_of(self, pid: int, window_id: int | None, *, limit: int | None = None) -> list[Snapshot]:
        """Stored snapshots of one app window (``window_id=None``: snapshots without a window),
        newest first, at most ``limit`` of them."""
        found = [snap for snap in self._newest_first() if view_of(snap) == (pid, window_id)]
        return found if limit is None else found[: max(limit, 0)]

    def _touch(self, snapshot_id: str) -> None:
        with self._lock:
            if snapshot_id in self._items:
                self._items.move_to_end(snapshot_id)

    def resolve(self, ref: str, snapshot_id: str | None = None) -> tuple[Snapshot, Element]:
        """Find the element for ``ref`` (``"e12"``, OCR text ``"o3"``, or ``"s_ab12:e12"``).

        With a snapshot id (in the ref or the argument) only that snapshot is searched.
        Without one the latest snapshot is searched first, then the other stored snapshots
        numbered by :attr:`refs`, newest first: a session ref names one element, whichever
        snapshot listed it (snapshots with plain local refs are only searched as the latest). A ref
        found only in an older snapshot is still refused (``STALE_REF``) when a newer snapshot
        of the same window lists its element under another ref (its content changed), or when
        it is OCR text and that window was captured again since (OCR positions cannot be
        re-checked). The element's engine handle is *not* checked here: callers act through
        :func:`~mobilepilot.perception.resolve.live_handle`.

        Raises ``INVALID_ARGUMENT`` (malformed ref, conflicting ids), ``SNAPSHOT_NOT_FOUND``
        (unknown snapshot id, or no snapshot at all) or ``STALE_REF``.
        """
        ref_sid, element_ref = parse_ref(ref)
        wanted = snapshot_id.strip().lower() if snapshot_id else None
        if ref_sid and wanted and ref_sid != wanted:
            raise MobilePilotError(
                ErrorCode.INVALID_ARGUMENT,
                f"Ref {ref!r} belongs to snapshot {ref_sid}, but snapshot_id {snapshot_id!r} was given.",
                hint="Pass matching values, or just the ref.",
            )
        sid = ref_sid or wanted
        if sid is not None:
            return self._resolve_in(self.get(sid), element_ref)
        ordered = self._newest_first()
        if not ordered:
            raise MobilePilotError(
                ErrorCode.SNAPSHOT_NOT_FOUND,
                "There is no snapshot to resolve refs against yet.",
                hint="Call mobile_snapshot first, then use the refs it returns.",
            )
        newer: list[Snapshot] = []
        for snap in ordered:
            # only session refs name one element everywhere; plain local refs (e1…eN of a bare
            # build_snapshot) are positional, so those snapshots answer only as the latest one
            session = snap is ordered[0] or snap.ref_session == self.refs.session
            element = snap.element(element_ref) if session else None
            if element is None:
                newer.append(snap)
                continue
            self._check_current(snap, element, [s for s in newer if view_of(s) == view_of(snap)])
            self._touch(snap.snapshot_id)
            return snap, element
        raise self._unknown(element_ref)

    def observed(self, ref: str) -> tuple[Snapshot, Element]:
        """The newest stored snapshot that listed ``ref``, as it was then, with no check that the
        element still looks the same (MobilePilot: ``mobile_locate`` describes what was observed,
        and a row whose summary changed afterwards was still observed). Raises ``STALE_REF``."""
        _, element_ref = parse_ref(ref)
        for snap in self._newest_first():
            element = snap.element(element_ref)
            if element is not None:
                return snap, element
        raise self._unknown(element_ref)

    @staticmethod
    def _resolve_in(snap: Snapshot, element_ref: str) -> tuple[Snapshot, Element]:
        element = snap.element(element_ref)
        if element is None:
            raise MobilePilotError(
                ErrorCode.STALE_REF,
                f"Ref {element_ref} is not part of snapshot {snap.snapshot_id} "
                f"(it lists {describe_refs(snap)}).",
                hint="Use a ref that snapshot lists, or omit snapshot_id to look the ref up in every "
                "recent snapshot.",
            )
        return snap, element

    @staticmethod
    def _check_current(snap: Snapshot, el: Element, later: list[Snapshot]) -> None:
        """Refuse an element found in ``snap`` that newer snapshots of its window (``later``,
        newest first) show to be outdated."""
        if not later:
            return
        if is_vision_element(el):
            raise MobilePilotError(
                ErrorCode.STALE_REF,
                f"{el.ref} ({_label(el)}) is OCR text from snapshot {snap.snapshot_id}; its window was "
                f"captured again since (snapshot {later[0].snapshot_id}) and OCR text cannot be re-checked.",
                hint='Take snapshot(mode="vision") for fresh o-refs, or use an e-ref.',
            )
        if el.handle is None:
            return
        for other_snap in later:
            for other in other_snap.all_elements:
                if other.ref != el.ref and same_handle(other.handle, el.handle):
                    raise MobilePilotError(
                        ErrorCode.STALE_REF,
                        f"{el.ref} ({_label(el)}) from snapshot {snap.snapshot_id} is stale: snapshot "
                        f"{other_snap.snapshot_id} lists that element as {other.ref} ({_label(other)}), "
                        "so its content or place changed.",
                        hint="Take a new snapshot and pick the element again by its name.",
                    )

    def _unknown(self, element_ref: str) -> MobilePilotError:
        parts = split_ref(element_ref)
        issued = self.refs.issued(parts[0]) if parts is not None else 0
        if parts is not None and parts[1] > issued:
            highest = f" (the highest so far is {parts[0]}{issued})" if issued else ""
            message = f"Ref {element_ref} was never issued in this session{highest}."
        else:
            message = (
                f"Ref {element_ref} is not listed in any recent snapshot: its element is gone, or "
                "the snapshots that listed it expired."
            )
        return MobilePilotError(
            ErrorCode.STALE_REF,
            message,
            hint="Take a new snapshot and use its refs (refs stay valid while their element exists).",
        )

    def clear(self) -> None:
        """Forget every snapshot."""
        with self._lock:
            self._items.clear()
            self._created.clear()


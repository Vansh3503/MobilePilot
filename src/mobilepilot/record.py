"""Write down what a person does on the phone by hand: ``mobile_record``.

Two streams, no model: the kernel's touch events (``adb shell getevent -lt``) and the screen tree
read in a loop. Each finger gesture is matched to the newest tree finished BEFORE the finger went
down, so the element under the finger is the one the person saw, not whatever the tap opened.

Rules that keep it honest and private:

* A gesture whose screen was still changing is marked ``sure: false`` with its coordinates; the
  element is a candidate, never presented as fact.
* Keyboard touches are never stored (their positions spell what was typed). Typing is read from
  the change in the focused field; a password field is written as ``••••``.
* Touches on the lock screen are never stored: one "unlock" step says it happened.
* Each step is appended to ``steps.jsonl`` as soon as it is known, so a crash keeps what was seen.

Measured on the API 34 emulator (2026-11-16): ``input tap`` and uiautomator injections never reach
getevent (they enter above the kernel); the emulator console's ``event mouse`` / ``event send``
do. The kernel leaves out a finger's position when it equals the previous touch's, so positions
are kept per slot across touches. Touch panels report in the display's natural orientation.
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mobilepilot.models import Rect, RawTree
from mobilepilot.perception.diff import diff_snapshots, element_changes
from mobilepilot.perception.locators import Candidate, best, candidates
from mobilepilot.perception.model import Element, Snapshot
from mobilepilot.perception.refs import HISTORY_LIMIT, carry_refs
from mobilepilot.perception.snapshot import build_snapshot
from mobilepilot.perception.store import SnapshotStore

SECRET = "••••"
SLOP_DP = 8.0
"""Android's touch slop: a finger that moves less than this is still a tap."""
EDGE_DP = 32.0
"""Width of the screen edges where gesture navigation starts (back, home, the notification shade)."""
NAV_BAR_DP = 48.0
DOUBLE_TAP_DP = 48.0
SETTLE_CAP_S = {"tap": 3.0, "double_tap": 3.0, "long_press": 3.0, "swipe": 2.5, "drag": 2.5, "pinch": 2.5,
                "back": 3.0, "home": 3.0, "recents": 3.0, "notifications": 2.0, "key": 2.0, "type": 1.5}
"""How long a step waits for its screen to stop changing before it is written as it is."""
SECRET_GRACE_S = 2.0
"""Android shows a password's last character for ~1.5 s: no screenshot until this long after it."""
TYPING_SHOT_IDLE_S = 1.0
FIRST_LOOK_S = 0.5
"""A step's screen is only judged from readings that began this long after the gesture: a tap's
new screen can take ~450 ms to start moving (the server's FIRST_LOOK_S, measured on Settings)."""
FRAMES_KEPT = 64
_KEYS = {"KEY_VOLUMEUP": "volume_up", "KEY_VOLUMEDOWN": "volume_down", "KEY_POWER": "power", "KEY_BACK": "back",
         "KEY_HOMEPAGE": "home", "KEY_HOME": "home", "KEY_APPSELECT": "app_switch", "KEY_CAMERA": "camera",
         "KEY_MUTE": "mute", "KEY_ASSISTANT": "assistant"}
"""Hardware keys worth a step. Every other key is a physical keyboard typing: never stored."""
_NOT_TYPING = {"KEY_WAKEUP", "KEY_SLEEP", "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_UNKNOWN"}
"""Keys that are neither a step nor typing: the Pixel 7a's touchscreen sends KEY_WAKEUP (tap to
wake) and its fingerprint sensor KEY_UP/DOWN/LEFT/RIGHT (swipe gestures), 2026-11-16."""


# --------------------------------------------------------------------------- the event stream
@dataclass(slots=True, frozen=True)
class Event:
    t: float
    """Kernel time (seconds since boot)."""
    device: str
    type: str
    code: str
    value: int


_LINE = re.compile(r"^\[\s*(\d+\.\d+)\]\s+(\S+?):?\s+(EV_\w+|\w+)\s+(\S+)\s+(\S+)")
_KEY_VALUES = {"DOWN": 1, "UP": 0, "REPEAT": 2}


def parse_line(line: str) -> Event | None:
    """One ``getevent -lt`` line, or None for anything else (device lists, warnings, a cut line)."""
    m = _LINE.match(line.strip())
    if not m:
        return None
    t, device, etype, code, raw = m.groups()
    if raw in _KEY_VALUES:
        value = _KEY_VALUES[raw]
    else:
        try:
            value = int(raw, 16)
        except ValueError:
            return None
        if value >= 0x80000000:  # getevent prints the 32-bit value: ffffffff is a finger lifted (-1)
            value -= 0x100000000
    return Event(float(t), device, etype, code, value)


@dataclass(slots=True)
class Panel:
    device: str
    max_x: int
    max_y: int


def parse_panels(text: str) -> dict[str, Panel]:
    """Touchscreens from ``getevent -pl``: devices reporting multi-touch positions."""
    panels: dict[str, Panel] = {}
    for block in re.split(r"(?m)^add device \d+: ", text):
        name = block.split("\n", 1)[0].strip()
        mx = re.search(r"ABS_MT_POSITION_X\s*:.*?max (\d+)", block)
        my = re.search(r"ABS_MT_POSITION_Y\s*:.*?max (\d+)", block)
        if name.startswith("/dev/input/") and mx and my:
            # ponytail: "-pl" also prints a current "value", but it is always 0 for multi-touch axes
            # (emulator, 2026-11-16): a touch whose position the kernel left out stays unknown
            panels[name] = Panel(name, int(mx.group(1)), int(my.group(1)))
    return panels


@dataclass(slots=True)
class TouchStart:
    device: str
    t: float
    x: float
    y: float
    """Where the first finger went down (natural orientation, pixels)."""


@dataclass(slots=True)
class Touch:
    """One gesture: from the first finger down until the last finger up. Points are (t, x, y) in
    the panel's natural orientation, in pixels."""

    device: str
    start: float
    end: float
    fingers: list[list[tuple[float, float, float]]]
    dropped: bool = False
    """The kernel dropped events during it (SYN_DROPPED): its path is incomplete."""


@dataclass(slots=True)
class KeyPress:
    code: str
    start: float
    end: float


class _Slot:
    __slots__ = ("tid", "x", "y", "open")

    def __init__(self) -> None:
        self.tid, self.x, self.y, self.open = -1, None, None, None


class TouchTracker:
    """Turns multi-touch (protocol B) events into gestures. Positions persist per slot across
    touches, because the kernel only sends what changed."""

    def __init__(self, panels: dict[str, Panel], natural: tuple[int, int]) -> None:
        self.panels = panels
        self.natural = natural
        self.unsupported: str | None = None
        self._dev: dict[str, dict[str, Any]] = {}
        self._keys: dict[str, float] = {}

    def _state(self, device: str) -> dict[str, Any]:
        return self._dev.setdefault(device, {"slot": 0, "slots": {}, "finished": [], "start": None, "dropped": False})

    def _px(self, panel: Panel, x: int, y: int) -> tuple[float, float]:
        w, h = self.natural
        return x * w / (panel.max_x + 1), y * h / (panel.max_y + 1)

    def feed(self, ev: Event) -> list[TouchStart | Touch | KeyPress]:
        if ev.type == "EV_KEY":
            return self._key(ev)
        panel = self.panels.get(ev.device)
        if panel is None:
            return []
        st = self._state(ev.device)
        if ev.type == "EV_ABS":
            if ev.code == "ABS_MT_SLOT":
                st["slot"] = ev.value
                return []
            slot = st["slots"].setdefault(st["slot"], _Slot())
            if ev.code == "ABS_MT_TRACKING_ID":
                slot.tid = ev.value
            elif ev.code == "ABS_MT_POSITION_X":
                slot.x = ev.value
            elif ev.code == "ABS_MT_POSITION_Y":
                slot.y = ev.value
            return []
        if ev.type == "EV_SYN" and ev.code == "SYN_MT_REPORT":
            self.unsupported = ("This touchscreen uses the old multi-touch protocol (type A): touches cannot be "
                                "followed reliably, so gestures are not recorded.")
            return []
        if ev.type == "EV_SYN" and ev.code == "SYN_DROPPED":
            st["dropped"] = True
            return []
        if ev.type == "EV_SYN" and ev.code == "SYN_REPORT":
            return self._commit(ev, panel, st)
        return []

    def _commit(self, ev: Event, panel: Panel, st: dict[str, Any]) -> list[TouchStart | Touch | KeyPress]:
        out: list[TouchStart | Touch | KeyPress] = []
        for slot in st["slots"].values():
            if slot.open is not None and slot.open[0] != slot.tid:  # lifted, or a new id in the same slot
                st["finished"].append(slot.open[1])
                slot.open = None
            if slot.tid >= 0:
                known = slot.x is not None and slot.y is not None
                x, y = self._px(panel, slot.x, slot.y) if known else (None, None)
                if slot.open is None:
                    if st["start"] is None:
                        st["start"] = ev.t
                        out.append(TouchStart(ev.device, ev.t, x if known else -1.0, y if known else -1.0))
                    slot.open = (slot.tid, [(ev.t, x, y)] if known else [])
                elif known and (not slot.open[1] or slot.open[1][-1][1:] != (x, y)):
                    slot.open[1].append((ev.t, x, y))
        if st["start"] is not None and all(s.open is None for s in st["slots"].values()):
            out.append(Touch(ev.device, st["start"], ev.t, st["finished"], dropped=st["dropped"]))
            st["start"], st["finished"], st["dropped"] = None, [], False
        return out

    def _key(self, ev: Event) -> list[TouchStart | Touch | KeyPress]:
        if ev.code.startswith("BTN_"):  # BTN_TOUCH, BTN_TOOL_FINGER: the tracking id already says it
            return []
        if ev.value == 1:
            self._keys[ev.code] = ev.t
        elif ev.value == 0 and ev.code in self._keys:
            return [KeyPress(ev.code, self._keys.pop(ev.code), ev.t)]
        return []

    def pending(self) -> bool:
        """A finger is down right now."""
        return any(st["start"] is not None for st in self._dev.values())


class Clock:
    """Kernel time to host time: the smallest (arrival - kernel) seen is the least-delayed line."""

    def __init__(self) -> None:
        self.offset = math.inf

    def host(self, kernel_t: float, arrival: float) -> float:
        self.offset = min(self.offset, arrival - kernel_t)
        return kernel_t + self.offset


# --------------------------------------------------------------------------- geometry and gestures
@dataclass(slots=True)
class InputConfig:
    density: float = 160.0
    long_press_s: float = 0.5
    double_tap_s: float = 0.3
    nav_mode: int = 2
    """``settings get secure navigation_mode``: 0 three buttons, 1 two buttons, 2 gestures."""

    @property
    def dp(self) -> float:
        return self.density / 160.0


def to_display(x: float, y: float, rotation: int, natural: tuple[int, int]) -> tuple[float, float]:
    """A panel point (natural orientation) on the rotated display. Measured on the emulator with
    the Clock app's tabs in both landscapes (2026-11-16)."""
    w, h = natural
    return {0: (x, y), 1: (y, w - x), 2: (w - x, h - y), 3: (h - y, x)}[rotation % 4]


def display_size(rotation: int, natural: tuple[int, int]) -> tuple[int, int]:
    return (natural[1], natural[0]) if rotation % 2 else natural


@dataclass(slots=True)
class Gesture:
    kind: str
    """tap, double_tap, long_press, swipe, drag, pinch, two_finger_swipe, two_finger_tap,
    multi_finger, back, home, recents, notifications."""
    at: tuple[float, float]
    to: tuple[float, float] | None = None
    finger: str | None = None
    """Which way the finger moved (up/down/left/right)."""
    zoom: str | None = None
    duration_s: float = 0.0
    fingers: int = 1
    note: str = ""


def _direction(dx: float, dy: float) -> str:
    return ("right" if dx > 0 else "left") if abs(dx) >= abs(dy) else ("down" if dy > 0 else "up")


SCROLL_OF_FINGER = {"up": "down", "down": "up", "left": "right", "right": "left"}
"""mobile_swipe's direction is where the CONTENT comes from: a finger moving up shows what is below."""


def classify(touch: Touch, cfg: InputConfig, rotation: int, natural: tuple[int, int]) -> Gesture:
    dp = cfg.dp
    slop = SLOP_DP * dp
    paths = [[(t, *to_display(x, y, rotation, natural)) for t, x, y in f] for f in touch.fingers if f]
    width, height = display_size(rotation, natural)
    duration = touch.end - touch.start
    count = len(touch.fingers)
    if not paths:
        return Gesture("unknown", (0.0, 0.0), duration_s=duration, fingers=count,
                       note="the touchscreen reported no position for this touch")
    if count >= 3:
        x, y = paths[0][0][1:]
        return Gesture("multi_finger", (x, y), duration_s=duration, fingers=count,
                       note=f"{count}-finger gesture (a system shortcut?)")
    if count == 2:
        if len(paths) < 2:
            x, y = paths[0][0][1:]
            return Gesture("two_finger_tap", (x, y), duration_s=duration, fingers=2,
                           note="one finger's position was not reported: the gesture is a guess")
        return _two(paths, duration, slop)
    path = paths[0]
    (t0, x0, y0), (t1, x1, y1) = path[0], path[-1]
    far = max(math.hypot(x - x0, y - y0) for _, x, y in path)
    if far < slop:
        kind = "long_press" if duration >= cfg.long_press_s else "tap"
        if kind == "tap" and cfg.nav_mode in (0, 1) and y0 >= height - NAV_BAR_DP * dp:
            third = x0 / max(width, 1)
            key = "back" if third < 1 / 3 else "home" if third < 2 / 3 else "recents"
            return Gesture(key, (x0, y0), duration_s=duration, note="navigation-bar button, told by its position")
        return Gesture(kind, (x0, y0), duration_s=duration)
    dx, dy = x1 - x0, y1 - y0
    finger = _direction(dx, dy)
    first_move = next(t for t, x, y in path if math.hypot(x - x0, y - y0) >= slop)
    if cfg.nav_mode == 2:
        edge = EDGE_DP * dp
        if (x0 <= edge and finger == "right") or (x0 >= width - edge and finger == "left"):
            return Gesture("back", (x0, y0), (x1, y1), finger, duration_s=duration, note="back gesture from the edge")
        if y0 >= height - edge and finger == "up":
            tail = [(t, x, y) for t, x, y in path if t >= t1 - 0.3]
            paused = duration > 0.5 and max(math.hypot(x - x1, y - y1) for _, x, y in tail) < slop
            return Gesture("recents" if paused else "home", (x0, y0), (x1, y1), finger, duration_s=duration)
    if y0 <= EDGE_DP * dp and finger == "down":
        return Gesture("notifications", (x0, y0), (x1, y1), finger, duration_s=duration)
    kind = "drag" if first_move - t0 >= cfg.long_press_s else "swipe"
    return Gesture(kind, (x0, y0), (x1, y1), finger, duration_s=duration)


def _two(paths: list[list[tuple[float, float, float]]], duration: float, slop: float) -> Gesture:
    a, b = paths
    both = max(a[0][0], b[0][0])

    def at(path, t):
        last = path[0]
        for p in path:
            if p[0] > t:
                break
            last = p
        return last[1:]

    end = min(a[-1][0], b[-1][0])
    (ax0, ay0), (bx0, by0) = at(a, both), at(b, both)
    (ax1, ay1), (bx1, by1) = at(a, end), at(b, end)
    d0, d1 = math.hypot(ax0 - bx0, ay0 - by0), math.hypot(ax1 - bx1, ay1 - by1)
    centre = ((ax0 + bx0) / 2, (ay0 + by0) / 2)
    if d0 > 0 and abs(d1 - d0) >= slop and (d1 / d0 >= 1.15 or d1 / d0 <= 0.87):
        # MobilePilot's naming: zoom "in" spreads the fingers apart
        return Gesture("pinch", centre, zoom="in" if d1 > d0 else "out", duration_s=duration, fingers=2)
    mx, my = ((ax1 + bx1) - (ax0 + bx0)) / 2, ((ay1 + by1) - (ay0 + by0)) / 2
    if math.hypot(mx, my) >= slop:
        return Gesture("two_finger_swipe", centre, (centre[0] + mx, centre[1] + my), _direction(mx, my),
                       duration_s=duration, fingers=2)
    return Gesture("two_finger_tap", centre, duration_s=duration, fingers=2)


# --------------------------------------------------------------------------- what the screen showed
@dataclass(slots=True)
class Frame:
    """One reading of the screen."""

    start: float
    end: float
    """Host times (time.monotonic) the read began and finished."""
    tree: RawTree
    fingerprint: int
    keyboard: Rect | None = None
    """Where the on-screen keyboard is, if it is up."""
    keyboard_unknown: bool = False
    """The keyboard is up but Android did not say where: its touches are treated as keys."""
    locked: bool = False
    """The lock screen is showing, or the screen is off: touches are not recorded."""
    rotation: int = 0
    seq: int = 0
    """Set by the recorder: identifies the frame (an id() can be reused once a frame is dropped)."""


def element_at(snap: Snapshot, x: float, y: float) -> Element | None:
    """The smallest interactive element under the point: the row, not the label inside it; the
    switch inside the row when the finger is on the switch."""
    hits = [el for el in snap.all_elements
            if el.interactive and el.frame is not None and el.frame.x <= x < el.frame.x + el.frame.w
            and el.frame.y <= y < el.frame.y + el.frame.h and el.frame.area > 0]
    screen = snap.window.frame.area if snap.window is not None else None
    hits = [el for el in hits if screen is None or el.frame.area < screen * 0.98 or "scroll" not in el.actions]
    taps = [el for el in hits if "scroll" not in el.actions or el.role in ("textfield", "searchfield")]
    pool = taps or hits
    return min(pool, key=lambda el: el.frame.area) if pool else None


def _same_element(a: Element, b: Element) -> bool:
    fa, fb = a.frame, b.frame
    return (a.role, a.name, a.extra.get("identifier"), a.extra.get("a11y_id")) == (
        b.role, b.name, b.extra.get("identifier"), b.extra.get("a11y_id")) and fa is not None and fb is not None and (
        round(fa.x), round(fa.y), round(fa.w), round(fa.h)) == (round(fb.x), round(fb.y), round(fb.w), round(fb.h))


def container_at(snap: Snapshot, x: float, y: float) -> Element | None:
    """The smallest scrollable element under the point (what a swipe there scrolls)."""
    hits = [el for el in snap.all_elements if "scroll" in el.actions and el.frame is not None
            and el.frame.x <= x < el.frame.x + el.frame.w and el.frame.y <= y < el.frame.y + el.frame.h]
    return min(hits, key=lambda el: el.frame.area) if hits else None


def keys_area(frame: Frame, natural: tuple[int, int]) -> Rect | None:
    """Where touches count as key presses. Fails closed: a keyboard that is up somewhere unknown
    is taken to fill the lower half of the screen, where every phone keyboard sits."""
    if frame.keyboard is not None or not frame.keyboard_unknown:
        return frame.keyboard
    w, h = display_size(frame.rotation, natural)
    return Rect(0, h // 2, w, h - h // 2)


_SECRET_WORDS = re.compile(r"(?i)(?:^|[^a-z])(password|passwd|passcode|pwd|pin|otp|cvv|cvc)(?:[^a-z]|$)")
"""A field named for a secret: its id (``pin_input``), hint or label."""
_PIN_WORDS = re.compile(r"(?i)\b(pin|passcode|pass code)\b")
_CALC_KEYS = {"+", "=", "×", "÷", "−", "*", "/", "plus", "minus", "multiply", "times", "divide", "equals"}
"""A keypad with these is a calculator, not a PIN pad. Google Calculator names its keys in words
(Pixel 7a, 2026-11-17): it was being taken for a PIN pad."""


def _clean_text(text: str) -> str:
    return text.strip()


def _inside(rect: Rect | None, x: float, y: float) -> bool:
    return rect is not None and rect.x <= x < rect.x + rect.w and rect.y <= y < rect.y + rect.h


def _cand(c: Candidate) -> dict[str, Any]:
    return {"strategy": c.strategy, "value": c.value, "tier": c.tier, "unique": c.matches == 1,
            **({"note": c.note} if c.note else {})}


def screen_title(tree: RawTree) -> str | None:
    """What the person would call the screen: its biggest text near the top, outside any tappable
    element (Android Settings' subpages all share the activity ``.SubSettings``, so the window title
    says nothing; the interactive snapshot leaves that title text out, so the raw tree is read)."""
    window = tree.window.frame if tree.window is not None else tree.root.frame
    fallback = tree.window.title.rsplit(".", 1)[-1] or None if tree.window is not None else None
    if window is None or not window.h:
        return fallback
    found: list[tuple[float, float, str]] = []

    def visit(node) -> None:
        if "tap" in node.actions or "long_press" in node.actions or node.role in ("AXList", "AXTable", "AXOutline"):
            return  # a row's or a list's text names the row, not the screen (Settings search, Pixel 7a)
        text = node.value if node.role == "AXStaticText" else node.title if node.role == "AXHeading" else None
        f = node.frame
        if (isinstance(text, str) and text.strip() and node.visible and f is not None and f.h > 0
                and f.y < window.y + window.h * 0.35 and f.h <= window.h * 0.4):  # Settings' collapsing title starts at y=0
            found.append((f.h, -f.y, text.strip()))
        for child in node.children:
            visit(child)

    visit(tree.root)
    return max(found)[2][:80] if found else fallback


def _label(el: Element, snap: Snapshot | None) -> str | None:
    """A nameless control's name, borrowed from the smallest named element around it (a switch
    takes its row's title)."""
    if el.name or snap is None or el.frame is None:
        return el.name
    c = el.frame.center
    around = [o for o in snap.all_elements if o is not el and o.name and o.frame is not None and o.frame.area > el.frame.area
              and o.frame.x <= c.x < o.frame.x + o.frame.w and o.frame.y <= c.y < o.frame.y + o.frame.h]
    if not around:
        return None
    return min(around, key=lambda o: o.frame.area).name.split(" — ")[0]


def describe(el: Element, tree: RawTree, platform: str, snap: Snapshot | None = None) -> dict[str, Any]:
    """An element as a step names it: what it is, and every locator worth trying, best first."""
    found = candidates(el, tree, platform)
    pick = best(found)
    out: dict[str, Any] = {"role": el.role, "name": el.name}
    if not el.name and (label := _label(el, snap)):
        out["in"] = label
    for key, extra in (("id", "identifier"), ("aid", "a11y_id"), ("class", "class")):
        if el.extra.get(extra):
            out[key] = el.extra[extra]
    if not el.enabled:
        out["enabled"] = False
    if el.extra.get("where"):
        out["where"] = el.extra["where"]
    out["locator"] = _cand(pick) if pick is not None else None
    out["alternatives"] = [_cand(c) for c in found if c is not pick]
    return out


_TEXT_ROLES = ("textfield", "searchfield", "textarea", "securefield")


def _is_field(el: Element) -> bool:
    return el.role in _TEXT_ROLES or el.secure or el.ax_role in ("AXTextField", "AXTextArea", "AXSearchField")


def _field_key(el: Element) -> tuple:
    return (el.extra.get("class"), el.extra.get("identifier"), el.extra.get("a11y_id"), el.ax_role)


# --------------------------------------------------------------------------- the recorder
@dataclass(slots=True)
class _Pending:
    step: dict[str, Any]
    end: float
    cap: float
    before: Snapshot | None
    before_tree: RawTree | None
    target: Element | None = None


@dataclass(slots=True)
class _Field:
    el: Element
    tree: RawTree
    first: str | None
    last: str | None
    since: float
    changed_at: float = 0.0
    keys: int = 0
    idle_keys: int = 0
    """Keyboard touches since the text last changed: Enter, Done, Shift, a suggestion that matched,
    or a miss. Their positions are never kept, so which one it was cannot be told."""
    shot: str | None = None
    shot_value: str | None = None
    secret: bool = False
    """Once a password field, always: a reading without the flag (a show-password toggle) is not typed data."""
    seen: int | None = None
    """A hash of a secret field's plain text, when a reading shows it: change is noticed, the text is not kept."""
    key_times: list[float] = field(default_factory=list)
    """When each key went down (times only, never where): keys after the last reading may not be in the text."""
    seen_start: float = 0.0
    """When the last reading that showed this field began."""


class Recorder:
    """Turns gestures, keys and screen readings into steps. Every method runs on ONE thread (the
    recording loop), so the device is never driven from two threads at once."""

    def __init__(self, folder: Path, *, platform: str = "android", natural: tuple[int, int] = (1080, 2400),
                 config: InputConfig | None = None, shoot: Callable[[Path], bool] | None = None,
                 device: str = "", clock: Callable[[], float] = time.monotonic) -> None:
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.platform = platform
        self.natural = natural
        self.cfg = config or InputConfig()
        self.shoot = shoot
        self.device = device
        self.clock = clock
        self.frames: deque[Frame] = deque(maxlen=FRAMES_KEPT)
        self.store = SnapshotStore()
        self._snaps: dict[int, Snapshot] = {}
        self.steps: list[dict[str, Any]] = []
        self.pending: list[_Pending] = []
        self.started = clock()
        self.notes: list[str] = []
        self.keyboard_touches = 0
        self._last_touch_end = -math.inf
        self._fields: dict[tuple, _Field] = {}
        self._focus: tuple | None = None
        self._secret_until = -math.inf
        self._secret_ids: set[str] = set()
        self._unlock: dict[str, Any] | None = None
        self._down: float | None = None
        self._pinned: tuple[float, Frame | None, bool] | None = None
        self._pinned_sure = False
        self._seq = 0
        self._reading: Frame | None = None
        self._orphan_keys: list[float] = []
        """Keys pressed before any reading showed which field they went to."""
        self._jsonl = self.folder / "steps.jsonl"
        self._jsonl.write_text("")

    # ---------------------------------------------------------------- snapshots
    def _snap(self, frame: Frame) -> Snapshot:
        key = frame.seq
        if key not in self._snaps:
            snap = build_snapshot(frame.tree, snapshot_id=self.store.new_id(), mode="interactive", max_elements=10_000)
            window_id = snap.window.window_id if snap.window else None
            history = self.store.snapshots_of(snap.app.pid, window_id, limit=HISTORY_LIMIT)
            previous = self.store.latest()
            snap = carry_refs(snap, previous, self.store.refs, history=history)
            self.store.put(snap)
            self._snaps[key] = snap
            live = {f.seq for f in self.frames}
            for k in [k for k in self._snaps if k not in live and k != key]:
                del self._snaps[k]
        return self._snaps[key]

    def _frame_before(self, t: float) -> Frame | None:
        for f in reversed(self.frames):
            if f.end <= t:
                return f
        return None

    def _sure(self, frame: Frame, x: float | None = None, y: float | None = None) -> bool:
        """The element under the finger had stopped changing when it went down: in the two readings
        before it, that element is the same one in the same place, and the newer reading began
        after the previous gesture ended (so it shows that gesture's result). The WHOLE screen need
        not agree: Amazon's carousels and timers never stop, and asking for that marked 19 of 25
        steps unsure on a Pixel 7a (2026-11-16) whose targets had not moved at all."""
        frames = list(self.frames)
        i = next((k for k, f in enumerate(frames) if f is frame), None)
        if i is None or i == 0:
            return False
        prev = frames[i - 1]
        if prev.start < self._last_touch_end:
            return False
        if prev.fingerprint == frame.fingerprint:
            return True
        if x is None or y is None:
            return False
        a, b = element_at(self._snap(frame), x, y), element_at(self._snap(prev), x, y)
        return a is not None and b is not None and _same_element(a, b)

    def _t(self, t: float) -> float:
        return round(max(t - self.started, 0.0), 2)

    # ---------------------------------------------------------------- inputs
    def reading(self, f: Frame) -> None:
        """A reading has finished but the touches made while it ran are still being handed over."""
        self._seq += 1
        f.seq = self._seq
        self._reading = f

    def _screen_at(self, t: float) -> Frame | None:
        """The reading a touch at ``t`` landed on. Normally the last one finished before it; but when
        that one began before the previous gesture ended (the next screen was still loading) and the
        reading running at ``t`` began after it, the running one shows the screen the finger hit.
        Pixel 7a 2026-11-17: "Open" on YouTube's page was recorded as "157 MB" of the results before it."""
        frame = self._frame_before(t)
        up = self._reading
        if (up is not None and not up.locked and self._last_touch_end <= up.start <= t
                and (frame is None or frame.start < self._last_touch_end)):
            return up
        return frame

    def _on_keys(self, frame: Frame | None, t: float, x: float, y: float) -> bool:
        """A touch is a key if the reading before it, OR the one running when it happened, shows a
        keyboard there. Keys come faster than a reading (0.26 s apart against 0.4 s, Pixel 7a
        2026-11-17): judged only by the reading before, the first keys after a keyboard opens were
        written as taps with their positions. Fails closed: a tap that opened the keyboard under
        itself is lost, which is better than keeping where keys were pressed."""
        if frame is not None and _inside(keys_area(frame, self.natural), x, y):
            return True
        up = self._reading
        return up is not None and up.start <= t and not up.locked and _inside(keys_area(up, self.natural), x, y)

    def _key_pressed(self, at: float, end: float) -> None:
        self.keyboard_touches += 1
        if self._focus is not None and self._focus in self._fields:
            fld = self._fields[self._focus]
            fld.keys += 1
            fld.idle_keys += 1
            fld.key_times.append(at)
            fld.changed_at = end
            if fld.secret:
                self._secret_until = end + SECRET_GRACE_S
        else:
            self._orphan_keys.append(at)

    def frame(self, f: Frame) -> None:
        self._reading = None
        prev = self.frames[-1] if self.frames else None
        if not f.seq:   # reading() numbered it already
            self._seq += 1
            f.seq = self._seq
        self.frames.append(f)
        if prev is not None and prev.rotation % 2 != f.rotation % 2 and not f.locked:
            self._flush_typing(f, submitted=False)
            orientation = "landscape" if display_size(f.rotation, self.natural)[0] > display_size(f.rotation, self.natural)[1] else "portrait"
            self._add({"action": "rotate", "orientation": orientation, "sure": True}, f.end, None, None)
        if self._unlock is not None and not f.locked:
            self._unlock = None  # the next lock-screen touch starts a new "unlock" step
        if not f.locked:
            self._watch_fields(f, prev)
        self.tick(f)

    def touch_start(self, t: float, x: float, y: float) -> None:
        """A finger went down: write the earlier steps with the screen as it is now (before this
        touch changes it) and close any typing. A key on the keyboard or the lock screen changes
        nothing here. A tap that may become a double tap waits."""
        self._down = t
        frame = self._screen_at(t)
        if frame is None or frame.locked:
            self._pinned = (t, frame, False)
            return
        dx, dy = to_display(x, y, frame.rotation, self.natural)
        # pinned now: by the time the finger lifts (a long drag) this reading may have left the buffer
        self._pinned = (t, frame, x >= 0 and self._sure(frame, dx, dy))
        if self._on_keys(frame, t, dx, dy):
            return
        self._flush_typing(frame, submitted=False)
        for p in list(self.pending):
            if p.step["action"] == "tap" and t - p.end <= self.cfg.double_tap_s:
                continue
            self._finish(p, self.frames[-1], settled=False)

    def touch(self, touch: Touch) -> None:
        pinned, self._pinned = self._pinned, None
        if pinned is not None and abs(pinned[0] - touch.start) < 0.5:
            frame, self._pinned_sure = pinned[1], pinned[2]
        else:
            frame = self._screen_at(touch.start)
            self._pinned_sure = None
        self._down = None
        rotation = frame.rotation if frame is not None else 0
        first = next((f[0] for f in touch.fingers if f), None)
        x0, y0 = to_display(*first[1:], rotation, self.natural) if first else (-1.0, -1.0)
        if self._pinned_sure is None:
            self._pinned_sure = frame is not None and first is not None and self._sure(frame, x0, y0)
        if frame is not None and frame.locked:
            self._lock_touch(touch)
            return
        if frame is not None and self._on_keys(frame, touch.start, x0, y0):
            # never stored: where keys were pressed spells what was typed
            self._key_pressed(touch.start, touch.end)
            return
        if frame is None:
            # nothing read yet: it may have been a key on a keyboard nobody has seen, so no position
            self.keyboard_touches += 1
            step = {"action": "tap" if len(touch.fingers) <= 1 else "gesture", "redacted": True, "sure": False,
                    "note": "before the first screen reading: where it was is not kept (it may have been a key)"}
            self._last_touch_end = touch.end
            self._add(step, touch.end, None, None)
            return
        g = classify(touch, self.cfg, rotation, self.natural)
        if frame is not None:
            self._flush_typing(frame, submitted=False, shoot=False)  # the finger is up: the screen may have moved on
        if g.kind == "tap" and self._merge_double(g, touch):
            self._last_touch_end = touch.end
            return
        for p in list(self.pending):
            self._finish(p, self.frames[-1] if self.frames else None, settled=False)
        step = self._gesture_step(g, frame, touch)
        self._last_touch_end = touch.end
        snap = self._snap(frame) if frame is not None else None
        self._add(step, touch.end, snap, frame.tree if frame is not None else None,
                  target=step.pop("_target", None))

    def key(self, k: KeyPress) -> None:
        if k.code in _NOT_TYPING:
            return
        name = _KEYS.get(k.code)
        if name is None:  # a physical keyboard: what was typed shows up in the field, never here
            self.keyboard_touches += 1
            if self._focus in self._fields:
                fld = self._fields[self._focus]
                fld.keys += 1
                fld.changed_at = k.end
                if fld.secret:
                    self._secret_until = k.end + SECRET_GRACE_S
            return
        frame = self._frame_before(k.start)
        if frame is not None and frame.locked and name not in ("power", "volume_up", "volume_down"):
            self._lock_touch(None)
            return
        if frame is not None:
            self._flush_typing(frame, submitted=False)
        for p in list(self.pending):
            self._finish(p, self.frames[-1] if self.frames else None, settled=False)
        held = k.end - k.start >= 0.8
        step = {"action": "key", "key": name, "sure": True}
        if held:
            step["held_s"] = round(k.end - k.start, 2)
            step["note"] = "held down" + (" (power menu or assistant)" if name == "power" else "")
        snap = self._snap(frame) if frame is not None else None
        self._add(step, k.end, snap, frame.tree if frame is not None else None)

    # ---------------------------------------------------------------- steps
    def _gesture_step(self, g: Gesture, frame: Frame | None, touch: Touch) -> dict[str, Any]:
        w, h = display_size(frame.rotation if frame else 0, self.natural)
        step: dict[str, Any] = {"action": g.kind, "at": [round(g.at[0]), round(g.at[1])],
                                "at_pct": [round(g.at[0] / w, 3), round(g.at[1] / h, 3)]}
        if g.to is not None:
            step["to"] = [round(g.to[0]), round(g.to[1])]
            step["to_pct"] = [round(g.to[0] / w, 3), round(g.to[1] / h, 3)]
        if g.finger:
            step["finger"] = g.finger
            if g.kind in ("swipe", "two_finger_swipe"):
                step["scroll"] = SCROLL_OF_FINGER[g.finger]
        if g.zoom:
            step["zoom"] = g.zoom
        if g.kind not in ("tap",):
            step["duration_s"] = round(g.duration_s, 2)
        if g.fingers > 1:
            step["fingers"] = g.fingers
        notes = [g.note] if g.note else []
        if touch.dropped:
            notes.append("the phone dropped touch events during this gesture: its path is incomplete")
        sure = frame is not None and self._pinned_sure and not touch.dropped
        if frame is None:
            notes.append("the screen had not been read yet when this started")
        system = g.kind in ("back", "home", "recents", "notifications")
        if frame is not None and not system:
            snap = self._snap(frame)
            if g.kind in ("swipe", "two_finger_swipe", "pinch"):
                box = container_at(snap, *g.at)
                if box is not None:
                    step["container"] = describe(box, frame.tree, self.platform, snap)
                    step["_target"] = box
            else:
                el = element_at(snap, *g.at)
                if self._redact_pin(snap, el, frame.tree):
                    self._secret_until = touch.end + SECRET_GRACE_S
                    step = {"action": g.kind, "redacted": True, "sure": sure,
                            "note": "this looked like a PIN-pad key: not recorded"}
                    return step
                if el is not None:
                    step["target"] = describe(el, frame.tree, self.platform, snap)
                    step["_target"] = el
                    if _is_field(el):
                        step["focuses_field"] = True
                    if el.ax_role == "AXWebArea":
                        step["in_web"] = True
                        notes.append("inside web content (a WebView): Android lists no elements in it, so this step "
                                     "is a position. A test can switch Appium to the WEBVIEW context when the app "
                                     "allows WebView debugging; otherwise replay by position")
                else:
                    notes.append("nothing tappable in the screen tree there (a game, canvas, map or web content): "
                                 "replay by position")
                if g.kind == "drag" and g.to is not None:
                    drop = element_at(snap, *g.to)
                    if drop is not None and drop is not el:
                        step["drop_on"] = describe(drop, frame.tree, self.platform, snap)
            step["app"] = snap.app.name
            step["screen"] = screen_title(frame.tree)
        step["sure"] = sure
        if not sure and frame is not None:
            notes.append("the screen was still changing when the finger went down: the target is the likeliest "
                         "element, check it against the screenshot")
        if notes:
            step["note"] = "; ".join(notes)
        return step

    def _redact_pin(self, snap: Snapshot, el: Element | None, tree: RawTree | None = None) -> bool:
        """A key on a PIN pad: a password field has focus, or the screen is one (a 0-9 grid with no
        calculator keys, or it asks for a PIN). A tap on nothing there may be a key drawn on a canvas."""
        if el is not None and (_is_field(el) or not (len((el.name or "").strip()) <= 2
                                                    or (el.name or "").strip().isdigit())):
            return False
        if any(e.secure and e.focused for e in snap.all_elements):
            return True
        names = {(e.name or "").strip() for e in snap.all_elements}
        if tree is not None:
            names |= {_clean_text(t) for n in tree.root.walk() if n.visible
                      for t in (n.title, n.label, n.value if isinstance(n.value, str) else None) if t}
        digits = {n for n in names if len(n) == 1 and n.isdigit()}
        if len(digits) >= 9 and not {n.lower() for n in names} & _CALC_KEYS:
            return True
        return any(_PIN_WORDS.search(n) for n in names if n)

    def _merge_double(self, g: Gesture, touch: Touch) -> bool:
        if not self.pending:
            return False
        p = self.pending[-1]
        if p.step["action"] != "tap" or touch.start - p.end > self.cfg.double_tap_s:
            return False
        px, py = p.step["at"]
        if math.hypot(g.at[0] - px, g.at[1] - py) > DOUBLE_TAP_DP * self.cfg.dp:
            return False
        p.step["action"] = "double_tap"
        p.end = touch.end
        p.cap = SETTLE_CAP_S["double_tap"]
        return True

    def _lock_touch(self, touch: Touch | None) -> None:
        if self._unlock is None:
            for p in list(self.pending):
                self._finish(p, self.frames[-1] if self.frames else None, settled=False)
            self._unlock = {"action": "unlock", "sure": True,
                            "note": "touches on the lock screen are not recorded (a PIN or pattern would show)"}
            self._add(self._unlock, touch.end if touch else self.clock(), None, None, cap=0.0, shot=False)

    def _add(self, step: dict[str, Any], end: float, before: Snapshot | None, before_tree: RawTree | None, *,
             target: Element | None = None, cap: float | None = None, shot: bool = True) -> None:
        step = {"n": len(self.steps) + 1, "t": self._t(end), **step}
        self.steps.append(step)
        p = _Pending(step, end, SETTLE_CAP_S.get(step["action"], 2.0) if cap is None else cap, before, before_tree, target)
        if not shot:
            p.step["screenshot"] = None
            self._write(p.step)
            return
        self.pending.append(p)

    def tick(self, latest: Frame | None = None) -> None:
        """Write steps whose screen has settled (two readings in a row agree after the step), or
        whose wait ran out."""
        frames = list(self.frames)
        for p in list(self.pending):
            after = [f for f in frames if f.start >= p.end + FIRST_LOOK_S]
            if len(after) >= 2 and after[-1].fingerprint == after[-2].fingerprint:
                self._finish(p, after[-1], settled=True)
            elif after and after[-1].end - p.end >= p.cap:
                self._finish(p, after[-1], settled=False)
        for fld in self._fields.values():
            self._typing_shot(fld)

    def _finish(self, p: _Pending, after: Frame | None, *, settled: bool) -> None:
        if p not in self.pending:
            return
        self.pending.remove(p)
        step = p.step
        if after is not None and p.before is not None and not after.locked:
            now = self._snap(after)
            step["result"] = self.effect(p.before, p.before_tree, now, after.tree, p.target)
            self._check_system(step, p.before, p.before_tree, now, after.tree)
            if ("launcher" in (p.before.app.name or "") and now.app.name != p.before.app.name
                    and "launcher" not in (now.app.name or "")):
                step["launches"] = now.app.name  # a test starts the app by its package, not by its icon
                # The icon's page and position are the person's launcher layout, not the app: a
                # replay that looked for it failed on the next run (Amazon, Pixel 7a 2026-11-17).
                if (t := step.get("target")) is not None:
                    if t.get("locator"):
                        t.setdefault("alternatives", []).insert(0, t["locator"])
                    t["locator"] = {"strategy": "app", "value": now.app.name, "tier": "strong", "unique": True,
                                    "note": "start it by package (Appium activate_app); the icon's place on the "
                                            "launcher is not part of the app"}
        if not settled and step.get("action") not in ("unlock",):
            step["settled"] = False
        step["screenshot"] = self._screenshot(step)
        self._write(step)

    def _check_system(self, step: dict[str, Any], before: Snapshot, before_tree: RawTree | None, after: Snapshot,
                      after_tree: RawTree) -> None:
        """A back or home gesture is told by its shape; the screen says whether the phone took it.
        An edge swipe the system ignored (too short, an app that claims the edge) is not "back"."""
        action = step["action"]
        if action not in ("back", "home", "recents"):
            return
        result = step.get("result", "")
        if action == "back":
            took = (after.app.name != before.app.name
                    or (before_tree is not None and screen_title(before_tree) != screen_title(after_tree))
                    or "closed:" in result or "keyboard closed" in result
                    or (before.window and after.window and before.window.title != after.window.title))
        else:
            took = after.app.name != before.app.name or "launcher" in after.app.name
        if not took:
            step["sure"] = False
            step["note"] = "; ".join(filter(None, [step.get("note"), f"shaped like {action}, but the screen did not "
                                                    f"change the way {action} does: check the screenshot"]))

    def _screenshot(self, step: dict[str, Any]) -> str | None:
        if self.shoot is None:
            return None
        if self.clock() < self._secret_until:
            step["screenshot_note"] = "not taken: a password was just typed (Android shows its last character)"
            return None
        name = f"step-{step['n']:02d}-{step['action']}.png"
        try:
            ok = self.shoot(self.folder / name)
        except Exception as exc:  # noqa: BLE001 - evidence is optional, the step is not
            step["screenshot_note"] = f"failed: {exc}"
            return None
        if not ok:
            step["screenshot_note"] = "failed"
            return None
        return name

    def _write(self, step: dict[str, Any]) -> None:
        with self._jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(step, ensure_ascii=False) + "\n")
            fh.flush()

    # ---------------------------------------------------------------- typing
    def _watch_fields(self, f: Frame, prev: Frame | None) -> None:
        snap = self._snap(f)
        fields = [el for el in snap.all_elements if _is_field(el)]
        counts: dict[tuple, int] = {}
        focus = None
        seen: set[tuple] = set()
        for el in sorted(fields, key=lambda e: (e.frame.y, e.frame.x) if e.frame else (0, 0)):
            base = _field_key(el)
            k = base + (counts.get(base, 0),)
            counts[base] = counts.get(base, 0) + 1
            seen.add(k)
            secret = self._is_secret(el)
            value = None if secret else (el.value or "")
            if el.focused:
                focus = k
            fld = self._fields.get(k)
            if fld is None:
                fld = self._fields[k] = _Field(el, f.tree, value, value, f.end, secret=secret,
                                               seen=hash(el.value) if secret and el.value else None, seen_start=f.start)
                if el.focused and self._orphan_keys:
                    # the keys went here before any reading showed the field: what it held before is unknown
                    fld.first, fld.keys, fld.key_times = None, len(self._orphan_keys), self._orphan_keys
                    fld.changed_at = f.end
                    if secret:
                        self._secret_until = max(self._secret_until, self._orphan_keys[-1] + SECRET_GRACE_S)
                    self._orphan_keys = []
                continue
            fld.seen_start = f.start
            if secret and not fld.secret:  # a field turned out to be a password: forget what it showed before
                fld.secret, fld.first, fld.last = True, None, None
                fld.keys = max(fld.keys, 1)
            if fld.secret:
                value = None
                plain = hash(el.value) if el.value else None
                if plain is not None and plain != fld.seen:  # new text, kept only as a hash
                    fld.keys += 1
                    fld.changed_at = f.end
                fld.seen = plain
            if value != fld.last:
                fld.last = value
                fld.changed_at = f.end
                fld.idle_keys = 0
            fld.el, fld.tree = el, f.tree
        if self._focus is not None and focus != self._focus and self._focus in self._fields:
            keyboard_closed = prev is not None and prev.keyboard is not None and f.keyboard is None
            self._flush_field(self._focus, f, submitted=keyboard_closed)
        elif prev is not None and prev.keyboard is not None and f.keyboard is None and self._focus is not None:
            self._flush_field(self._focus, f, submitted=True)
        self._focus = focus
        self._orphan_keys = []   # keys no reading could tie to a field stay counted, nothing more
        for k in [k for k in self._fields if k not in seen]:  # a field that left the screen
            self._flush_field(k, f, submitted=prev is not None and prev.keyboard is not None and f.keyboard is None)
            del self._fields[k]

    def _is_secret(self, el: Element) -> bool:
        ident = el.extra.get("identifier") or ""
        if el.secure:
            if ident:
                self._secret_ids.add(ident)
            return True
        if ident and ident in self._secret_ids:
            return True
        words = [ident.rpartition("/")[2], el.name or "", el.extra.get("description") or "",
                 el.extra.get("placeholder") or ""]
        return any(_SECRET_WORDS.search(w.replace("_", " ")) for w in words if w)

    def _changed(self, fld: _Field) -> bool:
        return fld.last != fld.first or (fld.secret and fld.keys > 0)

    def _typing_shot(self, fld: _Field) -> None:
        """A field that has stopped changing gets its picture now, before the next touch moves on."""
        if (self.shoot is None or not self._changed(fld) or fld.secret or fld.shot_value == fld.last
                or self.clock() - fld.changed_at < TYPING_SHOT_IDLE_S):
            return
        name = f"typing-{len(self.steps) + 1:02d}.png"
        try:
            if self.shoot(self.folder / name):
                fld.shot, fld.shot_value = name, fld.last
        except Exception:  # noqa: BLE001
            pass

    def _flush_typing(self, f: Frame, *, submitted: bool, shoot: bool = True) -> None:
        for k in list(self._fields):
            self._flush_field(k, f, submitted=submitted, shoot=shoot)

    def _flush_field(self, k: tuple, f: Frame, *, submitted: bool, shoot: bool = True) -> None:
        fld = self._fields.get(k)
        if fld is None or not self._changed(fld):
            return
        el = fld.el
        step: dict[str, Any] = {"action": "type", "target": describe(el, fld.tree, self.platform, self._snap(f))}
        if fld.secret:
            step["data"], step["secret"] = SECRET, True
        else:
            step["data"] = fld.last or ""
            if fld.first and not (fld.last or "").startswith(fld.first):
                step["cleared"] = True  # the field held text that was deleted first
            notes = []
            if el.value_digest is not None:
                notes.append("the value is longer than the screen tree shows: cut")
            if not fld.last and fld.first is None:
                step["data"] = None
                notes.append(f"{fld.keys} key presses, but no screen reading saw the text: fill it in from what was typed")
            elif not fld.last:
                step["cleared"], step["data"] = True, ""
            late = sum(1 for t in fld.key_times if t > fld.seen_start) - (1 if submitted else 0)
            if late > 0 and step["data"]:
                notes.append(f"{late} key press{'es' if late > 1 else ''} came after the last reading of the field: "
                             "the text may be incomplete; check it against the next screenshot")
            if notes:
                step["note"] = "; ".join(notes)
        if submitted:
            step["submitted"] = True
            step["submit_note"] = "the keyboard closed with no other touch: Enter / Done was pressed"
        elif fld.idle_keys and not fld.secret:
            step["maybe_submitted"] = True
            step["submit_note"] = (f"{fld.idle_keys} keyboard touch{'es' if fld.idle_keys > 1 else ''} after the last "
                                   "change did not change the text: Enter / search, Shift, or a missed key")
        step["sure"] = True
        step["app"] = f.tree.app.name
        step = {"n": len(self.steps) + 1, "t": self._t(fld.changed_at or f.end), **step}
        if fld.secret:
            step["screenshot"] = None
            step["screenshot_note"] = "not taken for a password field"
        elif fld.shot is not None and fld.shot_value == fld.last:
            final = f"step-{step['n']:02d}-type.png"
            try:
                os.replace(self.folder / fld.shot, self.folder / final)
                step["screenshot"] = final
            except OSError:
                step["screenshot"] = fld.shot
        elif shoot:
            step["screenshot"] = self._screenshot(step)
        else:
            step["screenshot"] = None
        self.steps.append(step)
        self._write(step)
        fld.first, fld.keys, fld.idle_keys, fld.shot, fld.shot_value = fld.last, 0, 0, None, None
        fld.key_times = []

    # ---------------------------------------------------------------- what an action did
    def effect(self, before: Snapshot, before_tree: RawTree | None, after: Snapshot, after_tree: RawTree,
               target: Element | None) -> str:
        if after.app.name != before.app.name:
            if "launcher" in after.app.name:
                return f"home screen ({after.app.name})"
            return f"opened {after.app.name}"
        parts: list[str] = []
        old = {(o.kind, o.package) for o in (before_tree.overlays if before_tree else [])}
        new = {(o.kind, o.package) for o in after_tree.overlays}
        parts += [f"opened: {kind}" for kind, _ in sorted(new - old)]
        parts += [f"closed: {kind}" for kind, _ in sorted(old - new)]
        old_title = screen_title(before_tree) if before_tree is not None else None
        new_title = screen_title(after_tree)
        if old_title != new_title and new_title:
            parts.append(f"new screen: {new_title}")
        if (before_tree is not None and not before_tree.keyboard) and after_tree.keyboard:
            parts.append("keyboard opened")
        elif before_tree is not None and before_tree.keyboard and not after_tree.keyboard:
            parts.append("keyboard closed")
        diff = diff_snapshots(before, after)
        if target is not None:
            now = after.element(target.ref)
            if now is not None:
                changes = element_changes(target, now)
                label = json.dumps(_label(target, before) or "", ensure_ascii=False)
                for name, (a, b) in changes.items():
                    if name in ("value", "checked", "selected", "expanded", "name"):
                        if target.role in ("switch", "checkbox", "toggle", "radio") and name == "value":
                            words = {"true": "on", "false": "off", "1": "on", "0": "off"}
                            a, b = words.get(str(a), a), words.get(str(b), b)
                            parts.append(f"{label} {target.role}: {a} → {b}")
                        else:
                            parts.append(f"{target.role} {label} {name}: {a} → {b}")
                        break
        if not parts:
            n = len(diff.added) + len(diff.removed) + len(diff.changed)
            if n:
                parts.append(f"{len(diff.changed)} changed, {len(diff.added)} added, {len(diff.removed)} removed")
            else:
                parts.append("no visible change in the screen tree")
        return "; ".join(parts)

    # ---------------------------------------------------------------- the end
    def stop(self, *, reason: str | None = None) -> dict[str, Any]:
        latest = self.frames[-1] if self.frames else None
        if self._down is not None:
            self.notes.append("a finger was still down when the recording stopped: that gesture is not recorded")
        if latest is not None:
            self._flush_typing(latest, submitted=False)
        for p in list(self.pending):
            self._finish(p, latest, settled=False)
        if reason:
            self.notes.append(reason)
        if self.keyboard_touches:
            self.notes.append(f"{self.keyboard_touches} keyboard touches were not stored (only the text they produced)")
        workflow = {
            "platform": self.platform,
            "device": self.device,
            "duration_s": round(self.clock() - self.started, 1),
            "steps": self.steps,
            "unsure": sum(1 for s in self.steps if s.get("sure") is False),
            "notes": self.notes,
        }
        (self.folder / "workflow.json").write_text(json.dumps(workflow, ensure_ascii=False, indent=1))
        return workflow


# --------------------------------------------------------------------------- the table the tool returns
def _short(text: str | None, n: int) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def step_target(step: dict[str, Any]) -> str:
    t = step.get("target") or step.get("container")
    if step.get("redacted"):
        return "(not recorded)"
    if step.get("launches"):
        return f"launches {step['launches']}"
    if step.get("in_web") and "at_pct" in step:
        return f"web content at {step['at_pct'][0]:.0%},{step['at_pct'][1]:.0%}"
    if t is None:
        if step["action"] in ("tap", "long_press", "double_tap", "drag") and "at_pct" in step:
            return f"at {step['at_pct'][0]:.0%},{step['at_pct'][1]:.0%}"
        return ""
    name = (f'"{_short(t.get("name"), 28)}"' if t.get("name") else f'in "{_short(t["in"], 25)}"' if t.get("in")
            else (t.get("where") and f"unlabeled, {t['where']}") or "")
    return f"{t.get('role', '')} {name}".strip()


def step_locator(step: dict[str, Any]) -> str:
    t = step.get("target") or step.get("container")
    loc = t.get("locator") if t else None
    if not loc:
        return "none unique" if t else ""
    return f"{loc['tier']}: {loc['strategy']} {_short(loc['value'], 48)}"


def step_action(step: dict[str, Any]) -> str:
    a = step["action"]
    if a == "key":
        return f"key {step['key']}"
    if a in ("swipe", "two_finger_swipe"):
        return f"{a.replace('_', ' ')} {step.get('finger', '')}".strip()
    if a == "pinch":
        return f"pinch {step.get('zoom', '')}"
    if a == "rotate":
        return f"rotate {step.get('orientation', '')}"
    return a.replace("_", " ")


def format_table(workflow: dict[str, Any], folder: Path) -> str:
    steps = workflow["steps"]
    head = (f"Recorded {len(steps)} step{'s' if len(steps) != 1 else ''} on {workflow['device'] or 'the device'} "
            f"in {workflow['duration_s']:g} s" + (f", {workflow['unsure']} unsure" if workflow["unsure"] else "") + ".")
    lines = [head, f"Folder: {folder}", "",
             "#   action          target                          locator (best)                                    data          screenshot"]
    for s in steps:
        data = s.get("data", "")
        flags = []
        if s.get("sure") is False:
            flags.append("UNSURE")
        if s.get("settled") is False:
            flags.append("not settled")
        if s.get("submitted"):
            flags.append("submitted")
        elif s.get("maybe_submitted"):
            flags.append("Enter?")
        lines.append(f"{s['n']:<3} {_short(step_action(s), 15):<15} {_short(step_target(s), 31):<31} "
                     f"{_short(step_locator(s), 49):<49} {_short(data, 13):<13} {s.get('screenshot') or '-'}"
                     + (f"  [{', '.join(flags)}]" if flags else ""))
        if s.get("result"):
            lines.append(f"      → {_short(s['result'], 110)}")
    if workflow["notes"]:
        lines.append("")
        lines += [f"note: {n}" for n in workflow["notes"]]
    lines.append(f"\nFull detail (every locator, coordinates, timings): {folder / 'workflow.json'}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- running it on a device
class Session:
    """A recording in progress: a reader thread queues touch events; one loop thread reads the
    screen and feeds the :class:`Recorder`, so the device is only ever driven from that thread."""

    def __init__(self, engine: Any, folder: Path, *, device: str = "", poll_s: float = 0.03,
                 shoot: bool = True) -> None:
        self.engine = engine
        self.folder = folder
        stream = engine.touch_stream()
        self.stream = stream
        cfg = engine.input_config()
        self.recorder = Recorder(folder, platform=engine.platform, natural=stream.natural, config=cfg,
                                 shoot=(lambda p: bool(engine.screenshot(str(p)))) if shoot else None, device=device)
        self.tracker = TouchTracker(stream.panels, stream.natural)
        self.clock = Clock()
        self.events: queue.Queue = queue.Queue()
        self.poll_s = poll_s
        self._stop = threading.Event()
        self.error: str | None = None
        self.result: dict[str, Any] | None = None
        self._reader = threading.Thread(target=self._read, name="mobilepilot-record-read", daemon=True)
        self._loop = threading.Thread(target=self._run, name="mobilepilot-record-loop", daemon=True)
        self.reader_ended = False

    def start(self) -> None:
        self._reader.start()
        self._loop.start()

    def _read(self) -> None:
        try:
            for arrival, line in self.stream.lines():
                ev = parse_line(line)
                if ev is None:
                    continue
                for out in self.tracker.feed(ev):
                    self.events.put((arrival, out))
                if self._stop.is_set():
                    return
        except Exception as exc:  # noqa: BLE001
            self.error = f"the touch stream failed: {exc}"
        finally:
            self.reader_ended = True

    def _drain(self) -> None:
        while True:
            try:
                arrival, out = self.events.get_nowait()
            except queue.Empty:
                return
            if isinstance(out, TouchStart):
                self.recorder.touch_start(self.clock.host(out.t, arrival), out.x, out.y)
            elif isinstance(out, Touch):
                start = self.clock.host(out.start, arrival)
                end = self.clock.host(out.end, arrival)
                shift = start - out.start
                fingers = [[(t + shift, x, y) for t, x, y in f] for f in out.fingers]
                self.recorder.touch(Touch(out.device, start, end, fingers, out.dropped))
            elif isinstance(out, KeyPress):
                self.recorder.key(KeyPress(out.code, self.clock.host(out.start, arrival), self.clock.host(out.end, arrival)))

    def _run(self) -> None:
        failures = 0
        try:
            while not self._stop.is_set():
                self._drain()
                begin = time.monotonic()
                try:
                    state = self.engine.record_state()
                    failures = 0
                except Exception as exc:  # noqa: BLE001 - a dump can fail once (u2 "shell output invalid")
                    failures += 1
                    if failures >= 5:
                        self.error = f"reading the screen failed 5 times in a row: {exc}"
                        break
                    time.sleep(0.3)
                    continue
                state.start, state.end = begin, time.monotonic()
                self.recorder.reading(state)   # touches made while it ran are judged by it too
                self._drain()
                self.recorder.frame(state)
                if self.reader_ended and self.events.empty():
                    self.error = self.error or "the touch stream ended (phone unplugged?)"
                    break
                if self.poll_s:
                    time.sleep(self.poll_s)
        finally:
            self._drain()
            self.result = self.recorder.stop(reason=self.error or self.tracker.unsupported)

    def stop(self, timeout_s: float = 20.0) -> dict[str, Any]:
        self._stop.set()
        self._loop.join(timeout_s)
        try:
            self.stream.close()
        except Exception:  # noqa: BLE001
            pass
        if self.result is None:
            self.result = self.recorder.stop(reason="the recording loop did not finish in time")
        return self.result

    @property
    def running(self) -> bool:
        return self._loop.is_alive()


@dataclass(slots=True)
class ScriptedStream:
    """A touch stream for tests: getevent lines with the host time each arrives."""

    panels: dict[str, Panel]
    natural: tuple[int, int]
    script: list[tuple[float, str]] = field(default_factory=list)
    """(seconds after start, line)."""
    _closed: threading.Event = field(default_factory=threading.Event)

    def lines(self) -> Iterator[tuple[float, str]]:
        begin = time.monotonic()
        for delay, line in self.script:
            wait = begin + delay - time.monotonic()
            if wait > 0 and self._closed.wait(wait):
                return
            if self._closed.is_set():
                return
            yield time.monotonic(), line
        self._closed.wait()

    def close(self) -> None:
        self._closed.set()


class ProcessStream:
    """``getevent -lt`` from a subprocess, read with ``os.read`` as bytes arrive (a buffered reader,
    and adbutils' stream, delivered nothing until the process ended: emulator, 2026-11-16)."""

    def __init__(self, argv: list[str], panels: dict[str, Panel], natural: tuple[int, int]) -> None:
        import subprocess

        self.panels = panels
        self.natural = natural
        self._proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                      bufsize=0)

    def lines(self) -> Iterator[tuple[float, str]]:
        fd = self._proc.stdout.fileno()
        tail = b""
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return
            arrival = time.monotonic()
            tail += chunk
            *done, tail = tail.split(b"\n")
            for raw in done:
                yield arrival, raw.decode("utf-8", "replace").rstrip("\r")

    def close(self) -> None:
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(3)
            except Exception:  # noqa: BLE001
                self._proc.kill()

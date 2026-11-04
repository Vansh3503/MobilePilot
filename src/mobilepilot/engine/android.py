"""Android: emulators and real phones (USB or wireless adb) through uiautomator2.

Measured on the qa-test emulator (API 34), 2026-11-03: ``uiautomator2.dump_hierarchy`` takes
~51 ms per screen against ~2,200 ms for ``adb exec-out uiautomator dump``, because uiautomator2
keeps a server running on the device instead of starting the uiautomator CLI for every dump
(``spikes/android_spike.out``). Taps take ~150 ms, screenshots ~30 ms.

:func:`parse_hierarchy` turns the dump into AX-style :class:`RawNode` roles so the perception
layer copied from AXPilot (pruning, session refs, diffs) works unchanged.
"""

from __future__ import annotations

import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from typing import Literal

from mobilepilot.engine.base import AppAction, Device, Engine
from mobilepilot.errors import ErrorCode, MobilePilotError
from mobilepilot.models import AppInfo, Overlay, RawNode, RawTree, Rect, WindowInfo

# Simple class name (after the last dot) → (AX role, AX subrole). Anything missing is a layout.
_ROLES: dict[str, tuple[str, str | None]] = {
    "Button": ("AXButton", None), "ImageButton": ("AXButton", None), "MaterialButton": ("AXButton", None),
    "FloatingActionButton": ("AXButton", None), "ExtendedFloatingActionButton": ("AXButton", None),
    "Chip": ("AXButton", None),
    "TextView": ("AXStaticText", None), "AppCompatTextView": ("AXStaticText", None),
    "MaterialTextView": ("AXStaticText", None),
    "EditText": ("AXTextField", None), "AppCompatEditText": ("AXTextField", None),
    "AutoCompleteTextView": ("AXTextField", None), "MultiAutoCompleteTextView": ("AXTextField", None),
    "TextInputEditText": ("AXTextField", None),
    "CheckBox": ("AXCheckBox", None), "AppCompatCheckBox": ("AXCheckBox", None),
    "MaterialCheckBox": ("AXCheckBox", None), "CheckedTextView": ("AXCheckBox", None),
    "Switch": ("AXCheckBox", "AXSwitch"), "SwitchCompat": ("AXCheckBox", "AXSwitch"),
    "SwitchMaterial": ("AXCheckBox", "AXSwitch"), "MaterialSwitch": ("AXCheckBox", "AXSwitch"),
    "ToggleButton": ("AXCheckBox", "AXToggle"), "CompoundButton": ("AXCheckBox", None),
    "RadioButton": ("AXRadioButton", None), "AppCompatRadioButton": ("AXRadioButton", None),
    "Spinner": ("AXPopUpButton", None), "AppCompatSpinner": ("AXPopUpButton", None),
    "SeekBar": ("AXSlider", None), "RatingBar": ("AXSlider", None), "Slider": ("AXSlider", None),
    "ProgressBar": ("AXProgressIndicator", None),
    "ImageView": ("AXImage", None), "AppCompatImageView": ("AXImage", None),
    "WebView": ("AXWebArea", None),
    "RecyclerView": ("AXList", None), "ListView": ("AXList", None), "GridView": ("AXList", None),
    "ExpandableListView": ("AXList", None),
    "ScrollView": ("AXScrollArea", None), "HorizontalScrollView": ("AXScrollArea", None),
    "NestedScrollView": ("AXScrollArea", None),
    "TabWidget": ("AXTabGroup", None), "TabLayout": ("AXTabGroup", None),
    "Toolbar": ("AXToolbar", None),
}
_TEXT_INPUT = {"AXTextField"}
_TOGGLES = {"AXCheckBox", "AXRadioButton"}
_PASSIVE_IF_CLICKABLE = {"AXStaticText", "AXImage", "AXUnknown"}
"""A tappable TextView/ImageView/layout is a button to the user; inside a list it is a row."""

_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")
_SYSTEM_UI = "com.android.systemui"
_PERMISSION_PACKAGES = {"com.google.android.permissioncontroller", "com.android.permissioncontroller"}
SYSTEM_DIALOG_PACKAGES = _PERMISSION_PACKAGES | {"android", _SYSTEM_UI}
"""Foreground packages whose windows ask the person something (permissions, choosers, crashes)."""
_DIALOG_IDS = ("alertTitle", "parentPanel", "buttonPanel", "button1", "button2")
_DIALOG_BUTTON_ID = re.compile(r"(ok|cancel|positive|negative|confirm|dismiss)_?button", re.I)
_DIALOG_BUTTONS = {"ok", "cancel", "done", "discard", "keep", "delete", "allow", "don't allow", "not now"}
"""A Material time/date picker has none of AlertDialog's ids; its Cancel/OK pair names it."""
_CRASH_WORDS = ("isn't responding", "keeps stopping", "has stopped")
_SLIDER_ID = re.compile(r"(slider|seek)", re.I)
"""Android 17's volume sliders are an empty ``slider_frame`` with nothing inside in the tree
(Pixel 7a, 2026-11-04); a frame with such an id is the only trace of the control."""


def _flag(attrs: dict[str, str], name: str) -> bool:
    return attrs.get(name) == "true"


def _frame(text: str | None) -> Rect | None:
    m = _BOUNDS.match(text or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return Rect(x1, y1, x2 - x1, y2 - y1)


def _node(el: ET.Element, parent_role: str | None) -> RawNode:
    a = el.attrib
    cls = a.get("class", "")
    role, subrole = _ROLES.get(cls.rpartition(".")[2], ("AXUnknown", None))
    if role == "AXUnknown" and not len(el) and _SLIDER_ID.search(a.get("resource-id", "").rpartition("/")[2]):
        role = "AXSlider"
    clickable = _flag(a, "clickable") or _flag(a, "long-clickable")
    text, desc, hint = a.get("text") or None, a.get("content-desc") or None, a.get("hint") or None
    if role == "AXUnknown" and not clickable and desc and not text:
        # Jetpack Compose puts a view's text in its content-desc (Play Store's result rows,
        # Pixel 7a 2026-11-04): as static text it names the button around it.
        role, text = "AXStaticText", desc
    if a.get("resource-id", "").endswith("/touch_outside") and not desc:
        desc = "Outside the sheet (closes it)"
    if clickable and role in _PASSIVE_IF_CLICKABLE:
        role = "AXRow" if parent_role == "AXList" else "AXButton"
    elif role == "AXButton" and not clickable:
        role = "AXStaticText" if text else "AXImage"  # a decorative icon inside a tappable parent
    secure = _flag(a, "password")
    actions: list[str] = []
    if _flag(a, "clickable"):
        actions.append("tap")
    if _flag(a, "long-clickable"):
        actions.append("long_press")
    if _flag(a, "checkable") and role not in _TOGGLES:
        actions.append("toggle")
    if _flag(a, "scrollable"):
        actions.append("scroll")
    node = RawNode(
        role=role,
        subrole=subrole,
        description=desc,
        placeholder=hint,
        identifier=a.get("resource-id") or None,
        a11y_id=desc,
        native_class=cls or None,
        enabled=a.get("enabled", "true") == "true",
        focused=_flag(a, "focused"),
        selected=_flag(a, "selected"),
        secure=secure,
        visible=a.get("visible-to-user", "true") == "true",
        frame=_frame(a.get("bounds")),
        actions=tuple(actions),
    )
    if role in _TEXT_INPUT:
        # An empty EditText reports its hint as its text on several Android versions.
        node.value = None if secure else ("" if text == hint else (text or ""))
    elif role in _TOGGLES or "toggle" in actions:
        node.title = text
        node.value = "true" if _flag(a, "checked") else "false"
    elif role == "AXStaticText":
        node.value = text
    else:
        node.title = text
    node.handle = handle_of(node)
    node.children = [_node(child, role) for child in el if child.tag == "node"]
    return node


def handle_of(node: RawNode) -> tuple | None:
    """A dump has no live element handles, so a node's identity is what it shows and where:
    class, ids, its own text and its frame. Equal handles in two dumps are the same element, which
    lets a ref come back when its element returns (``carry_refs`` searches older snapshots by
    handle). A field's typed value and a toggle's state are left out: they change, the element
    does not. A recycled list row keeps its handle but changes its name, and ref carrying refuses
    a row whose name changed."""
    f = node.frame
    if f is None:
        return None
    text = node.value if node.role == "AXStaticText" else node.title
    return (node.native_class, node.identifier, node.a11y_id, text, round(f.x), round(f.y), round(f.w), round(f.h))


def parse_hierarchy(
    xml: str, *, package: str | None = None, activity: str | None = None, pid: int = 0,
    size: tuple[int, int] | None = None,
) -> RawTree:
    """Turn a ``dump_hierarchy`` XML into a :class:`RawTree` of the foreground screen.

    Each top-level ``<node>`` is a window. The keyboard's window is dropped (``keyboard=True``
    instead): its keys would double the tree and are never what a test targets. System-UI
    windows (status and navigation bars) are dropped too, unless nothing else is on screen
    (the notification shade is open). Permission dialogs are other apps' windows and stay.
    """
    start = time.perf_counter()
    # The device's XmlSerializer never writes a DTD (app text is escaped), so one here is hostile:
    # refuse it rather than let expat expand entities. Cheaper than a defusedxml dependency.
    if "<!DOCTYPE" in xml or "<!ENTITY" in xml:
        raise MobilePilotError(ErrorCode.COMMAND_FAILED, "The hierarchy dump contains a DTD; refusing to parse it.")
    root_el = ET.fromstring(xml)
    windows = [w for w in root_el if w.tag == "node"]
    ime = [w for w in windows if "inputmethod" in w.get("package", "")]
    keyboard = bool(ime)
    keyboard_frame = _frame(ime[0].get("bounds")) if ime else None
    windows = [w for w in windows if "inputmethod" not in w.get("package", "")]
    if any(w.get("package") != _SYSTEM_UI for w in windows):
        windows = [w for w in windows if w.get("package") != _SYSTEM_UI]
    if package is None:
        package = next((w.get("package") for w in windows if w.get("package")), None) or "unknown"
    nodes = [_node(w, None) for w in windows]
    if size is None:
        frames = [n.frame for n in nodes if n.frame is not None]
        size = (int(max((f.x + f.w for f in frames), default=0)), int(max((f.y + f.h for f in frames), default=0)))
    screen = Rect(0, 0, size[0], size[1])
    overlays = _overlays(windows, nodes, package, screen)
    empty_web = any(n.role == "AXWebArea" and not n.children for n in nodes for n in n.walk())
    title = activity or package
    root = RawNode(role="AXApplication", title=package, frame=screen, children=nodes)
    return RawTree(
        app=AppInfo(name=package, pid=pid),
        window=WindowInfo(window_id=zlib.crc32(title.encode()), pid=pid, app=package, title=title, frame=screen),
        root=root,
        node_count=sum(1 for _ in root.walk()),
        elapsed_ms=(time.perf_counter() - start) * 1000,
        keyboard=keyboard,
        keyboard_frame=keyboard_frame,
        overlays=overlays,
        empty_web=empty_web,
    )


def _overlays(windows: list[ET.Element], nodes: list[RawNode], package: str, screen: Rect) -> list[Overlay]:
    """Every window except the app's main one (its largest own window), named by what it is.
    A system dialog in the foreground (a permission prompt is its own activity) counts whole."""
    def area(n: RawNode) -> float:
        return n.frame.area if n.frame is not None else 0.0

    own = [n for w, n in zip(windows, nodes, strict=True) if w.get("package") == package]
    main = None if package in SYSTEM_DIALOG_PACKAGES or not own else max(own, key=area)
    sheet_ids = ("touch_outside", "design_bottom_sheet")
    if main is not None and any(x.identifier and x.identifier.rpartition("/")[2] in sheet_ids for x in main.walk()):
        # a BottomSheetDialog's window fills the screen behind a dimmed touch_outside backdrop
        # (Amazon's location sheet, Photos' backup sheet; Pixel 7a 2026-11-04)
        main = None
    if main is not None and area(main) < 0.8 * screen.area:
        # Android 17 dumps only the focused window: with a menu open, the menu is the app's only
        # window and the screen under it is missing (Pixel 7a, Clock's overflow, 2026-11-04).
        main = None
    found = []
    for w, n in zip(windows, nodes, strict=True):
        if n is main:
            continue
        pkg = w.get("package", "")
        ids = [e.get("resource-id", "").rpartition("/")[2] for e in w.iter("node")]
        words = " ".join(f"{e.get('text', '')} {e.get('content-desc', '')}" for e in w.iter("node")).casefold()
        f = n.frame
        if pkg in _PERMISSION_PACKAGES:
            kind = "permission dialog"
        elif pkg == _SYSTEM_UI:
            kind = "notification shade"  # pulled down on purpose (mobile_key notifications), not a question
        elif pkg == "android" and any(word in words for word in _CRASH_WORDS):
            kind = "crash/ANR dialog"
        elif pkg != package or pkg in SYSTEM_DIALOG_PACKAGES:
            kind = "system dialog"
        elif any("bottom_sheet" in i for i in ids) or (
                f is not None and f.y + f.h >= screen.h - 2 and f.w >= screen.w - 2 and f.h < screen.h * 0.9):
            kind = "bottom sheet"
        elif any(i in _DIALOG_IDS or _DIALOG_BUTTON_ID.search(i) for i in ids) or _DIALOG_BUTTONS & {
                (e.get("text") or "").strip().casefold() for e in w.iter("node") if e.get("class", "").endswith("Button")}:
            kind = "dialog"
        elif any(e.get("class", "").rpartition(".")[2] in ("ListView", "RecyclerView") for e in w.iter("node")):
            kind = "popup menu"
        else:
            kind = "popup"
        found.append(Overlay(kind, pkg, f, frozenset(x.handle for x in n.walk() if x.handle is not None)))
    return found


_TICKING = re.compile(r'(?:text|content-desc)="[^"]*\d[^"]*"')


def fingerprint(xml: str) -> int:
    """CRC of a dump with the digits inside text left out: a clock, a video position or a
    download percentage changes every frame and would keep a screen "unsettled" for the whole
    budget. Layout (bounds) still counts, so scrolling and transitions are waited for."""
    return zlib.crc32(_TICKING.sub(lambda m: re.sub(r"\d", "", m.group()), xml).encode())


# --------------------------------------------------------------------------- adb discovery
_SDK_DIRS = ("ANDROID_HOME", "ANDROID_SDK_ROOT")
_DEFAULT_SDKS = (
    "~/Library/Android/sdk",                       # Android Studio, macOS
    "~/Android/Sdk",                               # Android Studio, Linux
    "~/AppData/Local/Android/Sdk",                 # Android Studio, Windows
    "/opt/homebrew/share/android-commandlinetools",  # brew install android-commandlinetools
    "/usr/local/share/android-commandlinetools",
)


def find_adb() -> str | None:
    """adb on PATH, else in an Android SDK (``$ANDROID_HOME``, Android Studio's or Homebrew's)."""
    found = shutil.which("adb")
    if found:
        return found
    roots = [os.environ.get(v) for v in _SDK_DIRS] + [os.path.expanduser(p) for p in _DEFAULT_SDKS]
    for root in filter(None, roots):
        for name in ("adb", "adb.exe"):
            candidate = Path(root, "platform-tools", name)
            if candidate.is_file():
                return str(candidate)
    return None


def _adbutils():
    """adbutils talks to the adb server over its socket; it needs the adb binary only to start
    that server, and finds it through ``ADBUTILS_ADB_PATH``."""
    adb = find_adb()
    if adb:
        os.environ.setdefault("ADBUTILS_ADB_PATH", adb)
    import adbutils

    return adbutils


def list_devices() -> list[Device]:
    """Every adb device, including ``unauthorized`` and ``offline`` ones (``doctor`` explains those)."""
    if find_adb() is None:
        return []
    adbutils = _adbutils()
    try:
        adbutils.adb.server_version()  # starts the adb server if it is not running
        infos = adbutils.adb.list(extended=True)
    except Exception:  # no server and no way to start one: no Android devices, not a crash
        return []
    devices = []
    for info in infos:
        kind = "emulator" if info.serial.startswith("emulator-") else "device"
        devices.append(Device(info.serial, "android", kind, info.state, info.tags.get("model", "")))
    return devices


# --------------------------------------------------------------------------- the engine
_KEYS = {"back": "back", "home": "home", "enter": "enter", "delete": "delete", "tab": 61,
         "app_switch": "recent", "volume_up": "volume_up", "volume_down": "volume_down", "search": "search"}

_INJECT_HINT = (
    "The phone refused an injected tap or keystroke. Xiaomi/MIUI: turn on Developer options → "
    "\"USB debugging (Security settings)\". Oppo/Vivo/Realme: turn on \"Disable permission "
    "monitoring\". Then retry."
)


class AndroidEngine(Engine):
    platform = "android"

    def __init__(self, serial: str) -> None:
        _adbutils()
        import uiautomator2 as u2

        self.serial = serial
        try:
            # First connection installs uiautomator2's small server APK through adb; some phones
            # ask the user to allow "install via USB" for that.
            self._d = u2.connect(serial)
            self._size = self._d.window_size()
        except Exception as exc:
            raise MobilePilotError(
                ErrorCode.ENGINE_UNAVAILABLE, f"Could not start uiautomator2 on {serial}: {exc}",
                hint="Unlock the phone and accept any \"install via USB\" prompt, then retry. "
                "`mobilepilot doctor` checks the connection.",
            ) from exc

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except MobilePilotError:
            raise
        except Exception as exc:
            text = str(exc)
            hint = _INJECT_HINT if "INJECT_EVENTS" in text or "SecurityException" in text else None
            raise MobilePilotError(ErrorCode.COMMAND_FAILED, f"{getattr(fn, '__name__', 'call')}: {text}",
                                   hint=hint) from exc

    def _foreground(self) -> dict:
        """The resumed activity. u2's app_current took 5.3 s per call on Android 17 (Pixel 7a,
        2026-11-04); `dumpsys activity activities` answers in ~150 ms. pid comes from pidof."""
        out = self._call(self._d.shell, "dumpsys activity activities | grep -m1 topResumedActivity").output
        m = re.search(r"u\d+ ([\w.]+)/([\w.$]+)", out)
        if not m:
            return self._call(self._d.app_current)
        package, activity = m.group(1), m.group(2)
        pid = self._call(self._d.shell, ["pidof", "-s", package]).output.strip()
        return {"package": package, "activity": activity, "pid": int(pid) if pid.isdigit() else 0}

    def capture_tree(self) -> RawTree:
        xml = self._call(self._d.dump_hierarchy)
        self.last_fingerprint = fingerprint(xml)
        current = self._foreground()
        rotation = re.search(r'<hierarchy rotation="(\d)"', xml)
        if rotation and rotation.group(1) in "13" and self._size[0] < self._size[1]:
            self._size = (self._size[1], self._size[0])
        elif rotation and rotation.group(1) in "02" and self._size[0] > self._size[1]:
            self._size = (self._size[1], self._size[0])
        return parse_hierarchy(xml, package=current.get("package"), activity=current.get("activity"),
                               pid=current.get("pid") or 0, size=self._size)

    def signature(self) -> int:
        """A cheap fingerprint of the screen (no app lookup), for waiting until it settles."""
        return fingerprint(self._call(self._d.dump_hierarchy))

    def tap(self, x: float, y: float, *, hold_s: float = 0.0) -> None:
        if hold_s > 0:
            self._call(self._d.long_click, x, y, hold_s)
        else:
            self._call(self._d.click, x, y)

    def type_text(self, text: str, *, clear: bool = False) -> None:
        # ponytail: set the focused field through accessibility. u2's send_keys installs its
        # ATX keyboard app, which Play Protect blocks as unsafe on Android 17 (Pixel 7a,
        # 2026-11-04) and the call then hangs. A field with no focus falls back to `input text`.
        field = self._d(focused=True)
        if self._call(field.exists):
            current = "" if clear else (self._call(field.get_text) or "")
            self._call(field.set_text, current + text)
        else:
            self._call(self._d.shell, ["input", "text", text.replace(" ", "%s")])

    def swipe(self, x1: float, y1: float, x2: float, y2: float, *, duration_s: float = 0.3) -> None:
        self._call(self._d.swipe, x1, y1, x2, y2, duration_s)

    def pixels(self) -> int | None:
        image = self._call(self._d.screenshot).convert("L")
        w, h = image.size
        small = image.crop((0, h // 20, w, h)).resize((36, 80))  # no status bar: its clock ticks
        return zlib.crc32(bytes(v >> 5 for v in small.tobytes()))  # coarse: compression noise is not a change

    def double_tap(self, x: float, y: float) -> None:
        self._call(self._d.double_click, x, y)

    def drag(self, x1: float, y1: float, x2: float, y2: float, *, hold_s: float = 0.6,
             duration_s: float = 0.8) -> None:
        # u2's own drag never holds first, and Android only starts a drag-and-drop (list
        # reordering, launcher icons) after a long press; sliders and maps follow either way.
        # each move is one RPC (~50 ms on a Pixel 7a): one per 50 ms, not per 20 ms (a 0.8 s drag took 6 s)
        touch, steps = self._d.touch, max(5, int(duration_s / 0.05))
        self._call(touch.down, x1, y1)
        try:
            time.sleep(hold_s)
            for i in range(1, steps + 1):
                self._call(touch.move, x1 + (x2 - x1) * i / steps, y1 + (y2 - y1) * i / steps)
                time.sleep(duration_s / steps)
            time.sleep(0.2)  # a drop target registers the hover before the release
        finally:
            self._call(touch.up, x2, y2)

    def pinch(self, cx: float, cy: float, spread: float, *, zoom: Literal["in", "out"]) -> None:
        near, far = spread * 0.1, spread * 0.4
        start, end = (near, far) if zoom == "in" else (far, near)
        self._call(self._d().gesture, (cx - start, cy), (cx + start, cy), (cx - end, cy), (cx + end, cy), 60)

    def rotate(self, orientation: Literal["portrait", "landscape", "auto"]) -> None:
        # set_orientation also turns auto-rotate off; "auto" puts back the phone's own two
        # settings (it was off on the Pixel 7a), not "rotation on", which would change them.
        keys = ("accelerometer_rotation", "user_rotation")
        if orientation == "auto":
            saved, self._rotation = getattr(self, "_rotation", None), None
            if saved is None:
                return self._call(self._d.freeze_rotation, False)
            for key, value in zip(keys, saved):
                self._call(self._d.shell, ["settings", "put", "system", key, value])
            if saved[0] == "0" and saved[1].isdigit():  # a fixed rotation: wait until it is on screen
                self._wait_rotation(int(saved[1]) % 2 == 1)
            return None
        if getattr(self, "_rotation", None) is None:
            self._rotation = tuple(self._call(self._d.shell, ["settings", "get", "system", k]).output.strip()
                                   for k in keys)
        self._call(self._d.set_orientation, "natural" if orientation == "portrait" else "left")
        self._wait_rotation(orientation == "landscape")

    def _wait_rotation(self, landscape: bool, timeout_s: float = 4.0) -> None:
        """Rotation finishes after the call returns; a dump taken in between still shows the old
        layout (Pixel 7a, Calculator, 2026-11-04). Wait until the dump reports the new one."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            m = re.search(r'<hierarchy rotation="(\d)"', self._call(self._d.dump_hierarchy))
            if m and (int(m.group(1)) % 2 == 1) == landscape:
                return
            time.sleep(0.15)

    def press_key(self, key: str) -> None:
        if key == "notifications":
            return self._call(self._d.open_notification)
        if key == "quick_settings":
            return self._call(self._d.open_quick_settings)
        if key not in _KEYS:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown key {key!r}.",
                                   hint="Use one of: " + ", ".join(_KEYS) + ".")
        self._call(self._d.press, _KEYS[key])

    def ensure_awake(self) -> str | None:
        # ~one shell round trip; the server calls this only after 10 s without an action, since a
        # phone never times out while it is being driven.
        out = self._call(self._d.shell, "dumpsys power 2>/dev/null | grep -m1 -o 'mWakefulness=[A-Za-z]*'; "
                         "dumpsys window 2>/dev/null | grep -m1 -o 'isKeyguardShowing=[a-z]*'; "
                         "dumpsys trust 2>/dev/null | grep -m1 -o 'deviceLocked=[01]'").output
        did = []
        if "mWakefulness=Awake" not in out:
            self._call(self._d.shell, ["input", "keyevent", "KEYCODE_WAKEUP"])
            did.append("woke the screen")
            time.sleep(0.6)
        if "isKeyguardShowing=true" in out or did:
            if "deviceLocked=1" in out:
                raise MobilePilotError(ErrorCode.NEEDS_USER, "The phone is locked with a PIN, pattern or password.",
                                       hint="Ask the person to unlock it (and keep the screen on), then retry. "
                                       "Never type a PIN.")
            if "isKeyguardShowing=true" in out:
                self._call(self._d.shell, ["wm", "dismiss-keyguard"])
                did.append("dismissed the lock screen (no PIN)")
                time.sleep(0.6)
        return ", ".join(did) or None

    def app(self, action: AppAction, *, app: str | None = None, path: str | None = None,
            permission: str | None = None, url: str | None = None) -> str:
        d = self._d
        if action == "open":
            cmd = ["am", "start", "-W", "-a", "android.intent.action.VIEW", "-d", url or ""]
            out = self._call(d.shell, cmd + (["-p", app] if app else []), timeout=60).output
            if "Error" in out or "unable to resolve" in out.lower():
                raise MobilePilotError(ErrorCode.APP_NOT_FOUND, f"No app opens {url}: {out.strip()[:300]}",
                                       hint="Check the link, or pass app with the package that should handle it.")
            return f"opened {url}" + (f" in {app}" if app else "")
        if action == "current":
            cur = self._foreground()
            return f"{cur.get('package')} / {cur.get('activity')} (pid {cur.get('pid')})"
        if action == "install":
            if not path or not Path(path).is_file():
                raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"No APK at {path!r}.",
                                       hint="Pass the local path of a built .apk.")
            self._call(d.app_install, path)
            return f"installed {Path(path).name}"
        if not app:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"{action} needs app (the package name).")
        if action == "launch":
            # uiautomator2's app_start uses `monkey`, which left the launcher in front on the API 34
            # emulator; resolve the launcher activity and start it with `am start -W` instead.
            found = self._call(d.shell, ["cmd", "package", "resolve-activity", "--brief", "-c",
                                         "android.intent.category.LAUNCHER", app]).output.strip().splitlines()
            component = found[-1].strip() if found and "/" in found[-1] else None
            if component is None and not any("No activity found" in line for line in found):
                # `cmd package` only exists from Android 7.0 (API 24); older phones get monkey.
                if self._call(d.shell, ["pm", "path", app]).output.strip():
                    self._call(d.app_start, app, wait=True)
                    return f"launched {app}"
            if component is None:
                raise MobilePilotError(ErrorCode.APP_NOT_FOUND, f"{app} is not installed or has no launcher activity.",
                                       hint="Check the package name (mobile_app current shows the foreground one), "
                                       "or install it with mobile_app install.")
            out = self._call(d.shell, ["am", "start", "-W", "-n", component], timeout=60)
            if "Error" in out.output:
                raise MobilePilotError(ErrorCode.COMMAND_FAILED, f"am start {component}: {out.output.strip()[:300]}")
            return f"launched {app} ({component})"
        if action == "stop":
            self._call(d.app_stop, app)
            return f"stopped {app}"
        if action == "clear":
            self._call(d.app_clear, app)
            return f"cleared all data of {app}"
        if action == "grant":
            if not permission:
                raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "grant needs permission (android.permission.CAMERA…).")
            perm = permission if "." in permission else f"android.permission.{permission.upper()}"
            out = self._call(d.shell, ["pm", "grant", app, perm])
            if out.exit_code:
                raise MobilePilotError(ErrorCode.COMMAND_FAILED, f"pm grant {app} {perm}: {out.output.strip()}")
            return f"granted {perm} to {app}"
        raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown app action {action!r}.")

    def screenshot(self, path: str, *, max_side: int = 1280) -> tuple[int, int]:
        image = self._call(self._d.screenshot)
        image.thumbnail((max_side, max_side))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        image.save(path, format="PNG")
        return image.size

    def close(self) -> None:
        """Stop uiautomator2's on-device server: Android allows one UiAutomation client, and an
        Appium session cannot start while this one holds it (measured 2026-11-03)."""
        if self._d is not None:
            # stop_uiautomator only drops this client's handle when an earlier connection started
            # the server; the app_process kept running and Appium still could not start. End it.
            for stop in (self._d.stop_uiautomator, lambda: self._d.shell(["pkill", "-f", "com.wetest.uia2.Main"])):
                try:
                    stop()
                except Exception:  # noqa: BLE001 - the device may be gone; releasing must not fail
                    pass
        self._d = None

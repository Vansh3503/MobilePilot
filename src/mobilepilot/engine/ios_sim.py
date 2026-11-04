"""iOS Simulator through idb (``idb ui …``) and ``xcrun simctl``.

UNVERIFIED LIVE: written on a Mac without Xcode, so neither idb nor a simulator could be run.
:func:`parse_idb` is tested against a hand-written fixture in the shape idb documents for
``idb ui describe-all --json --nested``; the first live run should record a real fixture.

idb reads the simulator's accessibility tree through the same AX API AXPilot uses on macOS, so its
``role`` values are already AX names (``AXButton``); ``type`` (``Button``) is the XCUIElementType.
Real iPhones (WebDriverAgent, signing) are phase 2; until then they stay on appium-mcp.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import zlib
from pathlib import Path
from typing import Any

from mobilepilot.engine.android import handle_of
from mobilepilot.engine.base import AppAction, Device, Engine
from mobilepilot.errors import ErrorCode, MobilePilotError
from mobilepilot.models import AppInfo, RawNode, RawTree, Rect, WindowInfo

# XCUIElementType (idb "type") → (AX role, subrole), for elements whose idb role is missing.
_TYPES: dict[str, tuple[str, str | None]] = {
    "Button": ("AXButton", None), "StaticText": ("AXStaticText", None), "TextField": ("AXTextField", None),
    "SecureTextField": ("AXTextField", "AXSecureTextField"), "SearchField": ("AXTextField", "AXSearchField"),
    "TextView": ("AXTextArea", None), "Switch": ("AXCheckBox", "AXSwitch"), "Toggle": ("AXCheckBox", "AXSwitch"),
    "Cell": ("AXRow", None), "Table": ("AXList", None), "CollectionView": ("AXList", None),
    "ScrollView": ("AXScrollArea", None), "Image": ("AXImage", None), "Link": ("AXLink", None),
    "Slider": ("AXSlider", None), "NavigationBar": ("AXToolbar", None), "TabBar": ("AXTabGroup", None),
    "Alert": ("AXGroup", "AXDialog"), "Sheet": ("AXSheet", None), "PickerWheel": ("AXPopUpButton", None),
    "SegmentedControl": ("AXRadioGroup", None), "Application": ("AXApplication", None),
}
_KNOWN_ROLES = {role for role, _ in _TYPES.values()}


def _frame(el: dict[str, Any]) -> Rect | None:
    f = el.get("frame")
    if isinstance(f, dict) and {"x", "y", "width", "height"} <= f.keys():
        return Rect(float(f["x"]), float(f["y"]), float(f["width"]), float(f["height"]))
    return None


def _node(el: dict[str, Any]) -> RawNode:
    kind = str(el.get("type") or "")
    role, subrole = el.get("role"), el.get("subrole")
    if role not in _KNOWN_ROLES or kind in ("Switch", "Toggle", "SecureTextField", "Cell"):
        role, subrole = _TYPES.get(kind, (role or "AXUnknown", subrole))
    secure = subrole == "AXSecureTextField"
    ident = el.get("AXUniqueId") or None
    value = None if secure else el.get("AXValue")
    if subrole == "AXSwitch" and value is not None:
        value = "true" if str(value) in ("1", "true") else "false"
    node = RawNode(
        role=role,
        subrole=subrole,
        label=el.get("AXLabel") or None,
        title=el.get("title") or None,
        help=el.get("help") or None,
        value=value,
        identifier=ident,
        a11y_id=ident,
        native_class=f"XCUIElementType{kind}" if kind else None,
        enabled=bool(el.get("enabled", True)),
        secure=secure,
        frame=_frame(el),
        actions=tuple(str(a) for a in el.get("custom_actions") or ()),
    )
    node.handle = handle_of(node)
    node.children = [_node(child) for child in el.get("children") or ()]
    return node


def parse_idb(elements: list[dict[str, Any]], *, bundle_id: str | None = None) -> RawTree:
    """``idb ui describe-all --json [--nested]`` output → :class:`RawTree`. A flat list (no
    ``children``) becomes children of the application node, which the snapshot builder handles."""
    start = time.perf_counter()
    nodes = [_node(el) for el in elements]
    app_el = next((el for el in elements if el.get("type") == "Application" or el.get("role") == "AXApplication"), {})
    if len(nodes) == 1 and nodes[0].role == "AXApplication":
        root = nodes[0]
    else:
        root = RawNode(role="AXApplication", children=[n for n in nodes if n.role != "AXApplication"])
    name = app_el.get("AXLabel") or bundle_id or "app"
    pid = int(app_el.get("pid") or 0)
    screen = _frame(app_el) or next((n.frame for n in nodes if n.frame is not None), None) or Rect(0, 0, 0, 0)
    root.frame = screen
    return RawTree(
        app=AppInfo(name=name, pid=pid, bundle_id=bundle_id),
        window=WindowInfo(window_id=zlib.crc32(name.encode()), pid=pid, app=name, title=name, frame=screen),
        root=root,
        node_count=sum(1 for _ in root.walk()),
        elapsed_ms=(time.perf_counter() - start) * 1000,
    )


def _run(args: list[str], *, timeout: float = 30.0) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise MobilePilotError(ErrorCode.ENGINE_UNAVAILABLE, f"{args[0]} is not installed.",
                               hint="iOS needs Xcode and idb (`brew install idb-companion` and "
                               "`pip install fb-idb`). Run `mobilepilot doctor`.") from exc
    except subprocess.TimeoutExpired as exc:
        raise MobilePilotError(ErrorCode.TIMEOUT, f"{' '.join(args[:3])} did not finish in {timeout:g} s.") from exc
    if out.returncode:
        raise MobilePilotError(ErrorCode.COMMAND_FAILED, f"{' '.join(args[:3])}: {(out.stderr or out.stdout).strip()[:400]}")
    return out.stdout


def list_devices() -> list[Device]:
    """Booted simulators (``xcrun simctl``); empty without Xcode."""
    if shutil.which("xcrun") is None:
        return []
    try:
        data = json.loads(_run(["xcrun", "simctl", "list", "devices", "booted", "-j"], timeout=15))
    except (MobilePilotError, json.JSONDecodeError):
        return []  # Command Line Tools only: simctl is missing, so there are no simulators
    devices = []
    for runtime, sims in data.get("devices", {}).items():
        version = runtime.rpartition(".")[2].replace("iOS-", "").replace("-", ".")
        for sim in sims:
            devices.append(Device(sim["udid"], "ios", "simulator", sim.get("state", ""), sim.get("name", ""), version))
    return devices


# HID usage ids for idb ui key; HOME has its own hardware button.
_KEYS = {"enter": "40", "delete": "42", "tab": "43"}


class IosSimulatorEngine(Engine):
    platform = "ios"

    def __init__(self, udid: str) -> None:
        if shutil.which("idb") is None:
            raise MobilePilotError(ErrorCode.ENGINE_UNAVAILABLE, "idb is not installed.",
                                   hint="`brew install facebook/fb/idb-companion` and `pip install fb-idb`, "
                                   "then retry. `mobilepilot doctor` checks the rest.")
        self.udid = udid
        self._bundle: str | None = None
        self._nested = True

    def _idb(self, *args: str, timeout: float = 30.0) -> str:
        return _run(["idb", *args, "--udid", self.udid], timeout=timeout)

    def capture_tree(self) -> RawTree:
        try:
            raw = self._idb("ui", "describe-all", "--json", *(["--nested"] if self._nested else []))
        except MobilePilotError:
            if not self._nested:
                raise
            self._nested = False  # an idb without --nested: fall back to the flat list
            raw = self._idb("ui", "describe-all", "--json")
        return parse_idb(json.loads(raw), bundle_id=self._bundle)

    def signature(self) -> int:
        return zlib.crc32(self._idb("ui", "describe-all", "--json").encode())

    def tap(self, x: float, y: float, *, hold_s: float = 0.0) -> None:
        extra = ["--duration", f"{hold_s:g}"] if hold_s > 0 else []
        self._idb("ui", "tap", str(round(x)), str(round(y)), *extra)

    def type_text(self, text: str, *, clear: bool = False) -> None:
        if clear:
            # ponytail: no select-all on iOS; 64 deletes in one call, so a longer value keeps its head
            self._idb("ui", "key-sequence", *([_KEYS["delete"]] * 64))
        self._idb("ui", "text", text)

    def swipe(self, x1: float, y1: float, x2: float, y2: float, *, duration_s: float = 0.3) -> None:
        self._idb("ui", "swipe", *(str(round(v)) for v in (x1, y1, x2, y2)), "--duration", f"{duration_s:g}")

    def press_key(self, key: str) -> None:
        if key == "home":
            self._idb("ui", "button", "HOME")
        elif key in _KEYS:
            self._idb("ui", "key", _KEYS[key])
        else:
            raise MobilePilotError(ErrorCode.NOT_SUPPORTED, f"iOS has no {key!r} key.",
                                   hint="There is no back key on iOS: tap the navigation bar's back button.")

    def app(self, action: AppAction, *, app: str | None = None, path: str | None = None,
            permission: str | None = None, url: str | None = None) -> str:
        sim = ["xcrun", "simctl"]
        if action == "open":
            _run([*sim, "openurl", self.udid, url or ""])
            return f"opened {url}"
        if action == "current":
            return self._bundle or "unknown (MobilePilot only knows apps it launched)"
        if action == "install":
            if not path or not Path(path).exists():
                raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"No .app at {path!r}.",
                                       hint="Pass the path of a simulator build (.app).")
            _run([*sim, "install", self.udid, path], timeout=120)
            return f"installed {Path(path).name}"
        if not app:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"{action} needs app (the bundle id).")
        if action == "launch":
            _run([*sim, "launch", self.udid, app])
            self._bundle = app
            return f"launched {app}"
        if action == "stop":
            _run([*sim, "terminate", self.udid, app])
            return f"stopped {app}"
        if action == "grant":
            if not permission:
                raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "grant needs permission (camera, photos, location…).")
            _run([*sim, "privacy", self.udid, "grant", permission, app])
            return f"granted {permission} to {app}"
        if action == "clear":
            raise MobilePilotError(ErrorCode.NOT_SUPPORTED, "iOS has no clear-data command.",
                                   hint="Uninstall and reinstall the app (mobile_app install) for a clean state.")
        raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown app action {action!r}.")

    def screenshot(self, path: str, *, max_side: int = 1280) -> tuple[int, int]:
        from PIL import Image

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        _run(["xcrun", "simctl", "io", self.udid, "screenshot", "--type=png", path])
        with Image.open(path) as image:
            image.thumbnail((max_side, max_side))
            image.save(path, format="PNG")
            return image.size

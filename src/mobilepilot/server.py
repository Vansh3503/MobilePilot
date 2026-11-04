"""The MCP server: eleven ``mobile_*`` tools over one persistent device connection.

Built for few model round trips per screen (``mobile_snapshot`` → act and read the diff →
optional evidence): every action waits until the screen settles and returns what changed, rows
carry their locators inline, and ``mobile_find`` / ``mobile_wait_for`` loop on the server.

Nothing here touches a device until a tool needs one, so the MCP handshake always completes; with
no device every tool answers ``NO_DEVICE`` at once instead of hanging.
"""

from __future__ import annotations

import dataclasses
import functools
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

import anyio

from mobilepilot import __version__

try:  # mcp 2.x
    from mcp.server import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.server.mcpserver.utilities.types import Image

    _SERVER_KW: dict[str, Any] = {"version": __version__}
except ImportError:  # mcp 1.x: Stuntman bundles MobilePilot in a backend venv pinned to mcp<2
    from mcp.server.fastmcp import FastMCP as _Server  # type: ignore[no-redef]
    from mcp.server.fastmcp.exceptions import ToolError  # type: ignore[no-redef]
    from mcp.server.fastmcp.utilities.types import Image  # type: ignore[no-redef]

    _SERVER_KW = {}
from mobilepilot.engine.base import KEYS, Device, Engine
from mobilepilot.errors import ErrorCode, MobilePilotError
from mobilepilot.models import RawNode, RawTree
from mobilepilot.perception.diff import diff_snapshots
from mobilepilot.perception.format import format_diff, format_element_line, format_snapshot
from mobilepilot.perception.locators import format_locate
from mobilepilot.perception.model import Element, Snapshot, SnapshotMode
from mobilepilot.perception.refs import HISTORY_LIMIT, carry_refs
from mobilepilot.perception.snapshot import build_snapshot
from mobilepilot.perception.store import SnapshotStore

T = TypeVar("T")
Report = Literal["diff", "snapshot", "none"]

SETTLE_S = 3.0
"""How long an action waits for the screen to stop changing before reporting what it shows."""
POLL_S = 0.15
FIRST_LOOK_S = 0.5
"""Measured on the API 34 emulator: two dumps 150 ms apart can agree in the middle of a screen
transition (accessibility bounds update lazily). With no first wait a diff showed every row
shifted by 128 px; after 350 ms rows were still 4 px off. Settings' transitions run ~450 ms."""
# ponytail: a fixed first look; a device with animations off (adb shell settings put global
# *_animation_scale 0) would settle at once, but changing device settings is the developer's call.
ENGINE_TIMEOUT_S = 30.0
MAX_ELEMENTS = 150

INSTRUCTIONS = """MobilePilot drives an Android emulator/phone or an iOS simulator through its accessibility tree.

Per screen: mobile_snapshot once, then act (mobile_tap / mobile_type / mobile_swipe / mobile_key) and READ THE DIFF the action returns — it already waited for the screen to settle, so do not snapshot again to check. Pass screenshot=true on the action whose result is the step's evidence. Refs (e12) stay valid while their element is on screen.

Each row shows its locators inline: aid = accessibility id (strongest), id= = Android resource-id. Call mobile_locate once at the end with the refs you used, for graded locators to put in a script. Long lists: mobile_find(text, scroll=true) scrolls on the server. Slow screens: mobile_wait_for. No device: every tool says NO_DEVICE; ask the developer to start one."""


def _default_list_devices() -> list[Device]:
    from mobilepilot.engine import android, ios_sim

    return android.list_devices() + ios_sim.list_devices()


def _default_connect(device: Device) -> Engine:
    if device.platform == "android":
        from mobilepilot.engine.android import AndroidEngine

        return AndroidEngine(device.serial)
    from mobilepilot.engine.ios_sim import IosSimulatorEngine

    return IosSimulatorEngine(device.serial)


def _matches(node: RawNode | Element, needle: str) -> bool:
    if isinstance(node, Element):
        texts = (node.name, node.value, node.extra.get("identifier"), node.extra.get("a11y_id"))
    else:
        texts = (node.title, node.label, node.description, node.identifier,
                 node.value if isinstance(node.value, str) else None)
    return any(t and needle in t.casefold() for t in texts)


class Session:
    """One server process: the selected device, the snapshot store and the evidence counter."""

    def __init__(
        self,
        *,
        list_devices: Callable[[], list[Device]] = _default_list_devices,
        connect: Callable[[Device], Engine] = _default_connect,
        screenshots_dir: str | Path | None = None,
        settle_s: float = SETTLE_S,
        poll_s: float = POLL_S,
        first_look_s: float = FIRST_LOOK_S,
    ) -> None:
        self._list_devices = list_devices
        self._connect = connect
        self.engine: Engine | None = None
        self.device: Device | None = None
        self.store = SnapshotStore()
        self.trees: OrderedDict[str, RawTree] = OrderedDict()
        """Raw tree of each stored snapshot, so ``mobile_locate`` can check uniqueness later."""
        self.screenshots_dir = Path(screenshots_dir or os.environ.get("MOBILEPILOT_SCREENSHOTS_DIR")
                                    or Path.cwd() / "mobilepilot-screenshots")
        self.settle_s = settle_s
        self.poll_s = poll_s
        self.first_look_s = first_look_s
        self.step = 0
        self._lock = anyio.Lock()

    # ------------------------------------------------------------------ plumbing
    async def run(self, fn: Callable[..., T], *args: Any, timeout_s: float = ENGINE_TIMEOUT_S, **kwargs: Any) -> T:
        """Run blocking device work in a worker thread, one call at a time, with a deadline."""
        call = functools.partial(fn, *args, **kwargs)
        async with self._lock:
            with anyio.move_on_after(timeout_s):
                return await anyio.to_thread.run_sync(call, abandon_on_cancel=True)
        raise MobilePilotError(ErrorCode.TIMEOUT, f"{getattr(fn, '__name__', 'device call')} did not finish in {timeout_s:g} s.",
                               hint="The device may be busy or the app frozen (an ANR dialog?). Take a screenshot.")

    def _engine(self) -> Engine:
        if self.engine is not None:
            return self.engine
        devices = self._list_devices()
        ready = [d for d in devices if d.ready]
        if not ready:
            waiting = [d for d in devices if not d.ready]
            if any(d.state == "unauthorized" for d in waiting):
                raise MobilePilotError(ErrorCode.NO_DEVICE, "The phone is connected but has not allowed this computer.",
                                       hint="Unlock the phone and accept \"Allow USB debugging?\" (tick \"Always allow\").")
            raise MobilePilotError(ErrorCode.NO_DEVICE, "No emulator, simulator or device is connected.")
        if len(ready) > 1:
            names = ", ".join(f"{d.serial} ({d.platform})" for d in ready)
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"Several devices are connected: {names}.",
                                   hint="Pick one with mobile_devices(select=\"<serial>\").")
        return self._select(ready[0])

    def _select(self, device: Device) -> Engine:
        if self.engine is not None:
            self.engine.close()
        self.engine, self.device = self._connect(device), device
        self.store.clear()
        self.trees.clear()
        return self.engine

    def _keep(self, snap: Snapshot, tree: RawTree) -> Snapshot:
        self.store.put(snap)
        self.trees[snap.snapshot_id] = tree
        while len(self.trees) > 32:
            self.trees.popitem(last=False)
        return snap

    def _build(self, tree: RawTree, *, mode: SnapshotMode = "interactive", previous: Snapshot | None = None,
               offset: int = 0) -> Snapshot:
        snap = build_snapshot(tree, snapshot_id=self.store.new_id(), mode=mode, max_elements=MAX_ELEMENTS, offset=offset)
        window_id = snap.window.window_id if snap.window else None
        history = self.store.snapshots_of(snap.app.pid, window_id, limit=HISTORY_LIMIT)
        return self._keep(carry_refs(snap, previous, self.store.refs, history=history), tree)

    def _settle(self, engine: Engine) -> bool:
        """Wait until two fingerprints in a row agree (True), or give up after ``settle_s``."""
        time.sleep(self.first_look_s)
        prev = engine.signature()
        deadline = time.monotonic() + self.settle_s
        while time.monotonic() < deadline:
            time.sleep(self.poll_s)
            cur = engine.signature()
            if cur == prev:
                return True
            prev = cur
        return False

    def _evidence_path(self, path: str | None, action: str) -> Path:
        """A model-chosen ``path`` stays inside the evidence folder and is a .png: a tool that
        writes wherever it is told could overwrite any file the server can reach."""
        if not path:
            self.step += 1
            return self.screenshots_dir / f"step-{self.step:02d}-{action}.png"
        root = self.screenshots_dir.resolve()
        target = (root / Path(path).expanduser()).resolve()
        if root != target and root not in target.parents or target.suffix.lower() != ".png":
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"Screenshots go inside {root} as .png files, not {path!r}.",
                                   hint="Pass a file name such as \"login-error.png\", or no path.")
        return target

    def _shoot(self, engine: Engine, path: Path) -> str:
        width, height = engine.screenshot(str(path))
        note = ""
        try:
            from PIL import Image as PILImage

            with PILImage.open(path) as img:
                if img.convert("L").getextrema()[1] < 8:
                    note = " — all black: the app marks this screen secure (FLAG_SECURE); the tree still works"
        except OSError:
            pass
        return f"screenshot {path} ({width}×{height}){note}"

    def _resolve(self, ref: str) -> tuple[Snapshot, Element]:
        return self.store.resolve(ref)

    def _on_screen(self, ref: str) -> tuple[Snapshot, Element]:
        """The element as the LATEST snapshot shows it, for acting on: its position there, not
        where an earlier screen had it. A ref that is not on the current screen is refused, or a
        tap would land on whatever now occupies its old place."""
        snap, el = self._resolve(ref)
        latest = self.store.latest()
        if latest is None or snap is latest:
            return snap, el
        fresh = latest.element(el.ref)
        if fresh is None:
            raise MobilePilotError(ErrorCode.STALE_REF, f"{el.ref} was on an earlier screen and is not on the current one "
                                   f"(snapshot {latest.snapshot_id}).",
                                   hint="Act on a ref from the latest result, or go back to that screen first.")
        return latest, fresh

    # ------------------------------------------------------------------ the action loop
    def _act_sync(self, do: Callable[[Engine], str], report: Report, screenshot: bool, action: str,
                  before: Snapshot | None) -> str:
        engine = self._engine()
        summary = do(engine)
        settled = self._settle(engine)
        lines = [summary]
        if report != "none":
            tree = engine.capture_tree()
            mode = before.mode if before is not None else "interactive"
            after = self._build(tree, mode=mode, previous=before)
            if report == "snapshot" or before is None:
                lines.append(format_snapshot(after))
            else:
                diff = diff_snapshots(before, after)
                lines.append(("no visible change — the action did nothing on screen" if diff.is_empty()
                              else format_diff(diff)) + f"\n(snapshot {after.snapshot_id})")
            if tree.keyboard:
                lines.append("keyboard is open (mobile_key back hides it on Android)")
        if not settled:
            lines.append(f"note: the screen was still changing after {self.settle_s:g} s (animation or loading)")
        if screenshot:
            lines.append(self._shoot(engine, self._evidence_path(None, action)))
        return "\n".join(lines)

    async def act(self, do: Callable[[Engine], str], *, report: Report, screenshot: bool, action: str,
                  before: Snapshot | None = None) -> str:
        if before is None:
            before = self.store.latest()
        return await self.run(self._act_sync, do, report, screenshot, action, before)

    # ------------------------------------------------------------------ tools
    def _release(self) -> str:
        if self.engine is None:
            return "nothing to release"
        serial = self.device.serial if self.device else "the device"
        self.engine.close()
        self.engine = self.device = None
        return f"released {serial}: another tool (an Appium suite) can drive it now; the next mobile_* call reconnects"

    async def devices(self, select: str | None = None, release: bool = False) -> str:
        if release:
            return await self.run(self._release)
        devices = await anyio.to_thread.run_sync(self._list_devices)
        if select:
            match = next((d for d in devices if d.serial == select), None)
            if match is None:
                raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"No device {select!r}.",
                                       hint="Use a serial from the list mobile_devices returns.")
            if not match.ready:
                raise MobilePilotError(ErrorCode.NO_DEVICE, f"{select} is {match.state}, not ready.")
            await self.run(self._select, match)
        if not devices:
            return "No devices. Start an emulator or simulator, or connect a phone with USB debugging on."
        lines = []
        for d in devices:
            mark = "*" if self.device is not None and d.serial == self.device.serial else " "
            extra = " ".join(x for x in (d.name, d.os_version) if x)
            lines.append(f"{mark} {d.serial}  {d.platform} {d.kind}  {d.state}" + (f"  {extra}" if extra else ""))
        if any(d.state == "unauthorized" for d in devices):
            lines.append("unauthorized: unlock the phone and accept \"Allow USB debugging?\"")
        if self.device is None and len([d for d in devices if d.ready]) > 1:
            lines.append("several ready devices: pick one with select=\"<serial>\"")
        return "\n".join(lines)

    async def snapshot(self, mode: SnapshotMode = "interactive", filter: str | None = None, offset: int = 0) -> str:
        def work() -> str:
            engine = self._engine()
            tree = engine.capture_tree()
            snap = self._build(tree, mode=mode, previous=self.store.latest(), offset=offset)
            shown = snap
            if filter:
                needle = filter.casefold()
                hits = [el for el in snap.all_elements if _matches(el, needle)]
                # format-only view: refs still resolve against the stored, unfiltered snapshot
                shown = dataclasses.replace(snap, elements=hits[offset:offset + MAX_ELEMENTS], all_elements=hits,
                                            offset=offset)
            text = format_snapshot(shown)
            if filter:
                text += f" Filter {filter!r} matched {len(shown.all_elements)} of {snap.total}."
            if tree.keyboard:
                text += "\nkeyboard is open (mobile_key back hides it on Android)"
            return text

        return await self.run(work)

    async def tap(self, ref: str | None = None, x: float | None = None, y: float | None = None,
                  long_press: bool = False, report: Report = "diff", screenshot: bool = False,
                  double: bool = False) -> str:
        if long_press and double:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "Pass long_press or double, not both.")
        before = None
        if ref:
            before, el = self._on_screen(ref)
            if el.center is None:
                raise MobilePilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{ref} has no position on screen.")
            point, label = el.center, format_element_line(el)
            warn = "" if el.enabled else " (it is disabled)"
        elif x is not None and y is not None:
            from mobilepilot.models import Point

            point, label, warn = Point(x, y), f"({x:g},{y:g})", ""
        else:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "Pass ref, or x and y.")
        hold = 1.0 if long_press else 0.0

        def do(engine: Engine) -> str:
            if double:
                engine.double_tap(point.x, point.y)
                return f"double-tapped {label}{warn}"
            engine.tap(point.x, point.y, hold_s=hold)
            return f"{'long-pressed' if long_press else 'tapped'} {label}{warn}"

        return await self.act(do, report=report, screenshot=screenshot, action="tap", before=before)

    async def type(self, text: str, ref: str | None = None, clear: bool = False, submit: bool = False,
                   secret: bool = False, report: Report = "diff", screenshot: bool = False) -> str:
        before, target = (self._on_screen(ref) if ref else (self.store.latest(), None))
        if target is None and before is not None:
            target = before.focused_element
        if target is not None and target.secure and not secret:
            raise MobilePilotError(
                ErrorCode.INVALID_ARGUMENT, f"{target.ref} is a password field.",
                hint="Type into it only with a value the developer gave you for this test, and pass secret=true.")

        def do(engine: Engine) -> str:
            if target is not None and not target.focused and target.center is not None:
                engine.tap(target.center.x, target.center.y)
                time.sleep(self.poll_s)
            engine.type_text(text, clear=clear)
            if submit:
                engine.press_key("enter")
            shown = "•" * len(text) if secret or (target is not None and target.secure) else repr(text)
            where = f" into {format_element_line(target)}" if target is not None else ""
            return f"typed {shown}{where}" + (" and pressed enter" if submit else "")

        return await self.act(do, report=report, screenshot=screenshot, action="type", before=before)

    async def swipe(self, direction: Literal["up", "down", "left", "right"] | None = None, ref: str | None = None,
                    from_x: float | None = None, from_y: float | None = None, to_x: float | None = None,
                    to_y: float | None = None, report: Report = "diff", screenshot: bool = False,
                    distance: float = 0.6, duration_s: float = 0.3) -> str:
        if not 0.1 <= distance <= 0.9:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "distance is a fraction of the area: 0.1 to 0.9.")
        if not 0.05 <= duration_s <= 5:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "duration_s must be 0.05 to 5 seconds.")
        before = None
        if direction is None:
            if None in (from_x, from_y, to_x, to_y):
                raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "Pass direction, or from_x/from_y/to_x/to_y.")
            path = (from_x, from_y, to_x, to_y)
        else:
            if ref:
                before, el = self._on_screen(ref)
                box = el.frame
            else:
                before = self.store.latest()
                box = before.window.frame if before is not None and before.window else None
            if box is None:
                raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "Nothing to swipe in yet.",
                                       hint="Call mobile_snapshot first, or pass coordinates.")
            cx, cy = box.x + box.w / 2, box.y + box.h / 2
            dx, dy = box.w * distance / 2, box.h * distance / 2
            # direction is where the CONTENT goes into view: "down" shows what is below, so the finger moves up
            path = {"down": (cx, cy + dy, cx, cy - dy), "up": (cx, cy - dy, cx, cy + dy),
                    "right": (cx + dx, cy, cx - dx, cy), "left": (cx - dx, cy, cx + dx, cy)}[direction]

        def do(engine: Engine) -> str:
            engine.swipe(*path, duration_s=duration_s)
            return f"swiped {direction or 'from ({:g},{:g}) to ({:g},{:g})'.format(*path)}" + (f" in {ref}" if ref else "")

        return await self.act(do, report=report, screenshot=screenshot, action="swipe", before=before)

    def _point(self, ref: str | None, x: float | None, y: float | None, what: str):
        """A ref's centre on the latest screen, or x/y. Returns (snapshot or None, point, label)."""
        from mobilepilot.models import Point

        if ref:
            snap, el = self._on_screen(ref)
            if el.center is None:
                raise MobilePilotError(ErrorCode.ELEMENT_NOT_ACTIONABLE, f"{ref} has no position on screen.")
            return snap, el.center, format_element_line(el)
        if x is not None and y is not None:
            return None, Point(x, y), f"({x:g},{y:g})"
        raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"Pass {what}_ref, or {what}_x and {what}_y.")

    async def drag(self, from_ref: str | None = None, from_x: float | None = None, from_y: float | None = None,
                   to_ref: str | None = None, to_x: float | None = None, to_y: float | None = None,
                   hold_s: float = 0.6, duration_s: float = 0.8, report: Report = "diff",
                   screenshot: bool = False) -> str:
        if not 0 <= hold_s <= 5 or not 0.1 <= duration_s <= 10:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "hold_s is 0-5 s and duration_s 0.1-10 s.")
        before, start, start_label = self._point(from_ref, from_x, from_y, "from")
        other, end, end_label = self._point(to_ref, to_x, to_y, "to")
        before = before or other

        def do(engine: Engine) -> str:
            engine.drag(start.x, start.y, end.x, end.y, hold_s=hold_s, duration_s=duration_s)
            return f"dragged {start_label} to {end_label}"

        return await self.act(do, report=report, screenshot=screenshot, action="drag", before=before)

    async def pinch(self, zoom: Literal["in", "out"], ref: str | None = None, report: Report = "diff",
                    screenshot: bool = False) -> str:
        if ref:
            before, el = self._on_screen(ref)
            box, label = el.frame, f" on {format_element_line(el)}"
        else:
            before = self.store.latest()
            box, label = (before.window.frame if before is not None and before.window else None), ""
        if box is None:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "Nothing to pinch yet.",
                                   hint="Call mobile_snapshot first, or pass the ref of the map or image.")
        cx, cy, spread = box.x + box.w / 2, box.y + box.h / 2, min(box.w, box.h)

        def do(engine: Engine) -> str:
            engine.pinch(cx, cy, spread, zoom=zoom)
            return f"pinched to zoom {zoom}{label}"

        return await self.act(do, report=report, screenshot=screenshot, action="pinch", before=before)

    async def rotate(self, orientation: Literal["portrait", "landscape", "auto"], report: Report = "diff",
                     screenshot: bool = False) -> str:
        def do(engine: Engine) -> str:
            engine.rotate(orientation)
            return "rotation follows the device again" if orientation == "auto" else f"rotated to {orientation}"

        return await self.act(do, report=report, screenshot=screenshot, action="rotate")

    async def key(self, key: str, report: Report = "diff", screenshot: bool = False) -> str:
        if key not in KEYS:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"Unknown key {key!r}.", hint="Use one of: " + ", ".join(KEYS) + ".")

        def do(engine: Engine) -> str:
            engine.press_key(key)
            return f"pressed {key}"

        return await self.act(do, report=report, screenshot=screenshot, action=key)

    async def app(self, action: Literal["launch", "restart", "stop", "clear", "install", "grant", "current"], app: str | None = None,
                  path: str | None = None, permission: str | None = None, screenshot: bool = False) -> str:
        if action in ("launch", "restart"):
            def do(engine: Engine) -> str:
                if action == "restart":
                    engine.app("stop", app=app)
                return engine.app("launch", app=app)

            return await self.act(do, report="snapshot", screenshot=screenshot, action="launch")
        return await self.run(lambda: self._engine().app(action, app=app, path=path, permission=permission),
                              timeout_s=180 if action == "install" else ENGINE_TIMEOUT_S)

    def _find_sync(self, needle: str, scroll: bool, max_scrolls: int) -> str:
        engine = self._engine()
        scrolls = 0
        while True:
            tree = engine.capture_tree()
            snap = self._build(tree, previous=self.store.latest())
            hits = [el for el in snap.all_elements if _matches(el, needle)]
            if not hits:  # text that is not its own element (a row's title): report the element holding it
                for node in tree.root.walk():
                    if node.visible and node.frame is not None and _matches(node, needle):
                        c = node.frame.center
                        holders = [el for el in snap.all_elements if el.frame is not None and el.frame.contains(c)
                                   and el.interactive]
                        if holders:
                            hits = [min(holders, key=lambda el: el.frame.area)]
                            break
            if hits:
                where = f" after {scrolls} scroll{'s' if scrolls != 1 else ''}" if scrolls else ""
                return f"found{where} (snapshot {snap.snapshot_id}):\n" + "\n".join(format_element_line(el) for el in hits[:10])
            scrollers = [el for el in snap.all_elements if el.extra.get("scrollable") and el.frame is not None]
            if not scroll or not scrollers or scrolls >= max_scrolls:
                why = "no scrollable list on screen" if scroll and not scrollers else (
                    f"gave up after {scrolls} scrolls" if scroll else "scroll=false")
                raise MobilePilotError(ErrorCode.ELEMENT_NOT_FOUND, f"Nothing matching {needle!r} is on screen ({why}).")
            box = max(scrollers, key=lambda el: el.frame.area).frame
            before_sig = engine.signature()
            engine.swipe(box.x + box.w / 2, box.y + box.h * 0.75, box.x + box.w / 2, box.y + box.h * 0.25)
            scrolls += 1
            self._settle(engine)
            if engine.signature() == before_sig:
                raise MobilePilotError(ErrorCode.ELEMENT_NOT_FOUND,
                                       f"Nothing matching {needle!r}: scrolled to the end of the list ({scrolls} scrolls).",
                                       hint="It may be above: mobile_swipe direction=up, then mobile_find again.")

    async def find(self, text: str, scroll: bool = True, max_scrolls: int = 8) -> str:
        return await self.run(self._find_sync, text.casefold(), scroll, max_scrolls, timeout_s=ENGINE_TIMEOUT_S * 2)

    def _wait_sync(self, needle: str | None, until: str, timeout_s: float) -> str:
        engine = self._engine()
        if until == "idle":
            start = time.monotonic()
            settle, self.settle_s = self.settle_s, timeout_s
            try:
                ok = self._settle(engine)
            finally:
                self.settle_s = settle
            took = time.monotonic() - start
            return f"screen idle after {took:.1f} s" if ok else f"still changing after {timeout_s:g} s"
        if not needle:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, f"until={until} needs text.")
        deadline = time.monotonic() + timeout_s
        while True:
            tree = engine.capture_tree()
            present = any(n.visible and _matches(n, needle) for n in tree.root.walk())
            if present == (until == "visible"):
                snap = self._build(tree, previous=self.store.latest())
                if until == "gone":
                    return f"{needle!r} is gone (snapshot {snap.snapshot_id})"
                hits = [el for el in snap.all_elements if _matches(el, needle)]
                return f"{needle!r} is on screen (snapshot {snap.snapshot_id})" + (
                    ":\n" + "\n".join(format_element_line(el) for el in hits[:10]) if hits else "")
            if time.monotonic() >= deadline:
                state = "never appeared" if until == "visible" else "is still on screen"
                raise MobilePilotError(ErrorCode.TIMEOUT, f"{needle!r} {state} after {timeout_s:g} s.")
            time.sleep(max(self.poll_s, 0.3))

    async def wait_for(self, text: str | None = None, until: Literal["visible", "gone", "idle"] = "visible",
                       timeout_s: float = 10.0) -> str:
        timeout_s = min(max(timeout_s, 0.5), 120.0)
        return await self.run(self._wait_sync, text.casefold() if text else None, until, timeout_s,
                              timeout_s=timeout_s + ENGINE_TIMEOUT_S)

    async def screenshot(self, path: str | None = None, show: bool = False) -> str | list[Any]:
        target = self._evidence_path(path, "screen")
        text = await self.run(lambda: self._shoot(self._engine(), target))
        return [text, Image(path=target)] if show else text

    async def locate(self, refs: list[str]) -> str:
        if not refs:
            raise MobilePilotError(ErrorCode.INVALID_ARGUMENT, "Pass the refs you want locators for.")
        platform = self.device.platform if self.device is not None else "android"
        blocks = []
        for ref in refs:  # one bad ref is reported on its own line, never sinks the others
            try:
                snap, el = self.store.observed(ref)
                tree = self.trees.get(snap.snapshot_id)
                if tree is None:
                    raise MobilePilotError(ErrorCode.SNAPSHOT_NOT_FOUND, f"The screen {ref} came from has expired.")
                blocks.append(format_locate(el, tree, platform))
            except MobilePilotError as err:
                blocks.append(f"{ref}: {err.code.value}: {err.message}")
        return "\n".join(blocks)


# ---------------------------------------------------------------------------- MCP wiring
def build_server(session: Session | None = None) -> _Server:
    """The MCP server around ``session`` (a new one with real devices by default). Runs on mcp 2.x
    (``MCPServer``) and on mcp 1.x (``FastMCP``), which share the tool decorator."""
    session = session or Session()
    server = _Server("mobilepilot", instructions=INSTRUCTIONS, **_SERVER_KW)

    def tool(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except MobilePilotError as err:
                raise ToolError(err.to_text()) from err

        return server.tool(structured_output=False)(wrapper)

    @tool
    async def mobile_devices(select: str | None = None, release: bool = False) -> str:
        """List emulators, simulators and phones (USB or wireless adb); `select` one by serial when
        several are connected. With one device you never need this: every tool picks it.
        release=true lets go of the device so another automation tool can drive it: call it
        before running an Appium suite, which cannot start while MobilePilot holds the device."""
        return await session.devices(select, release)

    @tool
    async def mobile_app(action: Literal["launch", "restart", "stop", "clear", "install", "grant", "current"],
                         app: str | None = None, path: str | None = None, permission: str | None = None,
                         screenshot: bool = False) -> str:
        """App lifecycle. launch/restart/stop/clear take `app` (package or bundle id); launch and
        restart return the first screen's snapshot, and restart (stop, then launch) is how a crawl
        starts from a known screen. install takes a local `path` (.apk, or a simulator .app). grant takes
        `app` + `permission` (CAMERA, android.permission.POST_NOTIFICATIONS; iOS: camera, photos,
        location). clear ERASES the app's data. current names the foreground app. screenshot=true
        on launch/restart saves the first screen as evidence in the same call."""
        return await session.app(action, app, path, permission, screenshot)

    @tool
    async def mobile_snapshot(mode: Literal["interactive", "full", "text"] = "interactive", filter: str | None = None,
                              offset: int = 0) -> str:
        """The screen as compact lines: ref, role, name, state, inline locators (aid = accessibility id,
        id= = resource-id) and the tap point. Take one per new screen; actions then return diffs.
        `filter` keeps elements whose text or id contains it; `offset` pages long screens."""
        return await session.snapshot(mode, filter, offset)

    @tool
    async def mobile_tap(ref: str | None = None, x: float | None = None, y: float | None = None,
                         long_press: bool = False, report: Literal["diff", "snapshot", "none"] = "diff",
                         screenshot: bool = False, double: bool = False) -> str:
        """Tap an element by ref (preferred) or x/y; long_press holds it, double taps twice. Waits for the screen to
        settle and returns what changed. screenshot=true saves the step's evidence in the same call."""
        return await session.tap(ref, x, y, long_press, report, screenshot, double)

    @tool
    async def mobile_type(text: str, ref: str | None = None, clear: bool = False, submit: bool = False,
                          secret: bool = False, report: Literal["diff", "snapshot", "none"] = "diff",
                          screenshot: bool = False) -> str:
        """Type into a field (ref, or the focused one). clear replaces its text; submit presses enter.
        Password fields are refused unless secret=true, which you pass only with a value the
        developer gave you for this test. Returns the diff."""
        return await session.type(text, ref, clear, submit, secret, report, screenshot)

    @tool
    async def mobile_swipe(direction: Literal["up", "down", "left", "right"] | None = None, ref: str | None = None,
                           from_x: float | None = None, from_y: float | None = None, to_x: float | None = None,
                           to_y: float | None = None, report: Literal["diff", "snapshot", "none"] = "diff",
                           screenshot: bool = False, distance: float = 0.6, duration_s: float = 0.3) -> str:
        """Scroll or swipe. `direction` is where content comes from: "down" reveals what is below
        (the finger moves up); "left"/"right" page carousels and tabs. Inside `ref` (a list, pager)
        or the whole screen; or exact from/to points. distance is the share of the area the finger
        travels (0.1-0.9); duration_s is its speed (0.1 flings, 1+ scrolls slowly). Returns the
        diff. To reach a known item, mobile_find(scroll=true) is one call. Pull-to-refresh is
        direction="up" at the top of a list."""
        return await session.swipe(direction, ref, from_x, from_y, to_x, to_y, report, screenshot,
                                   distance, duration_s)

    @tool
    async def mobile_drag(from_ref: str | None = None, from_x: float | None = None, from_y: float | None = None,
                          to_ref: str | None = None, to_x: float | None = None, to_y: float | None = None,
                          hold_s: float = 0.6, duration_s: float = 0.8,
                          report: Literal["diff", "snapshot", "none"] = "diff", screenshot: bool = False) -> str:
        """Press, hold, move and release: drag and drop, reorder a list, move a slider's thumb.
        From a ref or point to a ref or point. hold_s is the press before moving (0.6 starts
        Android drag-and-drop; 0 for sliders and maps). Returns the diff."""
        return await session.drag(from_ref, from_x, from_y, to_ref, to_x, to_y, hold_s, duration_s,
                                  report, screenshot)

    @tool
    async def mobile_pinch(zoom: Literal["in", "out"], ref: str | None = None,
                           report: Literal["diff", "snapshot", "none"] = "diff", screenshot: bool = False) -> str:
        """Two-finger pinch on a ref (a map, image, web view) or the screen centre. zoom="in"
        spreads the fingers to enlarge; "out" brings them together. Pinches rarely change the
        tree: pass screenshot=true to see the effect."""
        return await session.pinch(zoom, ref, report, screenshot)

    @tool
    async def mobile_rotate(orientation: Literal["portrait", "landscape", "auto"],
                            report: Literal["diff", "snapshot", "none"] = "diff", screenshot: bool = False) -> str:
        """Rotate the screen. portrait/landscape lock it; "auto" restores the phone's own rotation
        settings from before the first rotate: always end with it. Returns the diff."""
        return await session.rotate(orientation, report, screenshot)

    @tool
    async def mobile_key(key: Literal["back", "home", "enter", "delete", "tab", "app_switch", "volume_up",
                                      "volume_down", "search", "notifications", "quick_settings"],
                         report: Literal["diff", "snapshot", "none"] = "diff", screenshot: bool = False) -> str:
        """Press a device key and return the diff. notifications/quick_settings pull down the shade
        (back closes it). iOS has no back key: tap the navigation bar's back button."""
        return await session.key(key, report, screenshot)

    @tool
    async def mobile_find(text: str, scroll: bool = True, max_scrolls: int = 8) -> str:
        """Find an element whose text, accessibility id or resource-id contains `text`, scrolling the
        main list on the server until it appears or the list ends. Returns the matching rows with refs."""
        return await session.find(text, scroll, max_scrolls)

    @tool
    async def mobile_wait_for(text: str | None = None, until: Literal["visible", "gone", "idle"] = "visible",
                              timeout_s: float = 10.0) -> str:
        """Wait on the server until `text` is visible, is gone, or the screen is idle. Use it for
        loading screens instead of snapshotting in a loop; a timeout is a failed step, not a retry."""
        return await session.wait_for(text, until, timeout_s)

    @tool
    async def mobile_screenshot(path: str | None = None, show: bool = False) -> str | list[Any]:
        """Save a PNG (longest side ≤1280) and return its path; a relative `path` lands in the
        evidence folder. show=true also returns the image, for screens the tree cannot describe."""
        return await session.screenshot(path, show)

    @tool
    async def mobile_locate(refs: list[str]) -> str:
        """Graded, stable Appium locators for the refs you used (strong: accessibility id; acceptable:
        resource-id / iOS label; fragile: text). Works for earlier screens too: call it once at the end."""
        return await session.locate(refs)

    return server

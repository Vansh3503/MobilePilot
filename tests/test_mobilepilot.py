"""Offline tests: recorded Android dumps (API 34 emulator, Settings) through the real perception
layer and the real MCP server, with a scripted engine standing in for the device."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio
import pytest
from mcp.client import Client

from mobilepilot.engine.android import parse_hierarchy
from mobilepilot.engine.base import Device
from mobilepilot.engine.fake import FakeEngine
from mobilepilot.engine.ios_sim import parse_idb
from mobilepilot.perception.format import format_snapshot
from mobilepilot.perception.snapshot import build_snapshot
from mobilepilot.server import Session, build_server

FIX = Path(__file__).parent / "fixtures"
SCREENS = {name: FIX / f"android_{name}.xml" for name in ("settings_home", "network", "internet", "internet_toggled")}
TAPS = {
    ("settings_home", "Network & internet"): "network",
    ("network", "Internet"): "internet",
    ("internet", "android:id/switch_widget"): "internet_toggled",
    ("internet_toggled", "android:id/switch_widget"): "internet",
}
BACK = {"internet": "network", "internet_toggled": "network", "network": "settings_home"}
ACTIVITIES = {"settings_home": ".Settings", "network": ".SubSettings", "internet": ".SubSettings",
              "internet_toggled": ".SubSettings"}  # as recorded: Settings' subpages share one activity
EMULATOR = Device("emulator-5554", "android", "emulator", "device", "sdk_gphone64_arm64")


def _session(tmp_path, engine=None, devices=(EMULATOR,)):
    engine = engine or FakeEngine(SCREENS, "settings_home", taps=TAPS, back=BACK, activities=ACTIVITIES)
    session = Session(list_devices=lambda: list(devices), connect=lambda d: engine,
                      screenshots_dir=tmp_path, poll_s=0, first_look_s=0, settle_s=0.05)
    return session, engine


def _call(server, name, **args):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(name, args)

    return anyio.run(go)


def _text(result) -> str:
    return "\n".join(b.text for b in result.content if hasattr(b, "text"))


def _ref(text: str, pattern: str) -> str:
    return re.search(rf"^[+~]?\s*(e\d+)\s+.*{pattern}", text, re.M).group(1)


class _Driver:
    """One MCP client session over a server, so refs and snapshots persist between calls."""

    def __init__(self, server):
        self.server = server

    def run(self, steps):
        out = []

        async def go():
            async with Client(self.server) as client:
                for name, args in steps:
                    args = args(out) if callable(args) else args
                    out.append(await client.call_tool(name, args))

        anyio.run(go)
        return out


# --------------------------------------------------------------------------- perception
def test_settings_home_is_compact_and_names_rows_from_their_text():
    tree = parse_hierarchy(SCREENS["settings_home"].read_text(), package="com.android.settings",
                           activity=".Settings", pid=1, size=(320, 640))
    text = format_snapshot(build_snapshot(tree, snapshot_id="s_test"))
    assert re.search(r'row\s+"Network & internet — Mobile, Wi‑Fi, hotspot"', text)
    assert "aid  id=account_avatar" in text            # accessibility id + resource-id inline
    assert "scrollable" in text                         # scrollers are kept so a swipe can target them
    assert "systemui" not in text and "Battery" not in text   # status bar window dropped
    assert "(48,227)" not in text                       # decorative, non-clickable ImageButton demoted
    assert len(text) < 1200


def test_keyboard_window_is_dropped_and_reported():
    xml = SCREENS["internet"].read_text().replace(
        "</hierarchy>",
        '<node index="0" text="q" resource-id="" class="android.widget.Button" '
        'package="com.google.android.inputmethod.latin" content-desc="" clickable="true" '
        'bounds="[0,400][32,440]" /></hierarchy>')
    tree = parse_hierarchy(xml, package="com.android.settings", size=(320, 640))
    assert tree.keyboard
    assert '"q"' not in format_snapshot(build_snapshot(tree, snapshot_id="s_k"))


def test_hierarchy_with_a_dtd_is_refused():
    with pytest.raises(Exception, match="DTD"):
        parse_hierarchy('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><hierarchy/>')


def test_ios_idb_output_maps_to_ax_roles():
    data = json.loads((FIX / "ios_synthetic_settings.json").read_text())
    tree = parse_idb(data, bundle_id="com.apple.Preferences")
    text = format_snapshot(build_snapshot(tree, snapshot_id="s_ios"))
    assert re.search(r'switch\s+"Airplane Mode"\s+\(', text)        # off: no "checked"
    assert re.search(r'switch\s+"Wi-Fi"\s+checked\s+aid="wifi-switch"', text)
    assert re.search(r'secure\s+"Password"', text) and "hunter2" not in text
    assert re.search(r'row\s+"General"', text)
    assert tree.app.name == "Settings" and tree.app.pid == 4321


# --------------------------------------------------------------------------- the server
def test_handshake_and_tools_work_with_no_device(tmp_path):
    session, _ = _session(tmp_path, devices=())
    server = build_server(session)

    async def go():
        with anyio.fail_after(10):
            async with Client(server) as client:
                tools = sorted(t.name for t in (await client.list_tools()).tools)
                snap = await client.call_tool("mobile_snapshot", {})
                listing = await client.call_tool("mobile_devices", {})
                return tools, snap, listing

    tools, snap, listing = anyio.run(go)
    assert tools == ["mobile_app", "mobile_devices", "mobile_drag", "mobile_find", "mobile_key", "mobile_locate",
                     "mobile_pinch", "mobile_rotate", "mobile_screenshot", "mobile_snapshot", "mobile_swipe",
                     "mobile_tap", "mobile_type", "mobile_wait_for"]
    assert snap.is_error and "NO_DEVICE" in _text(snap)
    assert not listing.is_error and "No devices" in _text(listing)


def test_an_unauthorized_phone_says_how_to_allow_it(tmp_path):
    phone = Device("R5CT1234", "android", "device", "unauthorized")
    session, _ = _session(tmp_path, devices=(phone,))
    result = _call(build_server(session), "mobile_tap", x=1, y=1)
    assert result.is_error and "Allow USB debugging" in _text(result)


def test_several_devices_must_be_chosen(tmp_path):
    other = Device("emulator-5556", "android", "emulator", "device")
    session, engine = _session(tmp_path, devices=(EMULATOR, other))
    out = _Driver(build_server(session)).run([
        ("mobile_snapshot", {}),
        ("mobile_devices", {"select": "emulator-5554"}),
        ("mobile_snapshot", {}),
    ])
    assert out[0].is_error and "mobile_devices(select" in _text(out[0])
    assert "* emulator-5554" in _text(out[1])
    assert not out[2].is_error


def test_actions_return_diffs_and_refs_survive_going_back(tmp_path):
    session, engine = _session(tmp_path)
    out = _Driver(build_server(session)).run([
        ("mobile_snapshot", {}),
        ("mobile_tap", lambda o: {"ref": _ref(_text(o[0]), "Network & internet")}),
        ("mobile_tap", lambda o: {"ref": _ref(_text(o[1]), '"Internet')}),
        ("mobile_tap", lambda o: {"ref": _ref(_text(o[2]), "switch"), "screenshot": True}),
        ("mobile_key", {"key": "back"}),
    ])
    texts = [_text(r) for r in out]
    assert not any(r.is_error for r in out), texts
    assert 'screen: ".Settings" → ".SubSettings"' in texts[1]
    switch = _ref(texts[2], "switch")
    assert f"~ {switch} switch unchecked" in texts[3]           # toggling is a change, not add+remove
    assert "step-01-tap.png" in texts[3] and (tmp_path / "step-01-tap.png").exists()
    airplane = _ref(texts[1], '"Airplane mode"')
    # back: the old refs return with their elements, as one line instead of one per row
    back = next(line for line in texts[4].splitlines() if "elements back as snapshot" in line)
    assert airplane in back.split(": ", 1)[1].split(", "), texts[4]
    assert '"Airplane mode"' not in texts[4]
    assert engine.log[-1] == "key back"


def test_gestures_reach_the_engine_with_their_own_geometry(tmp_path):
    session, engine = _session(tmp_path)
    out = _Driver(build_server(session)).run([
        ("mobile_snapshot", {}),
        ("mobile_drag", lambda o: {"from_ref": _ref(_text(o[0]), "Network & internet"),
                                   "to_ref": _ref(_text(o[0]), "Connected devices")}),
        ("mobile_drag", {"from_x": 100, "from_y": 500, "to_x": 300, "to_y": 500, "hold_s": 0}),
        ("mobile_tap", {"x": 160, "y": 300, "double": True}),
        ("mobile_pinch", {"zoom": "in"}),
        ("mobile_pinch", {"zoom": "out"}),
        ("mobile_swipe", {"direction": "left", "distance": 0.8, "duration_s": 1.0}),
        ("mobile_swipe", {"direction": "down"}),
        ("mobile_rotate", {"orientation": "landscape"}),
        ("mobile_rotate", {"orientation": "auto"}),
        ("mobile_key", {"key": "notifications"}),
    ])
    texts = [_text(r) for r in out]
    assert not any(r.is_error for r in out), texts
    log = [line for line in engine.log if not line.startswith("app ")]
    drag = next(line for line in log if line.startswith("drag") and "hold 0.6s" in line)
    (x1, y1), (x2, y2) = (map(float, p.split(",")) for p in drag.split()[1].split("->"))
    assert x1 == x2 and y2 > y1, drag                        # down the list, from one row to the next
    assert "drag 100,500->300,500 hold 0s" in log
    assert "double-tap 160,300" in log
    pinches = [line for line in log if line.startswith("pinch")]
    assert [p.split()[1] for p in pinches] == ["in", "out"] and "at 160,320" in pinches[0]
    left = next(line for line in log if line.startswith("swipe") and "in 1s" in line)
    (lx1, _), (lx2, _) = (map(float, p.split(",")) for p in left.split()[1].split("->"))
    assert lx2 - lx1 == 0.8 * 320                            # "left" reveals what is left: finger moves right
    assert log[-3:] == ["rotate landscape", "rotate auto", "key notifications"]
    assert texts[1].startswith("dragged ") and texts[4].startswith("pinched to zoom in")


def test_gesture_arguments_are_checked_before_touching_the_device(tmp_path):
    session, engine = _session(tmp_path)
    out = _Driver(build_server(session)).run([
        ("mobile_snapshot", {}),
        ("mobile_tap", {"x": 1, "y": 1, "double": True, "long_press": True}),
        ("mobile_swipe", {"direction": "up", "distance": 1.5}),
        ("mobile_drag", {"from_x": 1, "from_y": 1}),
        ("mobile_drag", {"from_x": 1, "from_y": 1, "to_x": 2, "to_y": 2, "hold_s": 9}),
    ])
    assert [r.is_error for r in out] == [False, True, True, True, True], [_text(r) for r in out]
    assert not any(line.startswith(("tap", "swipe", "drag", "double")) for line in engine.log)


def test_a_shared_resource_id_is_not_reported_as_the_same_switch(tmp_path):
    """Airplane mode and Wi-Fi both use android:id/switch_widget; navigating between them once
    reported "~ switch checked" (AXPilot's identifier pass). Now: one removed, one added."""
    session, _ = _session(tmp_path)
    out = _Driver(build_server(session)).run([
        ("mobile_app", {"action": "restart", "app": "com.android.settings"}),
        ("mobile_tap", lambda o: {"ref": _ref(_text(o[0]), "Network & internet")}),
        ("mobile_tap", lambda o: {"ref": _ref(_text(o[1]), '"Internet')}),
    ])
    diff = _text(out[2])
    assert "~ " not in diff
    assert "- switch" in diff and re.search(r"^\+ e\d+ switch  checked", diff, re.M)


def test_find_locate_and_filter(tmp_path):
    session, _ = _session(tmp_path)
    out = _Driver(build_server(session)).run([
        ("mobile_find", {"text": "connected devices"}),
        ("mobile_snapshot", {"filter": "network"}),
        ("mobile_locate", lambda o: {"refs": [_ref(_text(o[0]), "Connected devices")]}),
    ])
    found, filtered, located = map(_text, out)
    assert found.startswith("found") and "Connected devices" in found
    assert "Filter 'network' matched 1 of" in filtered
    assert 'new UiSelector().text("Connected devices")' in located and "[unique]" in located
    assert "no accessibility id" in located


def test_locate_grades_accessibility_id_above_resource_id(tmp_path):
    session, _ = _session(tmp_path)
    out = _Driver(build_server(session)).run([
        ("mobile_snapshot", {}),
        ("mobile_locate", lambda o: {"refs": [_ref(_text(o[0]), "Profile picture")]}),
    ])
    located = _text(out[1])
    assert "best strong     accessibility id: Profile picture, double tap to open Google Account  [unique]" in located
    assert "acceptable id: com.android.settings:id/account_avatar" in located


def test_find_scrolls_until_the_list_ends(tmp_path):
    session, engine = _session(tmp_path)
    result = _call(build_server(session), "mobile_find", text="no such setting", max_scrolls=3)
    assert result.is_error and "scrolled to the end" in _text(result)   # fake screen never moves
    assert any(line.startswith("swipe") for line in engine.log)


def test_password_fields_need_secret(tmp_path):
    xml = SCREENS["internet"].read_text().replace(
        "</hierarchy>",
        '<node index="0" text="" resource-id="app:id/pw" class="android.widget.EditText" package="com.android.settings" '
        'content-desc="" password="true" clickable="true" focusable="true" bounds="[10,100][310,140]" /></hierarchy>')
    engine = FakeEngine({"login": xml}, "login")
    session, _ = _session(tmp_path, engine=engine)
    out = _Driver(build_server(session)).run([
        ("mobile_snapshot", {}),
        ("mobile_type", lambda o: {"text": "hunter2", "ref": _ref(_text(o[0]), "secure")}),
        ("mobile_type", lambda o: {"text": "hunter2", "ref": _ref(_text(o[0]), "secure"), "secret": True}),
    ])
    assert out[1].is_error and "password field" in _text(out[1])
    assert not out[2].is_error and "•••••••" in _text(out[2]) and "hunter2" not in _text(out[2])
    assert engine.typed == ["hunter2"]


def test_stdio_server_answers_the_handshake_without_touching_a_device():
    from mcp.client.stdio import StdioServerParameters

    params = StdioServerParameters(command=sys.executable, args=["-m", "mobilepilot.cli", "stdio"],
                                   env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"})

    async def go():
        with anyio.fail_after(30):
            async with Client(params) as client:
                return [t.name for t in (await client.list_tools()).tools]

    assert len(anyio.run(go)) == 14


def test_screenshot_paths_stay_in_the_evidence_folder(tmp_path):
    session, _ = _session(tmp_path)
    server = build_server(session)
    named = _call(server, "mobile_screenshot", path="login-error.png")
    assert not named.is_error and (tmp_path / "login-error.png").exists()
    for bad in ("../outside.png", "/etc/x.png", "notes.txt"):
        result = _call(server, "mobile_screenshot", path=bad)
        assert result.is_error and "inside" in _text(result), bad


def test_a_ref_from_an_earlier_screen_is_not_tapped(tmp_path):
    """Tapping an old ref would hit whatever now sits at its old position."""
    session, engine = _session(tmp_path)
    out = _Driver(build_server(session)).run([
        ("mobile_snapshot", {}),
        ("mobile_tap", lambda o: {"ref": _ref(_text(o[0]), "Network & internet")}),
        ("mobile_tap", lambda o: {"ref": _ref(_text(o[0]), "Connected devices")}),
    ])
    assert not out[1].is_error
    assert out[2].is_error and "earlier screen" in _text(out[2])
    assert sum(line.startswith("tap") for line in engine.log) == 1


def test_locate_works_for_a_row_whose_text_changed_later(tmp_path):
    """Live /mobile run 2026-11-03: the Internet row read "Internet — AndroidWifi", Wi-Fi was
    toggled, and back on that screen it read "Internet — T-Mobile". mobile_locate([e13, …]) then
    failed the whole call with STALE_REF. Locating describes the element as it was observed."""
    network = SCREENS["network"].read_text()
    screens = {**SCREENS, "network_after": network.replace("AndroidWifi", "T-Mobile")}
    back = {**BACK, "internet_toggled": "network_after"}
    engine = FakeEngine(screens, "network", taps=TAPS, back=back, activities={**ACTIVITIES, "network_after": ".SubSettings"})
    session, _ = _session(tmp_path, engine=engine)
    out = _Driver(build_server(session)).run([
        ("mobile_snapshot", {}),
        ("mobile_tap", lambda o: {"ref": _ref(_text(o[0]), '"Internet')}),
        ("mobile_tap", lambda o: {"ref": _ref(_text(o[1]), "switch")}),
        ("mobile_key", {"key": "back"}),
        ("mobile_locate", lambda o: {"refs": [_ref(_text(o[0]), '"Internet'), _ref(_text(o[1]), "switch"), "e999"]}),
    ])
    located = _text(out[4])
    assert not out[4].is_error, located
    assert 'new UiSelector().text("Internet")' in located
    assert "android:id/switch_widget" in located
    assert "e999" in located and "never issued" in located


def test_restart_can_take_the_first_steps_evidence(tmp_path):
    session, _ = _session(tmp_path)
    result = _call(build_server(session), "mobile_app", action="restart", app="com.android.settings", screenshot=True)
    assert not result.is_error and "step-01-launch.png" in _text(result)
    assert (tmp_path / "step-01-launch.png").exists()


def test_release_hands_the_device_to_another_tool(tmp_path):
    """Live 2026-11-03: while MobilePilot's uiautomator2 server held the device, an Appium suite
    could not start a session (UiAutomation is exclusive). release=true lets go; the next call
    reconnects on its own."""
    connects: list[str] = []

    class Closing(FakeEngine):
        closed = 0

        def close(self) -> None:
            Closing.closed += 1

    def connect(device):
        connects.append(device.serial)
        return Closing(SCREENS, "settings_home", taps=TAPS, back=BACK, activities=ACTIVITIES)

    session = Session(list_devices=lambda: [EMULATOR], connect=connect, screenshots_dir=tmp_path,
                      poll_s=0, first_look_s=0, settle_s=0.05)
    out = _Driver(build_server(session)).run([
        ("mobile_snapshot", {}),
        ("mobile_devices", {"release": True}),
        ("mobile_snapshot", {}),
    ])
    assert not any(r.is_error for r in out), [_text(r) for r in out]
    assert "released" in _text(out[1])
    assert Closing.closed == 1 and connects == ["emulator-5554", "emulator-5554"]

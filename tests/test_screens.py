"""Offline tests for what a tap opens, unlabeled icons, screenshot coordinates, settle budgets and
the device guards: synthetic two-window dumps through the real parser, perception and MCP server."""

from __future__ import annotations

import pytest
from PIL import Image

from mobilepilot.engine.android import fingerprint, parse_hierarchy
from mobilepilot.engine.base import Device
from mobilepilot.engine.fake import FakeEngine
from mobilepilot.errors import ErrorCode, MobilePilotError
from mobilepilot.server import DEFAULT_SETTLE_S, SETTLE_S, Session, build_server
from test_mobilepilot import _Driver, _ref, _text

PKG = "com.example.notes"
PHONE = Device("emulator-5554", "android", "emulator", "device")


def node(cls, bounds, *, text="", desc="", rid="", clickable=False, pkg=PKG, children=""):
    return (f'<node text="{text}" resource-id="{rid}" class="android.widget.{cls}" package="{pkg}" '
            f'content-desc="{desc}" clickable="{str(clickable).lower()}" enabled="true" bounds="{bounds}">'
            f'{children}</node>')


MAIN = node("FrameLayout", "[0,0][320,640]", children=(
    node("ImageButton", "[0,24][48,72]", rid=f"{PKG}:id/up", clickable=True)
    + node("ImageButton", "[272,24][320,72]", rid=f"{PKG}:id/overflow", clickable=True)
    + node("Spinner", "[16,100][304,148]", text="Sort: date", clickable=True)
    + node("EditText", "[16,560][304,608]", text="", rid=f"{PKG}:id/note", clickable=True)
    + node("Button", "[16,400][304,448]", text="Save", clickable=True)))
MENU = node("FrameLayout", "[150,24][320,200]", children=node("ListView", "[150,24][320,200]", children=(
    node("TextView", "[150,24][320,80]", text="Share", clickable=True)
    + node("TextView", "[150,80][320,136]", text="Rename", clickable=True)
    + node("TextView", "[150,136][320,200]", text="Delete", clickable=True))))
PERMISSION = node("FrameLayout", "[0,200][320,440]", pkg="com.google.android.permissioncontroller", children=(
    node("TextView", "[16,220][304,260]", text="Allow Notes to send you notifications?",
         pkg="com.google.android.permissioncontroller")
    + node("Button", "[16,300][304,340]", text="Allow", clickable=True, pkg="com.google.android.permissioncontroller")
    + node("Button", "[16,360][304,400]", text="Don't allow", clickable=True,
           pkg="com.google.android.permissioncontroller")))
SHEET = node("FrameLayout", "[0,400][320,640]", rid=f"{PKG}:id/design_bottom_sheet", children=(
    node("TextView", "[0,420][320,470]", text="Copy link", clickable=True)))
KEYBOARD = node("FrameLayout", "[0,380][320,640]", pkg="com.google.android.inputmethod.latin")


def dump(*windows: str) -> str:
    return '<?xml version="1.0"?><hierarchy rotation="0">' + "".join(windows) + "</hierarchy>"


SCREENS = {"main": dump(MAIN), "menu": dump(MAIN, MENU), "sort": dump(MAIN, MENU),
           "permission": dump(PERMISSION), "sheet": dump(MAIN, SHEET), "keyboard": dump(MAIN, KEYBOARD)}


def _driver(tmp_path, start="main", taps=None):
    engine = FakeEngine(SCREENS, start, package=PKG, activities=dict.fromkeys(SCREENS, ".Main"),
                        taps={("main", f"{PKG}:id/overflow"): "menu", ("main", "Sort: date"): "sort",
                              ("main", "Save"): "permission", **(taps or {})},
                        back={"menu": "main", "keyboard": "main", "menu_only": "main"})
    session = Session(list_devices=lambda: [PHONE], connect=lambda d: engine, screenshots_dir=tmp_path,
                      poll_s=0, first_look_s=0, settle_s=0.05)
    return _Driver(build_server(session)), engine, session


def test_overlay_windows_are_named_by_what_they_are():
    def kinds(xml, package=PKG):
        return [o.kind for o in parse_hierarchy(xml, package=package, size=(320, 640)).overlays]

    assert kinds(dump(MAIN)) == []
    assert kinds(dump(MAIN, MENU)) == ["popup menu"]
    assert kinds(dump(MAIN, SHEET)) == ["bottom sheet"]
    assert kinds(dump(PERMISSION), "com.google.android.permissioncontroller") == ["permission dialog"]
    alert = node("FrameLayout", "[20,200][300,440]", children=node("TextView", "[30,210][290,250]", text="Discard?",
                                                                  rid="android:id/alertTitle"))
    assert kinds(dump(MAIN, alert)) == ["dialog"]
    anr = node("FrameLayout", "[20,200][300,440]", pkg="android",
               children=node("TextView", "[30,210][290,250]", text="Notes isn't responding", pkg="android"))
    assert kinds(dump(MAIN, anr)) == ["crash/ANR dialog"]
    tree = parse_hierarchy(dump(MAIN, KEYBOARD), package=PKG, size=(320, 640))
    assert tree.keyboard and tree.keyboard_frame.y == 380 and tree.overlays == []


def test_a_tap_that_opens_a_menu_lists_every_option_once(tmp_path):
    driver, _, _ = _driver(tmp_path)
    snap, tapped = driver.run([("mobile_snapshot", {}),
                               ("mobile_tap", lambda out: {"ref": _ref(_text(out[0]), "id=overflow")})])
    text = _text(tapped)
    assert "opened: popup menu (3 items): pick one by ref" in text, text
    for item in ("Share", "Rename", "Delete"):
        assert text.count(f'"{item}"') == 1, text  # listed under "opened", not again as "+" lines


def test_a_spinner_opens_a_drop_down_list(tmp_path):
    driver, _, _ = _driver(tmp_path)
    out = driver.run([("mobile_snapshot", {}),
                      ("mobile_tap", lambda out: {"ref": _ref(_text(out[0]), "Sort: date")})])
    assert "opened: drop-down list (3 items)" in _text(out[1])


def test_a_permission_prompt_is_left_to_the_person(tmp_path):
    driver, engine, _ = _driver(tmp_path)
    engine.package = "com.google.android.permissioncontroller"  # the prompt is its own activity
    out = driver.run([("mobile_snapshot", {}),
                      ("mobile_tap", lambda out: {"ref": _ref(_text(out[0]), '"Save"')})])
    text = _text(out[1])
    assert "opened: permission dialog (2 items) from com.google.android.permissioncontroller" in text, text
    assert "Do not answer it yourself" in text and '"Allow"' in text


def test_closing_a_menu_says_so(tmp_path):
    driver, _, _ = _driver(tmp_path, start="menu")
    out = driver.run([("mobile_snapshot", {}), ("mobile_key", {"key": "back"})])
    assert "over the app: popup menu" in _text(out[0])
    assert "closed: popup menu" in _text(out[1])


def test_unlabeled_buttons_say_where_they_are(tmp_path):
    driver, _, _ = _driver(tmp_path)
    text = _text(driver.run([("mobile_snapshot", {})])[0])
    assert "unlabeled, top-left  id=up" in text and "unlabeled, top-right  id=overflow" in text, text
    assert "Save" in text and "unlabeled" not in next(line for line in text.splitlines() if '"Save"' in line)


def test_no_change_on_an_unlabeled_button_points_to_the_annotated_screenshot(tmp_path):
    driver, _, _ = _driver(tmp_path)
    out = driver.run([("mobile_snapshot", {}),
                      ("mobile_tap", lambda out: {"ref": _ref(_text(out[0]), "id=up")})])
    assert "no visible change" in _text(out[1]) and "annotate=true" in _text(out[1])


def test_screenshot_pixels_map_back_to_device_pixels(tmp_path):
    driver, engine, _ = _driver(tmp_path)
    engine.shot_size = (160, 320)  # half the 320×640 screen
    out = driver.run([("mobile_screenshot", {"annotate": True}),
                      ("mobile_tap", {"x": 50, "y": 100, "coordinate_space": "screenshot", "screenshot_id": "shot_1"}),
                      ("mobile_drag", {"from_x": 10, "from_y": 20, "to_x": 30, "to_y": 40,
                                       "coordinate_space": "screenshot", "screenshot_id": "shot_1"})])
    shot = _text(out[0])
    assert "id shot_1, 0.5 image px per device px" in shot and "drawn on it" in shot, shot
    assert "tap 100,200" in engine.log and "drag 20,40->60,80 hold 0.6s" in engine.log, engine.log
    path = shot.split("screenshot ", 1)[1].split(" (", 1)[0]
    with Image.open(path) as img:
        assert (255, 0, 140) in {img.getpixel((x, 12)) for x in range(160)}  # the overflow button's box


def test_an_unknown_screenshot_id_is_refused(tmp_path):
    driver, engine, _ = _driver(tmp_path)
    out = driver.run([("mobile_tap", {"x": 1, "y": 1, "coordinate_space": "screenshot", "screenshot_id": "shot_9"})])
    assert out[0].is_error and "Unknown screenshot_id" in _text(out[0])
    assert not any(line.startswith("tap") for line in engine.log)


def test_a_keyboard_over_the_target_is_hidden_first(tmp_path):
    driver, engine, _ = _driver(tmp_path, start="keyboard")
    out = driver.run([("mobile_snapshot", {}),
                      ("mobile_tap", lambda out: {"ref": _ref(_text(out[0]), '"Save"')})])
    assert engine.log[:2] == ["key back", "tap 160,424"], engine.log
    assert "hid the keyboard first" in _text(out[1])


def test_each_action_has_its_own_settle_budget():
    session = Session(list_devices=list, connect=lambda d: None)
    assert session._cap("launch") == SETTLE_S["launch"] == 10.0
    assert session._cap("type") == 1.5 and session._cap("tap") == DEFAULT_SETTLE_S == 3.0
    assert Session(list_devices=list, connect=lambda d: None, settle_s=0.2)._cap("launch") == 0.2


def test_ticking_text_does_not_keep_a_screen_unsettled():
    def screen(clock, y=0):
        return dump(node("FrameLayout", f"[0,{y}][320,640]", children=node("TextView", "[0,0][320,40]", text=clock)))

    assert fingerprint(screen("10:41")) == fingerprint(screen("10:42"))
    assert fingerprint(screen("10:41")) != fingerprint(screen("10:41", y=10))  # layout still counts


def test_an_empty_slider_frame_is_a_slider():
    frame = node("FrameLayout", "[40,900][1040,980]", rid="com.android.settings:id/slider_frame")
    tree = parse_hierarchy(dump(node("FrameLayout", "[0,0][1080,2400]", children=frame)), package="x")
    assert [n.role for n in tree.root.walk()][-1] == "AXSlider"


def test_a_locked_phone_needs_the_person_and_is_checked_only_after_a_pause(tmp_path):
    driver, engine, session = _driver(tmp_path)
    calls = []

    def locked():
        calls.append(1)
        raise MobilePilotError(ErrorCode.NEEDS_USER, "The phone is locked with a PIN, pattern or password.")

    engine.ensure_awake = locked
    out = driver.run([("mobile_snapshot", {})])
    assert out[0].is_error and "NEEDS_USER" in _text(out[0])
    engine.ensure_awake = lambda: calls.append(2)
    driver.run([("mobile_snapshot", {}), ("mobile_key", {"key": "back"})])
    assert calls == [1, 2], calls  # the second call was within 10 s of the first: not checked again


def test_deep_links_open_and_bad_ones_are_refused(tmp_path):
    driver, engine, _ = _driver(tmp_path)
    out = driver.run([("mobile_app", {"action": "open", "url": "notes://note/42"}),
                      ("mobile_app", {"action": "open", "url": "not a link"})])
    assert "app open notes://note/42" in engine.log and "screen" in _text(out[0])
    assert out[1].is_error and "open needs url" in _text(out[1])


@pytest.mark.parametrize("name", ["mobile_tap", "mobile_drag", "mobile_screenshot", "mobile_app"])
def test_new_parameters_are_in_the_tool_schemas(name):
    import anyio
    from mcp.client import Client

    async def go():
        async with Client(build_server(Session(list_devices=list, connect=lambda d: None))) as client:
            return {t.name: t for t in (await client.list_tools()).tools}

    props = anyio.run(go)[name].input_schema["properties"]
    expected = {"mobile_tap": "screenshot_id", "mobile_drag": "coordinate_space", "mobile_screenshot": "annotate",
                "mobile_app": "url"}[name]
    assert expected in props


def test_android_17_menu_alone_in_the_dump_is_still_a_menu_and_closing_it_is_one_line(tmp_path):
    # Android 17 dumps only the focused window: the open menu, without the screen under it
    SCREENS["menu_only"] = dump(MENU)
    driver, _, _ = _driver(tmp_path, taps={("main", f"{PKG}:id/overflow"): "menu_only"})
    out = driver.run([("mobile_snapshot", {}),
                      ("mobile_tap", lambda out: {"ref": _ref(_text(out[0]), "id=overflow")}),
                      ("mobile_key", {"key": "back"})])
    opened, closed = _text(out[1]), _text(out[2])
    assert "opened: popup menu (3 items)" in opened, opened
    assert "elements of the screen behind it" in opened and '- button' not in opened, opened
    assert "closed: popup menu" in closed and "elements back as snapshot" in closed, closed
    assert '"Save"' not in closed and '"Share"' not in closed, closed  # one line each way

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


def test_when_the_person_changes_the_screen_the_action_works_from_the_real_one(tmp_path):
    driver, engine, _ = _driver(tmp_path)
    first = driver.run([("mobile_snapshot", {})])[0]
    up = _ref(_text(first), "id=up")
    engine.current = "menu"  # the person opened the menu by hand
    out = driver.run([("mobile_tap", {"ref": up})])
    assert not out[0].is_error and "since the last result" not in _text(out[0]), _text(out[0])  # same place: no noise
    assert engine.log[-1] == "tap 24,48"

    engine.screens["other"] = dump(node("FrameLayout", "[0,0][320,640]", children=node(
        "Button", "[0,0][320,80]", text="Unrelated", clickable=True)))
    engine.current = "other"  # then went somewhere else entirely
    taps = len(engine.log)
    out = driver.run([("mobile_tap", {"ref": up})])
    text = _text(out[0])
    assert out[0].is_error and "STALE_REF" in text and "Nothing was done" in text and '"Unrelated"' in text, text
    assert len(engine.log) == taps  # nothing was tapped


def test_an_unchanged_screen_costs_no_second_capture(tmp_path):
    driver, engine, _ = _driver(tmp_path)
    captures = []
    real = engine.capture_tree
    engine.capture_tree = lambda: captures.append(1) or real()
    out = driver.run([("mobile_snapshot", {}), ("mobile_tap", lambda out: {"ref": _ref(_text(out[0]), "id=up")})])
    assert "had changed" not in _text(out[1])
    assert len(captures) == 3  # the snapshot, the fake's own hit test, the diff; no rebase capture


def test_a_rotation_is_reported_although_no_name_or_state_changed(tmp_path):
    driver, _, _ = _driver(tmp_path)
    out = driver.run([("mobile_snapshot", {}), ("mobile_rotate", {"orientation": "landscape"}),
                      ("mobile_rotate", {"orientation": "auto"})])
    text = _text(out[1])
    assert "screen is now 640×320 (landscape)" in text and "no visible change" not in text, text
    assert "screen is now 320×640 (portrait)" in _text(out[2])


def test_a_material_picker_with_cancel_and_ok_is_a_dialog():
    picker = node("FrameLayout", "[100,600][980,1900]", children=(
        node("Button", "[600,1780][720,1860]", text="Cancel", rid=f"{PKG}:id/material_timepicker_cancel_button",
             clickable=True)
        + node("Button", "[800,1780][920,1860]", text="OK", rid=f"{PKG}:id/material_timepicker_ok_button",
               clickable=True)))
    tree = parse_hierarchy(dump(picker), package=PKG, size=(1080, 2400))
    assert [o.kind for o in tree.overlays] == ["dialog"]


def test_typing_on_a_screen_without_a_text_field_is_refused(tmp_path):
    keypad = dump(node("FrameLayout", "[0,0][320,640]", children=node("Button", "[0,0][100,100]", text="1",
                                                                          clickable=True)))
    engine = FakeEngine({"keypad": keypad}, "keypad", package=PKG)
    session = Session(list_devices=lambda: [PHONE], connect=lambda d: engine, screenshots_dir=tmp_path,
                      poll_s=0, first_look_s=0, settle_s=0.05)
    out = _Driver(build_server(session)).run([("mobile_snapshot", {}), ("mobile_type", {"text": "130"})])
    assert out[1].is_error and "no text field" in _text(out[1]) and engine.typed == []


def test_an_empty_web_view_is_not_reported_as_nothing_happening(tmp_path):
    web = dump(node("FrameLayout", "[0,0][320,640]", children=node("WebView", "[0,40][320,640]")))
    assert parse_hierarchy(web, package=PKG, size=(320, 640)).empty_web
    assert not parse_hierarchy(dump(MAIN), package=PKG, size=(320, 640)).empty_web
    engine = FakeEngine({"web": web}, "web", package=PKG)
    session = Session(list_devices=lambda: [PHONE], connect=lambda d: engine, screenshots_dir=tmp_path,
                      poll_s=0, first_look_s=0, settle_s=0.05)
    out = _Driver(build_server(session)).run([("mobile_snapshot", {}), ("mobile_swipe", {"direction": "down"}),
                                              ("mobile_find", {"text": "History"})])
    assert "no visible change" not in _text(out[1]) and "not in the accessibility tree" in _text(out[1])
    assert out[2].is_error and "Chrome drops it" in _text(out[2]) and "end of the list" not in _text(out[2])


def test_many_removed_rows_collapse_and_few_stay_listed(tmp_path):
    def screen(n):
        return dump(node("FrameLayout", "[0,0][320,640]", children="".join(
            node("Button", f"[0,{i * 60}][300,{i * 60 + 50}]", text=f"Item {i}", clickable=True) for i in range(n))
            + node("Button", "[0,600][300,640]", text="Go", clickable=True)))

    for count, collapsed in ((8, True), (3, False)):
        engine = FakeEngine({"list": screen(count), "empty": dump(node("FrameLayout", "[0,0][320,640]"))}, "list",
                            package=PKG, taps={("list", "Go"): "empty"}, activities={"list": ".A", "empty": ".A"})
        session = Session(list_devices=lambda: [PHONE], connect=lambda d, e=engine: e, screenshots_dir=tmp_path,
                          poll_s=0, first_look_s=0, settle_s=0.05)
        out = _Driver(build_server(session)).run([("mobile_snapshot", {}),
                                                  ("mobile_tap", lambda out: {"ref": _ref(_text(out[0]), '"Go"')})])
        text = _text(out[1])
        if collapsed:
            assert f'- {count + 1} elements gone, e.g. "Item 0", "Item 1", "Item 2"' in text, text
        else:
            assert text.count('- button "Item') == count, text


def test_a_moved_target_is_tapped_where_it_is_now_and_said_so(tmp_path):
    driver, engine, _ = _driver(tmp_path)
    first = driver.run([("mobile_snapshot", {})])[0]
    save = _ref(_text(first), '"Save"')
    engine.screens["main"] = engine.screens["main"].replace("[16,400][304,448]", "[16,300][304,348]")  # scrolled up
    out = driver.run([("mobile_tap", {"ref": save})])
    assert f"{save} had moved since the last result" in _text(out[0]) and engine.log[-1] == "tap 160,324", engine.log


def test_a_gesture_on_a_picture_says_the_picture_changed(tmp_path):
    driver, engine, _ = _driver(tmp_path)
    engine.picture = 1
    out = driver.run([("mobile_snapshot", {}), ("mobile_drag", {"from_x": 10, "from_y": 300, "to_x": 200, "to_y": 300,
                                                                "hold_s": 0})])
    assert "the picture did" in _text(out[1]) and "did nothing" not in _text(out[1]), _text(out[1])
    engine.picture = None
    out = driver.run([("mobile_drag", {"from_x": 10, "from_y": 300, "to_x": 200, "to_y": 300, "hold_s": 0})])
    assert "no visible change" in _text(out[0])


def test_the_no_tree_hint_is_for_nameless_screens_not_small_dialogs():
    from mobilepilot.perception.format import format_snapshot
    from mobilepilot.perception.snapshot import build_snapshot

    def text(xml):
        return format_snapshot(build_snapshot(parse_hierarchy(xml, package=PKG, size=(320, 640)), snapshot_id="s"))

    game = dump(node("FrameLayout", "[0,0][320,640]", children=node("EditText", "[0,0][320,40]", clickable=True)))
    dialog = dump(node("FrameLayout", "[0,0][320,640]", children=node("Button", "[0,0][100,40]", text="OK",
                                                                      clickable=True)))
    assert "Little or nothing in the tree" in text(game)
    assert "Little or nothing in the tree" not in text(dialog)


def test_find_behind_a_dialog_says_the_dialog_is_in_the_way(tmp_path):
    driver, _, _ = _driver(tmp_path, start="sheet")
    out = driver.run([("mobile_find", {"text": "nothing like this"})])
    assert out[0].is_error and "A bottom sheet is open over the app" in _text(out[0]), _text(out[0])


def test_the_notification_shade_is_not_a_question():
    shade = node("FrameLayout", "[0,0][320,640]", pkg="com.android.systemui", children=node(
        "Button", "[0,100][320,160]", text="Wi-Fi", clickable=True, pkg="com.android.systemui"))
    # as dumped live: the shade is the focused window, so it is all there is
    tree = parse_hierarchy(dump(shade), package="com.android.systemui", size=(320, 640))
    assert [o.kind for o in tree.overlays] == ["notification shade"]


def test_a_compose_button_is_named_from_the_content_desc_inside_it():
    from mobilepilot.perception.format import format_snapshot
    from mobilepilot.perception.snapshot import build_snapshot

    row = node("View", "[63,1048][891,1211]", clickable=True, children=node("View", "[63,1048][891,1211]", children=(
        node("View", "[252,1048][891,1211]", desc="Calculator&#10;SOLITUDE&#10;Tools"))))
    xml = dump(node("FrameLayout", "[0,0][1080,2400]", children=row))
    text = format_snapshot(build_snapshot(parse_hierarchy(xml, package=PKG, size=(1080, 2400)), snapshot_id="s"))
    assert "Calculator" in text and "unlabeled" not in text, text


def test_a_full_screen_bottom_sheet_dialog_is_a_sheet():
    sheet = node("FrameLayout", "[0,0][1080,2400]", children=(
        node("View", "[0,0][1080,2400]", rid=f"{PKG}:id/touch_outside", clickable=True)
        + node("FrameLayout", "[0,1900][1080,2400]", rid=f"{PKG}:id/design_bottom_sheet", children=node(
            "Button", "[40,2000][1040,2100]", text="Use my current location", clickable=True))))
    tree = parse_hierarchy(dump(sheet), package=PKG, size=(1080, 2400))
    assert [o.kind for o in tree.overlays] == ["bottom sheet"]
    assert any(n.description == "Outside the sheet (closes it)" for n in tree.root.walk())

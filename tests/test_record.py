"""Offline tests for mobile_record: real getevent captures from the API 34 emulator, synthetic
dumps through the real parser, the Recorder, and the tool through the real MCP server."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from mobilepilot.engine.android import fingerprint, ime_frame, parse_hierarchy
from mobilepilot.engine.base import Device
from mobilepilot.engine.fake import FakeEngine
from mobilepilot.models import Rect
from mobilepilot.record import (
    SECRET,
    Clock,
    Frame,
    InputConfig,
    KeyPress,
    Panel,
    Recorder,
    ScriptedStream,
    Touch,
    TouchStart,
    TouchTracker,
    classify,
    format_table,
    parse_line,
    parse_panels,
    to_display,
)
from mobilepilot.server import Session, build_server

FIX = Path(__file__).parent / "fixtures" / "record"
DEV = "/dev/input/event1"
PANELS = {DEV: Panel(DEV, 32767, 32767)}
NATURAL = (320, 640)
CFG = InputConfig(density=160, long_press_s=0.4, nav_mode=2)
PKG = "com.android.settings"


def lines(name: str) -> list[str]:
    return (FIX / f"getevent_{name}.txt").read_text().splitlines()


def track(*texts: list[str]) -> list:
    tracker = TouchTracker(PANELS, NATURAL)
    out = []
    for text in texts:
        for line in text:
            ev = parse_line(line)
            if ev is not None:
                out += tracker.feed(ev)
    return out


def touches(*texts) -> list[Touch]:
    return [o for o in track(*texts) if isinstance(o, Touch)]


def raw(t: float, code: str, value: int, etype: str = "EV_ABS") -> str:
    return f"[{t:14.6f}] {DEV}: {etype:<12} {code:<20} {value & 0xffffffff:08x}"


def syn(t: float) -> str:
    return raw(t, "SYN_REPORT", 0, "EV_SYN")


def finger(path, *, t0=100.0, dt=0.016, slot=0, tid=1) -> list[str]:
    """A one-finger gesture through panel pixels (natural orientation)."""
    out = []
    for i, (x, y) in enumerate(path):
        t = t0 + i * dt
        if i == 0:
            out += [raw(t, "ABS_MT_SLOT", slot), raw(t, "ABS_MT_TRACKING_ID", tid)]
        out += [raw(t, "ABS_MT_POSITION_X", round(x / 320 * 32768)), raw(t, "ABS_MT_POSITION_Y", round(y / 640 * 32768)),
                syn(t)]
    t = t0 + len(path) * dt
    return out + [raw(t, "ABS_MT_TRACKING_ID", -1), syn(t)]


def line_path(a, b, n=10):
    return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n) for i in range(n + 1)]


def gesture(path, *, hold=0.0, dt=0.016, t0=100.0) -> Touch:
    pts = [(t0 + i * dt + (hold if i else 0), x, y) for i, (x, y) in enumerate(path)]
    end = pts[-1][0] + (hold if len(pts) == 1 else dt)
    return Touch(DEV, t0, end, [pts])


# --------------------------------------------------------------------------- the stream
def test_parse_line_reads_signed_values_keys_and_skips_noise():
    ev = parse_line("[   10438.766507] /dev/input/event1: EV_ABS       ABS_MT_TRACKING_ID   ffffffff            ")
    assert ev.value == -1 and ev.device == DEV and ev.t == pytest.approx(10438.766507)
    key = parse_line("[  200.1] /dev/input/event0: EV_KEY       KEY_VOLUMEUP         DOWN")
    assert (key.type, key.code, key.value) == ("EV_KEY", "KEY_VOLUMEUP", 1)
    for junk in ("add device 1: /dev/input/event11", "  name:     \"gpio-keys\"", "could not get driver version",
                 "[   10438.76", ""):
        assert parse_line(junk) is None


def test_parse_panels_finds_every_touchscreen_and_no_key_device():
    panels = parse_panels((FIX / "getevent_pl_emulator.txt").read_text())
    assert len(panels) == 11 and DEV in panels and "/dev/input/event0" not in panels  # event0 is gpio-keys
    assert (panels[DEV].max_x, panels[DEV].max_y) == (32767, 32767)


@pytest.mark.parametrize("name, kind", [("tap", "tap"), ("swipe_up", "swipe"), ("edge_back", "back"), ("drag", "drag")])
def test_real_captures_classify(name, kind):
    found = touches(lines(name))
    assert len(found) == 1
    assert classify(found[0], CFG, 0, NATURAL).kind == kind


def test_a_touch_at_the_same_place_as_the_last_one_keeps_its_position():
    # the kernel left out the long press's position: it is where the previous touch was
    found = touches(lines("tap"), lines("long_press"))
    assert [classify(t, CFG, 0, NATURAL).kind for t in found] == ["tap", "long_press"]
    assert found[1].fingers[0][0][1:] == pytest.approx((160, 300), abs=1)


def test_a_touch_with_no_position_at_all_is_reported_unknown_not_placed():
    found = touches(lines("long_press"))  # no earlier touch told where the finger was
    assert len(found) == 1 and classify(found[0], CFG, 0, NATURAL).kind == "unknown"


def test_a_real_two_finger_pinch_spreading_apart_is_zoom_in():
    before = [raw(1.0, "ABS_MT_SLOT", 0), raw(1.0, "ABS_MT_TRACKING_ID", 40), raw(1.0, "ABS_MT_POSITION_X", 14000),
              raw(1.0, "ABS_MT_POSITION_Y", 16000), raw(1.0, "ABS_MT_SLOT", 1), raw(1.0, "ABS_MT_TRACKING_ID", 41),
              raw(1.0, "ABS_MT_POSITION_X", 18000), raw(1.0, "ABS_MT_POSITION_Y", 16000), syn(1.0),
              raw(1.1, "ABS_MT_SLOT", 0), raw(1.1, "ABS_MT_TRACKING_ID", -1), raw(1.1, "ABS_MT_SLOT", 1),
              raw(1.1, "ABS_MT_TRACKING_ID", -1), syn(1.1)]
    found = touches(before, lines("pinch_out"))
    g = classify(found[-1], CFG, 0, NATURAL)
    assert (g.kind, g.zoom, g.fingers) == ("pinch", "in", 2)


def test_touch_start_is_announced_once_with_where_the_finger_went_down():
    out = track(lines("tap"))
    starts = [o for o in out if isinstance(o, TouchStart)]
    assert len(starts) == 1 and (starts[0].x, starts[0].y) == pytest.approx((160, 300), abs=1)


def test_dropped_events_mark_the_gesture():
    text = finger(line_path((100, 300), (100, 100)))
    text.insert(4, raw(100.0, "SYN_DROPPED", 0, "EV_SYN"))
    assert touches(text)[0].dropped


def test_the_old_multitouch_protocol_is_refused_with_a_reason():
    tracker = TouchTracker(PANELS, NATURAL)
    tracker.feed(parse_line(raw(1.0, "SYN_MT_REPORT", 0, "EV_SYN")))
    assert "type A" in tracker.unsupported


def test_keys_come_out_as_presses_and_touch_buttons_are_ignored():
    text = ["[  5.000000] /dev/input/event0: EV_KEY       KEY_VOLUMEDOWN       DOWN",
            "[  5.000000] /dev/input/event1: EV_KEY       BTN_TOUCH            DOWN",
            "[  5.100000] /dev/input/event0: EV_KEY       KEY_VOLUMEDOWN       UP"]
    presses = [o for o in track(text) if isinstance(o, KeyPress)]
    assert [(p.code, round(p.end - p.start, 2)) for p in presses] == [("KEY_VOLUMEDOWN", 0.1)]


def test_a_panel_with_its_own_resolution_maps_to_screen_pixels():
    tracker = TouchTracker({DEV: Panel(DEV, 1079, 2399)}, (1080, 2400))
    out = []
    for line in [raw(1, "ABS_MT_TRACKING_ID", 3), raw(1, "ABS_MT_POSITION_X", 540), raw(1, "ABS_MT_POSITION_Y", 1200),
                 syn(1), raw(1.05, "ABS_MT_TRACKING_ID", -1), syn(1.05)]:
        out += tracker.feed(parse_line(line))
    assert out[-1].fingers[0][0][1:] == (540, 1200)


def test_clock_uses_the_least_delayed_line():
    c = Clock()
    assert c.host(10.0, 1000.08) == pytest.approx(1000.08)
    c.host(11.0, 1001.04)  # this line came through faster
    assert c.host(12.0, 1003.0) == pytest.approx(1002.04)


# --------------------------------------------------------------------------- geometry and gestures
def test_rotation_mapping_matches_what_the_emulator_measured():
    # rotation 1: Stopwatch tab at display (448, 292) answered a raw touch at natural (28, 448)
    assert to_display(28, 448, 1, NATURAL) == (448, 292)
    # rotation 3: Timer tab at display (320, 292) answered natural (292, 320)
    assert to_display(292, 320, 3, NATURAL) == (320, 292)
    assert to_display(10, 20, 2, NATURAL) == (310, 620)


def test_tap_and_long_press_use_the_phones_own_timeout():
    hold = gesture([(160, 300)], hold=0.45)
    assert classify(hold, CFG, 0, NATURAL).kind == "long_press"
    assert classify(hold, InputConfig(long_press_s=0.5), 0, NATURAL).kind == "tap"


def test_edge_swipes_are_navigation_only_with_gesture_navigation():
    back = gesture(line_path((3, 300), (150, 305)))
    assert classify(back, CFG, 0, NATURAL).kind == "back"
    assert classify(gesture(line_path((317, 300), (150, 300))), CFG, 0, NATURAL).kind == "back"
    three_buttons = InputConfig(long_press_s=0.4, nav_mode=0)
    assert classify(back, three_buttons, 0, NATURAL).kind == "swipe"


def test_home_recents_and_the_notification_shade():
    assert classify(gesture(line_path((160, 636), (160, 400))), CFG, 0, NATURAL).kind == "home"
    pause = gesture(line_path((160, 636), (160, 400)) + [(160, 400)] * 30, dt=0.02)
    assert classify(pause, CFG, 0, NATURAL).kind == "recents"
    assert classify(gesture(line_path((160, 5), (160, 300))), CFG, 0, NATURAL).kind == "notifications"
    # in landscape the bottom edge is another panel side
    assert classify(gesture(line_path((3, 300), (240, 300))), CFG, 1, NATURAL).kind == "home"  # display bottom = natural left


def test_a_three_button_navigation_bar_is_told_by_position():
    cfg = InputConfig(long_press_s=0.4, nav_mode=0)
    assert [classify(gesture([(x, 620)]), cfg, 0, NATURAL).kind for x in (40, 160, 280)] == ["back", "home", "recents"]


def test_swipe_records_finger_and_scroll_direction_like_mobile_swipe():
    g = classify(gesture(line_path((160, 500), (160, 200))), CFG, 0, NATURAL)
    assert (g.kind, g.finger) == ("swipe", "up")


def test_two_and_three_finger_gestures():
    def two(a, b, c, d):
        return Touch(DEV, 0, 0.3, [[(0, *a), (0.3, *c)], [(0, *b), (0.3, *d)]])

    assert classify(two((150, 300), (170, 300), (100, 300), (220, 300)), CFG, 0, NATURAL).zoom == "in"
    assert classify(two((100, 300), (220, 300), (150, 300), (170, 300)), CFG, 0, NATURAL).zoom == "out"
    swipe = classify(two((100, 300), (200, 300), (100, 150), (200, 150)), CFG, 0, NATURAL)
    assert (swipe.kind, swipe.finger) == ("two_finger_swipe", "up")
    assert classify(two((100, 300), (200, 300), (101, 300), (201, 300)), CFG, 0, NATURAL).kind == "two_finger_tap"
    three = Touch(DEV, 0, 0.3, [[(0, 50, 50)], [(0, 60, 60)], [(0, 70, 70)]])
    assert classify(three, CFG, 0, NATURAL).kind == "multi_finger"


def test_keyboard_frame_from_dumpsys_window():
    out = "mGivenContentInsets=[0,381][0,0]\n frame=[0,24][320,640]\nisVisible=true\n"
    assert ime_frame(out) == Rect(0, 405, 320, 235)  # the tree's IME window said y=405 on the same screen
    assert ime_frame(out.replace("isVisible=true", "isVisible=false")) is None


# --------------------------------------------------------------------------- screens
def node(cls, bounds, *, text="", desc="", rid="", clickable=False, pkg=PKG, children="", **flags):
    attrs = " ".join(f'{k}="{str(v).lower()}"' for k, v in flags.items())
    return (f'<node text="{text}" resource-id="{rid}" class="{cls if "." in cls else "android.widget." + cls}" '
            f'package="{pkg}" content-desc="{desc}" clickable="{str(clickable).lower()}" enabled="true" '
            f'bounds="{bounds}" {attrs}>{children}</node>')


def row(y, title, extra=""):
    return node("LinearLayout", f"[0,{y}][320,{y + 80}]", clickable=True, children=(
        node("TextView", f"[16,{y + 10}][240,{y + 40}]", text=title, rid="android:id/title") + extra))


def screen(*children, pkg=PKG, rotation=0):
    body = node("FrameLayout", "[0,0][320,640]", pkg=pkg, children="".join(children))
    return f'<hierarchy rotation="{rotation}">{body}</hierarchy>'


HOME = screen(node("androidx.recyclerview.widget.RecyclerView", "[0,80][320,640]", rid=f"{PKG}:id/list",
                   scrollable=True, children=(
                       row(80, "Network &amp; internet")
                       + row(160, "Connected devices")
                       + row(240, "Wi-Fi", node("Switch", "[250,260][310,300]", rid="android:id/switch_widget",
                                                clickable=True, checkable=True, checked=False)))))
WIFI_ON = HOME.replace('checked="false"', 'checked="true"')
NETWORK = screen(row(80, "Internet"), row(160, "SIMs"))


def field_screen(value="", *, focused=True, password=False, extra=""):
    return screen(node("EditText", "[16,100][304,148]", text=value, rid=f"{PKG}:id/name", clickable=True,
                       focused=focused, password=password)
                  + node("Button", "[16,200][304,248]", text="Save", clickable=True) + extra)


KEYBOARD = Rect(0, 400, 320, 240)


def tree(xml):
    return parse_hierarchy(xml, package=PKG, activity="Main", size=NATURAL)


class Rig:
    """A Recorder fed by hand: frames and touches at explicit times."""

    def __init__(self, tmp_path, cfg=CFG):
        self.now = 0.0
        self.shots: list[str] = []

        def shoot(path):
            self.shots.append(path.name)
            path.write_bytes(b"png")
            return True

        self.rec = Recorder(tmp_path / "rec", natural=NATURAL, config=cfg, shoot=shoot, device="emu",
                            clock=lambda: self.now)

    def frame(self, xml, at, *, keyboard=None, locked=False, rotation=0, title="Main", pkg=PKG,
              keyboard_unknown=False):
        self.now = at
        t = parse_hierarchy(xml, package=pkg, activity=title, size=(640, 320) if rotation % 2 else NATURAL)
        self.rec.frame(Frame(at - 0.05, at, t, fingerprint(xml), keyboard=keyboard, locked=locked, rotation=rotation,
                             keyboard_unknown=keyboard_unknown))

    def settle(self, xml, at, **kw):
        self.frame(xml, at, **kw)
        self.frame(xml, at + 0.15, **kw)

    def tap(self, x, y, at, *, hold=0.08):
        self.now = at
        self.rec.touch_start(at, x, y)
        self.now = at + hold
        self.rec.touch(Touch(DEV, at, at + hold, [[(at, x, y)]]))

    def swipe(self, a, b, at):
        path = line_path(a, b)
        self.now = at
        self.rec.touch_start(at, *a)
        pts = [(at + i * 0.02, x, y) for i, (x, y) in enumerate(path)]
        self.now = pts[-1][0]
        self.rec.touch(Touch(DEV, at, pts[-1][0], [pts]))

    def stop(self):
        return self.rec.stop()

    def jsonl(self):
        return [json.loads(line) for line in (self.rec.folder / "steps.jsonl").read_text().splitlines()]


# --------------------------------------------------------------------------- the recorder
def test_a_tap_on_a_rows_label_records_the_row_with_its_locator(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.tap(100, 120, 2.0)
    r.settle(NETWORK, 2.6, title="Network")
    step = r.stop()["steps"][0]
    assert step["action"] == "tap" and step["target"]["role"] == "row"
    assert step["target"]["name"].startswith("Network & internet")
    assert step["target"]["locator"]["value"] == 'new UiSelector().text("Network & internet")'
    assert step["sure"] is True and step["screenshot"] == "step-01-tap.png"
    assert "new screen: Network" in step["result"]
    assert step["at_pct"] == [0.312, 0.188]


def test_a_tap_on_the_switch_inside_a_row_records_the_switch(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.tap(280, 280, 2.0)
    r.settle(WIFI_ON, 2.6)
    step = r.stop()["steps"][0]
    assert step["target"]["role"] == "switch"
    assert step["target"]["locator"]["value"] == "android:id/switch_widget"
    assert step["target"]["in"] == "Wi-Fi"  # a nameless switch borrows its row's title
    assert step["result"] == '"Wi-Fi" switch: off → on'


def test_the_element_is_read_from_the_screen_before_the_finger_went_down(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.tap(100, 120, 2.0)
    r.settle(NETWORK, 2.1, title="Network")  # this reading finished AFTER the finger went down
    r.tap(100, 120, 3.5)  # "Internet" sits where "Network & internet" was
    r.settle(NETWORK, 4.0, title="Network")
    steps = r.stop()["steps"]
    assert steps[0]["target"]["name"].startswith("Network & internet")
    assert steps[1]["target"]["name"].startswith("Internet")


def test_a_tap_before_the_screen_settled_is_marked_unsure_not_guessed_silently(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.tap(100, 120, 2.0)
    r.frame(NETWORK, 2.2, title="Network")  # one reading only: still moving
    r.tap(100, 200, 2.3)
    steps = r.stop()["steps"]
    assert steps[1]["sure"] is False and "still changing" in steps[1]["note"]
    assert steps[1]["at"] == [100, 200]


def test_nothing_tappable_under_the_finger_keeps_the_position(tmp_path):
    r = Rig(tmp_path)
    blank = screen(node("View", "[0,0][320,640]"))
    r.settle(blank, 1.0)
    r.tap(50, 50, 2.0)
    step = r.stop()["steps"][0]
    assert "target" not in step and "replay by position" in step["note"] and step["at"] == [50, 50]


def test_a_swipe_names_the_list_it_scrolled(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.swipe((160, 500), (160, 200), 2.0)
    step = r.stop()["steps"][0]
    assert (step["action"], step["finger"], step["scroll"]) == ("swipe", "up", "down")
    assert step["container"]["id"] == f"{PKG}:id/list"


def test_two_quick_taps_are_one_double_tap_and_slow_ones_are_two(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.tap(100, 120, 2.0)
    r.tap(102, 121, 2.2)
    r.settle(HOME, 3.0)
    r.tap(100, 120, 4.0)
    r.settle(HOME, 5.0)
    r.tap(100, 120, 6.0)
    assert [s["action"] for s in r.stop()["steps"]] == ["double_tap", "tap", "tap"]


def test_typing_is_read_from_the_field_and_keyboard_touches_are_never_stored(tmp_path):
    r = Rig(tmp_path)
    r.settle(field_screen(""), 1.0, keyboard=KEYBOARD)
    for i, (x, y) in enumerate([(30, 450), (90, 520), (200, 600)]):  # keys under the keyboard
        r.tap(x, y, 2.0 + i * 0.2)
    r.settle(field_screen("Office-5G"), 3.0, keyboard=KEYBOARD)
    r.tap(160, 224, 5.0)  # Save
    r.settle(field_screen("Office-5G", focused=False), 5.6)
    wf = r.stop()
    actions = [s["action"] for s in wf["steps"]]
    assert actions == ["type", "tap"]
    typed = wf["steps"][0]
    assert typed["data"] == "Office-5G" and typed["target"]["id"] == f"{PKG}:id/name"
    assert typed["screenshot"] == "step-01-type.png" and not typed.get("submitted")
    written = (r.rec.folder / "steps.jsonl").read_text() + (r.rec.folder / "workflow.json").read_text()
    for x, y in [(30, 450), (90, 520), (200, 600)]:
        assert f"[{x}, {y}]" not in written and f"[{x},{y}]" not in written
    assert "3 keyboard touches were not stored" in " ".join(wf["notes"])


def test_text_that_arrives_with_no_keyboard_touch_is_still_recorded(tmp_path):
    # adb shell input text, a paste, a hardware keyboard: the field changes, nothing was touched
    r = Rig(tmp_path)
    r.settle(field_screen(""), 1.0)
    r.settle(field_screen("hello"), 2.0)
    r.tap(160, 224, 4.0)
    assert [(s["action"], s.get("data")) for s in r.stop()["steps"]][0] == ("type", "hello")


def test_a_password_is_never_written_and_never_photographed(tmp_path):
    r = Rig(tmp_path)
    r.settle(field_screen("", password=True), 1.0, keyboard=KEYBOARD)
    r.tap(30, 450, 2.0)
    r.tap(90, 520, 2.2)
    r.settle(field_screen("", password=True), 2.5, keyboard=KEYBOARD)
    r.tap(160, 224, 3.0)  # Sign in, 0.8 s after the last key: Android still shows that character
    r.settle(field_screen("", password=True, focused=False), 3.4)
    wf = r.stop()
    typed, tap = wf["steps"]
    assert typed["data"] == SECRET and typed["secret"] and typed["screenshot"] is None
    assert tap["screenshot"] is None and "password" in tap["screenshot_note"]
    assert not any(name.startswith("typing") for name in r.shots)


def test_a_pin_pad_inside_an_app_is_not_recorded_while_a_password_field_has_focus(tmp_path):
    pad = node("Button", "[0,300][100,360]", text="1", clickable=True) + node("Button", "[100,300][200,360]", text="2",
                                                                               clickable=True)
    r = Rig(tmp_path)
    r.settle(field_screen("", password=True, extra=pad), 1.0)
    r.tap(50, 330, 2.0)
    step = r.stop()["steps"][0]
    assert step["redacted"] and "target" not in step and "at" not in step


def test_editing_prefilled_text_says_it_was_cleared_first(tmp_path):
    r = Rig(tmp_path)
    r.settle(field_screen("Old name"), 1.0, keyboard=KEYBOARD)
    r.settle(field_screen("New"), 2.0, keyboard=KEYBOARD)
    r.tap(160, 224, 4.0)
    typed = r.stop()["steps"][0]
    assert (typed["data"], typed["cleared"]) == ("New", True)


def test_closing_the_keyboard_with_no_other_touch_means_enter_was_pressed(tmp_path):
    r = Rig(tmp_path)
    r.settle(field_screen(""), 1.0, keyboard=KEYBOARD)
    r.tap(300, 620, 2.0)  # the keyboard's Enter key
    r.settle(field_screen("cats"), 2.5, keyboard=KEYBOARD)
    r.settle(field_screen("cats"), 3.0)  # keyboard gone
    typed = r.stop()["steps"][0]
    assert typed["submitted"] is True


def test_lock_screen_touches_become_one_unlock_step_with_nothing_in_it(tmp_path):
    lock = screen(node("FrameLayout", "[0,0][320,640]", rid="com.android.systemui:id/keyguard_bouncer_container",
                       pkg="com.android.systemui"), pkg="com.android.systemui")
    r = Rig(tmp_path)
    r.settle(lock, 1.0, locked=True, pkg="com.android.systemui")
    for i in range(4):
        r.tap(80 + i * 40, 400, 2.0 + i * 0.3)
    r.settle(HOME, 4.0)
    r.tap(100, 120, 5.0)
    steps = r.stop()["steps"]
    assert [s["action"] for s in steps] == ["unlock", "tap"]
    assert set(steps[0]) <= {"n", "t", "action", "sure", "note", "screenshot"} and steps[0]["screenshot"] is None


def test_hardware_keys_and_a_physical_keyboard(tmp_path):
    r = Rig(tmp_path)
    r.settle(field_screen(""), 1.0)
    r.rec.key(KeyPress("KEY_A", 1.5, 1.55))  # a physical keyboard letter: never a step
    r.rec.key(KeyPress("KEY_WAKEUP", 1.6, 1.65))  # tap-to-wake / fingerprint swipes: neither a step nor typing
    r.rec.key(KeyPress("KEY_VOLUMEUP", 2.0, 2.1))
    r.rec.key(KeyPress("KEY_POWER", 3.0, 4.0))
    steps = r.stop()["steps"]
    assert [(s["action"], s.get("key")) for s in steps] == [("key", "volume_up"), ("key", "power")]
    assert "power menu" in steps[1]["note"]
    assert "KEY_A" not in (r.rec.folder / "workflow.json").read_text()
    assert r.rec.keyboard_touches == 1


def test_turning_the_phone_is_a_rotate_step_and_later_touches_use_the_new_mapping(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    land = screen(node("Button", "[400,250][500,300]", text="Stopwatch", clickable=True), rotation=1)
    r.settle(land, 2.0, rotation=1)
    r.tap(40, 450, 3.0)  # natural (40, 450) is display (450, 280) in rotation 1
    steps = r.stop()["steps"]
    assert steps[0] == {**steps[0], "action": "rotate", "orientation": "landscape"}
    assert steps[1]["target"]["name"] == "Stopwatch"


def test_steps_are_on_disk_as_they_happen(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.tap(100, 120, 2.0)
    r.settle(NETWORK, 2.8, title="Network")
    assert [s["action"] for s in r.jsonl()] == ["tap"]  # before stop: a crash keeps it


def test_stopping_mid_gesture_and_before_settling_says_so(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.tap(100, 120, 2.0)
    r.rec.touch_start(2.5, 100, 200)
    wf = r.stop()
    assert wf["steps"][0]["settled"] is False
    assert any("still down" in n for n in wf["notes"])


def test_the_table_is_short_and_names_unsure_steps(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.tap(100, 120, 2.0)
    r.frame(NETWORK, 2.2, title="Network")
    r.tap(100, 200, 2.3)
    wf = r.stop()
    text = format_table(wf, r.rec.folder)
    assert "Recorded 2 steps" in text and "1 unsure" in text and "UNSURE" in text
    assert "workflow.json" in text and len(text) < 2000


# --------------------------------------------------------------------------- the tool
PHONE = Device("emulator-5554", "android", "emulator", "device")


def _rec_session(tmp_path, *, stream=True, timeline=(), screens=None):
    fake = FakeEngine(screens or {"home": HOME, "network": NETWORK}, "home")
    if stream:
        tap = finger([(100, 120)] * 5, t0=500.0)
        fake.stream = ScriptedStream(PANELS, NATURAL, [(0.6 + i * 0.001, line) for i, line in enumerate(tap)])
    fake.timeline = list(timeline)
    session = Session(list_devices=lambda: [PHONE], connect=lambda d: fake, screenshots_dir=tmp_path / "shots",
                      settle_s=0.2, poll_s=0.01, first_look_s=0.0)
    return session, fake


def _call(server, name, args):
    from test_mobilepilot import _call as call, _text

    return _text(call(server, name, **args))


def test_record_start_status_stop_through_the_mcp_server(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILEPILOT_RECORD_DIR", str(tmp_path / "record"))
    session, fake = _rec_session(tmp_path, timeline=[(0.7, "network")])
    server = build_server(session)
    started = _call(server, "mobile_record", {"action": "start"})
    assert "Recording on emulator-5554" in started
    refused = _call(server, "mobile_tap", {"x": 10, "y": 10})
    assert "recording" in refused.lower()
    again = _call(server, "mobile_record", {"action": "start"})
    assert "already" in again.lower()
    time.sleep(2.0)
    status = _call(server, "mobile_record", {"action": "status"})
    assert "1 step" in status
    done = _call(server, "mobile_record", {"action": "stop"})
    assert "Recorded 1 step" in done and "Network & internet" in done
    folder = next((tmp_path / "record").iterdir())
    wf = json.loads((folder / "workflow.json").read_text())
    assert wf["steps"][0]["target"]["name"].startswith("Network & internet")
    assert wf["steps"][0]["result"] == "new screen: network"  # this fake screen has no title text: the window name
    # the session works again afterwards
    assert "NO_DEVICE" not in _call(server, "mobile_snapshot", {})


def test_record_on_a_device_without_touch_events_says_why(tmp_path, monkeypatch):
    monkeypatch.setenv("MOBILEPILOT_RECORD_DIR", str(tmp_path / "record"))
    session, _ = _rec_session(tmp_path, stream=False)
    out = _call(build_server(session), "mobile_record", {"action": "start"})
    assert "NOT_SUPPORTED" in out


def test_stop_or_status_without_a_recording(tmp_path):
    session, _ = _rec_session(tmp_path)
    server = build_server(session)
    assert "No recording" in _call(server, "mobile_record", {"action": "stop"})
    assert "No recording" in _call(server, "mobile_record", {"action": "status"})


def test_a_long_gesture_keeps_the_screen_it_started_on(tmp_path):
    # many readings arrive while the finger is down (a slow drag): the one before it went down still counts
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.rec.touch_start(2.0, 100, 120)
    for i in range(80):
        r.frame(NETWORK, 2.1 + i * 0.1, title="Network")
    r.now = 10.5
    pts = [(2.0 + i * 0.1, 100, 120 + max(i - 6, 0) * 3) for i in range(85)]  # held 0.6 s, then moved
    r.rec.touch(Touch(DEV, 2.0, 10.5, [pts]))
    step = r.stop()["steps"][0]
    assert step["action"] == "drag" and step["target"]["name"].startswith("Network & internet")


def test_an_edge_swipe_the_phone_ignored_is_not_called_back_for_certain(tmp_path):
    r = Rig(tmp_path)
    r.settle(NETWORK, 1.0, title="Network")
    r.swipe((3, 300), (150, 305), 2.0)
    r.settle(NETWORK, 3.0, title="Network")  # nothing went back
    step = r.stop()["steps"][0]
    assert step["action"] == "back" and step["sure"] is False and "did not change" in step["note"]


def test_a_back_that_worked_stays_sure(tmp_path):
    r = Rig(tmp_path)
    r.settle(NETWORK, 1.0, title="Network")
    r.swipe((3, 300), (150, 305), 2.0)
    r.settle(HOME, 3.0, title="Settings")
    assert r.stop()["steps"][0]["sure"] is True


def test_a_step_is_not_judged_before_its_screen_could_start_moving(tmp_path):
    r = Rig(tmp_path)
    r.settle(HOME, 1.0)
    r.tap(100, 120, 2.0)
    r.settle(HOME, 2.2)  # 120 ms later the app has not reacted yet: not "no change"
    r.settle(NETWORK, 2.8, title="Network")
    assert r.stop()["steps"][0]["result"] == "new screen: Network"


class _EndingStream(ScriptedStream):
    """The phone unplugged: the event stream ends."""

    def lines(self):
        yield from ((time.monotonic(), line) for _, line in self.script)


def test_an_unplugged_phone_ends_the_recording_and_keeps_its_steps(tmp_path):
    from mobilepilot.record import Session as Recording

    fake = FakeEngine({"home": HOME}, "home")
    fake.stream = _EndingStream(PANELS, NATURAL, [(0, line) for line in finger([(100, 120)] * 5, t0=500.0)])
    rec = Recording(fake, tmp_path / "rec", device="emu")
    time.sleep(0.05)  # the screen is read once before the touch arrives
    rec.start()
    deadline = time.monotonic() + 5
    while rec.running and time.monotonic() < deadline:
        time.sleep(0.05)
    wf = rec.stop()
    assert not rec.running and any("touch stream ended" in n for n in wf["notes"])
    assert json.loads((tmp_path / "rec" / "workflow.json").read_text())["notes"] == wf["notes"]


def test_a_screen_that_cannot_be_read_stops_the_recording_with_the_reason(tmp_path):
    from mobilepilot.record import Session as Recording

    fake = FakeEngine({"home": HOME}, "home")
    fake.stream = ScriptedStream(PANELS, NATURAL, [])

    def broken():
        raise RuntimeError("shell output invalid")

    fake.record_state = broken
    rec = Recording(fake, tmp_path / "rec", device="emu")
    rec.start()
    deadline = time.monotonic() + 5
    while rec.running and time.monotonic() < deadline:
        time.sleep(0.05)
    wf = rec.stop()
    assert any("failed 5 times" in n and "shell output invalid" in n for n in wf["notes"])


def test_a_failed_screenshot_keeps_the_step(tmp_path):
    r = Rig(tmp_path)

    def broken(path):
        raise OSError("FLAG_SECURE: black frame")

    r.rec.shoot = broken
    r.settle(HOME, 1.0)
    r.tap(100, 120, 2.0)
    step = r.stop()["steps"][0]
    assert step["screenshot"] is None and "FLAG_SECURE" in step["screenshot_note"]
    assert step["target"]["name"].startswith("Network & internet")


def test_a_key_that_changed_nothing_after_typing_says_enter_may_have_been_pressed(tmp_path):
    # Settings search keeps the keyboard open on Enter (Pixel 7a): 8 touches, 7 letters
    r = Rig(tmp_path)
    r.settle(field_screen(""), 1.0, keyboard=KEYBOARD)
    r.tap(30, 450, 2.0)
    r.settle(field_screen("d"), 2.3, keyboard=KEYBOARD)
    r.tap(300, 620, 3.0)  # Enter: the text stays "d"
    r.settle(field_screen("d"), 3.5, keyboard=KEYBOARD)
    r.swipe((160, 380), (160, 150), 5.0)
    typed = r.stop()["steps"][0]
    assert typed["maybe_submitted"] and "1 keyboard touch after" in typed["submit_note"]
    assert "submitted" not in typed


def test_list_text_is_not_the_screen_title_and_the_fallback_is_readable():
    from mobilepilot.record import screen_title

    results = screen(node("androidx.recyclerview.widget.RecyclerView", "[0,80][320,640]", children=(
        node("TextView", "[16,90][300,140]", text="Developer options"))))
    tree_ = parse_hierarchy(results, package=PKG, activity=".modules.search.SearchActivity", size=NATURAL)
    assert screen_title(tree_) == "SearchActivity"


def test_effect_lines_keep_unicode_names(tmp_path):
    wifi = HOME.replace("Wi-Fi", "Use Wi\u2011Fi")
    r = Rig(tmp_path)
    r.settle(wifi, 1.0)
    r.tap(280, 280, 2.0)
    r.settle(wifi.replace('checked="false"', 'checked="true"'), 2.8)
    assert r.stop()["steps"][0]["result"] == '"Use Wi\u2011Fi" switch: off → on'


# --------------------------------------------------------------------------- found on Amazon, Pixel 7a, 2026-11-16
def _busy(n, *, button_y=200):
    """A screen whose banner changes every reading (a carousel) while the button stays put."""
    return screen(node("TextView", "[0,0][320,60]", text=f"Deal {n}")
                  + node("Button", f"[16,{button_y}][304,{button_y + 48}]", text="Mobiles", desc="Mobiles", clickable=True))


def test_a_busy_screen_does_not_make_a_still_target_unsure(tmp_path):
    r = Rig(tmp_path)
    for i in range(4):
        r.frame(_busy(i), 1.0 + i * 0.2)
    r.tap(100, 220, 2.0)
    step = r.stop()["steps"][0]
    assert step["sure"] is True and step["target"]["name"] == "Mobiles"


def test_a_target_that_moved_between_readings_is_still_unsure(tmp_path):
    r = Rig(tmp_path)
    r.frame(_busy(0, button_y=200), 1.0)
    r.frame(_busy(1, button_y=210), 1.2)   # the list is still scrolling
    r.tap(100, 230, 2.0)
    assert r.stop()["steps"][0]["sure"] is False


def test_a_tap_in_web_content_says_it_is_a_position(tmp_path):
    web = screen(node("android.webkit.WebView", "[0,0][320,600]", clickable=True))
    r = Rig(tmp_path)
    r.settle(web, 1.0)
    r.tap(160, 300, 2.0)
    wf = r.stop()
    step = wf["steps"][0]
    assert step["in_web"] and "WEBVIEW context" in step["note"]
    assert "web content at 50%,47%" in format_table(wf, r.rec.folder)


def test_tapping_an_app_icon_records_which_app_it_launches(tmp_path):
    home = screen(node("TextView", "[20,300][100,380]", text="Shop", desc="Shop", clickable=True,
                       pkg="com.google.android.apps.nexuslauncher"), pkg="com.google.android.apps.nexuslauncher")
    r = Rig(tmp_path)
    r.settle(home, 1.0, pkg="com.google.android.apps.nexuslauncher")
    r.tap(60, 340, 2.0)
    r.settle(HOME, 3.0, pkg="com.example.shop")
    wf = r.stop()
    assert wf["steps"][0]["launches"] == "com.example.shop"
    assert "launches com.example.shop" in format_table(wf, r.rec.folder)


def _tabs():
    def tab(x, label, desc):
        return node("androidx.appcompat.app.ActionBar$Tab", f"[{x},560][{x + 64},640]", clickable=True, children=(
            node("FrameLayout", f"[{x},560][{x + 64},640]", desc=desc)
            + node("TextView", f"[{x},600][{x + 64},630]", text=f" {label} ", rid=f"{PKG}:id/bottom_tab_label")))
    return screen(tab(0, "Home", "Home Tab 1 of 5") + tab(64, "Cart", "Cart 1 item Tab 4 of 5"))


def test_a_tab_with_nothing_of_its_own_borrows_a_stable_locator_from_inside():
    from mobilepilot.perception.locators import best, candidates
    from mobilepilot.perception.snapshot import build_snapshot

    t = tree(_tabs())
    snap = build_snapshot(t, snapshot_id="s_t", mode="interactive", max_elements=1000)
    cart = next(el for el in snap.all_elements if el.interactive and el.frame and el.frame.x == 64)
    found = candidates(cart, t, "android")
    pick = best(found)
    assert pick.value == 'new UiSelector().descriptionStartsWith("Cart")' and pick.tier == "strong"
    exact = next(c for c in found if c.value == "Cart 1 item Tab 4 of 5")
    assert exact.tier == "acceptable" and "count" in exact.note   # breaks when the cart count changes
    label = next(c for c in found if "bottom_tab_label" in c.value)
    assert label.value.endswith('.textContains("Cart")')          # the thin spaces would defeat .text()


def test_a_plain_description_stays_strong():
    from mobilepilot.perception.locators import candidates
    from mobilepilot.perception.snapshot import build_snapshot

    t = tree(_busy(0))
    snap = build_snapshot(t, snapshot_id="s_b", mode="interactive", max_elements=1000)
    btn = next(el for el in snap.all_elements if el.name == "Mobiles")
    assert [(c.strategy, c.tier) for c in candidates(btn, t, "android")][0] == ("accessibility id", "strong")


# --------------------------------------------------------------------------- locators from a live Amazon session
# Shapes copied from Pixel 7a dumps (2026-11-17): only the attributes the locator rules read.
def _el_at(t, x, y):
    from mobilepilot.perception.snapshot import build_snapshot

    snap = build_snapshot(t, snapshot_id="s_amz", mode="interactive", max_elements=1000)
    hits = [el for el in snap.all_elements if el.interactive and el.frame
            and el.frame.x <= x < el.frame.x + el.frame.w and el.frame.y <= y < el.frame.y + el.frame.h]
    return min(hits, key=lambda el: el.frame.area)


def _tiles():
    def tile(x, label):
        return node("LinearLayout", f"[{x},20][{x + 60},80]", rid=f"{PKG}:id/tiles_nav_item_holder", clickable=True,
                    children=node("ImageView", f"[{x + 10},22][{x + 50},60]", rid=f"{PKG}:id/tiles_nav_item_image")
                    + node("TextView", f"[{x + 15},60][{x + 45},78]", text=label, rid=f"{PKG}:id/tiles_nav_item_text"))
    return screen(tile(0, "Pay") + tile(70, "Now") + tile(140, "Fresh"))


def test_a_tile_whose_own_id_is_shared_borrows_its_label():
    """Amazon's "Now" tile: a clickable holder whose resource-id every tile shares, and a label
    inside it. The recording said "no unique locator" because the holder HAD an id."""
    from mobilepilot.perception.locators import best, candidates

    t = tree(_tiles())
    found = candidates(_el_at(t, 100, 40), t, "android")
    pick = best(found)
    assert pick is not None
    assert pick.value == f'new UiSelector().resourceId("{PKG}:id/tiles_nav_item_text").text("Now")'


_LINK = ("category?qcbrand=qqfsWw9RkO&amp;nodeId={node}&amp;ref=IN_Tez_{ref}"
         "&amp;pf_rd_r={token}&amp;pf_rd_p=66137edd-fd72-4dd8-947c-f5e7dcb5b632")


def _links(token):
    return screen(node("android.webkit.WebView", "[0,0][320,640]", children=node("View", "[0,100][320,160]", children=(
        node("View", "[0,100][100,160]", clickable=True, desc=_LINK.format(node="204924538031", ref="TopBev", token=token))
        + node("View", "[110,100][210,160]", clickable=True,
               desc=_LINK.format(node="215540425031", ref="Topchips", token=token))))))


def test_a_link_whose_description_carries_a_visit_token_is_found_by_its_stable_part():
    """The web link's content-desc is its href, and pf_rd_r changed between the recording and the
    next visit (F1H1X16GNQH0CAQZY5CK -> BX2TWQ74MADG41PYWZVX): the exact id never matches again."""
    from mobilepilot.perception.locators import best, candidates

    t = tree(_links("F1H1X16GNQH0CAQZY5CK"))
    found = candidates(_el_at(t, 50, 130), t, "android")
    pick = best(found)
    assert pick.value == 'new UiSelector().descriptionContains("nodeId=204924538031")', pick
    exact = next(c for c in found if c.strategy == "accessibility id")
    assert exact.tier == "fragile" and "every visit" in exact.note
    later = tree(_links("BX2TWQ74MADG41PYWZVX"))
    assert sum(1 for n in later.root.walk() if n.a11y_id and "nodeId=204924538031" in n.a11y_id) == 1


def test_a_web_card_whose_text_repeats_its_name_is_found_by_the_name():
    """A WebView product card is one Button whose text is the image alt, the title and "Add":
    the exact text breaks as soon as "Add" becomes a quantity."""
    from mobilepilot.perception.locators import best, candidates

    def card(x, name):
        return node("Button", f"[{x},100][{x + 150},400]", clickable=True, text=f"{name} {name} Add")
    t = tree(screen(node("android.webkit.WebView", "[0,0][320,640]",
                         children=card(0, "Bisleri Mineral Water Jar") + card(160, "Clear Premium Drinking Water"))))
    pick = best(candidates(_el_at(t, 50, 200), t, "android"))
    assert pick.value == 'new UiSelector().textStartsWith("Bisleri Mineral Water Jar")', pick


def test_a_launcher_step_tells_the_test_to_start_the_app_by_package(tmp_path):
    home = screen(node("TextView", "[20,300][100,380]", text="Shop", desc="Shop", clickable=True,
                       pkg="com.google.android.apps.nexuslauncher"), pkg="com.google.android.apps.nexuslauncher")
    r = Rig(tmp_path)
    r.settle(home, 1.0, pkg="com.google.android.apps.nexuslauncher")
    r.tap(60, 340, 2.0)
    r.settle(HOME, 3.0, pkg="com.example.shop")
    wf = r.stop()
    loc = wf["steps"][0]["target"]["locator"]
    assert loc == {"strategy": "app", "value": "com.example.shop", "tier": "strong", "unique": True,
                   "note": "start it by package (Appium activate_app); the icon's place on the launcher is not part of the app"}


# --------------------------------------------------------------------------- privacy: fail closed
def _written(r):
    return (r.rec.folder / "steps.jsonl").read_text() + (r.rec.folder / "workflow.json").read_text()


def test_a_keyboard_that_is_up_where_android_does_not_say_still_hides_its_keys(tmp_path):
    """dumpsys said the keyboard was visible but gave no area: its touches must not be stored."""
    r = Rig(tmp_path)
    r.settle(field_screen(""), 1.0, keyboard_unknown=True)
    for i, (x, y) in enumerate([(30, 450), (90, 520), (200, 600)]):
        r.tap(x, y, 2.0 + i * 0.2)
    r.settle(field_screen("Office-5G"), 3.0, keyboard_unknown=True)
    r.tap(160, 224, 5.0)  # Save, in the top half
    wf = r.stop()
    assert [s["action"] for s in wf["steps"]] == ["type", "tap"]
    for x, y in [(30, 450), (90, 520), (200, 600)]:
        assert f"[{x}, {y}]" not in _written(r)


def test_ime_visible_without_an_area_is_reported_as_unknown():
    from mobilepilot.engine.android import ime_frame, ime_visible

    out = "mGivenContentInsets=[0,0][0,0]\n frame=[0,24][320,640]\nisVisible=true\n"
    assert ime_frame(out) is None and ime_visible(out)
    assert not ime_visible(out.replace("isVisible=true", "isVisible=false"))


def test_a_touch_before_the_first_screen_reading_keeps_no_position(tmp_path):
    r = Rig(tmp_path)
    r.tap(90, 520, 0.5)
    r.settle(HOME, 1.0)
    step = r.stop()["steps"][0]
    assert step["redacted"] and "at" not in step and "at_pct" not in step
    assert "[90, 520]" not in _written(r)


def _pw_field(value, *, password, rid="name", focused=True):
    return screen(node("EditText", "[16,100][304,148]", text=value, rid=f"{PKG}:id/{rid}", clickable=True,
                       focused=focused, password=password)
                  + node("Button", "[16,200][304,248]", text="Save", clickable=True))


def test_a_field_once_seen_as_a_password_stays_secret(tmp_path):
    """Compose's show-password toggle, or a reading where the flag is missing: the plain value of
    a field that was a password field must never be taken as typed data."""
    r = Rig(tmp_path)
    r.settle(_pw_field("", password=True), 1.0, keyboard=KEYBOARD)
    r.tap(30, 450, 2.0)
    r.settle(_pw_field("hunter2", password=False), 2.5, keyboard=KEYBOARD)
    r.tap(160, 224, 4.0)
    wf = r.stop()
    typed = wf["steps"][0]
    assert typed["data"] == SECRET and typed["secret"] and typed["screenshot"] is None
    assert "hunter2" not in _written(r)


def test_a_field_named_for_a_pin_is_secret_without_the_password_flag(tmp_path):
    r = Rig(tmp_path)
    r.settle(_pw_field("", password=False, rid="pin_input"), 1.0, keyboard=KEYBOARD)
    r.settle(_pw_field("4821", password=False, rid="pin_input"), 2.0, keyboard=KEYBOARD)
    r.tap(160, 224, 4.0)
    wf = r.stop()
    assert wf["steps"][0]["data"] == SECRET and "4821" not in _written(r)


def _pad(extra="", digits="1234567890"):
    keys = "".join(node("Button", f"[{(i % 3) * 100},{300 + (i // 3) * 60}][{(i % 3) * 100 + 100},{360 + (i // 3) * 60}]",
                        text=d, clickable=True) for i, d in enumerate(digits))
    return screen(keys + extra)


def test_a_digit_pad_with_no_password_field_is_still_a_pin_pad(tmp_path):
    r = Rig(tmp_path)
    r.settle(_pad(), 1.0)
    r.tap(50, 330, 2.0)   # "1"
    step = r.stop()["steps"][0]
    assert step["redacted"] and "target" not in step and "at" not in step
    assert step["screenshot"] is None


def test_a_screen_asking_for_a_pin_redacts_short_keys(tmp_path):
    r = Rig(tmp_path)
    r.settle(screen(node("TextView", "[0,40][320,80]", text="Enter your PIN")
                    + node("Button", "[0,300][100,360]", text="7", clickable=True)), 1.0)
    r.tap(50, 330, 2.0)
    assert r.stop()["steps"][0]["redacted"]


def test_a_calculator_keypad_is_not_a_pin_pad(tmp_path):
    ops = "".join(node("Button", f"[300,{300 + i * 60}][320,{360 + i * 60}]", text=o, clickable=True)
                  for i, o in enumerate("+=×"))
    r = Rig(tmp_path)
    r.settle(_pad(ops), 1.0)
    r.tap(50, 330, 2.0)
    step = r.stop()["steps"][0]
    assert not step.get("redacted") and step["target"]["name"] == "1"


def test_a_prefilled_pin_field_that_nobody_typed_in_is_not_a_step(tmp_path):
    r = Rig(tmp_path)
    r.settle(_pw_field("4821", password=False, rid="pin_input"), 1.0)
    r.settle(_pw_field("4821", password=False, rid="pin_input"), 2.0)
    r.tap(160, 224, 4.0)
    wf = r.stop()
    assert [s["action"] for s in wf["steps"]] == ["tap"] and "4821" not in _written(r)


def test_a_calculator_whose_keys_are_named_in_words_is_not_a_pin_pad(tmp_path):
    """Google Calculator's keys are content-desc only, operators as words (Pixel 7a, 2026-11-17)."""
    keys = "".join(node("Button", f"[{(i % 3) * 100},{300 + (i // 3) * 60}][{(i % 3) * 100 + 100},{360 + (i // 3) * 60}]",
                        desc=d, clickable=True) for i, d in enumerate("1234567890"))
    ops = "".join(node("Button", f"[300,{300 + i * 60}][320,{360 + i * 60}]", desc=o, clickable=True)
                  for i, o in enumerate(["plus", "equals", "multiply"]))
    r = Rig(tmp_path)
    r.settle(screen(keys + ops), 1.0)
    r.tap(50, 330, 2.0)
    step = r.stop()["steps"][0]
    assert not step.get("redacted") and step["target"]["name"] == "1"

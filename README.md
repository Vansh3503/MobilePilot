# MobilePilot

An MCP server that drives Android emulators, Android phones (USB or wireless adb) and the iOS
Simulator through the accessibility tree. It is built on AXPilot's perception layer (compact ref
trees, refs that stay valid across snapshots, a diff after every action), copied in rather than
imported because AXPilot depends on PyObjC.

## Why

`appium-mcp` crawls take 6–7 model calls per screen and return ~15k-character page sources.
With MobilePilot a screen costs a snapshot, then one call per action:

```
app com.android.settings (pid 7433) · screen ".Settings" [0,0 320×640] · snapshot s_s4pk
e1  scrollarea                                 scrollable  id=settings_homepage_container
e2    button     "Profile picture, double tap to open Google Account"  aid  id=account_avatar  (272,104)
e5      row      "Network & internet — Mobile, Wi‑Fi, hotspot"  (160,313)
…
```

`aid` is the accessibility id and `id=` the resource-id, inline, so no separate locator call is
needed. Each action waits for the screen to settle and returns only what changed:

```
tapped e24 switch  checked  id=switch_widget  (285,315)
Changes since s_pgbs (now s_d6cl): 4 added, 5 removed, 1 changed
~ e24 switch unchecked
screenshot …/step-01-tap.png (320×640)
```

Measured on the API 34 emulator (`tests/test_live.py`): snapshot 271 ms, tap with diff 1.1–1.6 s,
every result under 1,000 characters. Tree dumps go through uiautomator2 (51 ms) rather than
`adb uiautomator dump` (2,216 ms); see `spikes/android_spike.out`.

## Tools

`mobile_devices`, `mobile_app` (launch, restart, stop, clear, install, grant, current, open a deep
link), `mobile_snapshot`, `mobile_tap` (long press, double tap), `mobile_type`, `mobile_swipe`,
`mobile_drag`, `mobile_pinch`, `mobile_rotate`, `mobile_key` (including the notification shade),
`mobile_find` (scrolls on the server), `mobile_wait_for`, `mobile_screenshot` (annotated with refs
on request), `mobile_locate` (graded Appium locators for the refs you used).

A tap that opens a menu, drop-down, dialog or sheet says so and lists every option with its ref:

```
tapped e3 button "More options"  aid  id=overflow_action_button  (1017,201)
- 14 elements of the screen behind it (they return when it closes)
opened: popup menu (5 items): pick one by ref, or mobile_key back to close it
  e15 row "Screensaver"  (792,354)
  …
```

Permission and system dialogs are reported, never answered. Unlabeled icons show where they are
(`button unlabeled, top-left  id=up`); `mobile_screenshot(annotate=true)` draws the refs on the
image, and taps and drags accept that image's pixels (`coordinate_space="screenshot"`) when no ref
exists. A phone that locked itself is woken; a PIN is left to the person (`NEEDS_USER`). If the person
uses the phone between calls, the next action works from the screen as it really is.

## Run

```
pip install -e .
mobilepilot doctor          # adb, Xcode, idb, devices, and what to fix
mobilepilot stdio           # the MCP server
mobilepilot call mobile_snapshot
```

`MOBILEPILOT_SCREENSHOTS_DIR` sets where evidence screenshots go.

## Devices

- **Android emulator or phone.** Needs `adb` (on PATH, `$ANDROID_HOME/platform-tools`, Android
  Studio's or Homebrew's SDK). On a phone, turn on Developer options → USB debugging and accept
  the prompt; `doctor` explains `unauthorized` and brand-specific switches (MIUI, ColorOS).
- **iOS Simulator.** Needs the full Xcode and idb. Not yet verified on a live simulator.
- **Real iPhones** are not supported yet (WebDriverAgent signing); use appium-mcp for those.

## Tests

```
pytest -q                                            # offline: recorded dumps + the real MCP server
MOBILEPILOT_LIVE=1 pytest -q tests/test_live.py -s   # a running emulator or phone
```

## Licence

Apache-2.0. `src/mobilepilot/perception/` is copied from [AXPilot](https://github.com/Vansh3503/Mac-Mcp) (Apache-2.0, same author) with
mobile changes marked in place.

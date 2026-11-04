# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

- If the person used the phone between calls (tapped, scrolled, switched apps), the next action
  notices (one 0.16 s fingerprint on a Pixel 7a) and works from the real screen: a ref still on
  it is used where it is now; a ref that is gone is refused with the current screen and the way
  back, and nothing is tapped.
- A tap that opens a menu, drop-down, dialog, bottom sheet or system prompt says `opened: <kind>`
  and lists all its options; closing it is one line. Android 17 dumps only the focused window, so
  a lone window smaller than the screen counts as an overlay.
- Elements returning unchanged (closing a menu, going back) are one line of refs, not a line each:
  back after a menu went from 1,159 to 150 characters on a Pixel 7a.
- Permission and system dialogs are never answered; the result says to ask the person.
- Unlabeled buttons show their position; `mobile_screenshot(annotate=true)` draws refs on the
  image, returns an id and scale, and `mobile_tap`/`mobile_drag` take its pixels.
- Empty `slider`/`seek` frames (Android 17 volume sliders) show as sliders.
- Per-action settle budgets (launch 10 s, type 1.5 s, swipe 2 s, rotate 4 s, tap 3 s); the settle
  check ignores ticking digits in text; every call stays under 55 s.
- A keyboard covering the target is hidden before the tap.
- A screen that locked itself is woken; a PIN raises `NEEDS_USER` (new code) and is never typed.
- `mobile_app open` opens a deep link or web address.
- Pinch verified on Google Maps: it worked all along; the earlier "no effect" was a place card
  covering the map.

## [0.1.0] - 2026-11-04

First public version.

- MCP server with `mobile_devices`, `mobile_app`, `mobile_snapshot`, `mobile_tap` (with `double`),
  `mobile_type`, `mobile_swipe` (distance, duration), `mobile_drag`, `mobile_pinch`,
  `mobile_rotate`, `mobile_key` (including the notification shade and quick settings),
  `mobile_find`, `mobile_wait_for`, `mobile_screenshot` and `mobile_locate`.
- Android engine on uiautomator2 and adb; iOS Simulator engine on idb (not yet verified live).
- Typing sets the focused field's text through accessibility instead of installing a keyboard app.
- Verified on the API 34 emulator and a Pixel 7a on Android 17.

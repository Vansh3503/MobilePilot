# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

Found by driving Calculator, Clock, Chrome, YouTube, Maps, Gmail, Messages, Contacts, Photos,
Play Store, Amazon and a game on a Pixel 7a (Android 17):

- Rotation was reported as "no visible change": a diff compares names and states, not positions,
  and the capture ran before the rotation finished. Now it waits for the new orientation and
  says `screen is now 2400×1080 (landscape)`.
- Jetpack Compose apps (Play Store) put a row's text in the content-desc of plain Views, so its
  buttons read "unlabeled"; they are now named from it ("ClevCalc - Calculator Cleveni Inc. …").
- Full-screen BottomSheetDialogs (Amazon's location sheet) were missed: their window fills the
  screen behind a `touch_outside` backdrop. They are `opened: bottom sheet`, and the backdrop is
  "Outside the sheet (closes it)". Material time/date pickers are dialogs; the notification
  shade is `notification shade`, not a question for the person.
- Gestures on maps, canvases and games no longer claim "did nothing" when the picture changed
  (a coarse pixel fingerprint, taken only for drag/pinch/swipe). A web view with no content in
  the tree (Chrome drops it after a scroll) is said so, in diffs and in `mobile_find`.
- `mobile_type` on a screen with no text field is refused instead of claiming it typed.
- Long removed lists collapse to a count and three names; a loading indicator is mentioned;
  `mobile_find` behind a dialog names the dialog; the no-tree hint is only for nameless screens.
- "Someone may have used the phone" is said only when the target moved or the app changed
  (Maps updates itself); drag sends one move per 50 ms (a 0.8 s drag took 6.2 s, now ~3.5 s).
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

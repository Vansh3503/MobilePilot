# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

### Security

- The recorder fails closed where it used to fail open:
  - When Android says the keyboard is up but not where, the lower half of the screen is treated as keys, and those touches are not stored. A touch before the first screen reading keeps no position.
  - A field stays secret for the rest of the recording once it has reported itself as a password field, or once its id, hint or label names a password, PIN, OTP or CVV. A later reading without the password flag is never taken as typed data. Changes to such a field are noticed through a hash, not stored.
  - A PIN pad is recognised without a focused password field: a 0-9 grid with no calculator keys, or a screen that asks for a PIN or passcode. Its keys, and taps on nothing there, are redacted, and no screenshot is taken right after. A calculator, including Google Calculator's word-named keys, still records.

### Changed

- Found replaying a recorded Amazon flow on a Pixel 7a (2026-11-17):
  - A launcher tap's locator is now the package it opened (strategy `app`: start it with `activate_app`). The icon was on another launcher page, so the replay could not find it.
  - A tappable tile whose own resource-id every sibling shares now borrows the label inside it (`resourceId(...tiles_nav_item_text).text("Now")`). Before, it had no locator at all.
  - A web link whose content-desc is a URL with a per-visit value (`pf_rd_r=…` changed between visits) is graded fragile, and `descriptionContains("nodeId=…")` on the part that stays is offered.
  - A WebView card whose text repeats its name ("X X Add") is found by `textStartsWith(X)`, which survives "Add" becoming a quantity.
- Found recording Amazon by hand on a Pixel 7a:
  - A recorded step is sure when the element under the finger did not change between the two readings before the touch. The whole screen no longer has to hold still; carousels had made 19 of 25 steps unsure.
  - A tap inside an empty WebView is reported as web content at a position.
  - A launcher tap records the package it launched.
- Locators (`mobile_locate` and recordings):
  - An accessibility id holding a count, price or percentage is graded acceptable, with a `descriptionStartsWith` alternative.
  - A tappable container with nothing of its own borrows a description or label from inside it.
  - Text padded with invisible spaces is matched with `textContains`.

### Added

- `mobile_record` (start, status, stop): a person uses an Android phone or emulator by hand and
  every step is written down with its graded locators, a screenshot and what it changed. It reads
  the kernel's touch events (`adb shell getevent -lt`) and the screen tree in a loop, and matches
  each gesture to the screen as it was just before the finger went down. Taps, double taps, long
  presses, swipes (with the list they scrolled), drags, pinches, back/home/recents gestures,
  three-button navigation bars, rotation, hardware keys and typed text are recognised. A gesture
  made while the screen was still changing is marked unsure, never presented as fact; an edge
  swipe the phone did not take as back says so. Keyboard and lock-screen touches are never
  stored, passwords are written as `••••` and never photographed. Steps are appended to
  `steps.jsonl` as they happen; `workflow.json` holds every detail. Folder: `MOBILEPILOT_RECORD_DIR`.
  While recording, every other tool answers `RECORDING`. iOS answers `NOT_SUPPORTED` with why.

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

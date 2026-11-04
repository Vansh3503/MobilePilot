# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-11-04

First public version.

- MCP server with `mobile_devices`, `mobile_app`, `mobile_snapshot`, `mobile_tap` (with `double`),
  `mobile_type`, `mobile_swipe` (distance, duration), `mobile_drag`, `mobile_pinch`,
  `mobile_rotate`, `mobile_key` (including the notification shade and quick settings),
  `mobile_find`, `mobile_wait_for`, `mobile_screenshot` and `mobile_locate`.
- Android engine on uiautomator2 and adb; iOS Simulator engine on idb (not yet verified live).
- Typing sets the focused field's text through accessibility instead of installing a keyboard app.
- Verified on the API 34 emulator and a Pixel 7a on Android 17.

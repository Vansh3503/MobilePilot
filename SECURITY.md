# Security policy

MobilePilot lets an AI agent operate a real phone, emulator or simulator, so security reports
matter. MobilePilot is pre-release (0.x); only `main` receives fixes.

## Reporting a vulnerability

**Do not open a public issue or pull request for a security problem.** Report it privately:
[Report a vulnerability](https://github.com/Vansh3503/MobilePilot/security/advisories/new)
(repository → Security → Advisories). Include the version or commit, the device and OS version,
the MCP client, reproduction steps and the impact. Remove personal data and device serials.

## What MobilePilot is designed against

- **Text on the device is untrusted data.** Screen text, notifications and web pages can contain
  prompt-injection attempts. Tool results carry it as data; MobilePilot never acts on it itself.
- **System and permission dialogs are reported, never answered automatically.** Accepting one is
  the person's decision.
- **Device data.** `mobile_app clear` erases an app's data. Hosts such as Stuntman gate it behind a
  question to the person; other hosts should do the same.
- **No network access or telemetry.** MobilePilot talks only to `adb`/uiautomator2 on the
  connected device or to the local simulator tools.

Out of scope: an attacker who already controls the computer running MobilePilot or has `adb`
access to the device.

# Contributing to MobilePilot

Bug reports from real devices, gesture fixes and tests are the most useful contributions right now.
Security problems go through [SECURITY.md](SECURITY.md), never a public issue. Everyone taking part
follows the [Code of Conduct](CODE_OF_CONDUCT.md).

## Setup

```sh
git clone https://github.com/Vansh3503/MobilePilot.git
cd MobilePilot
python3 -m venv .venv && .venv/bin/pip install -e ".[test]"
.venv/bin/pytest -q
```

## Layout

- `src/mobilepilot/server.py`: the MCP tools.
- `src/mobilepilot/engine/`: `android.py` (uiautomator2 + adb), `ios_sim.py` (idb), `fake.py`
  (offline tests).
- `src/mobilepilot/perception/`: snapshots, refs, diffs and locators, copied from
  [AXPilot](https://github.com/Vansh3503/Mac-Mcp) with mobile changes marked in place.

## Tests

- Every change gets an offline test on the fake engine or a recorded dump in `tests/fixtures/`.
- Every gesture or engine change also gets a live check on a real device or emulator:
  `MOBILEPILOT_LIVE=1 pytest -q tests/test_live.py -s`. Say in the pull request which device and
  OS version you used.
- Put back anything a live check changes on the device (volume, rotation, Wi-Fi, settings).

## Commits

Conventional Commits titles (`feat(android): …`, `fix(perception): …`). Update `CHANGELOG.md`
under **Unreleased** for user-visible changes.

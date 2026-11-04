"""``mobilepilot stdio | doctor | call <tool> [json]``."""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys

import anyio

_BRAND_HINTS = {
    ("xiaomi", "redmi", "poco"): "Xiaomi/MIUI: Developer options → \"USB debugging (Security settings)\" must be on, "
                                 "or taps and typing are refused.",
    ("oppo", "vivo", "realme", "oneplus"): "Oppo/Vivo/Realme: Developer options → \"Disable permission monitoring\" "
                                           "if taps or typing are refused.",
}


def _line(ok: bool | None, text: str) -> None:
    print(f"{'ok ' if ok else '-- ' if ok is None else 'NO '} {text}")


def doctor() -> int:
    """Check Android and iOS tooling and every connected device, and say what to fix."""
    from mobilepilot.engine import android, ios_sim

    print(f"MobilePilot on Python {platform.python_version()}, {platform.system()} {platform.machine()}")
    problems = 0
    adb = android.find_adb()
    _line(adb is not None, f"adb: {adb}" if adb else
          "adb not found. Install Android platform-tools (Android Studio's SDK Manager, or "
          "`brew install --cask android-platform-tools`), or set ANDROID_HOME.")
    problems += adb is None
    try:
        import uiautomator2  # noqa: F401

        _line(True, "uiautomator2 is installed")
    except ImportError:
        _line(False, "uiautomator2 is missing: pip install uiautomator2")
        problems += 1
    devices = android.list_devices() if adb else []
    for d in devices:
        if d.state == "unauthorized":
            _line(False, f"{d.serial}: unauthorized. Unlock the phone and accept \"Allow USB debugging?\" "
                         "(tick \"Always allow from this computer\").")
            problems += 1
            continue
        if d.state != "device":
            _line(False, f"{d.serial}: {d.state}. Reconnect the cable (or `adb reconnect`).")
            problems += 1
            continue
        brand = subprocess.run([adb, "-s", d.serial, "shell", "getprop", "ro.product.manufacturer"],
                               capture_output=True, text=True, timeout=10).stdout.strip().lower()
        _line(True, f"{d.serial}: {d.kind} {d.name} ({brand or 'unknown maker'})")
        for brands, hint in _BRAND_HINTS.items():
            if brand in brands:
                _line(None, hint)
    if adb and not devices:
        _line(None, "no Android devices: start an emulator, or plug a phone in with Developer options → USB debugging on")
    simctl = shutil.which("xcrun") and subprocess.run(["xcrun", "simctl", "help"], capture_output=True).returncode == 0
    if sys.platform == "darwin":
        _line(bool(simctl), "Xcode simulators available" if simctl else
              "no simctl: iOS Simulator needs the full Xcode (Command Line Tools are not enough), then "
              "`sudo xcode-select -s /Applications/Xcode.app`")
        idb = shutil.which("idb")
        _line(bool(idb) if simctl else None, f"idb: {idb}" if idb else
              "idb not found: `brew install facebook/fb/idb-companion` and `pip install fb-idb` (iOS Simulator only)")
        for d in ios_sim.list_devices():
            _line(True, f"{d.serial}: {d.name} iOS {d.os_version} ({d.state})")
    print("Real iPhones are not supported yet (WebDriverAgent signing is phase 2); use appium-mcp for those.")
    return 1 if problems else 0


async def _call(name: str, args: dict) -> int:
    from mobilepilot.server import build_server

    server = build_server()
    try:  # mcp 2.x
        from mcp.client import Client

        async with Client(server) as client:
            result = await client.call_tool(name, args)
        failed = result.is_error
    except ImportError:  # mcp 1.x
        from mcp.shared.memory import create_connected_server_and_client_session

        async with create_connected_server_and_client_session(server._mcp_server) as client:
            result = await client.call_tool(name, args)
        failed = result.isError
    for block in result.content:
        print(getattr(block, "text", f"<{block.type}>"))
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mobilepilot", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stdio", help="run the MCP server on stdin/stdout")
    sub.add_parser("doctor", help="check adb, Xcode, idb and connected devices")
    call = sub.add_parser("call", help="call one tool and print the result (for trying things out)")
    call.add_argument("tool")
    call.add_argument("args", nargs="?", default="{}", help="JSON object of arguments")
    ns = parser.parse_args(argv)
    if ns.cmd == "stdio":
        from mobilepilot.server import build_server

        build_server().run("stdio")
        return 0
    if ns.cmd == "doctor":
        return doctor()
    return anyio.run(_call, ns.tool, json.loads(ns.args))


if __name__ == "__main__":
    sys.exit(main())

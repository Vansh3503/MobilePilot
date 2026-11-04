"""Live smoke on a real emulator or phone (Android Settings). Skipped unless MOBILEPILOT_LIVE=1.

    MOBILEPILOT_LIVE=1 pytest -q tests/test_live.py -s

Drives Settings → Network & internet → Internet → toggles Wi-Fi off and on → back, and checks
what each step returns and how long it takes. Leaves Wi-Fi as it found it.
"""

from __future__ import annotations

import os
import re
import time

import anyio
import pytest
from mcp.client import Client

from mobilepilot.server import Session, build_server

pytestmark = pytest.mark.skipif(os.environ.get("MOBILEPILOT_LIVE") != "1", reason="set MOBILEPILOT_LIVE=1")


def _ref(text: str, pattern: str) -> str:
    match = re.search(rf"^[+~]?\s*(e\d+)\s+.*{pattern}", text, re.M)
    assert match, f"no element matching {pattern!r} in:\n{text}"
    return match.group(1)


def test_settings_wifi_flow(tmp_path):
    timings: list[tuple[str, float, int]] = []

    async def go():
        async with Client(build_server(Session(screenshots_dir=tmp_path))) as client:
            async def call(name, **args):
                start = time.perf_counter()
                result = await client.call_tool(name, args)
                text = "\n".join(b.text for b in result.content if hasattr(b, "text"))
                timings.append((name, (time.perf_counter() - start) * 1000, len(text)))
                assert not result.is_error, text
                return text

            home = await call("mobile_app", action="restart", app="com.android.settings")
            network = await call("mobile_tap", ref=_ref(home, "Network (&|and) internet"))
            internet = await call("mobile_tap", ref=_ref(network, '"Internet'))
            if not re.search(r"^[+~]?\s*e\d+\s+switch", internet, re.M):
                internet = await call("mobile_snapshot")  # a phone fills this screen in stages
            switch = _ref(internet, "switch")
            first = "checked" in re.search(rf"\b{switch}\s+switch.*", internet).group(0)
            off = await call("mobile_tap", ref=switch, screenshot=True)
            assert f"~ {switch} switch {'unchecked' if first else 'checked'}" in off
            on = await call("mobile_tap", ref=switch)
            assert f"~ {switch} switch {'checked' if first else 'unchecked'}" in on
            await call("mobile_key", key="back")
            found = await call("mobile_find", text="plane mode")
            located = await call("mobile_locate", refs=[switch, _ref(found, "(Air|Aero)plane mode")])
            assert re.search(r"switch_?[wW]idget", located), located
            snap = await call("mobile_snapshot")
            assert len(snap) < 3000

    anyio.run(go)
    assert list(tmp_path.glob("step-*.png"))
    for name, ms, chars in timings:
        print(f"{name:18s} {ms:7.0f} ms {chars:6d} chars")
    snapshot_ms = timings[-1][1]
    # 271 ms on the 320x640 emulator; 383-647 ms on a USB-2 Pixel 7a (1080x2400, 2026-11-04).
    assert snapshot_ms < 1000, f"a snapshot took {snapshot_ms:.0f} ms"

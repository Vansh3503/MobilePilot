"""One session through the real emulator: restart Settings → Network & internet → Internet →
toggle Wi-Fi (evidence) → toggle back → back → locate. Prints each result with its time."""
import re, time, anyio
from mcp.client import Client
from mobilepilot.server import Session, build_server

async def main():
    async with Client(build_server(Session(screenshots_dir="/tmp/mp-evidence"))) as c:
        async def call(name, **args):
            s = time.perf_counter(); r = await c.call_tool(name, args); ms = (time.perf_counter() - s) * 1000
            text = "\n".join(getattr(b, "text", "<img>") for b in r.content)
            print(f"\n=== {name} {args}  {ms:.0f} ms  {len(text)} chars{'  ERROR' if r.is_error else ''}\n{text}")
            return text
        ref = lambda text, pat: re.search(rf"^[+~]?\s*(e\d+)\s+.*{pat}", text, re.M).group(1)
        t = await call("mobile_app", action="restart", app="com.android.settings")
        t = await call("mobile_tap", ref=ref(t, "Network & internet"))
        t = await call("mobile_tap", ref=ref(t, '"Internet'))
        sw = ref(t, "switch")
        await call("mobile_tap", ref=sw, screenshot=True)
        await call("mobile_tap", ref=sw)
        t = await call("mobile_key", key="back")
        await call("mobile_find", text="Data Saver")
        await call("mobile_locate", refs=[sw, ref(t, "Internet")])
anyio.run(main)

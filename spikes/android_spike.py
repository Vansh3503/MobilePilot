"""Phase 0 spike: time a tree dump, a tap and a screenshot per Android engine. Prints timings."""
import os, statistics, subprocess, sys, time
ADB = os.environ.get("ADB", "adb")
def t(fn, n=5):
    out = []
    for _ in range(n):
        s = time.perf_counter(); r = fn(); out.append((time.perf_counter() - s) * 1000)
    return r, out
def show(label, ms): print(f"{label:38s} median {statistics.median(ms):7.0f} ms  min {min(ms):6.0f}  max {max(ms):6.0f}")
sh = lambda *a: subprocess.run([ADB, *a], capture_output=True, check=False)
sh("shell", "am", "start", "-a", "android.settings.SETTINGS"); time.sleep(2)
xml, ms = t(lambda: sh("exec-out", "uiautomator", "dump", "/dev/tty").stdout, 5); show("(a) adb uiautomator dump", ms); print("    bytes", len(xml))
_, ms = t(lambda: sh("shell", "input", "tap", "1", "1"), 5); show("(a) adb input tap", ms)
png, ms = t(lambda: sh("exec-out", "screencap", "-p").stdout, 5); show("adb exec-out screencap -p", ms); print("    bytes", len(png))
import uiautomator2 as u2
s = time.perf_counter(); d = u2.connect(); d.info; print(f"(b) u2 connect+server start            {(time.perf_counter()-s)*1000:7.0f} ms")
x, ms = t(lambda: d.dump_hierarchy(), 8); show("(b) u2 dump_hierarchy", ms); print("    chars", len(x))
x, ms = t(lambda: d.dump_hierarchy(compressed=True), 8); show("(b) u2 dump_hierarchy compressed", ms); print("    chars", len(x))
_, ms = t(lambda: d.click(1, 1), 8); show("(b) u2 click", ms)
_, ms = t(lambda: d.screenshot(), 5); show("(b) u2 screenshot (PIL)", ms)

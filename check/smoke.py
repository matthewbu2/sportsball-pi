"""Smoke test inside a Pi-like ARM container (check/check.py starts it):

    python3 smoke.py OUT_DIR       (the release tree is mounted at /src)

Copies the tree to /tmp/app and, like a Pi would: imports the vendored
compiled libraries and every module, renders frames offscreen from live ESPN
data (HD: main.py --shots, classic: main.py --png), builds the remote page
(OUT_DIR/remote_page.html, whose JS check.py checks) and seals a status
report. Prints "ok ..." / "FAIL ..." per step; exits 1 if any failed.
"""

import glob
import importlib
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback

OUT = sys.argv[1]
APP = "/tmp/app"
shutil.copytree("/src", APP)
os.chdir(APP)
os.environ["HOME"] = "/tmp/home"
os.makedirs("/tmp/home", exist_ok=True)
sys.path.insert(0, APP)
sys.path.append(os.path.join(APP, "vendor"))
HD = os.path.exists("PRODUCT")
bad = 0


def step(name, fn):
    global bad
    t = time.time()
    try:
        extra = fn()
        print(f"ok {name} ({time.time() - t:.0f} s){': ' + extra if extra else ''}", flush=True)
    except Exception:  # noqa: BLE001
        bad += 1
        print(f"FAIL {name}", flush=True)
        traceback.print_exc()


def natives():
    import aiohttp, frozenlist, multidict, propcache, yarl  # noqa: E401
    return f"{platform.machine()}, aiohttp {aiohttp.__version__}, " + \
        ", ".join(m.__name__ for m in (frozenlist, multidict, propcache, yarl))


def modules():
    skip = {"main"}
    names = sorted(os.path.basename(p)[:-3] for p in glob.glob("*.py"))
    for n in names:
        if n not in skip:
            importlib.import_module(n)
    return f"{len(names) - len(skip)} modules"


def frames():
    shots = os.path.join(OUT, "frames")
    cmd = ["python3", "main.py", "--shots", shots, "1360x768"] if HD else ["python3", "main.py", "--png", shots]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=40 * 60)
    pngs = glob.glob(os.path.join(shots, "**", "*.png"), recursive=True)
    if r.returncode or not pngs:
        raise RuntimeError(f"exit {r.returncode}, {len(pngs)} PNGs\n{(r.stdout + r.stderr)[-3000:]}")
    return f"{len(pngs)} frames"


def page():
    import remote
    html = remote._page()
    with open(os.path.join(OUT, "remote_page.html"), "wb") as f:
        f.write(html)
    return f"{len(html) // 1024} KB"


def status_report():
    import json
    import status
    rep = status.collect()
    json.dumps(rep)
    sealed = status.seal(rep)
    return f"{len(sealed)} bytes sealed"


step("vendored libraries", natives)
step("import all modules", modules)
step("remote page", page)
step("status report", status_report)
step("render frames from live ESPN data", frames)
sys.exit(1 if bad else 0)

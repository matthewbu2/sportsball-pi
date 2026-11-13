"""Pre-release checks for both looks (run by ./check.sh and release.sh).

  shared   the SHARED files match the other project (../sportsball-pi or
           ../sportsball-hd); --sync copies this project's copies across first
  lint     pyflakes on every .py of ours (venv in ~/.cache/sportsball-check)
  js       every <script> and on...="" handler in web/*.html parses (node --check)
  vendor   every compiled .so in vendor/ exists for the Pi 3 (aarch64) AND the
           Pi 2 (32-bit arm-linux-gnueabihf)
  git      status-token is never committed (the classic source repo is public)
  arm      (skipped with --fast) a Debian trixie container per Pi CPU, arm64
           and 32-bit armv7, run through qemu: install the Pi's apt packages,
           then check/smoke.py: import everything, render frames offscreen
           from live ESPN data, build the remote page (its JS is checked
           like web/*.html), seal a status report

Same file in both projects (it is in SHARED).
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PRODUCT = "hd" if os.path.exists(os.path.join(ROOT, "PRODUCT")) else "classic"
OTHER = os.path.join(os.path.dirname(ROOT), "sportsball-pi" if PRODUCT == "hd" else "sportsball-hd")
CACHE = os.path.expanduser("~/.cache/sportsball-check")
VENV = os.path.join(CACHE, "venv")
SKIP_DIRS = {"__pycache__", ".git", "cache", "dist", "image-cache", "image-out", "notes"}
ARCHES = {"arm64": "linux/arm64", "armv7": "linux/arm/v7"}     # Pi 3/4/5, Pi 2
QEMU = {"arm64": "qemu-aarch64", "armv7": "qemu-arm"}

failures = []


def say(msg):
    print(msg, flush=True)


def fail(check, msg):
    failures.append(f"{check}: {msg}")
    say(f"  FAIL {msg}")


# ---------------------------------------------------------------- shared
def _shared_list(root):
    with open(os.path.join(root, "SHARED")) as f:
        return [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]


def _files_under(root, rel):
    p = os.path.join(root, rel)
    if os.path.isfile(p):
        return {rel}
    out = set()
    for d, dirs, files in os.walk(p):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for name in files:
            if not name.endswith(".pyc"):
                out.add(os.path.relpath(os.path.join(d, name), root))
    return out


def _same(a, b):
    try:
        with open(a, "rb") as fa, open(b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def check_shared(sync):
    say(f"shared files vs {os.path.basename(OTHER)}")
    if not os.path.isdir(OTHER):
        say("  (the other project isn't next to this one; skipped)")
        return
    entries = _shared_list(ROOT)
    files = set()
    for rel in entries:
        files |= _files_under(ROOT, rel) | _files_under(OTHER, rel)
    diff = sorted(f for f in files if not _same(os.path.join(ROOT, f), os.path.join(OTHER, f)))
    if sync and diff:
        for f in diff:
            src, dst = os.path.join(ROOT, f), os.path.join(OTHER, f)
            if os.path.exists(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                say(f"  copied {f}")
            else:
                os.remove(dst)
                say(f"  removed {f} there (not here)")
        diff = []
    for f in diff:
        a, b = os.path.join(ROOT, f), os.path.join(OTHER, f)
        if not os.path.exists(a) or not os.path.exists(b):
            where = "only here" if os.path.exists(a) else "only there"
        else:
            where = "newer here" if os.path.getmtime(a) > os.path.getmtime(b) else "newer there"
        fail("shared", f"{f} differs ({where}); copy the right one across (./check.sh --sync copies from here)")
    if not diff:
        say(f"  {len(files)} files identical")


# ---------------------------------------------------------------- lint
def _ours(ext):
    out = []
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS | {"vendor", "assets", "web", "image"}]
        out += [os.path.join(d, f) for f in files if f.endswith(ext)]
    return sorted(out)


def check_lint():
    say("pyflakes")
    py = os.path.join(VENV, "bin", "python")
    if not os.path.exists(py):
        say("  making the checker venv (once)")
        subprocess.run([sys.executable, "-m", "venv", VENV], check=True)
        subprocess.run([py, "-m", "pip", "install", "-q", "pyflakes"], check=True)
    r = subprocess.run([py, "-m", "pyflakes", *_ours(".py")], capture_output=True, text=True)
    lines = [ln for ln in (r.stdout + r.stderr).splitlines() if ln.strip()]
    for ln in lines:
        fail("lint", os.path.relpath(ln, ROOT) if ln.startswith(ROOT) else ln)
    if not lines:
        say(f"  {len(_ours('.py'))} files clean")


# ---------------------------------------------------------------- js
SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
HANDLER = re.compile(r"""\son[a-z]+=(?:"([^"]*)"|'([^']*)')""", re.I)


def js_problems(html, name):
    """node --check every script block and every inline handler; returns errors."""
    if not shutil.which("node"):
        return ["node isn't installed"]
    import html as H
    html = re.sub(r"@[A-Z_]+@", "0", html)    # remote.py's placeholders
    parts = [m.group(1) for m in SCRIPT.finditer(html)]
    handlers = [H.unescape(m.group(1) if m.group(1) is not None else m.group(2)) for m in HANDLER.finditer(html)]
    parts += [f"function _h{i}(event){{{h}\n}}" for i, h in enumerate(handlers)]
    bad = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, code in enumerate(parts):
            p = os.path.join(tmp, f"{i}.js")
            with open(p, "w") as f:
                f.write(code)
            r = subprocess.run(["node", "--check", p], capture_output=True, text=True)
            if r.returncode:
                err = next((ln for ln in r.stderr.splitlines() if "Error" in ln), r.stderr.strip()[:200])
                bad.append(f"{name} script/handler #{i}: {err}  [{code.strip()[:80]!r}]")
    return bad


def check_js(extra=()):
    say("javascript")
    n = 0
    pages = [(os.path.relpath(p, ROOT), p) for p in sorted(_glob_web())] + list(extra)
    for name, path in pages:
        with open(path, encoding="utf-8") as f:
            for e in js_problems(f.read(), name):
                fail("js", e)
        n += 1
    if not any(f.startswith("js:") for f in failures):
        say(f"  {n} pages parse")


def _glob_web():
    d = os.path.join(ROOT, "web")
    return [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".html")] if os.path.isdir(d) else []


# ---------------------------------------------------------------- vendor
def check_vendor():
    say("vendor libraries for Pi 3 (aarch64) and Pi 2 (armhf)")
    sos = [f for f in _files_under(ROOT, "vendor") if ".so" in os.path.basename(f)]
    tag = re.compile(r"\.cpython-\d+-(aarch64-linux-gnu|arm-linux-gnueabihf)")
    have = {}
    for f in sos:
        m = tag.search(f)
        if not m:
            fail("vendor", f"{f}: can't tell which CPU it is for")
            continue
        have.setdefault(tag.sub("", f), set()).add(m.group(1))
    for base, arches in sorted(have.items()):
        for want in ("aarch64-linux-gnu", "arm-linux-gnueabihf"):
            if want not in arches:
                fail("vendor", f"{base}: no {want} build (piwheels / pypi wheel for that CPU)")
    say(f"  {len(have)} libraries, both CPUs" if not any(x.startswith("vendor") for x in failures) else "")


# ---------------------------------------------------------------- git
def check_git():
    say("secrets")
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        say("  (not a git repo)")
        return
    r = subprocess.run(["git", "-C", ROOT, "ls-files", "status-token"], capture_output=True, text=True)
    if r.stdout.strip():
        fail("git", "status-token is committed; git rm --cached it (the classic repo is public)")
    r = subprocess.run(["git", "-C", ROOT, "check-ignore", "-q", "status-token"])
    if r.returncode:
        fail("git", "status-token isn't in .gitignore")
    if not os.path.exists(os.path.join(ROOT, "status-token")):
        say("  note: no status-token here, so this build's tickers won't send status reports")
    if not any(x.startswith("git") for x in failures):
        say("  ok")


# ---------------------------------------------------------------- arm
def _docker(*args, **kw):
    return subprocess.run(["docker", *args], **kw)


def _qemu_image():
    tag = "sportsball-check:qemu"
    if _docker("image", "inspect", tag, capture_output=True).returncode:
        say("  building the qemu helper image (once)")
        df = "FROM debian:trixie\nRUN apt-get update -qq && apt-get install -qq -y qemu-user-static >/dev/null && apt-get clean\n"
        _docker("build", "-q", "-t", tag, "-", input=df.encode(), check=True, capture_output=True)
    return tag


def _register_qemu():
    """Like image/build-in-container.sh: register qemu with the F flag (works
    in every container afterwards). Returns the entries we added."""
    need = [a for a in ARCHES if not os.path.exists(f"/proc/sys/fs/binfmt_misc/{QEMU[a]}")]
    if not need:
        return []
    img = _qemu_image()
    script = "; ".join(
        f"c=$(ls /usr/lib/binfmt.d/{QEMU[a]}*.conf | head -1); "
        f"sed '/^:/!d; s/:\\([A-Z]*\\)$/:\\1F/; s/F\\(F*\\)$/F/' \"$c\" > /proc/sys/fs/binfmt_misc/register"
        for a in need)
    script = "[ -e /proc/sys/fs/binfmt_misc/register ] || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc; " + script
    _docker("run", "--rm", "--privileged", img, "sh", "-c", script, check=True)
    return [QEMU[a] for a in need]


def _unregister_qemu(names):
    if names:
        script = ("[ -e /proc/sys/fs/binfmt_misc/register ] || mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc; "
                  + "; ".join(f"echo -1 > /proc/sys/fs/binfmt_misc/{n}" for n in names))
        _docker("run", "--rm", "--privileged", _qemu_image(), "sh", "-c", script, capture_output=True)


def _pi_image(arch):
    df_path = os.path.join(ROOT, "check", "Dockerfile")
    with open(df_path, "rb") as f:
        digest = hashlib.sha256(f.read()).hexdigest()[:12]
    tag = f"sportsball-check:{arch}-{digest}"
    if _docker("image", "inspect", tag, capture_output=True).returncode:
        say(f"  building the {arch} Pi image (apt under qemu: several minutes, once)")
        _docker("build", "-q", "--platform", ARCHES[arch], "-t", tag, "-f", df_path, os.path.join(ROOT, "check"),
                check=True, capture_output=True)
    return tag


def _stage():
    """The tree as a release ships it (release.sh's excludes), in a temp dir."""
    tmp = tempfile.mkdtemp(prefix="sportsball-check-")
    excl = ["__pycache__", "cache", "dist", "notes", "image*", ".git", ".gitignore", "config.json*",
            "*.log", "*.log.*", "deploy.sh", "release.sh", "tools"]
    subprocess.run(["rsync", "-a", *[f"--exclude={e}" for e in excl], ROOT + "/", tmp + "/app/"], check=True)
    os.makedirs(os.path.join(tmp, "out"))
    return tmp


def check_arm():
    say("Pi smoke tests (arm64 = Pi 3/4/5, armv7 = Pi 2; qemu, slow)")
    if not shutil.which("docker"):
        fail("arm", "docker isn't installed")
        return
    added = _register_qemu()
    try:
        for arch in ARCHES:
            img = _pi_image(arch)
            tmp = _stage()
            t0 = time.time()
            r = _docker("run", "--rm", "--platform", ARCHES[arch], "--network", "host",
                        "-v", f"{tmp}/app:/src:ro", "-v", f"{tmp}/out:/out",
                        "-e", "SDL_VIDEODRIVER=offscreen", "-e", "SDL_AUDIODRIVER=dummy", "-e", f"PRODUCT={PRODUCT}",
                        img, "python3", "/src/check/smoke.py", "/out",
                        capture_output=True, text=True, timeout=45 * 60)
            out = (r.stdout + r.stderr).strip().splitlines()
            for ln in out:
                if ln.startswith(("ok ", "FAIL ", "  ")):
                    say(f"  [{arch}] {ln}")
            if r.returncode:
                fail("arm", f"{arch}: smoke test failed (exit {r.returncode}); last output:\n    "
                     + "\n    ".join(out[-15:]))
            page = os.path.join(tmp, "out", "remote_page.html")
            if os.path.exists(page):
                with open(page, encoding="utf-8") as f:
                    for e in js_problems(f.read(), f"{arch} rendered remote page"):
                        fail("arm", e)
            say(f"  [{arch}] {time.time() - t0:.0f} s")
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        _unregister_qemu(added)


# ---------------------------------------------------------------- main
def main():
    args = set(sys.argv[1:])
    if args - {"--fast", "--sync"}:
        say(__doc__)
        sys.exit(2)
    say(f"== checking {os.path.basename(ROOT)} ({PRODUCT})")
    check_shared("--sync" in args)
    check_lint()
    check_js()
    check_vendor()
    check_git()
    if "--fast" not in args:
        check_arm()
    if failures:
        say(f"\n{len(failures)} problem(s); not ready to release.")
        sys.exit(1)
    say("\nall checks passed")


if __name__ == "__main__":
    main()

"""Date and time at the start of every log line (11-13 15:41:02 ...).

The logs are plain files written by systemd (StandardOutput=append:), which
adds no times. main.py, updater.py, style.py and status.py call install()
first thing, so their prints and tracebacks are stamped; status.py relies on
it to tell new errors from old ones. Not on a terminal (you see the time).

The same file is in both projects; keep them identical.
"""

import sys
import time

STAMP = "%m-%d %H:%M:%S "


class _Stamped:
    def __init__(self, f):
        self._f = f
        self._bol = True            # at the beginning of a line

    def write(self, s):
        if not s:
            return 0
        out = []
        for part in s.splitlines(keepends=True):
            if self._bol:
                out.append(time.strftime(STAMP))
            out.append(part)
            self._bol = part.endswith("\n")
        self._f.write("".join(out))
        return len(s)

    def __getattr__(self, name):
        return getattr(self._f, name)


def install():
    for name in ("stdout", "stderr"):
        f = getattr(sys, name)
        if f is not None and not isinstance(f, _Stamped) and not f.isatty():
            setattr(sys, name, _Stamped(f))

"""Score fetches in a helper process, so parsing ESPN's JSON never stalls drawing.

json.loads of a scoreboard (240-460 KB) holds Python's GIL for 45-90 ms on a
Pi 2 (half that on a Pi 3); in the ticker's own process that freezes the frame
loop, a visible hitch in the scrolling strip every few seconds during live
games. Here a long-lived child (this file run as a script, importing only
espn) does the HTTP + parse and sends back the small list of Game records.

    fetch(...)   same arguments and result as espn.fetch; falls back to
                 espn.fetch in this process if the helper can't run.
"""

import os
import pickle
import struct
import subprocess
import sys
import threading

import espn

CALL_TIMEOUT_S = 90            # espn.fetch makes up to 3 requests, 15 s each
_lock = threading.Lock()
_proc = None
_failures = 0


def _send(f, obj):
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    f.write(struct.pack("<I", len(data)) + data)
    f.flush()


def _recv(f):
    head = f.read(4)
    if len(head) < 4:
        raise EOFError("helper closed")
    (n,) = struct.unpack("<I", head)
    data = f.read(n)
    if len(data) < n:
        raise EOFError("helper closed")
    return pickle.loads(data)


def _start():
    global _proc
    here = os.path.dirname(os.path.abspath(__file__))
    _proc = subprocess.Popen([sys.executable, "-u", os.path.join(here, "espn_worker.py")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=here)


def _stop():
    global _proc
    if _proc is not None:
        try:
            _proc.kill()
            _proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass
    _proc = None


def fetch(*args, **kw):
    global _failures
    with _lock:
        if _failures >= 3:                     # helper keeps dying: do it here
            return espn.fetch(*args, **kw)
        try:
            if _proc is None or _proc.poll() is not None:
                _start()
            timer = threading.Timer(CALL_TIMEOUT_S, _proc.kill)
            timer.start()
            try:
                _send(_proc.stdin, (args, kw))
                ok, result = _recv(_proc.stdout)
            finally:
                timer.cancel()
        except (OSError, EOFError, pickle.PickleError, struct.error) as e:
            _failures += 1
            print(f"[Fetch] helper failed ({e}), {'giving up on it' if _failures >= 3 else 'restarting it'}",
                  flush=True)
            _stop()
            raise espn.FetchError(f"helper: {e}") from None
        _failures = 0
        if ok:
            return result
        raise result


def _serve():
    """Child: requests on stdin, (ok, games or exception) on stdout. Prints
    (espn's log lines) go to stderr, which is the ticker's log."""
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr
    while True:
        try:
            args, kw = _recv(inp)
        except EOFError:
            return
        try:
            reply = (True, espn.fetch(*args, **kw))
        except Exception as e:                 # sent back and raised in the ticker
            try:
                pickle.dumps(e)
            except Exception:
                e = espn.FetchError(f"{type(e).__name__}: {e}")
            reply = (False, e)
        _send(out, reply)


if __name__ == "__main__":
    _serve()

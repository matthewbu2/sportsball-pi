"""Team/league logos as LOGO_SIZE x LOGO_SIZE RGB arrays.

Fetched in a background thread through ESPN's combiner resize proxy
(a.espncdn.com/combiner/i?img=<path>&w=&h= -- plain ?w=&h= on the original
URL is ignored and returns the 500px original), composited onto black and
cached on disk, so they survive restarts. get() never blocks: a logo not
loaded yet just isn't drawn this frame.
"""

import hashlib
import io
import os
import queue
import threading
import time
from urllib.parse import urlsplit

import numpy as np
from PIL import Image

from espn import SESSION

LOGO_SIZE = 24
CACHE_DIR = os.path.expanduser("~/ticker/cache/logos")

_mem = {}
_pending = set()
_q = queue.Queue()
_lock = threading.Lock()


def _path(url):
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")


def _to_array(data):
    im = Image.open(io.BytesIO(data)).convert("RGBA")
    if im.size != (LOGO_SIZE, LOGO_SIZE):
        im = im.resize((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
    bg = Image.new("RGBA", im.size, (0, 0, 0, 255))
    return np.asarray(Image.alpha_composite(bg, im).convert("RGB")).copy()


def _get(url, params=None):
    """ESPN's image CDN drops idle keep-alive connections ("connection reset
    by peer"): retry on a fresh connection."""
    import requests
    for attempt in range(3):
        try:
            return SESSION.get(url, params=params, timeout=10)
        except requests.ConnectionError:
            if attempt == 2:
                raise
            SESSION.close()
            time.sleep(0.5 + attempt)


def _load(url):
    p = _path(url)
    if os.path.exists(p):
        try:
            with open(p, "rb") as f:
                return _to_array(f.read())
        except Exception:  # noqa: BLE001 - torn by a power cut: fetch it again
            os.unlink(p)
    path = urlsplit(url).path
    r = _get("https://a.espncdn.com/combiner/i", params={"img": path, "w": LOGO_SIZE, "h": LOGO_SIZE})
    if r.status_code != 200:   # not every image goes through the combiner
        r = _get(url)
    r.raise_for_status()
    arr = _to_array(r.content)
    os.makedirs(CACHE_DIR, exist_ok=True)
    Image.fromarray(arr).save(p + ".tmp", "PNG")
    os.replace(p + ".tmp", p)
    return arr


def _worker():
    while True:
        url = _q.get()
        try:
            arr = _load(url)
        except Exception as e:  # noqa: BLE001 - a bad logo must not stop the others
            print(f"[Logo] {url}: {e}", flush=True)
            arr = None
        with _lock:
            _mem[url] = arr
            _pending.discard(url)


def get(url):
    if not url:
        return None
    arr = _mem.get(url)
    if arr is None and url not in _mem:
        want(url)
    return arr


def want(url):
    if not url:
        return
    with _lock:
        if url in _mem or url in _pending:
            return
        _pending.add(url)
    _q.put(url)


for _ in range(3):
    threading.Thread(target=_worker, daemon=True).start()

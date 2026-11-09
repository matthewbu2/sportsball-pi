"""The announcer: reads the play-by-play and calls the big moments aloud.

Speech comes from Microsoft's online neural voices (the Edge "Read aloud"
service, via the edge-tts package in vendor/): natural-sounding, ~0.8 s a
line. Offline, it falls back to Piper (~/piper), which sounds robotic and
takes 6-8 s a line on a Pi 3. Every live game's newest play is synthesized
as soon as it arrives, so it's ready by the time its card comes up.

Optionally (voice_radio) lines go through an old AM-radio treatment
(band-limited, a little saturation, faint hiss and crackle).

What gets said:
  * a call for each moment the cutscenes play ("Touchdown, Colts!")
  * while a live game's card is up, that game's latest play -- each play
    once; when the card moves on, the voice switches with it
  * "Red zone!" when the screen cuts to a drive inside the 20
  * "Around the league" score roundups: at a favorite's halftime or
    intermission, and every ROUNDUP_EVERY while 2+ games are live
  * "Kickoff in 15 minutes: the Colts at the Vikings, on FOX" for favorites
"""

import asyncio
import io
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time

import numpy as np

import anim as A
import settings
import sfx
from espn import IN, PRE

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

PIPER_DIR = os.path.expanduser("~/piper")
PIPER_VOICE = "en_US-ryan-low"
VOICES = {   # neural voice: label in the remote
    "en-US-GuyNeural": "Guy (energetic)",
    "en-US-ChristopherNeural": "Christopher (authoritative)",
    "en-US-RogerNeural": "Roger (lively)",
    "en-US-EricNeural": "Eric (calm)",
    "en-US-AndrewNeural": "Andrew (warm)",
    "en-US-BrianNeural": "Brian (casual)",
    "en-US-SteffanNeural": "Steffan (steady)",
}
RATE = "+8%"
ROUNDUP_EVERY = 20 * 60
ROUNDUP_MAX_AGE = 60
ROUNDUP_GAMES = 4
NEURAL_TIMEOUT = 8
CALL_MAX_AGE = 15          # seconds: a moment's call is dropped if it can't start by then
PLAY_MAX_AGE = 40

_lock = threading.Lock()
_jobs = queue.PriorityQueue()     # (priority, seq, text)
_ready = {}                       # text -> pygame Sound
_seq = 0
_want_call = None                 # (text, queued_at)
_want_play = None                 # (game key, text)
_latest = set()                   # newest play of each live game (prefetched)
_spoken = set()                   # play texts already read
_playing = None                   # ("call"|"play"|"roundup", text)
_want_roundup = None              # (text, queued_at)
_games = []                       # latest games (for roundups)
_pregame_done = set()             # game keys already announced
PREGAME_MIN = 15
_last_roundup = time.monotonic()
_proc = None
_ok = False
_neural_ok = True                 # last neural request worked


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------
def _location(long_name, nick):
    loc = long_name[:-len(nick)].strip() if nick and long_name.endswith(nick) else long_name
    return loc or nick


NOT_TEAMS = {"TD", "PAT", "FG", "INT", "OT", "YDS", "YD", "NO", "PEN", "ST", "QB", "RB", "WR", "TE", "OL", "DL"}


def which_team(code, g):
    """An ESPN team code -> 1 (away), 2 (home) or 0. Play text uses its own
    codes (ARZ, HST, BLT) that differ from the scoreboard's (ARI, HOU, BAL),
    so: exact first, then the code's letters in order in a team's names."""
    code = code.upper()
    if code == g.away_abbr.upper():
        return 1
    if code == g.home_abbr.upper():
        return 2
    if code in NOT_TEAMS or len(code) < 3:
        return 0
    best, side = 0, 0
    for sd, names in ((1, (g.away_abbr, g.away_long, g.away_name)), (2, (g.home_abbr, g.home_long, g.home_name))):
        hay = " ".join(n or "" for n in names).upper()
        if not hay or hay[0] != code[0]:
            continue
        i, ok = 0, True
        for ch in code:
            i = hay.find(ch, i)
            if i < 0:
                ok = False
                break
            i += 1
        if ok and len(code) > best:
            best, side = len(code), sd
    return side


def speakable(text, g=None):
    """ESPN play text -> something that reads well aloud (teams by name,
    never by code)."""
    s = re.sub(r"\([^)]*\)", "", text)                         # clock, tacklers, (Shotgun)
    s = re.sub(r"\b([A-Z][a-z]?\.)\s?(?=[A-Z][a-z])", "", s)    # J.Taylor -> Taylor
    if g is not None:
        def spot(m):
            side = which_team(m.group(1), g)
            if not side:
                return m.group(0)
            long_name, nick = (g.away_long, g.away_name) if side == 1 else (g.home_long, g.home_name)
            return f"the {_location(long_name, nick)} {m.group(2)}"
        s = re.sub(r"\b(?:the )?([A-Z]{2,4}) (\d{1,2})\b", spot, s)        # "ARZ 35" -> "the Arizona 35"

        def code(m):
            side = which_team(m.group(1), g)
            return (g.away_name if side == 1 else g.home_name) if side else m.group(0)
        s = re.sub(r"\b([A-Z]{3,4})\b", code, s)                           # a bare "ARZ" -> "Cardinals"
    for a, b in ((r"\byds\b", "yards"), (r"\byd\b", "yard"), (r"\bTD\b", "touchdown"), (r"\bINT\b", "interception"),
                 (r"\bPAT\b", "extra point"), (r"\bFG\b", "field goal"), (r"\bOT\b", "overtime"),
                 (r"\bno gain\b", "no gain")):
        s = re.sub(a, b, s)
    s = re.sub(r"\s+", " ", s).strip(" .,")
    return s[:220] + "." if s else ""


def _score_line(g, home):
    w, l = (g.home_name, g.away_name) if home else (g.away_name, g.home_name)
    ws, ls = (g.home_score, g.away_score) if home else (g.away_score, g.home_score)
    return f"{w} {ws}, {l} {ls}"


_ORD = {1: "first", 2: "second", 3: "third", 4: "fourth"}


def _nth(n):
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _when(g):
    st = g.status_text.upper()
    if "HALF" in st:
        return "at the half"
    if "END" in st or "INT" in st:
        return "at the break"
    p = g.period
    if g.sport_idx == 4:
        return f"in the {_ORD.get(p, 'overtime')} period" if p <= 3 else "in overtime"
    if g.sport_idx == 6:
        return f"in the {_ORD.get(p)} half" if p <= 2 else "in overtime"
    if g.sport_idx == 3:
        return f"in the {'top' if g.possession == 1 else 'bottom'} of the {_nth(p)}"
    return f"in the {_ORD.get(p)} quarter" if p <= 4 else "in overtime"


def roundup_text(games, exclude=None):
    """'Around the league. Michigan leads Indiana, 21 to 17, in the third quarter. ...'"""
    live = [g for g in games if g.state == IN and g.key != exclude and g.away_score and g.home_score]
    live.sort(key=lambda g: (not g.fav_mask, not (g.away_rank or g.home_rank), g.date))
    parts = []
    for g in live[:ROUNDUP_GAMES]:
        a, h = int(g.away_score or 0), int(g.home_score or 0)
        if a == h:
            parts.append(f"The {g.away_name} and {g.home_name} are tied at {a}, {_when(g)}")
        else:
            (w, ws), (l, ls) = sorted(((g.away_name, a), (g.home_name, h)), key=lambda t: -t[1])
            parts.append(f"The {w} lead the {l}, {ws} to {ls}, {_when(g)}")
    return ("Around the league. " + ". ".join(parts) + ".") if parts else ""


def call_for(ev):
    """What the announcer says for a moment."""
    g, home = ev.game, ev.home
    team = g.home_name if home else g.away_name
    other = g.away_name if home else g.home_name
    k, tag = ev.kind, ev.tag
    if k == A.EV_START:
        return f"And we're under way! The {g.away_name} at the {g.home_name}."
    if k == A.EV_UPSET:
        rank = g.home_rank if home else g.away_rank
        return f"Upset alert! Number {rank}, {team}, is in trouble. {_score_line(g, not home)}."
    if k == A.EV_HATTRICK:
        return f"Hat trick! Hats raining down on the ice for the {team}!"
    if k == A.EV_BREAK:
        when = "At the half" if "HALF" in g.status_text.upper() else "After the period"
        return f"{when}, it's {g.away_name} {g.away_score}, {g.home_name} {g.home_score}."
    if k == A.EV_TURNOVER:
        pick = ev.delta == 1 if ev.demo else g.last_play == A.PLAY_INT
        return f"Picked off! The {team} take it away!" if pick else f"Fumble! And the {team} recover!"
    if k == A.EV_LEAD:
        return f"And the {team} take the lead. {_score_line(g, home)}."
    if k == A.EV_FINAL:
        if g.away_score == g.home_score:
            return f"And that's the ball game. A tie, {g.away_score} to {g.home_score}."
        mine = g.fav_mask & (2 if home else 1)
        theirs = g.fav_mask & (1 if home else 2)
        line = f"And that's the ball game! {_score_line(g, home)}."
        if tag == "upset":
            line += " What an upset!"
        elif mine:
            line += " What a win!"
        elif theirs:
            line = f"And that'll do it. A tough one today. {_score_line(g, home)}."
        return line
    if k == A.EV_SCORE:
        if g.fav_mask & (1 if home else 2) and not g.fav_mask & (2 if home else 1) and tag not in ("buzzer",):
            return f"Oh no. The {team} score. {_score_line(g, home)}."
        sport, d = g.sport_idx, ev.delta
        if tag == "safety":
            return f"Safety! Two points for the {team}!"
        if tag == "two" and d == 2:
            return f"The two point try is good for the {team}!"
        if tag == "grandslam":
            return f"Swung on, and belted! It's a grand slam! Four runs score for the {team}!"
        if tag == "walkoff":
            return f"And the {team} win it! A walk-off! Ball game!"
        if tag == "buzzer":
            return f"At the buzzer! It's good! The {team} win it! {_score_line(g, home)}!"
        if sport in (1, 5):
            if d >= 6:
                return f"He's at the twenty, the ten, touchdown, {team}! {_score_line(g, home)}."
            if d in (1, 3):
                return "The kick is up, and it's good!" if d == 3 else f"Extra point is good. {_score_line(g, home)}."
        if sport == 3:
            if g.last_play == A.PLAY_HOMER or ev.demo:
                return f"Swung on, and belted! That ball is gone! Home run, {team}!"
            return f"{'A run scores' if d == 1 else f'{d} runs score'} for the {team}. {_score_line(g, home)}."
        if sport == 4:
            return f"He shoots, he scores! Goal, {team}! {_score_line(g, home)}."
        if sport in (2, 6):
            return f"Slam dunk, {team}!" if d == 2 else f"From downtown! Three for the {team}!"
        return f"Score, {team}. {_score_line(g, home)}."
    return ""


# ---------------------------------------------------------------------------
# Sound: neural voice (Piper offline) -> optional AM radio
# ---------------------------------------------------------------------------
_rng = np.random.default_rng(3)
_dns = {}
_getaddrinfo = socket.getaddrinfo


def _cached_getaddrinfo(host, *a, **k):
    """A dropped DNS lookup falls back to the last answer for that host."""
    try:
        r = _getaddrinfo(host, *a, **k)
        _dns[(host, a[:1])] = r
        return r
    except socket.gaierror:
        if (host, a[:1]) in _dns:
            return _dns[(host, a[:1])]
        raise


socket.getaddrinfo = _cached_getaddrinfo


def radio(x, sr):
    """Mono samples at sr -> 44.1 kHz with an old AM-radio sound."""
    n = int(len(x) * sfx.SR / sr)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    spec = np.fft.rfft(y)
    f = np.fft.rfftfreq(n, 1 / sfx.SR)
    band = 1 / (1 + (280 / np.maximum(f, 1)) ** 4) / (1 + (f / 3200) ** 4)   # 280 Hz - 3.2 kHz
    band *= 1 + 0.6 * np.exp(-((f - 1800) / 500) ** 2)                      # nasal mid bump
    y = np.fft.irfft(spec * band, n)
    y = y / (np.abs(y).max() + 1e-6)
    y = np.tanh(2.4 * y) / np.tanh(2.4)                                       # tube saturation
    hiss = sfx.lowpass(_rng.uniform(-1, 1, n), 2) * 0.012
    crackle = np.zeros(n)
    for i in _rng.integers(0, n, size=max(1, n // 9000)):
        crackle[i:i + 30] += _rng.uniform(-0.25, 0.25) * np.exp(-np.arange(min(30, n - i)) / 6)
    pad = np.zeros(int(sfx.SR * 0.15))
    return np.concatenate([pad, 0.85 * y + hiss + crackle, pad])


def _finish(x, sr):
    """Mono samples at sr -> 44.1 kHz, radio treatment if it's on."""
    if settings.get("voice_radio"):
        return level(radio(x, sr), sfx.SR)
    if sr != sfx.SR:
        n = int(len(x) * sfx.SR / sr)
        x = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    x = level(x, sfx.SR)
    pad = np.zeros(int(sfx.SR * 0.1))
    return np.concatenate([pad, x, pad])


TARGET_RMS = 0.13


def level(x, sr):
    """Same loudness for every line: scale the speech (50 ms windows above a
    gate, so pauses don't count) to TARGET_RMS, then a soft limiter. (Peak
    normalizing made some lines loud and others quiet.)"""
    x = x - x.mean()
    win = max(1, int(sr * 0.05))
    n = len(x) // win
    if n == 0:
        return x
    r = np.sqrt((x[: n * win].reshape(n, win) ** 2).mean(axis=1))
    voiced = r[r > r.max() * 0.15]
    rms = np.sqrt((voiced ** 2).mean()) if len(voiced) else r.max()
    y = x * (TARGET_RMS / (rms + 1e-9))
    knee = 0.6
    a = np.abs(y)
    over = a > knee
    y[over] = np.sign(y[over]) * (knee + (0.98 - knee) * np.tanh((a[over] - knee) / (0.98 - knee)))
    return y


def _neural(text):
    """Mono samples at sfx.SR from the online voice (raises when offline)."""
    import edge_tts

    async def fetch():
        buf = bytearray()
        async for c in edge_tts.Communicate(text, settings.get("voice"), rate=RATE).stream():
            if c["type"] == "audio":
                buf += c["data"]
        return bytes(buf)

    mp3 = asyncio.run(asyncio.wait_for(fetch(), NEURAL_TIMEOUT))
    if not mp3:
        raise RuntimeError("no audio")
    import pygame
    a = pygame.sndarray.array(pygame.mixer.Sound(file=io.BytesIO(mp3)))
    return (a[:, 0] if a.ndim == 2 else a).astype(np.float64) / 32768


def _start_piper():
    global _proc
    model = os.path.join(PIPER_DIR, PIPER_VOICE + ".onnx")
    cmd = ["taskset", "-c", "1-3", "nice", "-n", "10", os.path.join(PIPER_DIR, "piper", "piper"),
           "-m", model, "--json-input", "-q"]
    _proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, cwd=PIPER_DIR, bufsize=1)


def _piper(text, sr, path):
    import wave
    _proc.stdin.write(json.dumps({"text": text, "output_file": path}) + "\n")
    _proc.stdin.flush()
    if not _proc.stdout.readline():
        raise RuntimeError("piper exited")
    with wave.open(path) as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float64) / 32768
    os.unlink(path)
    return x, sr


def _synth(text, tmp):
    """Neural voice, or Piper when that fails (offline)."""
    global _neural_ok, _proc
    try:
        for attempt in range(3):     # the router's DNS drops a lookup now and then
            try:
                x = _neural(text)
                break
            except Exception:  # noqa: BLE001
                if attempt == 2:
                    raise
                time.sleep(0.7)
        if not _neural_ok:
            print("[Voice] neural voice back", flush=True)
        _neural_ok = True
        return _finish(x, sfx.SR), "neural"
    except Exception as e:  # noqa: BLE001
        if _neural_ok:
            print(f"[Voice] neural voice failed ({e!r}); using piper", flush=True)
        _neural_ok = False
    if _proc is None or _proc.poll() is not None:
        _start_piper()
    with open(os.path.join(PIPER_DIR, PIPER_VOICE + ".onnx.json")) as f:
        sr = json.load(f)["audio"]["sample_rate"]
    try:
        return _finish(*_piper(text, sr, os.path.join(tmp, "v.wav"))), "piper"
    except Exception:
        _proc = None
        raise


def _worker():
    global _ok
    _ok = True
    tmp = tempfile.mkdtemp(prefix="voice")
    style = None
    while True:
        _prio, _n, text = _jobs.get()
        now_style = (settings.get("voice"), settings.get("voice_radio"))
        with _lock:
            if now_style != style:     # voice changed in the remote: old lines are stale
                style = now_style
                _ready.clear()
            if text in _ready or not _still_wanted(text) or not enabled():
                continue
        t0 = time.time()
        try:
            x, how = _synth(text, tmp)
            snd = sfx.to_sound(x)
        except Exception as e:  # noqa: BLE001
            print(f"[Voice] {e}", flush=True)
            time.sleep(2)
            continue
        with _lock:
            _ready[text] = snd
            while len(_ready) > 40:
                _ready.pop(next(iter(_ready)))
        print(f"[Voice] {how} {time.time() - t0:.1f}s: {text[:60]}", flush=True)


def _still_wanted(text):
    if _want_call and _want_call[0] == text:
        return True
    if _want_roundup and _want_roundup[0] == text:
        return True
    return bool(_want_play and _want_play[1] == text) or text in _latest


def _queue(text, prio):
    global _seq
    if not enabled():          # muted / quiet hours: don't synthesize lines nobody hears
        return
    _seq += 1
    _jobs.put((prio, _seq, text))


# ---------------------------------------------------------------------------
# API (main thread)
# ---------------------------------------------------------------------------
def voice_state():
    """For the health panel."""
    if not settings.get("announcer"):
        return "off"
    if not _ok:
        return "starting"
    return "neural voice" if _neural_ok else "offline voice (Piper)"


def enabled():
    return _ok and settings.get("announcer") and sfx.volume() > 0


def start():
    threading.Thread(target=_worker, daemon=True).start()


def hook(sound, ev, seg):
    """anim.Player hook: queue the moment's call as its first segment starts."""
    global _want_call
    if seg != 0 or not enabled():
        return
    text = call_for(ev)
    if text:
        with _lock:
            _want_call = (text, time.monotonic())
        _queue(text, 0)
    if ev.kind == A.EV_BREAK and not ev.demo:
        _roundup(roundup_text(_games, exclude=ev.game.key))



def _say_between(text):
    """A line that waits for the voice to be free (doesn't cut anything off)."""
    global _want_roundup
    with _lock:
        _want_roundup = (text, time.monotonic())
    _queue(text, 1)


def _roundup(text):
    global _last_roundup
    if not text or not settings.get("roundups"):
        return
    _last_roundup = time.monotonic()
    _say_between(text)


_START_WORD = {1: "Kickoff", 5: "Kickoff", 2: "Tip-off", 6: "Tip-off", 3: "First pitch", 4: "Puck drop"}
_NETWORK_SAID = {"BTN": "the Big Ten Network", "PEACO": "Peacock", "ESPN2": "ESPN 2", "ESPNU": "ESPN U",
                 "NFLN": "NFL Network", "PRIME": "Prime Video", "CBSSN": "CBS Sports Network", "FS1": "FS 1"}


def _pregame(games):
    """A favorite starts within PREGAME_MIN minutes: say so, once."""
    if not settings.get("pregame"):
        return
    import cards
    now = time.time()
    for g in games:
        if not g.fav_mask or g.state != PRE or g.key in _pregame_done:
            continue
        mins = round((cards.iso_to_time(g.date) - now) / 60)
        if 0 < mins <= PREGAME_MIN:
            _pregame_done.add(g.key)
            word = _START_WORD.get(g.sport_idx, "Game time")
            line = f"{word} in {mins} minute{'s' if mins != 1 else ''}: the {g.away_name} at the {g.home_name}"
            if g.network:
                line += f", on {_NETWORK_SAID.get(g.network, g.network)}"
            _say_between(line + ".")
            return


def roundup_now(games):
    """A roundup right away (the live demo)."""
    _roundup(roundup_text(games))


def roundup_tick(games):
    """Every ROUNDUP_EVERY with 2+ games live: an around-the-league roundup."""
    global _games
    _games = games
    if not enabled():
        return
    _pregame(games)
    if time.monotonic() - _last_roundup < ROUNDUP_EVERY:
        return
    if sum(1 for g in games if g.state == IN) >= 2:
        _roundup(roundup_text(games))


def red_zone(g):
    """The screen just cut to g's red-zone drive."""
    global _want_call
    if not enabled():
        return
    team = g.away_name if g.possession == 1 else g.home_name if g.possession == 2 else ""
    other = g.home_name if g.possession == 1 else g.away_name
    text = f"Red zone! The {team} are knocking on the door against the {other}." if team else "Red zone!"
    with _lock:
        _want_call = (text, time.monotonic())
    _queue(text, 0)


def prefetch(games):
    """New scores: synthesize every live game's newest play right away."""
    global _latest
    if not enabled():
        return
    texts = [speakable(g.play_text, g) for g in games if g.state == IN and g.play_text]
    texts = [t for t in texts if t and t not in _spoken]
    with _lock:
        _latest = set(texts)
        todo = [t for t in texts if t not in _ready]
    for t in todo:
        _queue(t, 2)


def focus(game):
    """The card on screen changed (None = not a live game): read its play."""
    global _want_play
    text = speakable(game.play_text, game) if game is not None and game.state == IN and game.play_text else ""
    with _lock:
        _want_play = (game.key, text) if text and text not in _spoken else None
    if _want_play:
        _queue(text, 1)
    ch = sfx.voice_channel()
    if ch is not None and _playing and _playing[0] == "play" and _playing[1] != text:
        ch.fadeout(250)   # the voice switches with the card


def tick():
    """Starts whatever is ready (call first). Call once per frame."""
    global _want_call, _want_play, _playing, _want_roundup
    ch = sfx.voice_channel()
    if ch is None:
        return
    if not ch.get_busy():
        _playing = None
    if not enabled():
        if ch.get_busy():
            ch.fadeout(300)
        return
    with _lock:
        if _want_call and time.monotonic() - _want_call[1] > CALL_MAX_AGE:
            _want_call = None
        if _want_call and _want_call[0] in _ready:
            if _playing and _playing[0] == "play":
                ch.stop()
            if not ch.get_busy():
                text = _want_call[0]
                _want_call = None
                _start(ch, "call", text)
            return
        if _want_roundup and time.monotonic() - _want_roundup[1] > ROUNDUP_MAX_AGE:
            _want_roundup = None
        if _want_roundup and _want_roundup[0] in _ready and not ch.get_busy():
            text = _want_roundup[0]
            _want_roundup = None
            _start(ch, "roundup", text)
            return
        if _want_play and _want_play[1] in _ready and not ch.get_busy():
            text = _want_play[1]
            _spoken.add(text)
            if len(_spoken) > 500:
                _spoken.clear()
            _want_play = None
            _start(ch, "play", text)


def _start(ch, kind, text):
    global _playing
    snd = _ready[text]
    snd.set_volume(sfx.volume())
    ch.play(snd)
    _playing = (kind, text)

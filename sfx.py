"""Cutscene sound effects through the TV (HDMI audio), synthesized with numpy.

Original 8-bit-style sounds: fanfares on square waves, crowd roar from
filtered noise, a goal horn, bat crack, rim clang, swish, buzzer, sad
trombone... Each scene's sound is timed to its action (the touchdown
fanfare as the runner crosses the goal line, the crack at contact). Built
once in a background thread at startup; volume and quiet hours come from
settings. The announcer has its own reserved mixer channel (voice_channel()).
"""

import glob
import os
import re
import threading
import time

import numpy as np

import settings

SR = 44100
_sounds = {}
_ready = False
_mixer = None
_rng = np.random.default_rng(7)


# ---------------------------------------------------------------------------
# Synthesis
# ---------------------------------------------------------------------------
def _t(dur):
    return np.arange(int(SR * dur)) / SR


def env(n, a=0.01, r=0.08):
    """Attack/release envelope over n samples."""
    e = np.ones(n)
    na, nr = min(n, int(SR * a)), min(n, int(SR * r))
    if na:
        e[:na] = np.linspace(0, 1, na)
    if nr:
        e[-nr:] *= np.linspace(1, 0, nr)
    return e


def square(f, dur, duty=0.5, vol=0.3):
    t = _t(dur)
    return vol * np.where((t * f) % 1 < duty, 1.0, -1.0) * env(len(t))


def saw(f, dur, vol=0.3):
    t = _t(dur)
    return vol * (2 * ((t * f) % 1) - 1) * env(len(t), 0.02, 0.1)


def sine(f, dur, vol=0.3):
    t = _t(dur)
    return vol * np.sin(2 * np.pi * f * t) * env(len(t))


def lowpass(x, k):
    """Moving-average low-pass of width k samples."""
    if k <= 1:
        return x
    c = np.cumsum(np.concatenate([np.zeros(k), x]))
    return (c[k:] - c[:-k]) / k


def noise(dur, vol=0.3, smooth=1):
    return vol * lowpass(_rng.uniform(-1, 1, int(SR * dur)), smooth)


def crowd(dur, vol=0.25, swell=0.6):
    """Crowd roar: a few bands of noise, swelling in and fading out."""
    n = int(SR * dur)
    x = lowpass(_rng.uniform(-1, 1, n), 6) * 0.7 + lowpass(_rng.uniform(-1, 1, n), 30) * 1.4
    shape = np.minimum(1, np.linspace(0, dur / max(swell, 0.01), n)) * env(n, 0.01, min(1.0, dur / 2))
    wob = 1 + 0.15 * np.sin(2 * np.pi * 3.1 * _t(dur)) * np.sin(2 * np.pi * 0.7 * _t(dur))
    return vol * x * shape * wob


def notes(seq, wave=square, vol=0.25, **kw):
    """seq: [(freq or 0 for a rest, seconds)]."""
    parts = [wave(f, d, vol=vol, **kw) if f else np.zeros(int(SR * d)) for f, d in seq]
    return np.concatenate(parts)


def thump(vol=0.6, f=90):
    t = _t(0.18)
    return vol * np.sin(2 * np.pi * f * t * (1 - t * 2)) * np.exp(-t * 25)


def crack(vol=0.8):
    t = _t(0.08)
    return vol * (_rng.uniform(-1, 1, len(t)) * np.exp(-t * 60) + 0.5 * np.sin(2 * np.pi * 1800 * t) * np.exp(-t * 40))


def clang(vol=0.4):
    t = _t(0.7)
    return vol * sum(np.sin(2 * np.pi * f * t) * np.exp(-t * d) for f, d in ((523, 6), (1187, 8), (1763, 11), (2471, 14)))


def swish(vol=0.35):
    t = _t(0.45)
    x = _rng.uniform(-1, 1, len(t)) - lowpass(_rng.uniform(-1, 1, len(t)), 4)
    return vol * x * np.sin(np.pi * t / t[-1])


def pop(vol=0.5):
    t = _t(0.35)
    return vol * lowpass(_rng.uniform(-1, 1, len(t)), 3) * np.exp(-t * 14)


def horn(dur=2.4, vol=0.35):
    """Hockey goal horn: a low, slightly sour sawtooth chord."""
    x = sum(saw(f, dur, vol=1) for f in (110, 138.6, 165, 220.5))
    return vol * lowpass(x, 5) / 4


def buzzer(dur=1.2, vol=0.3):
    return (square(220, dur, vol=vol) + square(233, dur, vol=vol)) / 2


def whistle(vol=0.25):
    t = _t(0.9)
    f = 2900 + 120 * np.sign(np.sin(2 * np.pi * 28 * t))
    return vol * np.sin(2 * np.pi * np.cumsum(f) / SR) * env(len(t), 0.02, 0.1)


def mix(dur, *parts):
    """parts: (offset_seconds, samples). Returns dur seconds of audio."""
    out = np.zeros(int(SR * dur))
    for off, x in parts:
        i = int(SR * off)
        if i >= len(out):
            continue
        x = x[:len(out) - i]
        out[i:i + len(x)] += x
    return out


C4, D4, E4, F4, G4, A4, B4 = 261.6, 293.7, 329.6, 349.2, 392.0, 440.0, 493.9
C5, D5, E5, G5, C6 = 523.3, 587.3, 659.3, 784.0, 1046.5
Bb3, A3, Ab3, G3 = 233.1, 220.0, 207.7, 196.0

CHARGE = [(G4, .12), (C5, .12), (E5, .12), (G5, .24), (E5, .12), (G5, .5)]
FANFARE = [(C5, .1), (C5, .1), (C5, .1), (C5, .3), (Ab3 * 2, .3), (Bb3 * 2, .3), (C5, .2), (Bb3 * 2, .1), (C5, .6)]


def _fireworks(start, n=6, gap=0.35):
    return [(start + k * gap + (k % 3) * 0.07, pop(0.5)) for k in range(n)]


def _dribbles(start, n, gap=0.24):
    return [(start + k * gap, thump(0.5, 120)) for k in range(n)]


def build():
    """Every scene's sound, keyed by the scene's sound name (anim.sequence)."""
    sad_trombone = np.concatenate([saw(f, d, vol=0.3) for f, d in ((Bb3, .45), (A3, .45), (Ab3, .45))] +
                                  [saw(G3, 1.3, vol=0.3) * (1 + 0.3 * np.sin(2 * np.pi * 6 * _t(1.3)))])
    return {
        "td": mix(3.8, (0, crowd(3.8, 0.12, 2.0)), (2.1, notes(CHARGE, vol=0.22)), (2.1, crowd(1.7, 0.3, 0.2)),
                  (2.75, thump(0.6, 70))),
        "fg": mix(3.4, (0.0, crowd(3.4, 0.1, 1.0)), (0.7, thump(0.8, 80)), (0.75, swish(0.2)),
                  (1.9, notes([(C5, .1), (E5, .1), (G5, .1), (C6, .4)], vol=0.2)), (1.9, crowd(1.5, 0.3, 0.2))),
        "hr": mix(4.0, (0, crowd(4.0, 0.08, 1)), (0.75, crack()), (0.8, crowd(3.2, 0.32, 0.4)),
                  (1.6, notes(CHARGE, wave=sine, vol=0.25)), *_fireworks(1.5)),
        "grandslam": mix(4.0, (0, crowd(4.0, 0.08, 1)), (0.75, crack()), (0.8, crowd(3.2, 0.34, 0.3)),
                         (1.6, notes(FANFARE, wave=sine, vol=0.25)), *_fireworks(1.5, 8, 0.28)),
        "walkoff": mix(4.0, (0, crowd(4.0, 0.36, 0.3)), (0.1, notes(FANFARE, wave=sine, vol=0.25)),
                       *_fireworks(0.0, 8, 0.3)),
        "goal": mix(3.6, (0, crowd(3.6, 0.1, 1)), (1.65, horn()), (1.65, crowd(1.9, 0.3, 0.2))),
        "hattrick": mix(4.2, (0, horn(1.6)), (0, crowd(4.2, 0.35, 0.3))),
        "dunk": mix(3.6, (0, crowd(3.6, 0.1, 1)), *_dribbles(0, 4), (1.5, clang()), (1.5, crowd(2.0, 0.34, 0.15))),
        "three": mix(3.6, (0, crowd(3.6, 0.1, 1)), *_dribbles(0, 3, 0.3), (1.9, swish()),
                     (1.95, crowd(1.6, 0.32, 0.2))),
        "buzzer": mix(3.6, (0, buzzer()), (0.3, crowd(3.3, 0.38, 0.2))),
        "turnover": mix(3.6, *[(k * 0.45, notes([(880, .2), (660, .2)], vol=0.15)) for k in range(4)],
                        (0, crowd(3.6, 0.18, 0.5))),
        "lead": mix(2.6, (0, notes([(G4, .12), (C5, .12), (G5, .35)], vol=0.18)), (0, crowd(2.6, 0.15, 0.5))),
        "final": mix(4.4, (0, buzzer(1.0, 0.25)), (0.2, crowd(4.0, 0.2, 0.3))),
        "victory": mix(5.6, (0, buzzer(0.8, 0.22)), (1.4, notes(FANFARE, vol=0.2)), (1.0, crowd(4.6, 0.3, 0.3)),
                       *_fireworks(1.4, 10, 0.32)),
        "sad": mix(3.4, (0.2, sad_trombone)),
        "loss": mix(3.2, (0, notes([(A3, 1.0), (F4 / 2, 1.6)], wave=saw, vol=0.15))),
        "flash": mix(1.8, *[(k * 0.4, notes([(C6, .08), (G5, .12)], vol=0.15)) for k in range(4)]),
        "safety": mix(1.8, (0, notes([(G4, .2), (E4, .2), (C4, .4)], vol=0.2)), (0, crowd(1.8, 0.2, 0.3))),
        "start": mix(3.6, (0.2, whistle()), (0.9, crowd(2.7, 0.3, 0.6)),
                     (1.3, notes([(C5, .15), (E5, .15), (G5, .3)], vol=0.18))),
        "upset": mix(3.8, *[(k * 0.6, notes([(440, .25), (0, .05), (440, .25)], vol=0.18)) for k in range(5)]),
        "break": mix(2.6, (0, notes([(C5, .15), (E5, .15), (G5, .15), (E5, .15), (G5, .5)], wave=sine, vol=0.2))),
    }


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------
ASOUNDRC = os.path.expanduser("~/.asoundrc")


def hdmi_card(cards_text=None, connected=None):
    """The ALSA card id of the HDMI port the TV is on: "vc4hdmi" on a Pi 3,
    "vc4hdmi0" or "vc4hdmi1" on a Pi 4/5 (two ports; HDMI0 is HDMI-A-1)."""
    if cards_text is None:
        try:
            with open("/proc/asound/cards") as f:
                cards_text = f.read()
        except OSError:
            return None
    ids = sorted(re.findall(r"^\s*\d+\s+\[(\S+)\s*\]:\s*vc4-hdmi", cards_text, re.M))
    if len(ids) <= 1:
        return ids[0] if ids else None
    if connected is None:
        connected = set()
        for path in glob.glob("/sys/class/drm/card*-HDMI-A-*/status"):
            try:
                with open(path) as f:
                    if f.read().strip() == "connected":
                        connected.add(int(path.rsplit("HDMI-A-", 1)[1].split("/")[0]))
            except (OSError, ValueError):
                pass
    for i, card in enumerate(ids):
        if i + 1 in connected:
            return card
    return ids[0]


def point_alsa_at_tv():
    """Writes ~/.asoundrc so the default sound device is the TV's HDMI port."""
    card = hdmi_card()
    if not card:
        return
    text = ("# Written by the ticker at startup (sfx.py): default audio = the TV over HDMI.\n"
            f'pcm.!default {{ type plug slave.pcm "hw:CARD={card},DEV=0" }}\n'
            f"ctl.!default {{ type hw card {card} }}\n")
    try:
        with open(ASOUNDRC) as f:
            if f.read() == text:
                return
    except OSError:
        pass
    with open(ASOUNDRC, "w") as f:
        f.write(text)
    print(f"[Sfx] sound goes to {card}", flush=True)


def init():
    """Starts the mixer (on the default ALSA device: the TV) and builds the
    sounds in the background. Safe to call without audio: it just stays quiet."""
    global _mixer
    try:
        point_alsa_at_tv()
    except OSError as e:
        print(f"[Sfx] couldn't pick the HDMI sound device: {e}", flush=True)
    try:
        import pygame
        pygame.mixer.pre_init(SR, -16, 2, 1024)
        pygame.mixer.init()
        pygame.mixer.set_num_channels(8)
        pygame.mixer.set_reserved(1)       # channel 0 kept for the voice
        _mixer = pygame.mixer
    except Exception as e:  # noqa: BLE001
        print(f"[Sfx] no audio: {e}", flush=True)
        return
    threading.Thread(target=_build_all, daemon=True).start()


def _build_all():
    global _ready
    t0 = time.time()
    for name, x in build().items():
        _sounds[name] = to_sound(x)
    _ready = True
    print(f"[Sfx] {len(_sounds)} sounds in {time.time() - t0:.1f}s", flush=True)


def to_sound(x):
    """Mono float samples (-1..1) -> a pygame Sound (stereo int16)."""
    a = (np.clip(x, -1, 1) * 32000).astype(np.int16)
    return _mixer.Sound(buffer=np.repeat(a[:, None], 2, axis=1).tobytes())


def quiet_now():
    h = time.localtime().tm_hour
    return settings.in_hours(h, settings.get("quiet_start"), settings.get("quiet_end"))


def volume():
    return 0.0 if quiet_now() or settings.get("muted") else settings.get("volume") / 100


def voice_channel():
    return _mixer.Channel(0) if _mixer else None


def play(name):
    if not (_ready and settings.get("sound_fx")):
        return
    snd = _sounds.get(name)
    v = volume()
    if snd is not None and v > 0:
        snd.set_volume(v)
        snd.play()


def hook(sound, ev, seg):
    """anim.Player hook: each segment's sound as it starts."""
    if sound:
        play(sound)

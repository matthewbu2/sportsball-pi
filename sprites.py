"""Original 8-bit sports sprites for the scoring cutscenes (generated from the
ESP32 Sprites.h) -- drawn in the spirit of late-80s NES football/hockey games,
not copied from any game.

Each sprite is rows of palette characters; team colors resolve at draw time:
  .  transparent        K  outline (near-black)   W  white
  J  jersey (team fill) j  jersey shade           H  helmet (team text color)
  S  skin               B  ball/brown             G  facemask/grey
  P  pants (light grey) T  stick/bat (tan)        X  puck (black)
"""

import numpy as np

FB_RUN1 = [
    "....KKKK....",
    "...KHHHHK...",
    "..KHHHHHGK..",
    "..KHHHSSGK..",
    "...KKSSSK...",
    "...KJJJJK...",
    "..KJJJJJJK..",
    ".KSKJjJJBBK.",
    ".KSKJjJJBBK.",
    "..K.KJJJJK..",
    "....KPPPPK..",
    "...KPPKPPK..",
    "..KPPK.KPPK.",
    ".KPPK...KSK.",
    ".KKK.....KKK",
    "............",
]

FB_RUN2 = [
    "............",
    "....KKKK....",
    "...KHHHHK...",
    "..KHHHHHGK..",
    "..KHHHSSGK..",
    "...KKSSSK...",
    "...KJJJJK...",
    "..KJJJJJJK..",
    "..KSJjJJBBK.",
    "..KSJjJJBBK.",
    "...KKJJJJK..",
    "....KPPPPK..",
    "....KPPPK...",
    "....KPKPK...",
    "....KSKSK...",
    "...KKKKKKK..",
]

FB_CELEBRATE = [
    ".......KBBK.",
    "......KBBBBK",
    "....KKKKBBK.",
    "...KHHHHKSK.",
    "..KHHHHHGSK.",
    "..KHHHSSGSK.",
    "...KKSSSKSK.",
    "...KJJJJJJK.",
    "..KJJJJJJK..",
    "..KSJjJJJK..",
    "..KSKJJJJK..",
    "....KPPPPK..",
    "...KPPKPPK..",
    "...KPK.KPK..",
    "...KSK.KSK..",
    "..KKKK.KKKK.",
]

FB_SPIKE = [
    "............",
    "....KKKK....",
    "...KHHHHK...",
    "..KHHHHHGK..",
    "..KHHHSSGK..",
    "...KKSSSK...",
    "...KJJJJK...",
    "..KJJJJJJK..",
    "..KJjJJJSK..",
    "..KJjJJKSK..",
    "...KJJJJKSK.",
    "....KPPPPK..",
    "...KPPKPPK..",
    "...KPK.KPK..",
    "...KSK.KSK..",
    "..KKKK.KKKK.",
]

FB_BALL = [
    ".KK.",
    "KBBK",
    "KBBK",
    ".KK.",
]

HK_SKATE1 = [
    "....KKKK......",
    "...KHHHHK.....",
    "...KHHHGK.....",
    "...KKSSSK.....",
    "...KJJJJK.....",
    "..KJJJJJJK....",
    ".KJJjJJJJJK...",
    ".KJJjJJSKTK...",
    "..KJJJJKKT....",
    "...KWWWWK.T...",
    "...KJJJJK..T..",
    "...KjK.KjK..T.",
    "..KjK...KjK.TT",
    ".KKKK...KKKK..",
    ".GGGG....GGGG.",
    "..............",
]

HK_SKATE2 = [
    "..............",
    "....KKKK......",
    "...KHHHHK.....",
    "...KHHHGK.....",
    "...KKSSSK.....",
    "...KJJJJK.....",
    "..KJJJJJJK....",
    ".KJJjJJJJJK...",
    ".KJJjJJSKTK...",
    "..KJJJJKKT....",
    "...KWWWWK.T...",
    "...KJJJJK..T..",
    "....KjjK....T.",
    "....KjjK....TT",
    "...KKKKKK.....",
    "...GGGGGG.....",
]

HK_CELEBRATE = [
    "..........T...",
    ".........T....",
    "....KKKKT.....",
    "...KHHHHK.....",
    "...KHHHGK.....",
    "...KKSSSKS....",
    "...KJJJJKS....",
    "..KJJJJJJK....",
    ".KJJjJJJJK....",
    "..KJjJJJK.....",
    "...KWWWWK.....",
    "...KJJJJK.....",
    "...KjK.KjK....",
    "..KjK...KjK...",
    ".KKKK...KKKK..",
    ".GGGG...GGGG..",
]

KB_WALK1 = [
    "....KKKK....",
    "...KHHHHK...",
    "...KHHHHK...",
    "...KHHHHK...",
    "....KSSK....",
    "..KKJJJJKK..",
    ".KJJJWWJJJK.",
    ".KJJWJJWJJK.",
    "KSKJJJWJJKSK",
    "KSKJJWJJJKSK",
    ".K.KJWWWJK.K",
    "....KPPPPK..",
    "...KPPKPPK..",
    "...KPK.KPK..",
    "..KJK...KJK.",
    "..KKK...KKK.",
]

KB_WALK2 = [
    "............",
    "....KKKK....",
    "...KHHHHK...",
    "...KHHHHK...",
    "...KHHHHK...",
    "....KSSK....",
    "..KKJJJJKK..",
    ".KJJJWWJJJK.",
    ".KJJWJJWJJK.",
    "KSKJJJWJJKSK",
    "KSKJJWJJJKSK",
    ".K.KJWWWJK.K",
    "....KPPPPK..",
    "....KPKPK...",
    "....KJKJK...",
    "....KKKKK...",
]

KB_KICK = [
    "....KKKK....",
    "...KHHHHK...",
    "...KHHHHK...",
    "...KHHHHK...",
    "....KSSK....",
    "..KKJJJJKK..",
    ".KJJJWWJJJK.",
    "KSJJWJJWJJK.",
    "KSKJJJWJJKSK",
    ".K.JJWJJJKSK",
    "...KJWWWJK.K",
    "...KPPPPKJK.",
    "...KPPKKJK..",
    "...KPK.KK...",
    "..KJK.......",
    "..KKK.......",
]

KB_HOLDER = [
    "....KKKK....",
    "...KHHHHK...",
    "...KHHHHK...",
    "....KSSK....",
    "..KKJJJJKK..",
    ".KJJJWWJJJK.",
    ".KJJWJJWJJK.",
    ".KJJJJWJJJK.",
    "..KJJWJJJSK.",
    "..KPPPPPPK..",
    ".KPPPKKPPPK.",
    ".KJJK..KJJK.",
]

REF_GOOD = [
    "K.....K",
    "W.....W",
    "W.....W",
    "KW...WK",
    ".KKKKK.",
    ".WKWKW.",
    ".KWKWK.",
    ".WKWKW.",
    "..KKK..",
    "..K.K..",
    ".KK.KK.",
]

BAT_STANCE = [
    "..........T..",
    "..........T..",
    "....KKKK..T..",
    "...KHHHHK.T..",
    "...KHHHHHHT..",
    "...KSSSSK.T..",
    "....KSSK..T..",
    "...KJJJJKSS..",
    "..KJJjJJJSK..",
    "..KJJjJJJK...",
    "...KJJJJK....",
    "...KPPPPK....",
    "..KPPKKPPK...",
    "..KPK..KPK...",
    ".KPK....KPK..",
    ".KKK....KKK..",
]

BAT_SWING = [
    ".............",
    ".............",
    "....KKKK.....",
    "...KHHHHK....",
    "...KHHHHHHK..",
    "...KSSSSK....",
    "....KSSK.....",
    "...KJJJJKSSTTTTTTT",
    "..KJJjJJJJK..",
    "..KJjJJJJK...",
    "...KJJJJK....",
    "...KPPPPK....",
    "..KPPKKPPK...",
    "..KPK..KPK...",
    ".KPK....KPK..",
    ".KKK....KKK..",
]

BAT_TROT = [
    "..........S..",
    ".........KSK.",
    "....KKKK.KSK.",
    "...KHHHHKKSK.",
    "...KHHHHHHK..",
    "...KSSSSK....",
    "....KSSK.....",
    "...KJJJJJK...",
    "..KJJjJJJK...",
    "..KSJjJJK....",
    "..KSKJJK.....",
    "...KPPPPK....",
    "...KPKPK.....",
    "...KPKPK.....",
    "...KPKPK.....",
    "..KKK.KKK....",
]

PITCHER = [
    "...KKK....",
    "..KHHHK...",
    "..KHHHHHK.",
    "..KSSSK...",
    "...KSK....",
    "..KJJJK...",
    ".KJJJJJK..",
    ".KSJJJJSK.",
    "..KJJJJK..",
    "..KPPPPK..",
    ".KPPKPPK..",
    ".KPK.KPK..",
    ".KPK.KPK..",
    "KKK..KKK..",
]

BB_RUN1 = [
    "....KKKK....",
    "...KSSSSK...",
    "...KSSSSK...",
    "....KSSK....",
    "...KJJJJK...",
    "..KJJWJJJK..",
    ".KSKJJJJKSK.",
    ".KSKJJJJKSK.",
    "..K.KjjjjK.K",
    "....KjjjjK..",
    "...KSK.KSK..",
    "..KSK...KSK.",
    "..KSK...KSK.",
    ".KWWK...KWWK",
    ".KKK.....KKK",
]

BB_RUN2 = [
    "............",
    "....KKKK....",
    "...KSSSSK...",
    "...KSSSSK...",
    "....KSSK....",
    "...KJJJJK...",
    "..KJJWJJJK..",
    ".KSKJJJJKSK.",
    ".KSKJJJJKSK.",
    "..K.KjjjjK.K",
    "....KjjjjK..",
    "....KSKSK...",
    "....KSKSK...",
    "...KWWKWWK..",
    "...KKK.KKK..",
]

BB_JUMP = [
    ".KSK....KSK.",
    ".KSK....KSK.",
    "..KSKKKKSK..",
    "...KSSSSK...",
    "...KSSSSK...",
    "....KSSK....",
    "...KJJJJK...",
    "..KJJWJJJK..",
    "..KJJJJJJK..",
    "...KjjjjK...",
    "...KjjjjK...",
    "...KSK.KSK..",
    "..KSK...KSK.",
    "..KWWK..KWWK",
    "..KKK....KKK",
]


_FIXED = {
    "K": (8, 8, 8), "W": (255, 255, 255), "S": (232, 176, 128), "B": (150, 75, 25),
    "G": (150, 150, 150), "P": (210, 210, 210), "T": (200, 160, 90), "X": (8, 8, 8),
}


def shade(c):
    """~60% brightness."""
    return tuple(v * 3 // 5 for v in c)


class Pal:
    def __init__(self, jersey, helmet):
        self.colors = dict(_FIXED, J=jersey, j=shade(jersey), H=helmet)
        self.key = (jersey, helmet)


_cache = {}


def _compiled(rows, pal, flip):
    key = (id(rows), pal.key, flip)
    hit = _cache.get(key)
    if hit is None:
        w = max(len(r) for r in rows)
        img = np.zeros((len(rows), w, 3), dtype=np.uint8)
        mask = np.zeros((len(rows), w), dtype=bool)
        for y, row in enumerate(rows):
            row = row.ljust(w, ".")
            if flip:
                row = row[::-1]
            for x, ch in enumerate(row):
                if ch != ".":
                    img[y, x] = pal.colors[ch]
                    mask[y, x] = True
        hit = _cache[key] = (img, mask)
    return hit


def draw(cv, x, y, rows, pal, flip=False):
    """Sprite with its top-left at (x, y); flip mirrors it to face left."""
    img, mask = _compiled(rows, pal, flip)
    h, w = mask.shape
    x1, y1 = max(x, cv.clip_l), max(y, 0)
    x2, y2 = min(x + w, cv.clip_r), min(y + h, cv.h)
    if x1 >= x2 or y1 >= y2:
        return
    m = mask[y1 - y:y2 - y, x1 - x:x2 - x]
    cv.fb[y1:y2, x1:x2][m] = img[y1 - y:y2 - y, x1 - x:x2 - x][m]

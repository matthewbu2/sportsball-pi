"""Made-up live play-by-play for hearing the announcer (web remote button).

Three football matchups from the schedule (favorites first) become live
games with invented drives; every PLAY_EVERY seconds the next game gets a
new play, so plays change both between cards and while a card is up. The
crawl shows only these games for LENGTH seconds, then the real scores come
back. No scenes fire: the demo's games skip the moment detector.
"""

import dataclasses
import time

from espn import IN

LENGTH = 180
PLAY_EVERY = 7          # seconds between plays (rotating through the games)
ROUNDUP_AT = 100        # an around-the-league roundup this far in

_ROSTERS = [
    {"QB": "J.Carter", "RB": "D.Reyes", "WR": "M.Ellis", "TE": "B.Lund", "K": "C.Ortiz", "DEF": "T.Brooks"},
    {"QB": "R.Vaughn", "RB": "K.Mason", "WR": "A.Price", "TE": "S.Holt", "K": "P.Dunn", "DEF": "L.Grant"},
    {"QB": "N.Fisher", "RB": "E.Banks", "WR": "J.Wade", "TE": "O.Kemp", "K": "G.Shaw", "DEF": "W.Pierce"},
]

# (play text, down & distance after it, yard line "D 34" = the defense's 34,
#  points scored by the offense). {QB} etc. come from the offense's roster,
# {DEF} from the defense's; {O}/{D} are the team abbreviations.
_DRIVE = [
    ("{QB} pass short right to {WR} to the {O} 41 for 16 yards ({DEF}).", "1ST & 10", "O 41", 0),
    ("{RB} up the middle to the {O} 45 for 4 yards ({DEF}).", "2ND & 6", "O 45", 0),
    ("(Shotgun) {QB} pass deep left to {WR} to the {D} 22 for 33 yards ({DEF}).", "1ST & 10", "D 22", 0),
    ("{RB} left end to the {D} 17 for 5 yards ({DEF}).", "2ND & 5", "D 17", 0),
    ("{QB} sacked at the {D} 24 for -7 yards ({DEF}).", "3RD & 12", "D 24", 0),
    ("(Shotgun) {QB} scrambles right end pushed ob at the {D} 9 for 15 yards ({DEF}).", "1ST & GOAL", "D 9", 0),
    ("{QB} pass short middle to {TE} for 9 yards, TOUCHDOWN.", "", "", 6),
    ("{K} extra point is GOOD.", "", "", 1),
]
_DRIVE_STALLS = [
    ("{RB} right tackle to the {O} 31 for 6 yards ({DEF}).", "2ND & 4", "O 31", 0),
    ("(Shotgun) {QB} pass incomplete deep right to {WR}, broken up by {DEF}.", "3RD & 4", "O 31", 0),
    ("(Shotgun) {QB} pass short left to {TE} to the {O} 38 for 7 yards ({DEF}).", "1ST & 10", "O 38", 0),
    ("{RB} up the middle to the {D} 47 for 15 yards ({DEF}).", "1ST & 10", "D 47", 0),
    ("{QB} pass incomplete short right to {WR}.", "2ND & 10", "D 47", 0),
    ("{QB} pass short left to {RB} to the {D} 30 for 17 yards ({DEF}).", "1ST & 10", "D 30", 0),
    ("{RB} left guard to the {D} 28 for 2 yards ({DEF}).", "2ND & 8", "D 28", 0),
    ("{K} 45 yard field goal is GOOD.", "", "", 3),
]


class LiveDemo:
    def __init__(self, games):
        self.source = games
        pick = sorted((g for g in games if g.sport_idx in (1, 5)), key=lambda g: (not g.fav_mask, g.date))
        self.games = []
        for i, g in enumerate(pick[:3]):
            q = 2 + i % 2
            self.games.append(dataclasses.replace(
                g, state=IN, away_score=str(10 + 4 * i), home_score=str(14 - i), period=q, clock_sec=540 - 70 * i,
                possession=1 + i % 2, play_text="", play_id="", last_play=0, away_to=3, home_to=2 - i % 2,
                leaders=[]))
        self.step = [0] * len(self.games)
        self.start = time.monotonic()
        self.next_at = self.start
        self.turn = 0
        self.roundup_done = False
        for i in range(len(self.games)):    # every card has a play from the start
            self._play(i)

    @property
    def done(self):
        return not self.games or time.monotonic() - self.start > LENGTH

    def roundup_due(self):
        if not self.roundup_done and time.monotonic() - self.start > ROUNDUP_AT:
            self.roundup_done = True
            return True
        return False

    def update(self):
        """The demo's games when a new play happened, else None."""
        if time.monotonic() < self.next_at:
            return None
        self.next_at = time.monotonic() + PLAY_EVERY
        self._play(self.turn % len(self.games))
        self.turn += 1
        return list(self.games)

    def _play(self, i):
        g = self.games[i]
        drive = _DRIVE if i != 1 else _DRIVE_STALLS
        text, dd, spot, pts = drive[self.step[i] % len(drive)]
        self.step[i] += 1
        off, dfn = (g.away_abbr, g.home_abbr) if g.possession == 1 else (g.home_abbr, g.away_abbr)
        names = dict(_ROSTERS[i])
        names["DEF"] = _ROSTERS[(i + 1) % 3]["DEF"]
        text = text.format(O=off, D=dfn, **names)
        side = "away" if g.possession == 1 else "home"
        changes = {"play_text": text, "play_id": f"demo{i}-{self.step[i]}", "down_dist": dd,
                   "poss_text": spot.replace("O ", off + " ").replace("D ", dfn + " "),
                   "yards_to_goal": int(spot[2:]) if spot.startswith("D") else 100 - int(spot[2:]) if spot else -1,
                   "clock_sec": max(0, g.clock_sec - 38)}
        changes["is_red_zone"] = 0 <= changes["yards_to_goal"] <= 20
        if pts:
            changes[f"{side}_score"] = str(int(getattr(g, f"{side}_score")) + pts)
        if pts in (1, 3):     # kick: the other team gets the ball
            changes["possession"] = 3 - g.possession
            changes["down_dist"], changes["poss_text"] = "1ST & 10", (dfn + " 25")
            changes["yards_to_goal"] = 75
        m, s = divmod(changes["clock_sec"], 60)
        changes["status_text"] = f"Q{g.period} {m}:{s:02d}"
        self.games[i] = dataclasses.replace(g, **changes)

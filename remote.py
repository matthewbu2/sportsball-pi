"""Web remote on port 8080: http://sportsball.local:8080/

A phone-friendly page with three tabs:
  Demos     a button per scene
  Control   pin/skip the card on screen, TV on/off (CEC), test the voice
  Settings  teams, favorites, brightness and night dimming, sound effects,
            announcer, volume, quiet hours, TV power -- saved to config.json
            and applied live

/snap.png is the current frame (for checking the screen remotely).
Requests land on the HTTP thread; anything that touches the crawl is
queued and run on the frame loop (run_pending).
"""

import io
import json
import os
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anim as A
import settings
import style
from espn import IN, SPORT_LABELS

PORT = 8080
HERE = os.path.dirname(os.path.abspath(__file__))
_pending = []
_lock = threading.Lock()
_crawl = None
_snap_want = threading.Event()   # /snap.png asks the frame loop for a finished frame
_snap_done = threading.Event()
_snap = None                     # (framebuffer, QR overlays)

_GROUPS = [
    ("Scores", ["td", "fg", "safety", "two", "hr", "grandslam", "walkoff", "goal", "hattrick", "dunk", "three",
                "buzzer"]),
    ("Moments", ["int", "fumble", "lead", "start", "upset", "break", "final"]),
    ("My teams", ["sad", "short", "victory", "loss"]),
]
_LABELS = {"td": "Touchdown", "fg": "Field goal", "safety": "Safety", "two": "Two-point", "hr": "Home run",
           "grandslam": "Grand slam", "walkoff": "Walk-off", "goal": "Hockey goal", "hattrick": "Hat trick",
           "dunk": "Slam dunk", "three": "Three", "buzzer": "Buzzer beater", "int": "Interception",
           "fumble": "Fumble", "lead": "Lead change", "start": "Game start", "upset": "Upset alert",
           "break": "Halftime roundup", "final": "Final", "sad": "Opponent scores", "short": "Short flash",
           "victory": "Victory", "loss": "Loss"}

_CARD_NAMES = ["header", "game", "nodata", "msg", "live", "favs", "nextup", "ranks", "standings", "news", "remote", "setup"]

FLAVOR = "8-BIT"
FEATURES = [   # (setting, label, on = setting True)  -- switches on the Features tab
    ("muted", "Sound", False),
    ("announcer", "Announcer voice", True),
    ("roundups", "Around-the-league score roundups", True),
    ("pregame", "Announce favorites' kickoffs", True),
    ("red_zone", "Jump to red-zone drives", True),
    ("saver", "Overnight screensaver", True),
]


def _page_html():
    """The remote page (web/remote.html, read each time so edits show on refresh)."""
    with open(os.path.join(HERE, "web", "remote.html"), encoding="utf-8") as f:
        return f.read()


_SETUP = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Sportsball setup</title>
<style>
:root{--bg:#0b0d12;--card:#151a26;--line:#2c3448;--fg:#e8e8e8;--dim:#8a93a6;--acc:#ffd400}
*{box-sizing:border-box}body{background:var(--bg);color:var(--fg);font:17px system-ui,sans-serif;margin:0 auto;padding:18px;max-width:560px}
h1{font-size:21px;color:var(--acc);letter-spacing:.06em;margin:0}h2{font-size:22px;margin:18px 0 6px}
.dim{color:var(--dim);font-size:15px}.steps{color:var(--dim);font-size:14px;margin-top:2px}
.opt{display:flex;align-items:center;gap:12px;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:8px 0}
.opt input{width:22px;height:22px;accent-color:var(--acc)}.opt b{display:block}.opt span{color:var(--dim);font-size:14px}
input[type=search],#au,#ap,#ap2,#tn{width:100%;font-size:17px;padding:12px;border-radius:10px;border:1px solid var(--line);background:#1b2030;color:#fff;margin:8px 0}
#list{max-height:46vh;overflow-y:auto;border:1px solid var(--line);border-radius:12px}
.t{display:flex;align-items:center;gap:10px;padding:10px 12px;border-bottom:1px solid var(--line)}.t img{width:32px;height:32px}
.t .s{margin-left:auto;color:var(--dim);font-size:13px}.t.on{background:#2a2a10}.t.on .s{color:var(--acc)}
#chips{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0;min-height:10px}.chip{background:var(--acc);color:#000;border-radius:16px;padding:5px 12px;font-size:15px}
.nav{display:flex;gap:10px;margin-top:18px}button{flex:1;font-size:17px;padding:14px;border-radius:12px;border:1px solid var(--line);background:#1b2030;color:#fff}
button.go{background:var(--acc);color:#000;font-weight:600}.step{display:none}.step.on{display:block}
a{color:var(--acc)}
</style></head><body><h1>SPORTSBALL</h1><div class=steps id=steps></div>

<div class="step on"><div id=lookstep hidden><h2>Pick a look</h2><p class=dim>You can switch any time on the remote page.</p>
<label class=opt><input type=radio name=look value=hd><div><b>HD broadcast</b><span>A TV-network look: 3D court, field and rink with every play drawn on it, standings and news</span></div></label>
<label class=opt><input type=radio name=look value=classic><div><b>Classic 8-bit</b><span>The retro LED scoreboard: scrolling scores and Tecmo-style animations</span></div></label></div>
<h2>Name this ticker</h2><p class=dim>Its address on your network. Each ticker in the house needs its own name, like <i>den</i> or <i>basement</i>.</p>
<input id=tn autocapitalize=none autocomplete=off spellcheck=false oninput="tnShow()"><p class=dim id=tnp></p>
<h2>What do you want to follow?</h2><p class=dim>Every game of each sport you pick shows on the ticker. Sports out of season just stay quiet.</p>
<div id=sports></div><div class=nav><button class=go onclick="next()">Next</button></div></div>

<div class=step><h2>Your favorite teams</h2><p class=dim>Favorites get their own section, a countdown to the next game, full celebrations when they score, and the TV can switch on before they play. Tap to add or remove. You can skip this.</p>
<div id=chips></div><input type=search id=q placeholder="Search teams" oninput="draw()"><div id=list><p class=dim style="padding:12px">Loading teams...</p></div>
<div class=nav><button onclick="back()">Back</button><button class=go onclick="next()">Next</button></div></div>

<div class=step><h2>Sound</h2><p class=dim>It plays through the TV speakers.</p>
<label class=opt><input type=radio name=snd value=all checked><div><b>Effects and announcer</b><span>Crowd noise and a voice reading the plays</span></div></label>
<label class=opt><input type=radio name=snd value=fx><div><b>Effects only</b><span>Crowd noise and horns, no voice</span></div></label>
<label class=opt><input type=radio name=snd value=off><div><b>No sound</b><span>You can turn it on later</span></div></label>
<h2>How do you watch games?</h2><p class=dim>So a touchdown shows on the ticker when you see it on TV, not before.</p>
<label class=opt><input type=radio name=dly value=10 checked><div><b>Cable or antenna</b><span>About 10 seconds behind</span></div></label>
<label class=opt><input type=radio name=dly value=45><div><b>Streaming (YouTube TV, Hulu, Peacock...)</b><span>About 45 seconds behind</span></div></label>
<label class=opt><input type=radio name=dly value=0><div><b>I'm not watching along</b><span>Scores as fast as they come</span></div></label>
<div class=nav><button onclick="back()">Back</button><button class=go id=fin onclick="soundNext()">Next</button></div></div>

<div class=step><h2>Your login (optional)</h2><p class=dim>For tinkering: a user name and password to log in to the ticker over SSH (<code>ssh you@<span class=hn>@HOST_ONLY@</span></code>). It's an admin account, so pick a good password. It stays on the ticker; nobody else gets it. You can only make it now, during setup. Most people skip this.</p>
<input id=au placeholder="User name (lowercase)" autocapitalize=none autocomplete=off spellcheck=false>
<input id=ap type=password placeholder="Password (8 or more characters)" autocomplete=new-password>
<input id=ap2 type=password placeholder="Password again" autocomplete=new-password>
<p class=dim id=aerr style="color:#ff7a7a"></p>
<div class=nav><button onclick="back()">Back</button><button onclick="finish()">Skip</button><button class=go id=mk onclick="makeLogin()">Create</button></div></div>

<div class=step id=done><h2>All set!</h2><p>Look at the TV: your games are coming up. Scores update every few seconds.</p>
<p class=dim>Change anything later on the remote page, <a href="/">@HOST@</a>. Bookmark it, or scan the code on the ticker's REMOTE card.</p><p class=dim id=made></p></div>

<script>
const SPORTS=[[1,'NFL','Pro football'],[5,'College football','Top 25 and your favorites'],[3,'MLB','Baseball'],[2,'NBA','Pro basketball'],[4,'NHL','Hockey'],[6,'College basketball','Men’s']];
let cfg={},teams={},favs=[],step=0,login=false;
const DONE=4;
const $=id=>document.getElementById(id);
function steps(){const n=login?4:3;$('steps').textContent=step<DONE?'Step '+(step+1)+' of '+n:'';document.querySelectorAll('.step').forEach((s,i)=>s.classList.toggle('on',i==step));scrollTo(0,0)}
function chosen(){return [...document.querySelectorAll('#sports input:checked')].map(i=>Number(i.value))}
function slug(v){return v.toLowerCase().trim().replace(/[^a-z0-9]+/g,'-').replace(/^-+|-+$/g,'').slice(0,32)}
function tnShow(){const s=slug($('tn').value);$('tnp').textContent=s?'http://'+s+'.local:@PORT@':'Pick a name';
 if(s)document.querySelectorAll('.hn').forEach(e=>e.textContent=s+'.local')}
let host='';
function next(){if(step==0){if(!chosen().length)return alert('Pick at least one sport');if(!slug($('tn').value))return alert('Pick a name');loadTeams()}step++;steps()}
function back(){step--;steps()}
function loadTeams(){Promise.all(chosen().map(s=>teams[s]?null:fetch('/teams?sport='+s).then(r=>r.json()).then(l=>teams[s]=l).catch(()=>teams[s]=[]))).then(draw)}
function draw(){const q=$('q').value.trim().toLowerCase(),seen=new Set(),rows=[];
 for(const s of chosen())for(const t of teams[s]||[]){if(seen.has(t.name))continue;seen.add(t.name);
  if(!q||t.name.toLowerCase().includes(q)||t.abbr.toLowerCase()==q)rows.push([s,t])}
 const order=s=>SPORTS.findIndex(x=>x[0]==s);
 rows.sort((a,b)=>(favs.includes(b[1].name)-favs.includes(a[1].name))||order(a[0])-order(b[0])||a[1].name.localeCompare(b[1].name));
 const L=$('list');L.innerHTML='';if(!rows.length)L.innerHTML='<p class=dim style="padding:12px">No teams found</p>';
 rows.slice(0,150).forEach(([s,t])=>{const d=document.createElement('div');d.className='t'+(favs.includes(t.name)?' on':'');
  const img=document.createElement('img');img.src=t.logo||'';img.alt='';img.loading='lazy';const n=document.createElement('span');n.textContent=t.name;
  const st=document.createElement('span');st.className='s';st.textContent=favs.includes(t.name)?'FAVORITE':SPORTS.find(x=>x[0]==s)[1];
  d.append(img,n,st);d.onclick=()=>{favs=favs.includes(t.name)?favs.filter(f=>f!=t.name):favs.concat(t.name);draw()};L.appendChild(d)});
 $('chips').innerHTML='';favs.forEach(f=>{const c=document.createElement('span');c.className='chip';c.textContent=f;$('chips').appendChild(c)})}
function soundNext(){if(login){step=3;steps()}else finish()}
function makeLogin(){const u=$('au').value.trim(),p=$('ap').value;$('aerr').textContent='';
 if(!/^[a-z][a-z0-9_-]{0,30}$/.test(u))return $('aerr').textContent='User name: lowercase letters and numbers, starting with a letter.';
 if(p.length<8)return $('aerr').textContent='The password needs at least 8 characters.';
 if(p!=$('ap2').value)return $('aerr').textContent='The two passwords are different.';
 $('mk').disabled=true;$('mk').textContent='Creating...';
 fetch('/account',{method:'POST',body:JSON.stringify({user:u,password:p})}).then(r=>r.json()).then(r=>{
  $('ap').value=$('ap2').value='';
  if(!r.ok){$('aerr').textContent=r.error;$('mk').disabled=false;$('mk').textContent='Create';return}
  $('made').textContent='Your login: ssh '+r.user+'@'+(slug($('tn').value)||host)+'.local';finish()})
 .catch(()=>{$('aerr').textContent='Could not reach the ticker. Try again.';$('mk').disabled=false;$('mk').textContent='Create'})}
function finish(){const n=slug($('tn').value);
 if(n&&n!=host){fetch('/name',{method:'POST',body:JSON.stringify({name:n})}).then(r=>r.json()).then(r=>{
  if(!r.ok)return alert(r.error);host=n;const u='http://'+n+'.local:@PORT@';const a=document.querySelector('#done a');a.href=u;a.textContent=n+'.local:@PORT@';saveAll()})
  .catch(()=>alert('Could not reach the ticker. Try again.'));return}
 saveAll()}
let lookNow='';
fetch('/status').then(r=>r.json()).then(s=>{const ls=s.looks||[];if(ls.length<2)return;$('lookstep').hidden=false;
 const cur=ls.find(l=>l[2]);lookNow=cur?cur[0]:'';const i=document.querySelector('input[name=look][value="'+lookNow+'"]');if(i)i.checked=true}).catch(()=>{});
function pickLook(){const i=document.querySelector('input[name=look]:checked');if(!i||!lookNow||i.value==lookNow)return;
 fetch('/style/'+i.value,{method:'POST'}).then(()=>{const p=document.createElement('p');p.className='dim';
  p.textContent='Switching the TV to the '+(i.value=='hd'?'HD broadcast':'Classic 8-bit')+' look. It takes about 20 seconds.';$('done').appendChild(p)})}
function saveAll(){const snd=document.querySelector('input[name=snd]:checked').value;
 const o={teams:chosen().map(s=>[s,'']),favorites:favs.join(', '),sound_fx:snd!='off',announcer:snd=='all',muted:false,
  delay:Number(document.querySelector('input[name=dly]:checked').value),setup_done:true};
 $('fin').disabled=true;fetch('/settings',{method:'POST',body:JSON.stringify(o)}).then(r=>{if(!r.ok)throw 0;step=DONE;steps();pickLook()})
  .catch(()=>{alert('Could not save. Is the ticker still on?');$('fin').disabled=false})}
fetch('/account').then(r=>r.json()).then(a=>{login=a.available;steps()}).catch(()=>{});
fetch('/name').then(r=>r.json()).then(n=>{host=n.name;$('tn').value=n.suggest||n.name;tnShow()}).catch(()=>{});
fetch('/settings').then(r=>r.json()).then(c=>{cfg=c;const have=new Set(c.teams.map(t=>t[0]));favs=(c.favorites||'').split(',').map(s=>s.trim()).filter(Boolean);
 $('sports').innerHTML=SPORTS.map(([v,n,d])=>'<label class=opt><input type=checkbox value='+v+(have.has(v)?' checked':'')+'><div><b>'+n+'</b><span>'+d+'</span></div></label>').join('');
 const dl=document.querySelector('input[name=dly][value="'+c.delay+'"]');if(dl)dl.checked=true;
 const sv=c.muted||!c.sound_fx?'off':c.announcer?'all':'fx';document.querySelector('input[name=snd][value='+sv+']').checked=true;steps()})
</script></body></html>"""

_teams_cache = {}
ACCOUNT_DIR = "/run/sportsball/account"    # handed to the root side (account.py)


def _account_state():
    try:
        with open(os.path.join(ACCOUNT_DIR, "state.json")) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _account_available():
    """The login step is offered once: during first setup, while nobody can log in yet."""
    return bool(_account_state().get("available")) and not settings.get("setup_done")


def _root_request(req):
    """Hands req to the root side (account.py) and waits for its answer."""
    rid = os.urandom(8).hex()
    tmp = os.path.join(ACCOUNT_DIR, "request.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({**req, "id": rid}, f)
    os.replace(tmp, os.path.join(ACCOUNT_DIR, "request.json"))
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(0.5)
        res = _account_state().get("result") or {}
        if res.get("id") == rid:
            return {k: v for k, v in res.items() if k != "id"}
    return {"ok": False, "error": "No answer from the ticker. Try again in a minute."}


def _make_account(user, password):
    if not _account_available():
        return {"ok": False, "error": "This ticker already has a login, or setup is finished."}
    return _root_request({"user": user, "password": password})


def _names():
    """This ticker's name, and the one to suggest: if another ticker already
    answers to it, avahi has fallen back to name-2 here (see wifi_setup.py)."""
    name = socket.gethostname()
    r = subprocess.run(["busctl", "call", "org.freedesktop.Avahi", "/", "org.freedesktop.Avahi.Server",
                        "GetHostName"], capture_output=True, text=True)
    mdns = r.stdout.split('"')[1] if r.returncode == 0 and '"' in r.stdout else name
    return {"name": name, "suggest": mdns}


def _teams(sport):
    """[{name, abbr, logo}] of every team in a sport (ESPN), cached."""
    if sport not in _teams_cache:
        from espn import SESSION, SPORT_PATHS
        url = f"https://site.api.espn.com/apis/site/v2/sports/{SPORT_PATHS[sport]}/teams"
        d = SESSION.get(url, params={"limit": 1000}, timeout=15).json()
        out = []
        for t in d["sports"][0]["leagues"][0]["teams"]:
            t = t["team"]
            logo = next((l["href"] for l in t.get("logos") or [] if l.get("href")), "")
            out.append({"name": t.get("displayName", ""), "abbr": t.get("abbreviation", ""), "logo": logo})
        _teams_cache[sport] = out
    return _teams_cache[sport]


def _ago(t):
    if not t:
        return "never"
    s = int(time.time() - t)
    return f"{s}s ago" if s < 120 else f"{s // 60} min ago" if s < 7200 else f"{s // 3600} h ago"


_sys_cache = (0.0, [])


def _system_rows():
    """Slow-changing rows (timers, disk, last OS update), refreshed every minute."""
    global _sys_cache
    if time.time() - _sys_cache[0] < 60:
        return _sys_cache[1]
    rows = []
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            c = int(f.read()) / 1000
        rows.append(("CPU temperature", f"{c:.0f} C", c < 75))
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        rows.append(("Up since boot", f"{int(up // 3600)} h {int(up % 3600 // 60)} min", True))
    except (OSError, ValueError):
        pass
    st = os.statvfs("/")
    free = st.f_bavail * st.f_frsize / 1e9
    rows.append(("SD card free", f"{free:.1f} GB", free > 1))
    try:
        with open("/var/log/sportsball-update.log") as f:
            tail = f.read()[-4000:].strip().splitlines()
        ex = next((l for l in reversed(tail) if l.startswith("exit ")), "")
        rows.append(("Last system update", ex.replace("exit 0", "OK").replace("exit ", "failed, code ") or "running",
                     ex in ("", "exit 0")))
    except OSError:
        rows.append(("Last system update", "not yet", True))
    for scope, unit, label in (([], "sportsball-update.timer", "Next system update"),
                               ([], "sportsball-reboot.timer", "Next reboot"),
                               (["--user"], "sportsball-selfupdate.timer", "Next ticker update")):
        out = subprocess.run(["systemctl", *scope, "show", unit, "-p", "NextElapseUSecRealtime", "--value"],
                             capture_output=True, text=True).stdout.strip()
        rows.append((label, out[4:20] if out else "not scheduled", bool(out)))
    _sys_cache = (time.time(), rows)
    return rows


def _health():
    import announcer
    import extras
    import updater
    import weather
    f = _crawl.fetcher
    rows = []
    if f is not None:
        fresh = time.time() - f.last_ok < 15 * 60
        rows.append(("Scores", f"updated {_ago(f.last_ok)}", fresh))
        if f.last_error:
            rows.append(("Last score error", f.last_error, fresh))
    rows.append(("Standings, rankings, news", f"updated {_ago(extras.updated_at)}"
                 + (f" ({extras.last_error})" if extras.last_error else ""), time.time() - extras.updated_at < 3600))
    rows.append(("Weather", f"updated {_ago(weather.updated_at)}", time.time() - weather.updated_at < 3600))
    v = announcer.voice_state()
    rows.append(("Announcer", v, v != "offline voice (Piper)"))
    delay = settings.get("delay")
    rows.append(("Broadcast delay", f"{delay} s" if delay else "off", True))
    fps = getattr(_crawl, "fps", None)
    if fps:
        rows.append(("Frame rate", f"{fps[0]:.0f} fps, slowest frame {fps[1]:.0f} ms", fps[0] >= 25))
    rows.append(("Version", updater.current_version(), True))
    return rows + _system_rows()


def _page():
    # Only demos for the sports the ticker follows (football-only in the
    # fall, hoops and hockey in the winter).
    sports = {s for s, _q in settings.get("teams")}
    parts = []
    for title, names in _GROUPS:
        names = [n for n in names if sports & set(A.SPORT_FAMILY[A.DEMOS[n][0]])]
        btns = "".join(f'<button onclick="go(\'{n}\')">{_LABELS[n]}</button>' for n in names)
        if btns:
            parts.append(f"<h2>{title}</h2><div class=g>{btns}</div>")
    import announcer
    voices = "".join(f'<option value="{v}">{label}</option>' for v, label in announcer.VOICES.items())
    return _page_html().replace("@FLAVOR@", FLAVOR).replace("@DEMOS@", "".join(parts)).replace("@VOICES@", voices).replace("@SPORTS@", json.dumps(list(SPORT_LABELS))).encode()


class _Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj):
        self._send(200, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            self._send(200, _page(), "text/html; charset=utf-8")
        elif self.path.startswith("/snap.png"):
            from PIL import Image
            from display import expand
            buf = io.BytesIO()
            # Copying the framebuffer from this thread could catch it mid-draw
            # (cards just cleared); take_snapshot() hands over a whole frame.
            shot = _frame()
            if not shot:
                return self._send(503, b"busy")
            fb, overlays = shot
            Image.fromarray(expand(fb, 4, overlays=overlays)).save(buf, "PNG")
            self._send(200, buf.getvalue(), "image/png")
        elif self.path in ("/status", "/status?health"):
            import cec
            games = _crawl.games
            cur = _crawl.player.current
            playing = None
            if cur is not None:
                playing = f"{A.abbr_of(cur.game, cur.home)} {A.KIND_NAMES[cur.kind]}"
            import updater
            if "health" in self.path:
                return self._json(_health())
            self._json({"games": len(games), "live": sum(1 for g in games if g.state == IN), "playing": playing,
                        "pinned": _crawl.pinned, "cec": cec.state, "version": updater.current_version(),
                        "muted": settings.get("muted"), "looks": style.labels(),
                        "now": _crawl.now_info() if hasattr(_crawl, "now_info") else None,
                        "features": [(k, label, bool(settings.get(k)) == on) for k, label, on in FEATURES],
                        "card": f"{_CARD_NAMES[_crawl.cards[_crawl.cur].type]} {_crawl.cur + 1}/{len(_crawl.cards)}"})
        elif self.path == "/board":
            self._json(_crawl.board() if hasattr(_crawl, "board") else [])
        elif self.path.startswith("/font/"):
            name = os.path.basename(self.path[6:])
            path = os.path.join(HERE, "assets", "fonts", name)
            if not name.endswith(".ttf") or not os.path.isfile(path):
                return self._send(404, b"no")
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "font/ttf")
            self.send_header("Cache-Control", "max-age=604800")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/settings":
            self._json(settings.snapshot())
        elif self.path == "/account":
            self._json({"available": _account_available()})
        elif self.path == "/name":
            self._json(_names())
        elif self.path.startswith("/setup"):
            import socket
            self._send(200, _SETUP.replace("@HOST@", f"{socket.gethostname()}.local:{PORT}").replace("@HOST_ONLY@", f"{socket.gethostname()}.local").replace("@PORT@", str(PORT)).encode(),
                       "text/html; charset=utf-8")
        elif self.path.startswith("/teams?sport="):
            try:
                sport = int(self.path.split("=", 1)[1])
                if not 1 <= sport <= 6:
                    raise ValueError
                self._json(_teams(sport))
            except ValueError:
                self._send(400, b"bad sport")
            except Exception as e:  # noqa: BLE001 - ESPN down: the page says no teams found
                self._send(502, str(e).encode())
        else:
            self._send(404, b"not found")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        if self.path.startswith("/demo/"):
            name = self.path[len("/demo/"):]
            if name not in A.DEMOS:
                return self._send(404, b"no such demo")
            cmd = ("demo", name)
        elif self.path == "/settings":
            try:
                settings.update(json.loads(body))
            except (ValueError, TypeError) as e:
                return self._send(400, str(e).encode())
            return self._send(200, b"ok")
        elif self.path in ("/pin", "/skip", "/voice/test", "/livedemo"):
            cmd = (self.path.strip("/"), None)
        elif self.path == "/note":
            return self._send(200, _save_note(body.decode("utf-8", "replace")).encode())
        elif self.path.startswith("/show/"):
            from urllib.parse import unquote
            cmd = ("show", unquote(self.path[len("/show/"):]))
        elif self.path.startswith("/toggle/"):
            k = self.path[len("/toggle/"):]
            if k not in {f[0] for f in FEATURES}:
                return self._send(404, b"no such switch")
            settings.update({k: not settings.get(k)})
            return self._send(200, b"ok")
        elif self.path.startswith("/style/"):
            try:
                switched = style.switch_to(self.path[len("/style/"):])
            except (ValueError, OSError) as e:
                return self._send(400, str(e).encode())
            return self._send(200, b"switching" if switched else b"already on")
        elif self.path == "/mute":
            settings.update({"muted": not settings.get("muted")})
            return self._send(200, b"ok")
        elif self.path == "/update":
            # A transient unit, so it survives the ticker restarting under it.
            log = os.path.join(HERE, "ticker.log")
            subprocess.Popen(["systemd-run", "--user", "--collect", "--unit", "sportsball-update-now",
                              "-p", f"StandardOutput=append:{log}", "-p", f"StandardError=append:{log}",
                              "/usr/bin/python3", "-u", os.path.join(HERE, "updater.py"), "--force"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._send(200, b"ok")
        elif self.path == "/account":
            try:
                req = json.loads(body)
            except ValueError:
                return self._send(400, b"bad request")
            return self._json(_make_account(str(req.get("user", "")), str(req.get("password", ""))))
        elif self.path == "/name":
            try:
                name = str(json.loads(body).get("name", ""))
            except (ValueError, AttributeError):
                return self._send(400, b"bad request")
            return self._json(_root_request({"kind": "name", "name": name}))
        elif self.path in ("/tv/on", "/tv/off"):
            import cec
            threading.Thread(target=cec.tv_on if self.path == "/tv/on" else cec.tv_off, daemon=True).start()
            return self._send(200, b"ok")
        else:
            return self._send(404, b"not found")
        with _lock:
            _pending.append(cmd)
        self._send(200, b"ok")

    def log_message(self, *args):
        pass


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        import sys
        if not isinstance(sys.exc_info()[1], ConnectionError):   # a phone dropping off is normal
            super().handle_error(request, client_address)


def start(crawl):
    global _crawl
    _crawl = crawl
    srv = _Server(("", PORT), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print(f"[Remote] http://sportsball.local:{PORT}/", flush=True)


NOTES = os.path.join(HERE, "notes")


def _frame():
    """A finished frame from the frame loop: (framebuffer, overlays) or None."""
    _snap_done.clear()
    _snap_want.set()
    return _snap if _snap_done.wait(2) else None


def _save_note(text):
    """notes/<time>/: the note, the TV picture, the games and recent log, for
    working through later."""
    import dataclasses
    from PIL import Image
    from display import expand
    d = os.path.join(NOTES, time.strftime("%Y-%m-%d_%H%M%S"))
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "note.txt"), "w") as f:
        f.write(text.strip() + "\n")
    shot = _frame()
    if shot:
        Image.fromarray(expand(shot[0], 4, overlays=shot[1])).save(os.path.join(d, "screen.png"))
    c = _crawl.cards[_crawl.cur]
    info = {"card": f"{_CARD_NAMES[c.type]} {_crawl.cur + 1}/{len(_crawl.cards)}",
            "card_game": c.ref.key if c.type in (1, 6) else None, "settings": settings.snapshot(),
            "health": _health(), "games": [dataclasses.asdict(g) for g in _crawl.games]}
    with open(os.path.join(d, "state.json"), "w") as f:
        json.dump(info, f, indent=1, default=str)
    try:
        with open(os.path.join(HERE, "ticker.log")) as f:
            tail = f.readlines()[-300:]
        with open(os.path.join(d, "log.txt"), "w") as f:
            f.writelines(tail)
    except OSError:
        pass
    n = len(os.listdir(NOTES))
    print(f"[Note] {text.strip()[:60]}", flush=True)
    return f"Saved. {n} note{'s' if n != 1 else ''} so far."


def take_snapshot(crawl):
    """Frame loop, after drawing: hand a finished frame to /snap.png."""
    global _snap
    if _snap_want.is_set():
        import cards
        _snap = (crawl.fb.copy(), list(cards.OVERLAYS))
        _snap_want.clear()
        _snap_done.set()


def run_pending(crawl):
    with _lock:
        cmds = _pending[:]
        _pending.clear()
    for cmd, arg in cmds:
        if cmd == "demo":
            ev = A.demo_event(arg, crawl.games)
            if ev is not None:
                crawl.player.add([ev])
        elif cmd == "pin":
            crawl.pinned = not crawl.pinned
        elif cmd == "skip":
            crawl.skip()
        elif cmd == "show" and hasattr(crawl, "show"):
            crawl.show(arg)
        elif cmd == "livedemo":
            from livedemo import LiveDemo
            # Pressed again mid-demo: restart from the real games, not the demo's.
            crawl.live_demo = LiveDemo(crawl.live_demo.source if crawl.live_demo else crawl.games)
        elif cmd == "voice/test":
            ev = A.demo_event("td", crawl.games)
            if ev is not None:
                import announcer
                announcer.hook("", ev, 0)

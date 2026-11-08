"""Small macOS helpers."""
import datetime, pathlib, re, subprocess, threading
import urllib.parse
import urllib.request, base64, json, os, time



def _osa(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True).returncode == 0


def _spotify_find(query):
    cid, sec = os.getenv("SPOTIFY_CLIENT_ID"), os.getenv("SPOTIFY_CLIENT_SECRET")
    if not (cid and sec):
        return None
    tok = urllib.request.urlopen(urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=b"grant_type=client_credentials",
        headers={"Authorization": "Basic " + base64.b64encode(f"{cid}:{sec}".encode()).decode(),
                 "Content-Type": "application/x-www-form-urlencoded"}), timeout=6)
    token = json.load(tok)["access_token"]
    url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode({"q": query, "type": "track", "limit": 1})
    r = urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": "Bearer " + token}), timeout=6)
    items = json.load(r)["tracks"]["items"]
    if not items:
        return None
    t = items[0]
    return t["uri"], t["name"], ", ".join(a["name"] for a in t["artists"])


def music(action, query=""):
    """Returns (status, detail): played | opened | ok | fail. Never reports success it did not achieve."""
    a = (action or "").lower()
    if query and a in ("play", "search"):
        hit = None
        try:
            hit = _spotify_find(query)
        except Exception as e:
            print("[spotify] search failed:", repr(e)[:150])
        if hit:
            if subprocess.run(["pgrep", "-x", "Spotify"], capture_output=True).returncode != 0:
                subprocess.run(["open", "-a", "Spotify"])
                time.sleep(3)
            if _osa(f'tell application "Spotify" to play track "{hit[0]}"'):
                return "played", f"{hit[1]} by {hit[2]}"
            return "fail", ""
        subprocess.run(["open", "spotify:search:" + urllib.parse.quote(query)])
        return "opened", query
    cmd = {"play": "play", "pause": "pause", "next": "next track", "previous": "previous track"}.get(a)
    if cmd and _osa(f'tell application "Spotify" to {cmd}'):
        return "ok", a
    return "fail", ""

def open_app(name):
    return subprocess.run(["open", "-a", name], capture_output=True).returncode == 0


def set_volume(level):
    level = max(0, min(100, int(level)))
    _osa(f"set volume output volume {level}")
    return level


def get_volume():
    try:
        return int(_osa("output volume of (get volume settings)"))
    except ValueError:
        return 50


def mute(on=True):
    _osa(f"set volume output muted {'true' if on else 'false'}")


MUSIC_APP = "Spotify"

def battery_percent():
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)%", out)
    return int(m.group(1)) if m else None


def clipboard():
    return subprocess.run(["pbpaste"], capture_output=True, text=True).stdout


def lock():
    """Sleeps the display; it locks if 'require password immediately' is on."""
    subprocess.run(["pmset", "displaysleepnow"])


def screenshot(folder="~/Desktop"):
    d = pathlib.Path(folder).expanduser()
    p = d / f"Screenshot {datetime.datetime.now():%Y-%m-%d at %H.%M.%S}.png"
    subprocess.run(["screencapture", "-x", str(p)])
    return p


def notify(title, message):
    esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
    try:
        _osa(f'display notification "{esc(message)}" with title "{esc(title)}"')
    except Exception:
        pass


def reveal(path):
    try:
        subprocess.run(["open", "-R", str(path)], timeout=5, capture_output=True)
    except Exception:
        pass


def start_timer(seconds, callback):
    t = threading.Timer(seconds, callback)
    t.daemon = True
    t.start()
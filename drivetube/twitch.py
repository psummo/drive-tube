"""Twitch tramite yt-dlp: canali preferiti (nel cookie del browser), chi e' in
diretta, ultimi video salvati (VOD) e URL degli stream."""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote
from .config import UA
from .context import in_session, print
from .youtube import StreamError, run_yt

NAME = re.compile(r"^[A-Za-z0-9_]{2,25}$")
COOKIE = "dt_tw"
MAX_FAV = 30
LIVE_TTL = 120            # stato "in diretta" ricontrollato dopo 2 minuti
VODS_TTL = 900

_live = {}                # nome -> (quando, {"live", "title"})
_vods = {}                # nome -> (quando, [(id, titolo, durata)])
_lock = threading.Lock()
_pool = ThreadPoolExecutor(max_workers=6)


def clean_name(s):
    s = (s or "").strip()
    m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", s)
    if m:
        s = m.group(1)
    s = s.lstrip("@").lower()
    return s if NAME.match(s) else ""


def favs_from_cookie(value):
    out = []
    for x in unquote(value or "").split(","):
        x = clean_name(x)
        if x and x not in out:
            out.append(x)
    return out[:MAX_FAV]


def favs_cookie(favs):
    return quote(",".join(favs[:MAX_FAV]))


def live_url(name):
    return "https://www.twitch.tv/" + name


def vod_url(vid):
    return "https://www.twitch.tv/videos/" + vid


def preview(name):
    # anteprima dello stream che Twitch aggiorna da solo ogni pochi minuti
    return "https://static-cdn.jtvnw.net/previews-ttv/live_user_%s-440x248.jpg" % name


def _live_fetch(name):
    p = run_yt("--no-playlist", "--print", "%(is_live)s\t%(title)s", live_url(name),
               cookies=False, text=True, capture_output=True, timeout=40)
    for line in reversed((p.stdout or "").splitlines()):
        parts = line.split("\t", 1)
        if parts[0].strip() in ("True", "False", "NA"):
            return {"live": parts[0].strip() != "False", "title": parts[1].strip() if len(parts) > 1 else ""}
    err = p.stderr or ""
    if "not currently live" not in err and "does not exist" not in err:
        print("twitch, stato del canale:", (err.strip().splitlines() or ["nessuna risposta"])[-1][:200], flush=True)
    return {"live": False, "title": ""}


def live_status(names):
    """{nome: {"live", "title"}} per i canali dati, in parallelo, con cache."""
    now = time.time()
    out, todo = {}, []
    with _lock:
        for n in names:
            hit = _live.get(n)
            if hit and now - hit[0] < LIVE_TTL:
                out[n] = hit[1]
            else:
                todo.append(n)
    futs = {n: _pool.submit(in_session(_live_fetch), n) for n in todo}
    for n, f in futs.items():
        try:
            st = f.result(timeout=45)
        except Exception as e:
            print("twitch, stato del canale:", e, flush=True)
            st = {"live": False, "title": ""}
        with _lock:
            _live[n] = (time.time(), st)
        out[n] = st
    return out


def _vods_fetch(name):
    p = run_yt("--flat-playlist", "-I", "1:16", "--print", "%(id)s\t%(duration)s\t%(title)s",
               "https://www.twitch.tv/%s/videos?filter=archives&sort=time" % name,
               cookies=False, text=True, capture_output=True, timeout=60)
    rows = []
    for line in (p.stdout or "").splitlines():
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        vid = parts[0].strip().lstrip("v")
        if not vid.isdigit():
            continue
        try:
            dur = int(float(parts[1]))
        except ValueError:
            dur = 0
        rows.append((vid, parts[2].strip(), dur))
    if not rows and p.returncode:
        print("twitch, video salvati:", ((p.stderr or "").strip().splitlines() or ["?"])[-1][:200], flush=True)
    return rows


def vods(name):
    now = time.time()
    with _lock:
        hit = _vods.get(name)
    if hit and now - hit[0] < VODS_TTL:
        return hit[1]
    rows = _vods_fetch(name)
    with _lock:
        _vods[name] = (time.time() if rows else time.time() - VODS_TTL + 60, rows)
    return rows


def twitch_format(h264, max_h=720):
    # Twitch usa sempre H.264 + AAC nello stesso stream (HLS)
    return "best[height<=%d]/best" % max_h


def stream_urls(page_url, max_h=720):
    p = run_yt("-g", "--no-playlist", "--user-agent", UA, "-f", twitch_format(True, max_h), page_url,
               cookies=False, text=True, capture_output=True, timeout=60)
    lines = [x for x in (p.stdout or "").splitlines() if x.startswith("http")]
    if p.returncode != 0 or not lines:
        err = (p.stderr or "").strip()
        if "not currently live" in err:
            raise StreamError("none", "il canale non e' in diretta adesso")
        print(err[-600:], flush=True)
        raise StreamError("failed", err.splitlines()[-1][:200] if err else "yt-dlp non ha dato lo stream")
    return lines

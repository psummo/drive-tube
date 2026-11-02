"""Piattaforme di streamer (Twitch, Kick) tramite yt-dlp: canali preferiti (nel
cookie del browser), chi e' in diretta, ultimi video salvati (VOD), stream."""
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote
from .context import in_session, print
from . import generic
from .tools import ytdlp_full_bin, ytdlp_full_start
from .youtube import run_yt

MAX_FAV = 30
LIVE_TTL = 120            # stato "in diretta" ricontrollato dopo 2 minuti
VODS_TTL = 900
_pool = ThreadPoolExecutor(max_workers=6)


class Platform:
    """Una piattaforma. Le chiavi di /watch?v= sono <live_key>.<canale> per la
    diretta e <vod_key>.<id> per un video salvato."""
    name = ""             # per i messaggi e i titoli
    path = ""             # pagina dell'app (/twitch)
    cookie = ""           # cookie dei preferiti
    live_key = vod_key = ""
    color = "#7c3aed"
    name_re = re.compile(r"^[a-z0-9_]{2,25}$")
    host_re = None

    def __init__(self):
        self._live = {}   # canale -> (quando, {"live", "title", "thumb"})
        self._vods = {}   # canale -> (quando, [(id, titolo, durata)])
        self._lock = threading.Lock()

    # -- da specializzare --
    def live_url(self, ch):
        raise NotImplementedError

    def vod_url(self, vid):
        raise NotImplementedError

    def videos_url(self, ch):
        raise NotImplementedError

    def vod_id(self, ch, raw_id):
        """Parte dopo vod_key per un id dato da yt-dlp, o "" se non valido."""
        raise NotImplementedError

    def preview(self, ch, status=None):
        return (status or {}).get("thumb") or ""

    def binary(self):
        return None       # yt-dlp normale

    def ready(self):
        """"" se si puo' usare, altrimenti il motivo."""
        return ""

    # -- comune --
    def clean_name(self, s):
        s = (s or "").strip()
        if self.host_re:
            m = self.host_re.search(s)
            if m:
                s = m.group(1)
        s = s.lstrip("@").lower()
        return s if self.name_re.match(s) else ""

    def favs_from_cookie(self, value):
        out = []
        for x in unquote(value or "").split(","):
            x = self.clean_name(x)
            if x and x not in out:
                out.append(x)
        return out[:MAX_FAV]

    def favs_cookie(self, favs):
        return quote(",".join(favs[:MAX_FAV]))

    def _run(self, *args, timeout=60):
        return run_yt(*args, cookies=False, binary=self.binary(), text=True, capture_output=True, timeout=timeout)

    def _live_fetch(self, ch):
        p = self._run("--no-playlist", "--print", "%(is_live)s\t%(thumbnail)s\t%(title)s", self.live_url(ch), timeout=45)
        for line in reversed((p.stdout or "").splitlines()):
            parts = line.split("\t", 2)
            if parts[0].strip() in ("True", "False", "NA") and len(parts) == 3:
                thumb = parts[1].strip()
                return {"live": parts[0].strip() != "False", "title": parts[2].strip(),
                        "thumb": thumb if thumb.startswith("https://") else ""}
        err = p.stderr or ""
        if "not currently live" not in err and "does not exist" not in err:
            print("%s, stato del canale:" % self.name, generic.explain(err)[1], flush=True)
        return {"live": False, "title": "", "thumb": ""}

    def live_status(self, names):
        """{canale: {"live", "title", "thumb"}}, in parallelo, con cache."""
        now = time.time()
        out, todo = {}, []
        with self._lock:
            for n in names:
                hit = self._live.get(n)
                if hit and now - hit[0] < LIVE_TTL:
                    out[n] = hit[1]
                else:
                    todo.append(n)
        if todo and self.ready():
            return dict(out, **{n: {"live": False, "title": "", "thumb": ""} for n in todo})
        futs = {n: _pool.submit(in_session(self._live_fetch), n) for n in todo}
        for n, f in futs.items():
            try:
                st = f.result(timeout=50)
            except Exception as e:
                print("%s, stato del canale:" % self.name, e, flush=True)
                st = {"live": False, "title": "", "thumb": ""}
            with self._lock:
                self._live[n] = (time.time(), st)
            out[n] = st
        for n, st in out.items():
            st["thumb"] = self.preview(n, st)
        return out

    def _vods_fetch(self, ch):
        p = self._run("--flat-playlist", "-I", "1:16", "--print", "%(id)s\t%(duration)s\t%(title)s",
                      self.videos_url(ch))
        rows = []
        for line in (p.stdout or "").splitlines():
            parts = line.split("\t", 2)
            if len(parts) < 3:
                continue
            vid = self.vod_id(ch, parts[0].strip())
            if not vid:
                continue
            try:
                dur = int(float(parts[1]))
            except ValueError:
                dur = 0
            rows.append((vid, parts[2].strip(), dur))
        if not rows and p.returncode:
            print("%s, video salvati:" % self.name, generic.explain(p.stderr)[1], flush=True)
        return rows

    def vods(self, ch):
        if self.ready():
            return []
        now = time.time()
        with self._lock:
            hit = self._vods.get(ch)
        if hit and now - hit[0] < VODS_TTL:
            return hit[1]
        rows = self._vods_fetch(ch)
        with self._lock:
            self._vods[ch] = (time.time() if rows else time.time() - VODS_TTL + 60, rows)
        return rows

    def stream_urls(self, page_url, max_h=720):
        why = self.ready()
        if why:
            raise generic.StreamError("failed", why)
        return generic.stream_urls(page_url, max_h, binary=self.binary())

    def info(self, page_url):
        return generic.info(page_url, binary=self.binary()) if not self.ready() else None


class Twitch(Platform):
    name = "Twitch"
    path = "/twitch"
    cookie = "dt_tw"
    live_key, vod_key = "tw", "twv"
    color = "#7c3aed"
    name_re = re.compile(r"^[a-z0-9_]{2,25}$")
    host_re = re.compile(r"twitch\.tv/([A-Za-z0-9_]+)")

    def live_url(self, ch):
        return "https://www.twitch.tv/" + ch

    def vod_url(self, vid):
        return "https://www.twitch.tv/videos/" + vid

    def videos_url(self, ch):
        return "https://www.twitch.tv/%s/videos?filter=archives&sort=time" % ch

    def vod_id(self, ch, raw):
        raw = raw.lstrip("v")
        return raw if raw.isdigit() else ""

    def valid_vod(self, vid):
        return vid.isdigit()

    def preview(self, ch, status=None):
        # anteprima dello stream che Twitch aggiorna da solo ogni pochi minuti
        return "https://static-cdn.jtvnw.net/previews-ttv/live_user_%s-440x248.jpg" % ch


_UUID = re.compile(r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")


class Kick(Platform):
    name = "Kick"
    path = "/kick"
    cookie = "dt_kick"
    live_key, vod_key = "k", "kv"
    color = "#16a34a"
    name_re = re.compile(r"^[a-z0-9_-]{2,30}$")
    host_re = re.compile(r"kick\.com/([A-Za-z0-9_-]+)")

    def live_url(self, ch):
        return "https://kick.com/" + ch

    def vod_url(self, vid):
        ch, _, uuid = vid.partition("~")
        return "https://kick.com/%s/videos/%s" % (ch, uuid)

    def videos_url(self, ch):
        return "https://kick.com/%s/videos" % ch

    def vod_id(self, ch, raw):
        # nella chiave: canale~uuid (il punto separa gia' le parti della chiave)
        return "%s~%s" % (ch, raw) if _UUID.match(raw) else ""

    def valid_vod(self, vid):
        ch, _, uuid = vid.partition("~")
        return bool(self.name_re.match(ch) and _UUID.match(uuid))

    def binary(self):
        return ytdlp_full_bin() or None

    def ready(self):
        # Kick sta dietro Cloudflare: serve yt-dlp con curl_cffi ("completo")
        if not ytdlp_full_bin():
            ytdlp_full_start()
            return "Kick ha bisogno di yt-dlp completo: lo sto preparando (circa un minuto), poi riprova"
        return ""


TWITCH = Twitch()
KICK = Kick()
PLATFORMS = [TWITCH, KICK]


def by_key(prefix):
    """(piattaforma, e' un VOD) per il prefisso di una chiave /watch?v=."""
    for p in PLATFORMS:
        if prefix == p.live_key:
            return p, False
        if prefix == p.vod_key:
            return p, True
    return None, False

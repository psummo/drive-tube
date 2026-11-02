"""RaiPlay: catalogo letto dalle pagine JSON pubbliche di raiplay.it (le stesse che
usa il sito), riproduzione tramite yt-dlp (estrattori RaiPlay e RaiPlayLive).

Le pagine di raiplay.it hanno una versione .json allo stesso indirizzo:
  /index.json, /tipologia/<genere>/index.json   blocchi di riquadri (home, generi)
  /programmi/<nome>.json                         un programma: blocks -> sets
  /programmi/<nome>/<...>/<set>.json             gli episodi di un set: items
  /video/<anno>/<mese>/<titolo>-<uuid>.json      un video (lo legge yt-dlp)
L'elenco A-Z dei programmi serve per la ricerca."""
import base64
import json
import os
import re
import threading
import time
import urllib.request
from .config import UA
from .context import print

BASE = os.environ.get("DT_RAIPLAY_BASE") or "https://www.raiplay.it"   # (variabile solo per le prove)
AZ_PATH = "/dl/RaiTV/RaiPlayMobile/Prod/Config/programmiAZ-elenco.json"
TTL = 1800
AZ_TTL = 6 * 3600
MAX_JSON = 6 << 20

# generi del menu di RaiPlay (pagine /tipologia/<x>/index.json)
GENRES = [("Serie italiane", "serieitaliane"), ("Film", "film"), ("Serie internazionali", "serieinternazionali"),
          ("Programmi", "programmi"), ("Documentari", "documentari"), ("Bambini", "bambini"), ("Teen", "teen"),
          ("Sport", "sport"), ("Musica e teatro", "musica")]

# dirette di RaiPlay (estrattore RaiPlayLive di yt-dlp: raiplay.it/dirette/<nome>)
LIVES = [("Rai 1", "rai1"), ("Rai 2", "rai2"), ("Rai 3", "rai3"), ("Rai News 24", "rainews24"),
         ("Rai 4", "rai4"), ("Rai 5", "rai5"), ("Rai Movie", "raimovie"), ("Rai Premium", "raipremium"),
         ("Rai Storia", "raistoria"), ("Rai Scuola", "raiscuola"), ("Rai YoYo", "raiyoyo"), ("Rai Gulp", "raigulp"),
         ("Rai Sport", "raisport")]
LIVE_IDS = {k for _, k in LIVES}

_cache = {}                 # percorso -> (quando, json)
_lock = threading.Lock()
_set_of = {}                # percorso di un video -> percorso del set da cui e' stato aperto (per "dopo")

PATH_OK = re.compile(r"^/[A-Za-z0-9_\-/.%~]+$")


def norm_path(p):
    """Percorso su raiplay.it dalla forma che usano i JSON ("/raiplay/...", "//www...",
    "...?json", indirizzo completo). "" se non e' un percorso di raiplay.it."""
    p = (p or "").strip().replace(" ", "%20")
    for pre in ("https://www.raiplay.it", "http://www.raiplay.it", "https://raiplay.it", "//www.raiplay.it"):
        if p.startswith(pre):
            p = p[len(pre):]
    if p.startswith("/raiplay/"):
        p = p[len("/raiplay"):]
    if p.endswith(".html?json"):
        p = p[:-10] + ".json"
    elif p.endswith("/?json"):
        # un programma e' /programmi/<nome>.json, le altre pagine .../index.json
        p = p[:-6] + (".json" if re.match(r"^/programmi/[^/]+/\?json$", p) else "/index.json")
    elif p.endswith("?json"):
        p = p[:-5] + ".json"
    p = p.split("?")[0].split("#")[0]
    if not p.startswith("/") or ".." in p or not PATH_OK.match(p):
        return ""
    return p


def as_json(p):
    p = norm_path(p)
    if p.endswith(".html"):
        p = p[:-5] + ".json"
    elif re.match(r"^/programmi/[^/]+/$", p):
        p = p[:-1] + ".json"
    elif p.endswith("/"):
        p += "index.json"
    elif p and not p.endswith(".json"):
        p += ".json"
    return p


def fetch(path, ttl=TTL):
    path = as_json(path)
    if not path:
        raise ValueError("percorso non valido")
    now = time.time()
    with _lock:
        hit = _cache.get(path)
    if hit and now - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(BASE + path, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read(MAX_JSON).decode("utf-8", "replace"))
    except Exception as e:
        if hit:
            return hit[1]   # meglio la copia vecchia di niente
        raise RuntimeError("RaiPlay non risponde (%s)" % str(e)[:120])
    with _lock:
        if len(_cache) > 400:
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:100]:
                _cache.pop(k, None)
        _cache[path] = (time.time(), data)
    return data


def image(item, size="400x-"):
    imgs = item.get("images") if isinstance(item, dict) else None
    if not isinstance(imgs, dict):
        imgs = {}
    for k in ("landscape", "landscape43", "portrait_logo", "portrait", "portrait43", "square"):
        v = imgs.get(k)
        if isinstance(v, str) and v:
            v = v.replace("[RESOLUTION]", size)
            if v.startswith("//"):
                return "https:" + v
            if v.startswith("/"):
                return BASE + v
            if v.startswith("https://"):
                return v
    v = item.get("image") if isinstance(item, dict) else ""
    if isinstance(v, str) and v.startswith("/"):
        return BASE + v.replace("[RESOLUTION]", size)
    return ""


# ---- chiavi per /watch?v= ----

def video_key(path):
    """rai.<percorso del video in base64url>"""
    p = norm_path(path)
    if not p.startswith("/video/"):
        return ""
    p = p[:-5] + ".html" if p.endswith(".json") else p
    return "rai." + base64.urlsafe_b64encode(p.encode()).decode().rstrip("=")


def path_of_key(k):
    try:
        p = base64.urlsafe_b64decode(k + "=" * (-len(k) % 4)).decode()
    except Exception:
        return ""
    p = norm_path(p)
    return p if p.startswith("/video/") and p.endswith(".html") else ""


def video_url(path):
    return BASE + path


def live_url(ch):
    return BASE + "/dirette/" + ch


# ---- elenchi per le pagine ----

def _kind(item):
    p = norm_path(item.get("path_id") or item.get("PathID") or item.get("weblink") or "")
    t = str(item.get("type") or item.get("sub_type") or "")
    if "/video/" in p or "Video Item" in t:
        vp = norm_path(item.get("video_url") or "") if "/video/" not in p else p
        return ("video", vp or p)
    if "/dirette/" in p:
        return ("live", p.rsplit("/", 1)[-1].replace(".json", ""))
    if p:
        return ("page", p)
    return ("", "")


def card(item):
    """{"kind": video|live|page, "path", "title", "sub", "img"} o None."""
    if not isinstance(item, dict):
        return None
    kind, p = _kind(item)
    name = item.get("name") or item.get("title") or ""
    if not kind or not name:
        return None
    sub = item.get("subtitle") or item.get("episode_title") or ""
    if sub == name:
        sub = ""
    return {"kind": kind, "path": p, "title": str(name), "sub": str(sub), "img": image(item),
            "dur": str(item.get("duration") or item.get("duration_in_minutes") or "")}


def blocks(data):
    """[(titolo del blocco, [card])] da una pagina con "contents" (home, generi)."""
    out = []
    contents = data.get("contents") if isinstance(data, dict) else None
    if isinstance(contents, dict):
        # pagine A-Z: lettera -> elenco
        rows = [card(x) for k in sorted(contents) for x in (contents[k] or [])]
        rows = [r for r in rows if r]
        if rows:
            out.append(("", rows))
        return out
    for b in contents or []:
        if not isinstance(b, dict):
            continue
        items = b.get("contents") or b.get("items") or []
        rows = [r for r in (card(x) for x in items if isinstance(x, dict)) if r]
        if not rows and card(b):
            rows = [card(b)]
        title = b.get("name") or ""
        if "RCM" in title:
            title = title[:title.find("RCM")]
        if rows:
            out.append((title.strip(), rows[:40]))
    return out


def home():
    return blocks(fetch("/index.json"))


def genre(slug):
    return blocks(fetch("/tipologia/%s/index.json" % slug))


def programme(path):
    """{"title", "desc", "img", "sets": [(nome, percorso)], "film": percorso o ""}"""
    d = fetch(path)
    info = d.get("program_info") or {}
    sets = []
    bl = [b for b in d.get("blocks") or [] if b.get("sets")]
    for b in bl:
        for s in b.get("sets") or []:
            sp = norm_path(s.get("path_id") or "")
            if not sp:
                continue
            label = s.get("name") or b.get("name") or "Episodi"
            # il nome del blocco ("Episodi", "Clip"...) solo se ce n'e' piu' d'uno
            if len(bl) > 1 and b.get("name") and b.get("name") != label:
                label = "%s (%s)" % (label, b["name"])
            sets.append((label, sp))
    film = norm_path(d.get("first_item_path") or "")
    if film and "/video/" not in film:
        film = ""
    page_blocks = blocks(d) if not sets and not film else []
    return {"title": info.get("name") or d.get("name") or "", "desc": info.get("description") or "",
            "img": image(info) or image(d), "sets": sets, "film": film, "blocks": page_blocks}


def episodes(set_path):
    d = fetch(set_path)
    rows = [r for r in (card(x) for x in d.get("items") or []) if r and r["kind"] == "video"]
    for r in rows:
        _set_of[r["path"]] = set_path
    if len(_set_of) > 5000:
        _set_of.clear()
    return rows


def search(q):
    """Programmi il cui nome contiene q (dall'elenco A-Z)."""
    q = (q or "").strip().lower()
    if len(q) < 2:
        return []
    d = fetch(AZ_PATH, ttl=AZ_TTL)
    out = []
    groups = d.values() if isinstance(d, dict) else [d]
    for lst in groups:
        for x in lst if isinstance(lst, list) else []:
            if not isinstance(x, dict):
                continue
            name = x.get("name") or ""
            if q in name.lower():
                c = card(x)
                if c:
                    out.append(c)
    out.sort(key=lambda c: (not c["title"].lower().startswith(q), c["title"].lower()))
    return out[:60]


def next_after(video_path):
    """Gli episodi che seguono nello stesso set (se il video e' stato aperto da un set)."""
    sp = _set_of.get(video_path)
    if not sp:
        return []
    try:
        rows = episodes(sp)
    except Exception as e:
        print("raiplay, episodi successivi:", e, flush=True)
        return []
    i = next((n for n, r in enumerate(rows) if r["path"] == video_path), -1)
    return rows[i + 1:i + 9] if i >= 0 else rows[:8]

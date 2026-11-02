"""Internet Archive (archive.org): film, cartoni e serie TV di pubblico dominio.

Si cercano solo nelle collezioni curate dall'Archive per le opere libere (Feature
Films, Film Noir, Sci-Fi/Horror, cartoni, TV classica, film muti...), non tra i
caricamenti generici degli utenti. API pubbliche, senza chiavi:
  /advancedsearch.php   ricerca (identificativo, titolo, anno, scaricamenti)
  /metadata/<id>        file di un elemento (si sceglie la versione H.264)
  /services/img/<id>    copertina
  /download/<id>/<file> il file video (ffmpeg lo legge direttamente, con i salti)"""
import base64
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from .config import UA

BASE = os.environ.get("DT_ARCHIVE_BASE") or "https://archive.org"   # (variabile solo per le prove)

# (etichetta, query): le collezioni di opere di pubblico dominio
SHELVES = [
    ("Film", "collection:(feature_films)"),
    ("In italiano", "collection:(feature_films OR film_noir OR SciFi_Horror OR comedy_films OR silent_films) "
                    "AND language:(Italian OR ita OR italiano)"),
    ("Cartoni animati", "collection:(classic_cartoons OR animationandcartoons)"),
    ("Film noir", "collection:(film_noir)"),
    ("Fantascienza e horror", "collection:(SciFi_Horror)"),
    ("Commedie", "collection:(comedy_films)"),
    ("Film muti", "collection:(silent_films)"),
    ("TV classica", "collection:(classic_tv)"),
]
ALL = ("collection:(feature_films OR film_noir OR SciFi_Horror OR comedy_films OR silent_films OR "
       "classic_cartoons OR animationandcartoons OR classic_tv)")
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,99}$")
TTL = 1800

_cache = {}
_lock = threading.Lock()


def _get(path, params=None, ttl=TTL):
    url = BASE + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    now = time.time()
    with _lock:
        hit = _cache.get(url)
    if hit and now - hit[0] < ttl:
        return hit[1]
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read(8 << 20).decode("utf-8", "replace"))
    except Exception as e:
        if hit:
            return hit[1]
        raise RuntimeError("Internet Archive non risponde (%s)" % str(e)[:120])
    with _lock:
        if len(_cache) > 300:
            _cache.clear()
        _cache[url] = (time.time(), data)
    return data


def thumb(ident):
    return BASE + "/services/img/" + urllib.parse.quote(ident)


def search(query="", shelf=0, page=1, rows=48):
    """[{"id", "title", "year"}] per uno scaffale (indice in SHELVES) o una ricerca."""
    q = "mediatype:(movies) AND " + (SHELVES[shelf][1] if 0 <= shelf < len(SHELVES) else ALL)
    if query:
        words = re.sub(r'[^\w\s\'-]', " ", query).split()[:8]
        if words:
            q += " AND (" + " AND ".join('(title:(%s) OR subject:(%s))' % (w, w) for w in words) + ")"
    d = _get("/advancedsearch.php", {"q": q, "fl[]": ["identifier", "title", "year"],
                                     "sort[]": "downloads desc", "rows": rows, "page": page, "output": "json"})
    out = []
    for x in ((d.get("response") or {}).get("docs") or []) if isinstance(d, dict) else []:
        ident = x.get("identifier") or ""
        if not ID_RE.match(ident):
            continue
        title = x.get("title") or ident
        if isinstance(title, list):
            title = title[0]
        year = x.get("year") or ""
        if isinstance(year, list):
            year = year[0] if year else ""
        out.append({"id": ident, "title": str(title), "year": str(year)[:4]})
    return out


def _secs(v):
    try:
        if isinstance(v, str) and ":" in v:
            s = 0.0
            for part in v.split(":"):
                s = s * 60 + float(part)
            return int(s)
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# formati dell'Archive, dal migliore per il player: H.264 di dimensione adatta
# all'auto prima degli originali (spesso enormi) e del vecchio MPEG-4 parte 2
_FORMAT_RANK = ["h.264", "h.264 hd", "mpeg4", "h.264 ia", "512kb mpeg4", "matroska", "ogg video", "cinepack"]


def item(ident):
    """{"title", "desc", "year", "files": [{"name", "title", "dur", "size"}]} con un file
    video per episodio/film (la versione migliore di ciascuno)."""
    if not ID_RE.match(ident or ""):
        raise ValueError("identificativo non valido")
    d = _get("/metadata/" + urllib.parse.quote(ident))
    meta = d.get("metadata") or {}
    files = d.get("files") or []
    groups = {}
    for f in files:
        name = f.get("name") or ""
        fmt = (f.get("format") or "").lower()
        if fmt not in _FORMAT_RANK or name.startswith("__"):
            continue
        # le versioni ricavate dall'Archive indicano il file "original" da cui vengono
        root = f.get("original") or name
        g = groups.setdefault(root, [])
        g.append((_FORMAT_RANK.index(fmt), f))
    out = []
    for root, lst in groups.items():
        lst.sort(key=lambda x: x[0])
        f = lst[0][1]
        dur = max([_secs(x[1].get("length")) for x in lst] + [0])
        label = f.get("title") or re.sub(r"\.[^.]+$", "", os.path.basename(root)).replace("_", " ")
        out.append({"name": f["name"], "title": str(label), "dur": dur, "size": int(f.get("size") or 0)})
    out.sort(key=lambda x: x["name"].lower())
    title = meta.get("title") or ident
    desc = meta.get("description") or ""
    if isinstance(title, list):
        title = title[0]
    if isinstance(desc, list):
        desc = " ".join(desc)
    desc = re.sub(r"<[^>]+>", " ", str(desc))
    year = meta.get("year") or meta.get("date") or ""
    return {"title": str(title), "desc": re.sub(r"\s+", " ", desc).strip()[:800],
            "year": str(year)[:4], "files": out}


def file_url(ident, name):
    return "%s/download/%s/%s" % (BASE, urllib.parse.quote(ident), urllib.parse.quote(name))


def key(ident, name):
    """ia.<base64url di "id/file">"""
    return "ia." + base64.urlsafe_b64encode(("%s/%s" % (ident, name)).encode()).decode().rstrip("=")


def unkey(k):
    try:
        s = base64.urlsafe_b64decode(k + "=" * (-len(k) % 4)).decode()
    except Exception:
        return None
    ident, _, name = s.partition("/")
    if not ID_RE.match(ident) or not name or ".." in name or name.startswith("/"):
        return None
    return ident, name

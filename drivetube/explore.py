"""Dirette disponibili adesso su Twitch e Kick, per categoria e lingua.

Twitch: GraphQL pubblico del sito (gql.twitch.tv), lo stesso che usa yt-dlp, con
le query "streams", "game.streams" e "games" (le dirette piu' viste, quelle di
una categoria, le categorie piu' viste).
Kick: le API del sito (web.kick.com/api/v1/livestreams, kick.com/api/v1/subcategories).
Stanno dietro Cloudflare, quindi passano da curl_cffi (vedi tools.pylib_install)."""
import json
import os
import sys
import threading
import time
import urllib.parse
import urllib.request
from .config import UA
from .context import print
from .tools import PYLIB, pylib_ready

TW_GQL = os.environ.get("DT_TWITCH_GQL") or "https://gql.twitch.tv/gql"   # (variabili solo per le prove)
KICK_TEST = os.environ.get("DT_KICK_API", "")
TW_CLIENT_IDS = ["ue6666qo983tsx6so1t0vnawi233wa", "kimne78kx3ncx6brgo4mv6wki5h1ko"]
STREAMS_TTL = 90
CATS_TTL = 900

_cache = {}
_lock = threading.Lock()


def _cached(key, ttl, fn):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        val = fn()
    except Exception as e:
        if hit:
            return hit[1]
        raise
    with _lock:
        if len(_cache) > 300:
            _cache.clear()
        _cache[key] = (time.time(), val)
    return val


def _img(url, w, h):
    url = url or ""
    for a, b in (("{width}", str(w)), ("{height}", str(h)), ("%{width}", str(w)), ("%{height}", str(h))):
        url = url.replace(a, b)
    return url if url.startswith("https://") else ""


# ---------------- Twitch ----------------

_TW_NODE = """broadcaster { login displayName broadcastSettings { title } }
              game { displayName slug } previewImageURL viewersCount"""


def _tw_query(query, variables):
    err = ""
    body = json.dumps({"query": query, "variables": variables}).encode()
    for cid in TW_CLIENT_IDS:
        req = urllib.request.Request(TW_GQL, data=body, headers={
            "Client-ID": cid, "Content-Type": "text/plain;charset=UTF-8", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
        except Exception as e:
            err = str(e)[:150]
            continue
        if isinstance(d, dict) and d.get("data"):
            return d["data"]
        err = json.dumps(d.get("errors") if isinstance(d, dict) else d)[:200]
    raise RuntimeError("Twitch non risponde: " + err)


def _tw_rows(edges):
    out = []
    for e in edges or []:
        n = (e or {}).get("node") or {}
        b = n.get("broadcaster") or {}
        if not b.get("login"):
            continue
        out.append({"ch": b["login"].lower(), "name": b.get("displayName") or b["login"],
                    "title": ((b.get("broadcastSettings") or {}).get("title") or "").strip(),
                    "viewers": n.get("viewersCount") or 0,
                    "thumb": _img(n.get("previewImageURL"), 440, 248),
                    "cat": ((n.get("game") or {}).get("displayName") or "")})
    return out


def twitch_streams(cat="", lang="it"):
    langs = [lang.upper()] if lang else None

    def fetch():
        if cat:
            q = ("query($slug: String, $languages: [Language!]) { game(slug: $slug) { displayName "
                 "streams(first: 30, options: { sort: VIEWER_COUNT, broadcasterLanguages: $languages }) "
                 "{ edges { node { %s } } } } }" % _TW_NODE)
            d = _tw_query(q, {"slug": cat, "languages": langs})
            g = d.get("game") or {}
            rows = _tw_rows((g.get("streams") or {}).get("edges"))
            for r in rows:
                r["cat"] = r["cat"] or g.get("displayName", "")
            return rows
        q = ("query($languages: [Language!]) { streams(first: 30, options: { sort: VIEWER_COUNT, "
             "broadcasterLanguages: $languages }) { edges { node { %s } } } }" % _TW_NODE)
        return _tw_rows((_tw_query(q, {"languages": langs}).get("streams") or {}).get("edges"))
    return _cached(("tw", cat, lang), STREAMS_TTL, fetch)


def twitch_categories():
    def fetch():
        q = ("query { games(first: 40) { edges { node { displayName slug viewersCount "
             "boxArtURL(width: 144, height: 192) } } } }")
        out = []
        for e in (_tw_query(q, {}).get("games") or {}).get("edges") or []:
            n = (e or {}).get("node") or {}
            if n.get("slug"):
                out.append({"slug": n["slug"], "name": n.get("displayName") or n["slug"],
                            "viewers": n.get("viewersCount") or 0, "img": _img(n.get("boxArtURL"), 144, 192)})
        return out
    return _cached(("twc",), CATS_TTL, fetch)


# ---------------- Kick ----------------

def _curl():
    if not pylib_ready():
        return None
    if PYLIB not in sys.path:
        sys.path.append(PYLIB)
    try:
        from curl_cffi import requests as cr
        return cr
    except Exception as e:
        print("curl_cffi non caricato:", e, flush=True)
        return None


def kick_get(url, params=None):
    if KICK_TEST:
        url = KICK_TEST + "/" + url.split("://", 1)[1]
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    hdr = {"Accept": "application/json", "Referer": "https://kick.com/"}
    cr = _curl()
    if cr is not None:
        r = cr.get(url, headers=hdr, impersonate="chrome", timeout=20)
        if r.status_code >= 400:
            raise RuntimeError("Kick ha risposto %d%s" % (r.status_code, " (Cloudflare)" if r.status_code == 403 else ""))
        return r.json()
    # senza curl_cffi si prova lo stesso: a volte Cloudflare lascia passare
    req = urllib.request.Request(url, headers=dict(hdr, **{"User-Agent": UA}))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError("Kick blocca le richieste del server (errore %d): serve curl_cffi, "
                           "si installa da solo con la preparazione di Kick" % e.code)


def _kimg(x):
    if isinstance(x, dict):
        x = x.get("src") or x.get("url") or ""
    return x if isinstance(x, str) and x.startswith("https://") else ""


def kick_categories():
    def fetch():
        d = kick_get("https://kick.com/api/v1/subcategories", {"limit": 40, "page": 1})
        out = []
        for c in (d.get("data") if isinstance(d, dict) else d) or []:
            if isinstance(c, dict) and c.get("slug"):
                out.append({"slug": c["slug"], "name": c.get("name") or c["slug"], "id": c.get("id"),
                            "viewers": c.get("viewers") or 0, "img": _kimg(c.get("banner"))})
        return out
    return _cached(("kc",), CATS_TTL, fetch)


def _kick_cat_id(slug):
    for c in kick_categories():
        if c["slug"] == slug:
            return c["id"]
    d = _cached(("kcat", slug), CATS_TTL, lambda: kick_get("https://kick.com/api/v1/subcategories/" + urllib.parse.quote(slug)))
    return (d or {}).get("id")


def kick_streams(cat="", lang="it"):
    def fetch():
        cid = _kick_cat_id(cat) if cat else None
        if cat and not cid:
            return []
        d = kick_get("https://web.kick.com/api/v1/livestreams",
                     {"limit": 30, "sort": "viewer_count_desc", "category_id": cid, "language": lang})
        out = []
        for s in ((d.get("data") or {}).get("livestreams") or []) if isinstance(d, dict) else []:
            ch = (s.get("channel") or {})
            if not ch.get("slug"):
                continue
            out.append({"ch": ch["slug"].lower(), "name": ch.get("username") or ch["slug"],
                        "title": (s.get("title") or "").strip(), "viewers": s.get("viewer_count") or 0,
                        "thumb": _kimg(s.get("thumbnail")), "cat": ((s.get("category") or {}).get("name") or "")})
        return out
    return _cached(("k", cat, lang), STREAMS_TTL, fetch)


def streams(plat, cat="", lang="it"):
    return (twitch_streams if plat == "tw" else kick_streams)(cat, lang)


def categories(plat):
    return (twitch_categories if plat == "tw" else kick_categories)()

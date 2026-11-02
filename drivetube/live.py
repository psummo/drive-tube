"""TV in diretta da liste M3U (IPTV): elenchi configurati in admin, lettura delle
liste, scelta della variante HLS da passare a ffmpeg. Non usa yt-dlp."""
import hashlib
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from .access import ACCESS, ACCESS_LOCK, access_save
from .config import UA
from .context import print, sthread

# lista predefinita: i canali italiani in chiaro raccolti dal progetto Free-TV/IPTV
# (solo stream pubblici e gratuiti). Ⓖ nel nome = visibile solo dall'Italia.
DEFAULT_LISTS = [{
    "id": "it",
    "name": "TV italiane",
    "url": "https://raw.githubusercontent.com/Free-TV/IPTV/master/playlists/playlist_italy.m3u8",
}]
LIST_TTL = 6 * 3600       # una lista si ricarica (in background) dopo 6 ore
MAX_LIST = 8 << 20        # una lista oltre 8 MB non e' una lista di canali
GEO = "Ⓖ"            # Ⓖ

VOD_EXT = (".mp4", ".mkv", ".m4v", ".mov", ".avi", ".webm", ".ts", ".mpg", ".mpeg")

_cache = {}               # id lista -> {"t", "rows", "err"}
_cache_lock = threading.Lock()
_loading = {}             # id lista -> Event


def m3u_lists():
    """Liste configurate: [{"id", "name", "url"}]. Mai toccate: quella predefinita."""
    lst = ACCESS.get("m3u")
    if isinstance(lst, list):
        return [x for x in lst if isinstance(x, dict) and x.get("id") and x.get("url")]
    return [dict(x) for x in DEFAULT_LISTS]


def list_by_id(lid):
    for x in m3u_lists():
        if x["id"] == lid:
            return x
    return None


def m3u_add(name, url):
    url = (url or "").strip()
    name = re.sub(r"[\x00-\x1f<>]", "", (name or "").strip())[:60]
    if not re.match(r"^https?://[^\s]+$", url):
        return "indirizzo non valido: deve iniziare con http:// o https://"
    with ACCESS_LOCK:
        lst = m3u_lists()
        if any(x["url"] == url for x in lst):
            return "lista gia' presente"
        lid = hashlib.sha1(url.encode()).hexdigest()[:6]
        lst.append({"id": lid, "name": name or urllib.parse.urlsplit(url).hostname or "lista", "url": url})
        ACCESS["m3u"] = lst
        access_save()
    return ""


def m3u_del(lid):
    with ACCESS_LOCK:
        lst = [x for x in m3u_lists() if x["id"] != lid]
        ACCESS["m3u"] = lst
        access_save()
    with _cache_lock:
        _cache.pop(lid, None)


def m3u_reset():
    """Torna alla sola lista predefinita."""
    with ACCESS_LOCK:
        ACCESS.pop("m3u", None)
        access_save()


# ---- lettura della lista ----

_attr = re.compile(r'([\w-]+)="([^"]*)"')


def parse_m3u(text):
    """Canali di una lista M3U: [{"name", "logo", "group", "url", "ua", "ref", "geo", "key"}].
    Riconosce #EXTINF (tvg-logo, group-title), #EXTVLCOPT (user agent, referer),
    #EXTGRP e gli header in coda all'URL alla Kodi (url|User-Agent=...)."""
    rows, info, opts, grp = [], None, {}, ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF"):
            head, _, name = line.partition(",")
            # la virgola puo' stare anche dentro un attributo tra virgolette
            if head.count('"') % 2:
                q = line.rfind('"')
                head, name = line[:q + 1], line[q + 1:].lstrip(" ,")
            a = dict((k.lower(), v) for k, v in _attr.findall(head))
            m = re.match(r"#EXTINF:\s*(-?[\d.]+)", head)
            try:
                dur = int(float(m.group(1))) if m else -1
            except ValueError:
                dur = -1
            info = {"name": name.strip() or a.get("tvg-name", ""), "logo": a.get("tvg-logo", ""),
                    "group": a.get("group-title", ""), "dur": dur}
        elif line.startswith("#EXTVLCOPT:"):
            k, _, v = line[11:].partition("=")
            k = k.strip().lower()
            if k == "http-user-agent":
                opts["ua"] = v.strip()
            elif k in ("http-referrer", "http-referer"):
                opts["ref"] = v.strip()
        elif line.startswith("#EXTGRP:"):
            grp = line[8:].strip()
        elif line.startswith("#"):
            continue
        else:
            url, _, extra = line.partition("|")
            if not re.match(r"^https?://", url) or info is None:
                info, opts = None, {}
                continue
            row = dict(info)
            row["group"] = row["group"] or grp
            row["url"] = url
            row["ua"] = opts.get("ua", "")
            row["ref"] = opts.get("ref", "")
            for kv in extra.split("&"):
                k, _, v = kv.partition("=")
                k = k.strip().lower()
                v = urllib.parse.unquote(v.strip())
                if k == "user-agent":
                    row["ua"] = v
                elif k in ("referer", "referrer"):
                    row["ref"] = v
            row["geo"] = GEO in row["name"]
            row["name"] = row["name"].replace(GEO, "").strip() or "canale"
            if not re.match(r"^https?://", row["logo"]):
                row["logo"] = ""
            row["key"] = hashlib.sha1(url.encode()).hexdigest()[:8]
            # film ed episodi: una durata vera in #EXTINF (le dirette hanno -1 o 0)
            # oppure un file video invece di una playlist
            path = urllib.parse.urlsplit(url).path.lower()
            row["vod"] = row["dur"] > 0 or path.endswith(VOD_EXT)
            rows.append(row)
            info, opts = None, {}
    return rows


def _fetch_list(lst):
    req = urllib.request.Request(lst["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=25) as r:
        data = r.read(MAX_LIST + 1)
    if len(data) > MAX_LIST:
        raise ValueError("lista troppo grande")
    rows = parse_m3u(data.decode("utf-8", "replace"))
    if not rows:
        raise ValueError("nessun canale nella lista")
    return rows


def _load(lst):
    t0 = time.time()
    try:
        rows = _fetch_list(lst)
        with _cache_lock:
            _cache[lst["id"]] = {"t": time.time(), "rows": rows, "err": ""}
        print("lista %s: %d canali in %.1f s" % (lst["name"], len(rows), time.time() - t0), flush=True)
    except Exception as e:
        msg = str(e)[:200]
        with _cache_lock:
            old = _cache.get(lst["id"]) or {"rows": []}
            # lista vecchia tenuta (meglio di niente), nuovo tentativo tra 5 minuti
            _cache[lst["id"]] = {"t": time.time() - LIST_TTL + 300, "rows": old["rows"], "err": msg}
        print("lista %s non caricata: %s" % (lst["name"], msg), flush=True)


def channels(lid, wait=True):
    """(canali, errore) della lista `lid`. Una lista gia' letta si mostra subito
    e, se vecchia, si ricarica dietro le quinte."""
    lst = list_by_id(lid)
    if not lst:
        return [], "lista sconosciuta"
    with _cache_lock:
        hit = _cache.get(lid)
        stale = not hit or time.time() - hit["t"] > LIST_TTL
        ev = _loading.get(lid)
        start = stale and ev is None
        if start:
            ev = _loading[lid] = threading.Event()
    if start:
        def run():
            try:
                _load(lst)
            finally:
                with _cache_lock:
                    _loading.pop(lid, None)
                ev.set()
        sthread(run).start()
    if (not hit or not hit["rows"]) and wait and ev is not None:
        ev.wait(40)
        with _cache_lock:
            hit = _cache.get(lid)
    if not hit:
        return [], "lista in caricamento"
    return hit["rows"], hit["err"]


def list_info():
    """Per l'admin: le liste con numero di canali, errore ed eta'."""
    out = []
    for x in m3u_lists():
        with _cache_lock:
            hit = _cache.get(x["id"])
        out.append(dict(x, count=len(hit["rows"]) if hit else None, err=hit["err"] if hit else "",
                        age=int(time.time() - hit["t"]) if hit and not hit["err"] else None))
    return out


def m3u_reload(lid):
    with _cache_lock:
        if lid in _cache:
            _cache[lid]["t"] = 0
    return channels(lid)


def channel(lid, key):
    rows, _ = channels(lid)
    for i, r in enumerate(rows):
        if r["key"] == key:
            return i, r
    return -1, None


# ---- dalla pagina del canale agli URL per ffmpeg ----

class LiveError(RuntimeError):
    pass


def http_headers(row):
    return {"User-Agent": row.get("ua") or UA, **({"Referer": row["ref"]} if row.get("ref") else {})}


def ff_headers(row):
    """(user agent, intestazioni) da passare a ffmpeg per questo canale."""
    h = ""
    if row.get("ref"):
        h = "Referer: " + row["ref"] + "\r\n"
    return row.get("ua") or UA, h


_attr_hls = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def _hls_attrs(line):
    return dict((k, v.strip('"')) for k, v in _attr_hls.findall(line.split(":", 1)[1] if ":" in line else ""))


def pick_variant(text, base, max_h=720, h264_only=False):
    """Da una playlist HLS "master" sceglie la variante piu' alta fino a max_h
    (se possibile H.264) e l'eventuale traccia audio separata.
    Restituisce (url video, url audio o None) oppure None se non e' una master."""
    lines = [x.strip() for x in text.splitlines()]
    if not any(x.startswith("#EXT-X-STREAM-INF") for x in lines):
        return None
    audio = {}   # gruppo -> uri (la predefinita se ce n'e' una)
    for x in lines:
        if x.startswith("#EXT-X-MEDIA:"):
            a = _hls_attrs(x)
            if a.get("TYPE") == "AUDIO" and a.get("URI"):
                g = a.get("GROUP-ID", "")
                if g not in audio or a.get("DEFAULT") == "YES":
                    audio[g] = urllib.parse.urljoin(base, a["URI"])
    vars_ = []
    for i, x in enumerate(lines):
        if not x.startswith("#EXT-X-STREAM-INF"):
            continue
        a = _hls_attrs(x)
        uri = next((y for y in lines[i + 1:] if y and not y.startswith("#")), "")
        if not uri:
            continue
        m = re.match(r"(\d+)x(\d+)", a.get("RESOLUTION", ""))
        h = int(m.group(2)) if m else 0
        codecs = a.get("CODECS", "")
        avc = (not codecs) or "avc1" in codecs
        hevc = "hvc1" in codecs or "hev1" in codecs
        vars_.append({"h": h, "bw": int(a.get("BANDWIDTH") or 0), "avc": avc, "hevc": hevc,
                      "url": urllib.parse.urljoin(base, uri), "audio": audio.get(a.get("AUDIO", ""))})
    if not vars_:
        return None
    # il player decodifica H.264 e H.265: meglio H.264 (costa meno), poi H.265
    pool = ([v for v in vars_ if v["avc"]] or [v for v in vars_ if v["hevc"]]) if h264_only else vars_
    pool = pool or vars_
    fit = [v for v in pool if v["h"] and v["h"] <= max_h] or [v for v in pool if not v["h"]] or pool
    # la piu' alta che sta nel limite; a parita' la banda maggiore. Senza
    # risoluzione dichiarata si sceglie per banda (la media, per prudenza)
    if any(v["h"] for v in fit):
        best = max(fit, key=lambda v: (v["h"], v["bw"]))
    else:
        fit = sorted(fit, key=lambda v: v["bw"])
        best = fit[len(fit) // 2]
    return best["url"], best["audio"]


def stream_of(row, max_h=720, h264_only=False):
    """URL video e audio (None se nel video) per ffmpeg. Segue i redirect (es. i
    "relinker" Rai) e, se la playlist ha piu' qualita', sceglie la variante.
    Solleva LiveError con un messaggio comprensibile."""
    req = urllib.request.Request(row["url"], headers=http_headers(row))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            final = r.geturl()
            ctype = (r.headers.get("Content-Type") or "").lower()
            head = r.read(512 << 10) if ("mpegurl" in ctype or "text" in ctype or final.split("?")[0].endswith(".m3u8")) else b""
    except urllib.error.HTTPError as e:
        if e.code in (403, 451):
            raise LiveError("canale non visibile da qui (errore %d): probabilmente trasmesso solo in Italia" % e.code)
        if e.code == 404:
            raise LiveError("canale non trovato (404): la lista potrebbe essere vecchia")
        raise LiveError("il canale ha risposto con errore %d" % e.code)
    except Exception as e:
        raise LiveError("canale non raggiungibile: %s" % str(e)[:120])
    text = head.decode("utf-8", "replace")
    if text and "#EXTM3U" not in text[:200] and "<html" in text[:500].lower():
        raise LiveError("il canale ha risposto con una pagina web invece del video (forse geo-bloccato)")
    v = pick_variant(text, final, max_h, h264_only) if text else None
    if v:
        return v[0], v[1]
    return final, None

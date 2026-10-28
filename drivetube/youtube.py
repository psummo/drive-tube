"""Acquisizione dati da YouTube tramite yt-dlp: ricerca, canali, feed personale, suggeriti,
informazioni su un video e URL degli stream. Non dipende da ffmpeg ne' dal player: si puo'
usare da solo per altro."""
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from .config import UA, COOKIES, feed_cache
from .context import in_session, print
from .access import ACCESS, use_cookies
from .tools import find_node, ytdlp_bin


# Cookie che identificano l'account Google: senza questi resta solo il "visitatore"
COOKIE_ACCOUNT = re.compile(r"SID|LOGIN_INFO")


def cookies_youtube_only(text, anon=False):
    """Tiene solo i cookie di youtube.com (un export del browser contiene anche
    banca, posta, ecc.), uno per nome (il piu' recente). Con anon=True toglie
    anche quelli dell'account. Ritorna (testo, tenuti, scartati)."""
    best, dropped = {}, 0
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        p = line.split("\t")
        if len(p) != 7:
            dropped += 1
            continue
        dom = p[0][len("#HttpOnly_"):] if p[0].startswith("#HttpOnly_") else p[0]
        if not (dom == "youtube.com" or dom.endswith(".youtube.com")) or (anon and COOKIE_ACCOUNT.search(p[5])):
            dropped += 1
            continue
        key = (dom.lstrip("."), p[2], p[5])
        exp = int(p[4]) if p[4].isdigit() else 0
        if key in best:
            dropped += 1
            if exp <= best[key][0]:
                continue
        best[key] = (exp, line)
    out = "# Netscape HTTP Cookie File\n" + "".join(v[1] + "\n" for v in best.values())
    return out, len(best), dropped


# Rete verso YouTube, scelta dalla pagina admin: client di YouTube da usare,
# solo IPv4, proxy. Dagli IP dei server YouTube a volte blocca un client o gli
# indirizzi IPv6 ma non gli altri; la diagnosi in admin prova le combinazioni.
YT_VARIANTS = [
    ("", False), ("", True),
    ("tv", False), ("tv", True),
    ("mweb", False), ("web_safari", False), ("web_embedded", False), ("android_vr", True),
]


def yt_net(net=None):
    n = ACCESS.get("yt") if net is None else net
    n = n if isinstance(n, dict) else {}
    out = []
    if n.get("ipv4"):
        out.append("-4")
    if n.get("client"):
        out += ["--extractor-args", "youtube:player_client=" + n["client"]]
    if n.get("proxy"):
        out += ["--proxy", n["proxy"]]
    return out


def yt_net_label(n):
    t = ("client " + n["client"]) if n.get("client") else "client predefiniti"
    if n.get("ipv4"):
        t += ", solo IPv4"
    if n.get("proxy"):
        t += ", proxy"
    return t


def yt_cmd(*args, net=None):
    cmd = [ytdlp_bin()]
    if use_cookies():
        cmd += ["--cookies", COOKIES]
    cmd += yt_net(net)
    # yt-dlp attiva da solo solo Deno: se c'e' soltanto Node va abilitato,
    # altrimenti alcune richieste a YouTube falliscono
    if not shutil.which("deno"):
        node, _ = find_node()
        if node:
            cmd += ["--js-runtimes", "node:" + node]
    cmd += list(args)
    return cmd


def search_yt(q):
    ck = ("s", q, use_cookies())
    hit = feed_cache.get(ck)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    p = subprocess.run(
        yt_cmd('--flat-playlist', '--print', '%(id)s\t%(title)s\t%(channel_id)s\t%(url)s', 'ytsearch16:' + q),
        text=True, capture_output=True,
    )
    rows = []
    for line in p.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        vid, title = parts[0].strip(), parts[1].strip()
        url = parts[3].strip() if len(parts) > 3 else ''
        kind = 'channel' if vid.startswith('UC') or '/channel/' in url or '/@' in url else 'video'
        rows.append((vid, title, kind, url))
    feed_cache[ck] = (time.time(), rows)
    return rows


def flat_rows(url, limit=8, start=1):
    end = start + limit - 1
    key = (url, start, end, use_cookies())
    hit = feed_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        p = subprocess.run(
            yt_cmd(
                "--flat-playlist",
                "--playlist-start", str(start),
                "--playlist-end", str(end),
                "--print", "%(id)s\t%(title)s",
                url,
            ),
            text=True, capture_output=True, timeout=30,
        )
    except Exception as e:
        print("elenco non riuscito:", e, flush=True)
        return []
    rows = []
    for line in p.stdout.splitlines():
        if "\t" not in line:
            continue
        vid, title = line.split("\t", 1)
        vid = vid.strip()
        if len(vid) != 11:
            continue
        rows.append((vid, title.strip()))
    feed_cache[key] = (time.time(), rows)
    if not rows:
        err = (p.stderr or "")[-200:]
        print("elenco vuoto:", err, flush=True)
    return rows


def video_meta(vid):
    """(titolo, canale) di un video, da usare per cercare contenuti simili."""
    ck = ("m", vid, use_cookies())
    hit = feed_cache.get(ck)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    p = subprocess.run(
        yt_cmd("--print", "%(title)s\t%(channel)s", "--no-playlist",
               "https://www.youtube.com/watch?v=" + vid),
        text=True, capture_output=True,
    )
    title = chan = ""
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0].strip() and parts[0].strip() != "NA":
            title = parts[0].strip()
            chan = parts[1].strip() if len(parts) > 1 and parts[1].strip() != "NA" else ""
            break
    feed_cache[ck] = (time.time(), (title, chan))
    return title, chan


def videos_from_search(query, exclude=None, limit=12):
    """Solo i video (niente canali) da una ricerca, escludendo alcuni id."""
    exclude = exclude or set()
    rows = []
    for r in search_yt(query):
        vid, title = r[0], r[1]
        kind = r[2] if len(r) > 2 else 'video'
        if kind == 'video' and vid and vid not in exclude:
            exclude.add(vid)
            rows.append((vid, title))
        if len(rows) >= limit:
            break
    return rows


def related_of(vid):
    # Su IP datacenter YouTube blocca playlist/mix e pagine canale (youtube:tab);
    # la ricerca invece passa. Cerchiamo video simili dal canale e dal titolo.
    if not vid:
        return []
    ck = ("r", vid, use_cookies())
    hit = feed_cache.get(ck)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    title, chan = video_meta(vid)
    seen = {vid}
    rows = []
    for query in ([chan] if chan else []) + ([title] if title else []):
        rows += videos_from_search(query, seen, 12 - len(rows))
        if len(rows) >= 12:
            break
    feed_cache[ck] = (time.time(), rows)
    return rows


def account_sections():
    jobs = [
        ("Iscrizioni", "https://www.youtube.com/feed/subscriptions", 1, 15),
        ("Iscrizioni", "https://www.youtube.com/feed/subscriptions", 16, 15),
        ("Iscrizioni", "https://www.youtube.com/feed/subscriptions", 31, 15),
        ("Più tardi", "https://www.youtube.com/playlist?list=WL", 1, 12),
        ("Mi piace", "https://www.youtube.com/playlist?list=LL", 1, 12),
    ]
    out = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(in_session(flat_rows), url, n, start): name for name, url, start, n in jobs}
        for fut in futs:
            name = futs[fut]
            try:
                rows = fut.result()
            except Exception as e:
                print("elenco home non riuscito:", e, flush=True)
                rows = []
            got = out.setdefault(name, [])
            have = set(v for v, t in got)
            for vid, title in rows:
                if vid not in have:
                    got.append((vid, title))
                    have.add(vid)
    return [(name, out.get(name) or []) for name in ("Iscrizioni", "Più tardi", "Mi piace")]


def channel_videos(cid, name=None):
    if cid.startswith('http'):
        url = cid.rstrip('/') + '/videos'
    elif cid.startswith('@'):
        url = 'https://www.youtube.com/' + cid + '/videos'
    else:
        url = 'https://www.youtube.com/channel/' + cid + '/videos'
    rows = flat_rows(url, 24)
    # pagina canale bloccata dall'IP (youtube:tab): ripieghiamo sulla ricerca
    if not rows and name:
        rows = videos_from_search(name, None, 12)
    return rows


# Client provati in ordine quando YouTube rifiuta lo stream (403) anche con URL
# appena presi: dagli IP dei server capita che un client venga respinto e un
# altro no.
FALLBACK_CLIENTS = ["tv", "web_safari", "mweb", "android_vr"]


def net_with_client(client):
    n = dict(ACCESS.get("yt") or {})
    n["client"] = client
    return n


# ---- un singolo video: informazioni e URL degli stream ----

class StreamError(RuntimeError):
    """yt-dlp non ha dato gli URL. kind: 'bot' (verifica anti-bot), 'none'
    (nessuno stream), 'failed' (altro errore, per esempio formato assente)."""

    def __init__(self, kind, detail):
        super().__init__(detail)
        self.kind = kind


def stream_urls(page_url, fmt, net=None):
    """URL diretti degli stream (video e, se separato, audio) per il formato
    yt-dlp `fmt`. Solleva StreamError."""
    p = subprocess.run(
        yt_cmd("-g", "--no-playlist", "--user-agent", UA, "-f", fmt, page_url, net=net),
        text=True, capture_output=True,
    )
    if p.returncode != 0:
        err = p.stderr or ""
        print(err, flush=True)
        kind = "bot" if ("Sign in to confirm" in err or "not a bot" in err) else "failed"
        raise StreamError(kind, err.strip().splitlines()[-1][:200] if err.strip() else "yt-dlp fallito")
    warn = [x for x in (p.stderr or "").splitlines() if x.startswith("WARNING")]
    for x in warn[:4]:
        print("yt-dlp:", x[:220], flush=True)
    lines = [x for x in p.stdout.strip().splitlines() if x.startswith("http")]
    if not lines:
        raise StreamError("none", "nessuno stream")
    return lines


def video_info(page_url):
    """{"duration": secondi (0 se ignota), "is_live": bool, "title": str},
    oppure None se yt-dlp non risponde."""
    p = subprocess.run(
        yt_cmd("--print", "%(duration)s\t%(is_live)s\t%(live_status)s\t%(title)s", "--no-playlist", page_url),
        text=True, capture_output=True,
    )
    for line in reversed(p.stdout.splitlines()):
        parts = line.strip().split("\t")
        if len(parts) < 2:
            continue
        print("info video:", repr(line.strip()), flush=True)
        title = "\t".join(parts[3:]).strip() if len(parts) > 3 else ""
        live = parts[1].strip().lower() in ("true", "1") or (len(parts) > 2 and parts[2].strip().lower() == "is_live")
        try:
            dur = int(float(parts[0]))
        except ValueError:
            dur = 0
        return {"duration": dur, "is_live": live, "title": "" if title == "NA" else title}
    print("info video non disponibili", (p.stderr or "")[-400:], flush=True)
    return None


def channel_of(page_url):
    """Id del canale (UC...) di un video, o ""."""
    try:
        p = subprocess.run(
            yt_cmd("--print", "%(channel_id)s", "--no-playlist", page_url),
            text=True, capture_output=True, timeout=20,
        )
    except Exception as e:
        print("canale del video:", e, flush=True)
        return ""
    for line in p.stdout.splitlines():
        if line.strip().startswith("UC"):
            return line.strip()
    return ""

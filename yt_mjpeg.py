#!/usr/bin/env python3
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
import select
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

VERSION = "11.29"
UPDATE_URL = "https://raw.githubusercontent.com/psummo/drive-tube/main/yt_mjpeg.py"

PORT = int(os.environ.get("YT_PORT") or 8090)

# Impostazioni modificabili dal pannello dell'app. I default stanno qui nel codice;
# le scelte dell'utente vivono nel cookie "yt_cfg" del browser e, se manca o e'
# incompleto, si usano i default.
# (chiave, nome, spiegazione, default, [(valore, etichetta), ...])
SETTINGS = [
    ("HEIGHT", "Risoluzione", "Altezza dei frame. Piu' alta e' piu' nitida ma pesa di piu'.", 720,
     [(360, "360p"), (480, "480p"), (720, "720p"), (1080, "1080p")]),
    ("FPS", "Fluidita'", "Fotogrammi al secondo.", 15,
     [(10, "10"), (15, "15"), (20, "20"), (25, "25"), (30, "30")]),
    ("Q", "Qualita' video", "Compressione JPEG: piu' e' alta, piu' dati servono.", 10,
     [(15, "Minima"), (10, "Bassa"), (6, "Media"), (3, "Alta")]),
    ("AUDIO_KBPS", "Qualita' audio", "Bitrate dell'MP3.", 64,
     [(32, "32k"), (48, "48k"), (64, "64k"), (96, "96k"), (128, "128k"), (192, "192k")]),
    ("AUDIO_BUFFER_SEC", "Buffer audio", "Secondi di audio caricati prima di partire.", 4,
     [(2, "2 s"), (4, "4 s"), (8, "8 s"), (15, "15 s")]),
]
SETTINGS_UI = [
    {"key": k, "label": n, "help": h, "options": [[v, l] for v, l in opts]}
    for k, n, h, d, opts in SETTINGS
]
DEFAULTS = {k: d for k, n, h, d, opts in SETTINGS}
CFG = dict(DEFAULTS)
HEIGHT = CFG["HEIGHT"]
FPS = CFG["FPS"]
Q = CFG["Q"]
AUDIO_KBPS = CFG["AUDIO_KBPS"]
AUDIO_BUFFER_SEC = CFG["AUDIO_BUFFER_SEC"]
feed_cache = {}


def cookie_value(header, name):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return unquote(v)
    return ""


def settings_from_cookie(header):
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(cookie_value(header, "yt_cfg") or "{}")
    except ValueError:
        data = {}
    if isinstance(data, dict):
        for k, n, h, d, opts in SETTINGS:
            v = data.get(k)
            if type(v) is int and v in [o[0] for o in opts]:
                cfg[k] = v
    return cfg


def apply_settings(header):
    """Applica le impostazioni del cookie. Ritorna True se sono cambiate."""
    global CFG, HEIGHT, FPS, Q, AUDIO_KBPS, AUDIO_BUFFER_SEC
    new = settings_from_cookie(header)
    if new == CFG:
        return False
    CFG = new
    HEIGHT = CFG["HEIGHT"]
    FPS = CFG["FPS"]
    Q = CFG["Q"]
    AUDIO_KBPS = CFG["AUDIO_KBPS"]
    AUDIO_BUFFER_SEC = CFG["AUDIO_BUFFER_SEC"]
    print("impostazioni:", CFG, flush=True)
    return True


def recent_from_cookie(header):
    try:
        rows = json.loads(cookie_value(header, "yt_recent") or "[]")
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [x for x in rows if isinstance(x, str) and x][:10]


def push_recent(q, rows):
    q = (q or "").strip().replace("\n", " ")
    if not q:
        return rows
    return ([q] + [x for x in rows if x.lower() != q.lower()])[:10]


def recent_cookie_header(rows):
    return "yt_recent=%s; Max-Age=31536000; Path=/; SameSite=Lax" % quote(json.dumps(rows, ensure_ascii=False), safe="")


FIFO_V = "/tmp/yt_vid.mjpg"
FIFO_A = "/tmp/yt_aud.mp3"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = "User-Agent: " + UA + "\r\nReferer: https://www.youtube.com/\r\n"
COOKIES = os.path.expanduser("~/cookies.txt")


def yt_cmd(*args):
    cmd = ["yt-dlp"]
    if os.path.isfile(COOKIES):
        cmd += ["--cookies", COOKIES]
    cmd += list(args)
    return cmd

PAGE_URL = sys.argv[1] if len(sys.argv) > 1 else ""


def set_page(url):
    global PAGE_URL
    PAGE_URL = url
    state['duration'] = 0
    state['seek_base'] = 0.0
    threading.Thread(target=fetch_duration, daemon=True).start()
    new_session(0)


def search_yt(q):
    hit = feed_cache.get(("s", q))
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    p = subprocess.run(
        yt_cmd('--flat-playlist', '--print', '%(id)s\t%(title)s\t%(channel_id)s\t%(url)s', 'ytsearch8:' + q),
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
    feed_cache[("s", q)] = (time.time(), rows)
    return rows


def flat_rows(url, limit=8, start=1):
    end = start + limit - 1
    key = (url, start, end)
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
        print("flat", url, e, flush=True)
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
        print("flat vuoto", url, err, flush=True)
    return rows


def related_of(vid):
    if not vid:
        return []
    return flat_rows("https://www.youtube.com/playlist?list=RD" + vid, 8)


def account_sections():
    if not os.path.isfile(COOKIES):
        return [("<p>manca ~/cookies.txt</p>", [])]
    jobs = [
        ("Iscrizioni", "https://www.youtube.com/feed/subscriptions", 1, 15),
        ("Iscrizioni", "https://www.youtube.com/feed/subscriptions", 16, 15),
        ("Iscrizioni", "https://www.youtube.com/feed/subscriptions", 31, 15),
        ("Più tardi", "https://www.youtube.com/playlist?list=WL", 1, 12),
        ("Mi piace", "https://www.youtube.com/playlist?list=LL", 1, 12),
    ]
    out = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(flat_rows, url, n, start): name for name, url, start, n in jobs}
        for fut in futs:
            name = futs[fut]
            try:
                rows = fut.result()
            except Exception as e:
                print("feed", name, e, flush=True)
                rows = []
            got = out.setdefault(name, [])
            have = set(v for v, t in got)
            for vid, title in rows:
                if vid not in have:
                    got.append((vid, title))
                    have.add(vid)
    return [(name, out.get(name) or []) for name in ("Iscrizioni", "Più tardi", "Mi piace")]


def cards(rows, seen, cur):
    html = []
    for vid, title in rows:
        if not vid or vid == cur or vid in seen:
            continue
        seen.add(vid)
        html.append(
            '<a class="card" href="/watch?v=%s"><img src="https://i.ytimg.com/vi/%s/hqdefault.jpg"><span>%s</span></a>'
            % (vid, vid, title.replace("<", ""))
        )
    return html


def channel_videos(cid):
    if cid.startswith('http'):
        url = cid.rstrip('/') + '/videos'
    elif cid.startswith('@'):
        url = 'https://www.youtube.com/' + cid + '/videos'
    else:
        url = 'https://www.youtube.com/channel/' + cid + '/videos'
    return flat_rows(url, 24)
lock = threading.Lock()
state = {
    "seek_base": 0.0,
    "t_play": None,
    "paused": False,
    "gen": 0,
    "ff": None,
    "has_audio": False,
    "duration": 0,
    "v_q": deque(),
    "a_q": deque(),
    "v_evt": threading.Event(),
    "a_evt": threading.Event(),
    "ff_gen": -1,
    "ff_done": -1,
    "v_made": 0,
    "a_read": 0,
    "fails": 0,
    "rate": 0,
}


def next_of_current():
    if not PAGE_URL or "v=" not in PAGE_URL:
        return []
    cur = PAGE_URL.split("v=")[-1][:11]
    rows = [(vid, title) for vid, title in related_of(cur) if vid != cur]
    if len(rows) < 4:
        try:
            p = subprocess.run(
                yt_cmd("--print", "%(channel_id)s", "--no-playlist", PAGE_URL),
                text=True, capture_output=True, timeout=20,
            )
            cid = ""
            for line in p.stdout.splitlines():
                line = line.strip()
                if line.startswith("UC"):
                    cid = line
                    break
            if cid:
                for vid, title in channel_videos(cid):
                    if vid != cur and vid not in [x[0] for x in rows]:
                        rows.append((vid, title))
                    if len(rows) >= 8:
                        break
        except Exception as e:
            print("next canale", e, flush=True)
    return [{"id": vid, "title": title} for vid, title in rows[:8]]


def fetch_duration():
    if state["duration"] or not PAGE_URL:
        return
    p = subprocess.run(
        yt_cmd("--print", "duration", "--no-playlist", PAGE_URL),
        text=True,
        capture_output=True,
    )
    for line in reversed(p.stdout.splitlines()):
        try:
            state["duration"] = int(float(line.strip()))
            print("duration", state["duration"], flush=True)
            return
        except ValueError:
            continue
    print("duration sconosciuta", p.stderr[-400:], flush=True)


def now_pos():
    if state["paused"] or state["t_play"] is None:
        return state["seek_base"]
    return max(0.0, state["seek_base"] + (time.perf_counter() - state["t_play"]))


def ytdlp_urls():
    p = subprocess.run(
        yt_cmd(
            "-g",
            "--no-playlist",
            "--user-agent",
            UA,
            "-f",
            "bv*[height<=720]+ba/bv*+ba/b",
            PAGE_URL,
        ),
        text=True,
        capture_output=True,
    )
    if p.returncode != 0:
        err = p.stderr or ""
        print(err, flush=True)
        if "Sign in to confirm" in err or "not a bot" in err:
            state["err"] = "Cookie scaduti: YouTube chiede di verificare che non sei un robot."
        else:
            state["err"] = "yt-dlp fallito. Controlla il log."
        raise RuntimeError(state["err"])
    lines = [x for x in p.stdout.strip().splitlines() if x.startswith("http")]
    if not lines:
        state["err"] = "Nessuno stream. Cookie scaduti o video bloccato."
        raise RuntimeError(state["err"])
    for i, u in enumerate(lines):
        print("url[%d] %s ..." % (i, u[:80]), flush=True)
    return lines


def client_gone(handler):
    try:
        ready, _, _ = select.select([handler.connection], [], [], 0)
        if not ready:
            return False
        peek = socket.socket.recv(handler.connection, 1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        return peek == b""
    except BlockingIOError:
        return False
    except Exception:
        return True


def arm_stop(reason):
    print(reason, "- ffmpeg resta aperto 8s", flush=True)
    state["hold_until"] = time.time() + 8
    def later():
        time.sleep(8)
        with lock:
            due = state.get("hold_until") or 0
            if not due or time.time() + 0.3 < due:
                return
            state["hold_until"] = 0
            state["gen"] += 1
            stop_ff()
            print("ffmpeg spento, nessuno in ascolto", flush=True)
    threading.Thread(target=later, daemon=True).start()


def cancel_stop():
    if state.get("hold_until"):
        print("ffmpeg tenuto aperto", flush=True)
    state["hold_until"] = 0


def ffmpeg_alive():
    p = state.get("ff")
    return bool(p and p.poll() is None)


def stop_ff():
    p = state["ff"]
    if p and p.poll() is None:
        p.kill()
        try:
            p.wait(timeout=2)
        except Exception:
            pass
    state["ff"] = None
    state["rate"] = 0
    state["v_q"].clear()
    state["a_q"].clear()
    state["v_evt"].clear()
    state["a_evt"].clear()


def ensure_fifo(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    os.mkfifo(path)


def unblock_fifo(path):
    # se un pump e' fermo su open() perche' ffmpeg e' morto prima di aprire
    # l'uscita, apro e chiudo il lato scrittura: il pump riceve EOF ed esce
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


V_CAP = 6000


def pump_jpegs(gen, path):
    try:
        with open(path, "rb") as fh:
            buf = b""
            nbytes = 0
            idle = 0.0
            t0 = time.perf_counter()
            while state["gen"] == gen:
                if len(state["v_q"]) >= V_CAP:
                    # coda piena: smetto di leggere e ffmpeg si ferma da solo.
                    # Prima qui si scartavano frame e il video saltava avanti.
                    time.sleep(0.1)
                    idle += 0.1
                    continue
                chunk = fh.read(65536)
                if not chunk:
                    break
                nbytes += len(chunk)
                now = time.perf_counter()
                if now - t0 >= 5:
                    state["rate"] = nbytes / max(0.5, now - t0 - idle) / 1e6
                    print("video %.1f MB/s coda %d frame" % (state["rate"], len(state["v_q"])), flush=True)
                    nbytes = 0
                    idle = 0.0
                    t0 = now
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s != -1 else -1
                    if s == -1 or e == -1:
                        if s > 0:
                            buf = buf[s:]
                        break
                    state["v_q"].append(buf[s : e + 2])
                    state["v_made"] += 1
                    buf = buf[e + 2 :]
                    state["v_evt"].set()
    except Exception as e:
        print("pump video", e, flush=True)


def pump_audio(gen, path, skip=0):
    try:
        with open(path, "rb") as fh:
            n = 0
            while state["gen"] == gen:
                chunk = fh.read(4096)
                if not chunk:
                    break
                n += len(chunk)
                state["a_read"] = n
                if skip > 0:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk = chunk[skip:]
                    skip = 0
                state["a_q"].append(chunk)
                state["a_evt"].set()
            print("pump audio end (%d bytes)" % n, flush=True)
    except Exception as e:
        print("pump audio", e, flush=True)


def ffmpeg_cmd(vsrc, asrc, pos):
    net = [
        "-user_agent", UA, "-headers", HEADERS,
        "-ss", "%.3f" % pos, "-readrate", "16",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
    ]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"] + net + ["-i", vsrc]
    vout = [
        "-map", "0:v:0", "-an",
        "-vf", "scale=-2:%d" % HEIGHT, "-r", str(FPS), "-q:v", str(Q),
        "-f", "mjpeg", FIFO_V,
    ]
    if not asrc:
        return cmd + vout
    if asrc == vsrc:
        amap = "0:a:0"
    else:
        cmd += net + ["-i", asrc]
        amap = "1:a:0"
    # niente tag ID3 ne' header Xing: lo stream audio si puo' riprendere a meta'
    aout = [
        "-map", amap, "-vn",
        "-c:a", "libmp3lame", "-b:a", "%dk" % AUDIO_KBPS,
        "-id3v2_version", "0", "-write_xing", "0",
        "-f", "mp3", FIFO_A,
    ]
    return cmd + vout + aout


def read_err(p, tail):
    n = 0
    try:
        for raw in p.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            tail.append(line)
            n += 1
            if n <= 20:
                print("ff:", line[:240], flush=True)
    except Exception:
        pass


def spawn_ff(gen, pos, vsrc, asrc, askip=0):
    ensure_fifo(FIFO_V)
    ensure_fifo(FIFO_A)
    state["v_made"] = 0
    state["a_read"] = 0
    state["urls"] = (vsrc, asrc)
    tail = deque(maxlen=12)
    p = subprocess.Popen(
        ffmpeg_cmd(vsrc, asrc, pos),
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    state["ff"] = p
    print("ffmpeg @ %.1fs url=%d" % (pos, 1 if asrc == vsrc else 2), flush=True)
    threads = [threading.Thread(target=pump_jpegs, args=(gen, FIFO_V), daemon=True)]
    if asrc:
        threads.append(threading.Thread(target=pump_audio, args=(gen, FIFO_A, askip), daemon=True))
    for t in threads:
        t.start()
    threading.Thread(target=read_err, args=(p, tail), daemon=True).start()
    threading.Thread(target=watch_ff, args=(gen, p, pos, threads, tail), daemon=True).start()


def watch_ff(gen, p, pos, threads, tail):
    # Sorveglia ffmpeg. Se si chiude prima della fine del video (YouTube che
    # taglia la connessione, URL scaduto, 403) riparte dal punto esatto in cui
    # si era fermato, senza svuotare le code: il browser non se ne accorge.
    p.wait()
    unblock_fifo(FIFO_V)
    unblock_fifo(FIFO_A)
    for t in threads:
        while t.is_alive() and state["gen"] == gen:
            t.join(timeout=1)
    with lock:
        if state["gen"] != gen or state["ff"] is not p:
            return
        made = pos + state["v_made"] / float(FPS)
        apos = pos + state["a_read"] * 8.0 / (AUDIO_KBPS * 1000)
        dur = state["duration"] or 0
        rc = p.returncode
        if rc == 0 and (not dur or made >= dur - 3 or (state["v_made"] == 0 and pos >= dur - 30)):
            state["ff_done"] = gen
            print("ffmpeg finito a %.0fs" % made, flush=True)
            return
        print("ffmpeg interrotto a %.1fs su %ds (codice %s, %d frame)" % (made, dur, rc, state["v_made"]), flush=True)
        for line in list(tail)[-3:]:
            print("  ff:", line[:240], flush=True)
        if state["v_made"] >= FPS * 10:
            state["fails"] = 0
        else:
            state["fails"] = state.get("fails", 0) + 1
        fails = state["fails"]
        if fails >= 6:
            state["err"] = "403 dopo %d tentativi: YouTube interrompe lo stream" % fails
            print(state["err"], flush=True)
            return
        urls = state.get("urls")
        state["ff"] = None
    time.sleep(min(fails, 5) + 0.5)
    if fails >= 2 or not urls:
        try:
            lst = ytdlp_urls()
        except Exception as e:
            print("riavvio fallito:", e, flush=True)
            return
        urls = (lst[0], lst[1] if len(lst) > 1 else lst[0])
    with lock:
        if state["gen"] != gen or state["ff"] is not None:
            return
        askip = int(max(0.0, apos - made) * AUDIO_KBPS * 1000 / 8)
        print("riparto da %.1fs (audio avanti %.2fs)" % (made, max(0.0, apos - made)), flush=True)
        spawn_ff(gen, made, urls[0], urls[1], askip)


def start_ff():
    # una sola partenza per sessione: un secondo /stream.mp3 (o un ffmpeg gia'
    # finito) non deve svuotare le code ne' rilanciare ffmpeg
    gen = state["gen"]
    if state.get("ff_gen") == gen:
        return
    stop_ff()
    state["ff_gen"] = gen
    state["fails"] = 0
    pos = int(now_pos())
    lock.release()
    try:
        urls = ytdlp_urls()
    except Exception:
        lock.acquire()
        if state["gen"] == gen:
            state["ff_gen"] = -1
        raise
    lock.acquire()
    if state["gen"] != gen:
        return
    vsrc = urls[0]
    asrc = urls[1] if len(urls) > 1 else urls[0]
    state["has_audio"] = True
    state["seek_base"] = float(pos)
    state["t_play"] = time.perf_counter()
    state["paused"] = False
    state["err"] = ""
    spawn_ff(gen, pos, vsrc, asrc)


def new_session(pos=None):
    with lock:
        if pos is not None:
            state["seek_base"] = max(0.0, pos)
        state["t_play"] = None
        state["paused"] = False
        state["err"] = ""
        state["gen"] += 1
        stop_ff()


HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt</title>
<style>
  html,body{margin:0;height:100%;background:#000;color:#fff;font-family:sans-serif}
  #screen{width:100%;height:100%;object-fit:contain;background:#000;display:none}
  #overlay{
    position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
    background:rgba(0,0,0,.72);z-index:3;color:#fff;flex-direction:column;
  }
  #msg{font-size:28px;margin-bottom:24px;text-align:center;white-space:pre-line;line-height:1.35}
  #hud{position:fixed;top:14px;left:0;right:0;text-align:center;z-index:7;font-size:20px;color:#fff;text-shadow:0 1px 4px #000;pointer-events:none}
  #go{
    width:160px;height:160px;border-radius:50%;background:#fff;color:#000;border:0;
    font-size:72px;display:none;
  }
  #prog{position:fixed;left:12px;right:12px;bottom:24px;height:28px;background:transparent;z-index:4}
  #track{position:absolute;left:0;right:0;top:11px;height:6px;background:#333}
  #fill{display:block;height:100%;width:0;background:#fff}
  #time{position:fixed;right:12px;bottom:40px;font-size:14px;z-index:4}
  #log{display:none}
  #menu,#homebtn{
    position:fixed;top:12px;z-index:30;width:48px;height:48px;border:0;border-radius:8px;
    background:rgba(0,0,0,.55);color:#fff;font-size:26px;
  }
  #menu{left:12px}
  #homebtn{right:12px;display:none;text-decoration:none;text-align:center;line-height:48px}
  #side{
    position:fixed;top:0;left:0;bottom:0;width:440px;max-width:94vw;background:#141414;z-index:20;
    transform:translateX(-110%);transition:transform .2s;display:flex;flex-direction:column;
    pointer-events:none;
  }
  #side.on{transform:translateX(0);pointer-events:auto}
  #tabs{display:flex;gap:8px;padding:12px}
  #tabs button{flex:1;height:44px;border:0;border-radius:8px;background:#222;color:#fff;font-size:20px}
  #tabs button.on{background:#fff;color:#000}
  #panel{flex:1;overflow:auto;padding:8px 14px 24px}
  .row{display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid #333}
  .row p{margin:0;flex:1}
  .row b{display:block}
  .row small{color:#aaa}
  .row i{min-width:64px;text-align:center;font-style:normal;font-size:20px}
  .row button{width:40px;height:40px;border:0;border-radius:8px;background:#333;color:#fff;font-size:22px}
  .row button.wide{width:auto;padding:0 14px;font-size:16px}
  .row2{padding:10px 0;border-bottom:1px solid #333}
  .row2 p{margin:0 0 8px}
  .row2 b{display:block}
  .row2 small{color:#aaa}
  .opts{display:flex;flex-wrap:wrap;gap:6px}
  .opts button{min-width:56px;height:40px;padding:0 12px;border:0;border-radius:8px;background:#333;color:#fff;font-size:16px}
  .opts button.on{background:#fff;color:#000}
  #resetcfg{width:100%;height:44px;margin-top:10px;border:0;border-radius:8px;background:#333;color:#fff;font-size:16px}
  #logbox{white-space:pre-wrap;font:12px/1.4 monospace;color:#ddd}
  #nexts a, #nexts a:visited, #nexts a:hover{color:#fff;text-decoration:none}
  #savecfg{width:100%;height:48px;margin-top:16px;border:0;border-radius:8px;background:#fff;color:#000;font-size:18px}
  #freeze{position:fixed;inset:0;width:100%;height:100%;display:none;z-index:2;background:#000}
</style>
<img id="v" alt="">
<canvas id="screen"></canvas>
<canvas id="freeze"></canvas>
<div id="hud"></div>
<div id="overlay">
  <div id="msg">Loading...</div>
  <div id="ver" style="position:fixed;left:12px;bottom:12px;z-index:40;color:#aaa;font-size:13px">__VER__</div>
<button id="go" type="button">&#9654;</button>
</div>
<div id="prog"><div id="track"><i id="fill"></i></div></div>
<div id="time"></div>
<button id="menu" type="button">&#9881;</button>
<a id="homebtn" href="/home">&#8962;</a>
<div id="side">
  <div id="tabs">
    <button type="button" id="tabcfg" class="on">&#9881;</button>
    <button type="button" id="tablog">>_</button>
  </div>
  <div id="panel"></div>
</div>
<div id="log"></div>
<div id="nexts" style="display:none;position:fixed;left:8px;right:8px;bottom:58px;z-index:12;gap:8px;overflow:auto;background:rgba(0,0,0,.55);padding:8px"></div>
<script>
var DURATION = __DUR__;
var FPS = __FPS__;
var VID = 'WATCHID';
var BUF_SEC = 15;
var BUF_CAP = 6000;
var rateMb = 0;
var serverFrames = 0;
var syncOrigin = 0;
var playOrigin = 0;
var stalled = false;
var startAt = __START__;
var pos = startAt;
var playing = false;
var tick = null;
var gen = 0;
var warmV = null;
var warmA = null;
var screen = document.getElementById('screen');
var vAbort = null;
var frames = [];
var playTimer = null;
var vUrl = null;
var freezeC = document.getElementById('freeze');
var overlay = document.getElementById('overlay');
var actx = null;
var audioAbort = null;
var audioNext = 0;
var audioOn = false;
var audioCursor = 0;
var msg = document.getElementById('msg');
var go = document.getElementById('go');
var ppLabel = 'pausa';
var audioBase = 0;
var shownFrames = 0;
var drawnT = 0;
var painting = false;
var paintGen = 0;
var frameBase = 0;
var vEnded = false;
function statusLine(){
  var slow = rateMb > 0 && rateMb < 1;
  var head = slow ? ('Linea lenta ' + rateMb.toFixed(2) + ' MB/s') : (rateMb ? (rateMb.toFixed(2) + ' MB/s') : 'misuro la linea');
  return head + '\\n' + 'browser ' + frames.length + ' frame' + '\\n' + 'server ' + serverFrames + ' frame';
}
function showStatus(extra){
  overlay.style.display = 'flex';
  msg.style.display = 'block';
  go.style.display = 'none';
  msg.textContent = statusLine() + (extra ? '\\n' + extra : '');
}
var rateAt = 0;
function pollRate(){
  var now = Date.now();
  if (now - rateAt < 1000) return;
  rateAt = now;
  fetch('/buf').then(function(r){ return r.text(); }).then(function(t){
    var p = t.split(' ');
    var bits = (p[0] || '').split('/');
    serverFrames = parseInt(bits[0], 10) || 0;
    var v = parseFloat(p[1]);
    if (!isNaN(v) && v > 0) rateMb = v;
    var hud = document.getElementById('hud');
    if (hud) {
      var desync = '';
      if (actx && syncOrigin && drawnT > 0) {
        var ear = earTime();
        var diff = ear - drawnT;
        if (Math.abs(diff) > 0.15) desync = (diff > 0 ? '  audio avanti ' : '  video avanti ') + Math.abs(diff).toFixed(1) + 's';
        else desync = '  sync ok';
      }
      hud.textContent = 'browser ' + frames.length + '  server ' + serverFrames + (rateMb ? '  ' + rateMb.toFixed(2) + ' MB/s' : '') + desync;
    }
  }).catch(function(){});
}
function ui(line){
  var box = document.getElementById('log');
  var row = document.createElement('div');
  row.textContent = line;
  box.appendChild(row);
  while (box.childNodes.length > 8) box.removeChild(box.childNodes[1]);
}

function fmt(s){
  s = Math.max(0, Math.floor(s));
  var m = Math.floor(s/60), r = s%60;
  return m + ':' + (r<10?'0':'') + r;
}
function draw(){
  var p = DURATION ? Math.min(100, (pos/DURATION)*100) : 0;
  document.getElementById('fill').style.width = p + '%';
  document.getElementById('time').textContent = fmt(pos) + ' / ' + fmt(DURATION);
}
function pollDur(){
  if (DURATION) return;
  fetch('/dur').then(function(r){ return r.text(); }).then(function(t){
    var d = parseInt(t, 10) || 0;
    if (d > 0) { DURATION = d; draw(); }
    else setTimeout(pollDur, 1500);
  }).catch(function(){ setTimeout(pollDur, 3000); });
}
pollDur();
function startClock(){
  playing = true;
  if (tick) clearInterval(tick);
  tick = setInterval(function(){
    if (!playing) return;
    if (DURATION && pos > DURATION) pos = DURATION;
    draw();
    history.replaceState(null, '', '/?watch=' + VID + '&t=' + Math.floor(pos));
  }, 1000);
}
function stopClock(){ playing = false; }
function stopWarm(){
  if (warmV) { warmV.abort(); warmV = null; }
  if (warmA) { warmA.abort(); warmA = null; }
}
function preload(auto){
  var my = ++gen;
  stopWarm();
  overlay.style.display = 'flex';
  msg.style.display = 'block';
  msg.textContent = 'Loading...';
  go.style.display = 'none';
  screen.style.display = 'none';
  stopVideo();
  stopAudio();
  stopClock();
  draw();
  fetch('/prime').catch(function(){});
  function poll(){
    if (my !== gen) return;
    fetch('/status').then(function(r){ return r.text(); }).then(function(err){
      if (my !== gen) return;
      if (err && (err.indexOf('Cookie') === 0 || err.indexOf('yt-dlp') === 0 || err.indexOf('Nessuno stream') === 0 || err.indexOf('403 dopo') === 0)) {
        msg.textContent = err;
        go.style.display = 'block';
        ui(err);
        return;
      }
      if (err) { setTimeout(poll, 800); return; }
      fetch('/ready').then(function(r){ return r.text(); }).then(function(t){
        if (my !== gen) return;
        if (t === 'ok') { msg.textContent = 'In attesa del play'; go.style.display = 'block'; }
        else setTimeout(poll, 800);
      });
    }).catch(function(){ if (my === gen) setTimeout(poll, 1000); });
  }
  poll();
}
function stopVideo(){
  paintGen++;
  painting = false;
  if (vAbort) { vAbort.abort(); vAbort = null; }
  if (playTimer) { clearInterval(playTimer); playTimer = null; }
  frames = [];
  vEnded = false;
  if (vUrl) { URL.revokeObjectURL(vUrl); vUrl = null; }
}
function findBytes(u8, a, b, from){
  for (var i = from || 0; i < u8.length - 1; i++)
    if (u8[i] === a && u8[i + 1] === b) return i;
  return -1;
}
function startVideoBuf(){
  stopVideo();
  var ctrl = new AbortController();
  vAbort = ctrl;
  var my = gen;
  var pending = new Uint8Array(0);
  var started = false;
  screen.style.display = 'none';
  fetch('/stream.mjpg?t=' + Date.now(), {signal: ctrl.signal}).then(function(res){
    var reader = res.body.getReader();
    function read(){
      return reader.read().then(function(r){
        if (r.done && !ctrl.signal.aborted && my === gen) { vEnded = true; return; }
        if (r.done || ctrl.signal.aborted || my !== gen) return;
        pending = concatU8(pending, r.value);
        while (true) {
          var s = findBytes(pending, 0xff, 0xd8, 0);
          var e = s < 0 ? -1 : findBytes(pending, 0xff, 0xd9, s + 2);
          if (s < 0 || e < 0) {
            if (s > 0) pending = pending.slice(s);
            break;
          }
          frames.push(pending.slice(s, e + 2));
          pending = pending.slice(e + 2);
        }
        if (pending.length > 1500000) pending = pending.slice(-2000);
        if (frames.length >= BUF_CAP) {
          return new Promise(function(ok){ setTimeout(ok, 200); }).then(read);
        }
        return read();
      });
    }
    return read();
  }).catch(function(){});
}
var audioUrl = '';
function stallPlay(reason){
  if (!stalled) {
    stalled = true;
    stopClock();
    if (actx && actx.state === 'running') actx.suspend();
  }
  showStatus(reason || ('fermo, restano ' + (frames.length / FPS).toFixed(1) + 's di frame'));
}
function resumePlay(){
  stalled = false;
  overlay.style.display = 'none';
  if (!userPaused) startClock();
}
function sliceBuf(buf, skipSec){
  var rate = buf.sampleRate;
  var skip = Math.floor(skipSec * rate);
  var len = buf.length - skip;
  if (len < 200) return null;
  var out = actx.createBuffer(buf.numberOfChannels, len, rate);
  for (var c = 0; c < buf.numberOfChannels; c++) out.getChannelData(c).set(buf.getChannelData(c).subarray(skip));
  return out;
}
function scheduleBuf(buf){
  if (!buf) return;
  if (!audioArm || !actx) { if (audioHold.length < 30) { audioHold.push(buf); if (actx) audioNext = actx.currentTime + audioHold.reduce(function(s,b){ return s + b.duration; }, 0); } return; }
  var frameT = drawnT;
  if (audioCursor + 0.4 < frameT) {
    var late = frameT - audioCursor;
    if (buf.duration <= late) { audioCursor += buf.duration; return; }
    var cut = sliceBuf(buf, late);
    audioCursor += late;
    buf = cut;
    if (!buf) return;
  }
  var when = syncOrigin + audioCursor;
  if (when < actx.currentTime - 0.2) { audioCursor += buf.duration; return; }
  var src = actx.createBufferSource();
  src.buffer = buf;
  src.connect(actx.destination);
  if (when < actx.currentTime + 0.02) when = actx.currentTime + 0.02;
  try { src.start(when); audioSources.push(src); } catch (e) {}
  audioCursor += buf.duration;
  audioNext = syncOrigin + audioCursor;
}
function flushHeldAudio(){
  if (!audioArm || !audioHold.length) return;
  var pending = audioHold;
  audioHold = [];
  for (var i = 0; i < pending.length; i++) scheduleBuf(pending[i]);
}
function earTime(){
  if (!actx || !syncOrigin) return drawnT;
  return actx.currentTime - syncOrigin;
}
function holdEar(){
  var ear = earTime();
  if (!actx || userPaused || stalled) return ear;
  if (ear > drawnT + 0.12) {
    if (actx.state === 'running') actx.suspend();
    ear = earTime();
  } else if (actx.state === 'suspended' && drawnT >= ear - 0.03) {
    actx.resume();
    ear = earTime();
  }
  return ear;
}
function audioMustWait(){
  if (!actx || !audioOn || !syncOrigin || !audioArm) return false;
  flushHeldAudio();
  if (audioCursor + 0.2 < drawnT) return true;
  return false;
}
function showVideo(){
  screen.style.display = 'block';
  freezeC.style.display = 'none';
  overlay.style.display = 'none';
  stalled = false;
  startClock();
  draw();
  shownFrames = 0;
  drawnT = 0;
  frameBase = pos;
  painting = false;
  paintGen++;
  if (playTimer) clearInterval(playTimer);
  playTimer = setInterval(function(){
    if (userPaused) {
      if (actx && actx.state === 'running') actx.suspend();
      var hud = document.getElementById('hud');
      if (hud) hud.textContent = 'pausa, buffer ' + frames.length + ' frame';
      return;
    }
    pollRate();
    if (frames.length < FPS * 5 && !vEnded) {
      stallPlay();
      showStatus('buffer ' + frames.length + ', carico');
      return;
    }
    if (actx && syncOrigin && audioOn && !userPaused) {
      var ear = earTime();
      if (ear > drawnT + 0.5 && frames.length > FPS * 2) {
        overlay.style.display = 'flex';
        msg.style.display = 'block';
        msg.textContent = 'riallineamento in corso';
        go.style.display = 'none';
        while (frames.length > 2 && drawnT + 1/FPS < ear - 0.1) {
          frames.shift();
          drawnT += 1/FPS;
          shownFrames++;
        }
        pos = frameBase + drawnT;
        draw();
        if (Math.abs(ear - drawnT) < 0.3) overlay.style.display = 'none';
        return;
      } else if (overlay.style.display === 'flex' && msg.textContent === 'riallineamento in corso') {
        overlay.style.display = 'none';
      }
    }
    if (stalled) {
      if (frames.length < FPS * 10 && !vEnded) {
        showStatus('fermo a ' + frames.length + ', carico buffer');
        return;
      }
      resumePlay();
      playOrigin = performance.now() - drawnT * 1000;
      // durante lo stallo actx era sospeso: orologio audio e video fermi insieme,
      // quindi l'audio gia' programmato e' ancora allineato. Basta ripartire.
      if (actx && audioOn && actx.state === 'suspended' && earTime() <= drawnT + 0.12) actx.resume();
    }
    if (audioHold.length && audioArm) flushHeldAudio();
    if (painting) return;
    var elapsed = (performance.now() - playOrigin) / 1000;
    if (drawnT + 1 / FPS > elapsed + 0.04) return;
    while (frames.length > 2 && drawnT + 2 / FPS < elapsed) {
      frames.shift();
      drawnT += 1 / FPS;
    }
    if (!frames.length) {
      if (vEnded) {
        clearInterval(playTimer);
        playTimer = null;
        stopClock();
        draw();
        showNext(true);
      }
      return;
    }
    var jpg = frames.shift();
    shownFrames++;
    var token = ++paintGen;
    painting = true;
    createImageBitmap(new Blob([jpg], {type: 'image/jpeg'})).then(function(bmp){
      painting = false;
      if (token !== paintGen) { bmp.close && bmp.close(); return; }
      if (screen.width !== bmp.width) {
        screen.width = bmp.width;
        screen.height = bmp.height;
      }
      screen.getContext('2d').drawImage(bmp, 0, 0);
      bmp.close && bmp.close();
      drawnT += 1 / FPS;
      pos = frameBase + drawnT;
      draw();
      if (!userPaused && !stalled) holdEar();
    }).catch(function(){ painting = false; });
  }, 1000 / FPS);
  syncOrigin = actx ? actx.currentTime : 0;
  audioArm = syncOrigin;
  audioCursor = 0;
  playOrigin = performance.now();
  flushHeldAudio();
}
function hold(){
  stopClock();
  if (screen.width) {
    freezeC.width = screen.width;
    freezeC.height = screen.height;
    freezeC.getContext('2d').drawImage(screen, 0, 0);
    freezeC.style.display = 'block';
  }
}
function ensureCtx(){
  if (!actx) actx = new (window.AudioContext || window.webkitAudioContext)();
  return actx.state === 'suspended' ? actx.resume() : Promise.resolve();
}
var audioHold = [];
var audioSources = [];
var audioArm = null;
function stopAudio(){
  audioOn = false;
  audioArm = null;
  if (audioAbort) { audioAbort.abort(); audioAbort = null; }
  audioNext = 0;
  audioCursor = 0;
  audioHold = [];
  for (var i = 0; i < audioSources.length; i++) {
    try { audioSources[i].stop(); } catch (e) {}
  }
  audioSources = [];
  if (actx && actx.state === 'running') actx.suspend();
}
function concatU8(a, b){
  var c = new Uint8Array(a.length + b.length);
  c.set(a, 0); c.set(b, a.length);
  return c;
}
function syncAt(u8, from){
  for (var i = from; i < u8.length - 1; i++)
    if (u8[i] === 0xff && (u8[i + 1] & 0xe0) === 0xe0) return i;
  return -1;
}
function playAudioStream(url){
  stopAudio();
  var ctrl = new AbortController();
  audioAbort = ctrl;
  audioOn = true;
  var pending = new Uint8Array(0);
  var started = false;
  var my = gen;
  function armVideo(){
    if (started || my !== gen) return;
    started = true;
    showVideo();
  }
  return ensureCtx().then(function(){
    audioNext = actx.currentTime + 0.25;
    return fetch(url, {signal: ctrl.signal});
  }).then(function(res){
    if (!res.ok) { armVideo(); return; }
    var reader = res.body.getReader();
    function read(){
      if (actx && audioNext - actx.currentTime > 8) {
        return new Promise(function(ok){ setTimeout(ok, 300); }).then(read);
      }
      return reader.read().then(function(r){
        if (r.done && !ctrl.signal.aborted && my === gen && pending.length > 1000) {
          var rest = pending.buffer.slice(pending.byteOffset, pending.byteOffset + pending.byteLength);
          pending = new Uint8Array(0);
          return actx.decodeAudioData(rest).then(function(buf){
            if (!ctrl.signal.aborted && my === gen) scheduleBuf(buf);
          }).catch(function(){});
        }
        if (r.done || ctrl.signal.aborted || my !== gen) return;
        pending = concatU8(pending, r.value);
        if (pending.length < 48000) return read();
        var cut = syncAt(pending, 24000);
        if (cut < 1) {
          if (pending.length > 200000) pending = pending.slice(-16000);
          return read();
        }
        var piece = pending.slice(0, cut);
        pending = pending.slice(cut);
        var copy = piece.buffer.slice(piece.byteOffset, piece.byteOffset + piece.byteLength);
        return actx.decodeAudioData(copy).then(function(buf){
          if (ctrl.signal.aborted || my !== gen) return;
          scheduleBuf(buf);
          return read();
        }).catch(function(){ return read(); });
      });
    }
    return read();
  }).catch(function(){});
}
function startBoth(){
  stopWarm();
  var t = Date.now();
  if (startAt) pos = startAt;
  overlay.style.display = 'flex';
  msg.style.display = 'block';
  msg.textContent = 'Caricamento buffer 0';
  go.style.display = 'none';
  fetch('/ctrl?pos=' + (startAt || pos || 0)).then(function(){
    audioUrl = '/stream.mp3?t=' + t;
    playAudioStream(audioUrl);
    startVideoBuf();
  });
    var watch = setInterval(function(){
      fetch('/status').then(function(r){ return r.text(); }).then(function(err){
        if (err && (err.indexOf('Cookie') === 0 || err.indexOf('yt-dlp') === 0 || err.indexOf('Nessuno stream') === 0 || err.indexOf('403 dopo') === 0)) { msg.textContent = err; ui(err); clearInterval(watch); }
      });
      fetch('/buf').then(function(r){ return r.text(); }).then(function(t){
        var parts = t.split(' ');
        var n = parseInt(parts[0], 10) || 0;
        var rate = parseFloat(parts[1]) || 0;
        var slow = rate > 0 && rate < 1;
        var need = slow ? FPS * 12 : FPS * 4;
        msg.style.display = 'block';
        msg.textContent = (slow ? 'Linea lenta ' + rate.toFixed(1) + ' MB/s — ' : '') + 'Caricamento buffer ' + parts[0];
        if (n >= need && go.style.display === 'none') {
          go.style.display = 'block';
          ui('pronto');
        }
      });
    }, 1000);
}
var userPaused = false;
var clickAt = 0;
function showNext(on){
  document.getElementById('nexts').style.display = on ? 'flex' : 'none';
  if (on) loadNext();
}
function togglePlay(){
  var now = Date.now();
  if (now - clickAt < 700) return;
  clickAt = now;
  if (go.style.display !== 'none') { startBoth(); return; }
  fetch('/ctrl?pause=1').then(function(r){ return r.text(); }).then(function(t){
    if (t === 'paused') {
      userPaused = true;
      stopClock();
      if (actx && actx.state === 'running') actx.suspend();
      showNext(true);
      ui('pausa');
    } else {
      userPaused = false;
      showNext(false);
      if (actx && actx.state === 'suspended') actx.resume();
      flushHeldAudio();
      startClock();
      playOrigin = performance.now() - drawnT * 1000;
      ui('ripresa, buffer ' + frames.length);
    }
  }).catch(function(){
    userPaused = !userPaused;
    if (userPaused) {
      stopClock();
      if (actx && actx.state === 'running') actx.suspend();
      showNext(true);
    } else {
      showNext(false);
      if (actx && actx.state === 'suspended') actx.resume();
      flushHeldAudio();
      startClock();
      playOrigin = performance.now() - drawnT * 1000;
    }
  });
}
var fillWait = null;
function beginAt(sec){
  pos = Math.floor(sec || 0);
  startAt = pos;
  history.replaceState(null, '', '/?watch=' + VID + '&t=' + pos);
  if (fillWait) clearInterval(fillWait);
  var my = ++gen;
  stopVideo();
  stopAudio();
  stopClock();
  showStatus('apro lo stream a ' + pos + 's');
  var t0 = Date.now();
  fetch('/ctrl?pos=' + pos).then(function(){
    if (my !== gen) return;
    audioUrl = '/stream.mp3?t=' + Date.now();
    playAudioStream(audioUrl);
    startVideoBuf();
    var blocked = false;
    fillWait = setInterval(function(){
      if (my !== gen || blocked) { if (blocked) clearInterval(fillWait); return; }
      pollRate();
      fetch('/status').then(function(r){ return r.text(); }).then(function(err){
        if (my !== gen) return;
        if (err && (err.indexOf('Cookie') === 0 || err.indexOf('yt-dlp') === 0 || err.indexOf('Nessuno stream') === 0 || err.indexOf('403 dopo') === 0)) {
          blocked = true;
          clearInterval(fillWait);
          stopVideo();
          stopAudio();
          overlay.style.display = 'flex';
          msg.style.display = 'block';
          go.style.display = 'none';
          msg.innerHTML = err + '<br><a href="/cookies" style="color:#fff">carica i cookie nuovi</a>';
          ui(err);
        }
      });
      if (blocked) return;
      if (!rateMb && Date.now() - t0 < 8000) {
        showStatus('misuro la linea');
        return;
      }
      if (!frames.length && Date.now() - t0 > 12000) {
        showStatus('nessun frame. controllo cookie nel log');
        return;
      }
      var slow = rateMb > 0 && rateMb < 1;
      var need = slow ? FPS * 12 : FPS * 5;
      var audioReady = !audioOn || audioHold.length > 0;
      var elapsed_ms = Date.now() - t0;
      showStatus(slow ? 'linea lenta: carico ' + need + ' frame' : 'caricamento' + (frames.length >= need && !audioReady ? ' audio' : ''));
      if (frames.length >= need && (audioReady || elapsed_ms > 6000)) {
        clearInterval(fillWait);
        showVideo();
      }
    }, 300);
  });
}
go.onclick = function(){
  ensureCtx().then(function(){ beginAt(pos); });
};
screen.onclick = stageClick;
overlay.onclick = function(ev){
  if (ev.target === go) return;
  stageClick(ev);
};
var chromeOn = false;
var cfgNow = __CFG__;
var SETTINGS = __OPTS__;
function stageClick(ev){
  if (ev.target.closest && ev.target.closest('#side, #menu, #homebtn, #nexts, #prog')) return;
  if (go.style.display !== 'none') return;
  if (!chromeOn) {
    chromeOn = true;
    showNext(true);
    document.getElementById('homebtn').style.display = 'block';
    return;
  }
  togglePlay();
}
function saveCfgCookie(){
  var o = {};
  SETTINGS.forEach(function(f){ o[f.key] = cfgNow[f.key]; });
  document.cookie = 'yt_cfg=' + encodeURIComponent(JSON.stringify(o)) + '; max-age=31536000; path=/; samesite=lax';
}
function drawCfg(){
  var box = document.getElementById('panel');
  box.innerHTML = '';
  SETTINGS.forEach(function(f){
    var row = document.createElement('div');
    row.className = 'row2';
    row.innerHTML = '<p><b>' + f.label + '</b><small>' + f.help + '</small></p><div class="opts"></div>';
    var opts = row.querySelector('.opts');
    f.options.forEach(function(o){
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = o[1];
      if (cfgNow[f.key] === o[0]) b.className = 'on';
      b.onclick = function(ev){ ev.stopPropagation(); cfgNow[f.key] = o[0]; drawCfg(); };
      opts.appendChild(b);
    });
    box.appendChild(row);
  });
  var upd = document.createElement('div');
  upd.className = 'row';
  upd.innerHTML = '<p><b>AGGIORNAMENTI</b><small id="updtxt">Versione ' + document.getElementById('ver').textContent + '</small></p><button type="button" class="wide" id="updbtn">Controlla</button>';
  var updbtn = upd.querySelector('#updbtn');
  var updtxt = upd.querySelector('#updtxt');
  updbtn.onclick = function(ev){
    ev.stopPropagation();
    if (updbtn.getAttribute('data-mode') === 'install') {
      updbtn.disabled = true;
      updtxt.textContent = 'installo, il server si riavvia...';
      fetch('/update/install').then(function(r){ return r.json(); }).then(function(j){
        if (!j.ok) { updtxt.textContent = 'errore: ' + j.error; updbtn.disabled = false; return; }
        updtxt.textContent = 'installata ' + j.version + ', ricarico...';
        var tries = 0;
        (function wait(){
          setTimeout(function(){
            fetch('/health').then(function(r){ if (r.ok) location.reload(); else throw 0; }).catch(function(){ if (++tries < 40) wait(); else updtxt.textContent = 'riavvio lento, ricarica la pagina'; });
          }, 1500);
        })();
      }).catch(function(){ updtxt.textContent = 'errore di rete'; updbtn.disabled = false; });
      return;
    }
    updbtn.disabled = true;
    updtxt.textContent = 'controllo...';
    fetch('/update/check').then(function(r){ return r.json(); }).then(function(j){
      updbtn.disabled = false;
      if (j.error) { updtxt.textContent = j.error; return; }
      if (j.newer) {
        updtxt.textContent = 'Nuova versione ' + j.latest + ' (hai ' + j.current + ')';
        updbtn.textContent = 'Installa';
        updbtn.setAttribute('data-mode', 'install');
      } else {
        updtxt.textContent = 'Sei aggiornato (' + j.current + ')';
      }
    }).catch(function(){ updbtn.disabled = false; updtxt.textContent = 'errore di rete'; });
  };
  box.appendChild(upd);
  var save = document.createElement('button');
  save.id = 'savecfg';
  save.type = 'button';
  save.textContent = 'Salva e applica';
  save.onclick = function(ev){
    ev.stopPropagation();
    saveCfgCookie();
    box.innerHTML = '<p>salvo, riparto con le nuove impostazioni...</p>';
    location.reload();
  };
  box.appendChild(save);
  var reset = document.createElement('button');
  reset.id = 'resetcfg';
  reset.type = 'button';
  reset.textContent = 'Ripristina i default';
  reset.onclick = function(ev){
    ev.stopPropagation();
    document.cookie = 'yt_cfg=; max-age=0; path=/';
    location.reload();
  };
  box.appendChild(reset);
}
function openTab(which){
  document.getElementById('tabcfg').className = which === 'cfg' ? 'on' : '';
  document.getElementById('tablog').className = which === 'log' ? 'on' : '';
  var box = document.getElementById('panel');
  if (which === 'log') {
    box.innerHTML = '<div id="logbox">carico...</div>';
    fetch('/log.txt').then(function(r){ return r.text(); }).then(function(t){
      var el = document.getElementById('logbox');
      el.textContent = t;
      box.scrollTop = box.scrollHeight;
    });
    return;
  }
  if (!cfgNow) {
    box.textContent = 'config non letta';
    return;
  }
  drawCfg();
}
document.getElementById('menu').addEventListener('click', function(ev){
  ev.preventDefault();
  ev.stopPropagation();
  var side = document.getElementById('side');
  var open = side.className.indexOf('on') === -1;
  side.className = open ? 'on' : '';
  if (open) openTab('cfg');
}, true);
document.getElementById('tabcfg').onclick = function(ev){ ev.stopPropagation(); openTab('cfg'); };
document.getElementById('tablog').onclick = function(ev){ ev.stopPropagation(); openTab('log'); };
function seekAt(ev){
  if (!DURATION) return;
  var x = ev.clientX;
  if (ev.changedTouches && ev.changedTouches[0]) x = ev.changedTouches[0].clientX;
  var r = document.getElementById('prog').getBoundingClientRect();
  var sec = Math.floor(Math.max(0, Math.min(DURATION, ((x - r.left) / r.width) * DURATION)));
  pos = sec;
  draw();
  ensureCtx().then(function(){ beginAt(sec); });
}
document.getElementById('prog').onclick = seekAt;
document.getElementById('prog').ontouchend = function(ev){ ev.preventDefault(); seekAt(ev); };
draw();
preload();
window.addEventListener('pagehide', function(){
  fetch('/bye', {keepalive: true}).catch(function(){});
});
function loadNext(){
  fetch('/next').then(function(r){ return r.json(); }).then(function(list){
    var box = document.getElementById('nexts');
    box.innerHTML = '';
    (list || []).forEach(function(n){
      var a = document.createElement('a');
      a.href = '/watch?v=' + n.id;
      a.style.cssText = 'width:160px;background:#222;display:block;flex:0 0 auto';
      a.innerHTML = '<img src="https://i.ytimg.com/vi/' + n.id + '/hqdefault.jpg" style="width:100%;display:block"><span style="display:block;font-size:12px;padding:4px">' + (n.title || '').replace(/</g,'') + '</span>';
      box.appendChild(a);
    });
  }).catch(function(){});
}
loadNext();
</script>
"""


def _ver_key(v):
    nums = []
    for part in v.replace("-", ".").split("."):
        digits = "".join(c for c in part if c.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


def fetch_remote_source():
    import urllib.request
    req = urllib.request.Request(UPDATE_URL + "?t=%d" % int(time.time()), headers={"User-Agent": "drive-tube"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def remote_version(src):
    import re
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    return m.group(1) if m else ""


def check_update():
    try:
        remote = remote_version(fetch_remote_source())
    except Exception as e:
        return {"current": VERSION, "error": "controllo fallito: %s" % e}
    if not remote:
        return {"current": VERSION, "error": "versione remota non trovata"}
    return {"current": VERSION, "latest": remote, "newer": _ver_key(remote) > _ver_key(VERSION)}


def install_update():
    import py_compile
    import shutil
    import tempfile
    src = fetch_remote_source()
    remote = remote_version(src)
    if not remote:
        raise RuntimeError("versione remota non trovata")
    if _ver_key(remote) <= _ver_key(VERSION):
        raise RuntimeError("gia' aggiornato (%s)" % VERSION)
    me = os.path.abspath(__file__)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=os.path.dirname(me), encoding="utf-8")
    try:
        tmp.write(src)
        tmp.close()
        py_compile.compile(tmp.name, doraise=True)
        shutil.copy2(me, me + ".bak")
        os.replace(tmp.name, me)
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)
    print("aggiornato %s -> %s, riavvio" % (VERSION, remote), flush=True)
    return remote


def restart_self():
    time.sleep(1.0)
    try:
        stop_ff()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


WASMTEST_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test H.264 WASM</title>
<style>
body{background:#111;color:#eee;font:20px/1.35 sans-serif;margin:0;padding:14px}
h1{font-size:24px;margin:0 0 8px}
.r{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #333;padding:6px 0}
.r span:last-child{text-align:right}
.ok{color:#4f4;font-weight:bold}.ko{color:#f55;font-weight:bold}.na{color:#fa0;font-weight:bold}
canvas{display:none;width:100%;max-width:960px;background:#000;margin:8px 0}
#clock{font-size:28px;color:#6cf}
input,button{font-size:20px;background:#222;color:#fff;border:1px solid #555;padding:4px 8px}
button{background:#2a5;border:0;padding:8px 18px}
#bar{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;background:#1b1b1b;border:1px solid #333;padding:8px 10px;margin:0 0 10px}
#bar a{color:#6cf;text-decoration:none}
#bar .sp{flex:1}
#updbtn{background:#345;padding:6px 14px}
#updtxt{color:#aaa;font-size:17px}
</style></head><body>
<div id="bar">
  <a href="/">&larr; App</a>
  <a href="/test">Test browser</a>
  <span class="sp"></span>
  <span id="updtxt">Versione __VER__</span>
  <button type="button" id="updbtn">Controlla aggiornamenti</button>
</div>
<h1>Test H.264 in WebAssembly</h1>
<div id="clock"></div>
<div>Video YouTube (id, vuoto = quello aperto nell'app) <input id="vid" size="13">
 secondi <input id="sec" value="20" size="3"> <button id="go">Avvia</button></div>
<canvas id="cvgl"></canvas><canvas id="cv2d"></canvas>
<div id="out"></div>
<script>
var out = document.getElementById('out');
var t0 = Date.now();
setInterval(function(){ document.getElementById('clock').textContent = 'Contatore: ' + ((Date.now() - t0) / 1000).toFixed(1) + ' s'; }, 100);
function row(name, val, cls){
  var d = document.createElement('div'); d.className = 'r';
  d.innerHTML = '<span>' + name + '</span><span class="' + (cls || 'na') + '">' + val + '</span>';
  out.appendChild(d); return d.lastChild;
}
function sleep(ms){ return new Promise(function(ok){ setTimeout(ok, ms); }); }

// --- WASI minimo: il decoder non usa file ne' rete ---
var mem = null, ex = null, onFrame = function(){};
function dv(){ return new DataView(mem.buffer); }
var wasi = {
  environ_get: function(){ return 0; },
  environ_sizes_get: function(a, b){ dv().setUint32(a, 0, true); dv().setUint32(b, 0, true); return 0; },
  clock_time_get: function(id, prec, o){ dv().setBigUint64(o, BigInt(Math.round(performance.now() * 1e6)), true); return 0; },
  fd_close: function(){ return 0; }, fd_fdstat_get: function(){ return 52; }, fd_fdstat_set_flags: function(){ return 52; },
  fd_prestat_get: function(){ return 8; }, fd_prestat_dir_name: function(){ return 52; },
  fd_read: function(){ return 52; }, fd_seek: function(){ return 52; }, path_open: function(){ return 52; },
  fd_write: function(fd, iovs, n, nw){ var t = 0; for (var i = 0; i < n; i++) t += dv().getUint32(iovs + i * 8 + 4, true); dv().setUint32(nw, t, true); return 0; },
  proc_exit: function(c){ throw new Error('exit ' + c); }
};
function loadWasm(){
  return fetch('/h264.wasm').then(function(r){
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.arrayBuffer();
  }).then(function(buf){
    return WebAssembly.instantiate(buf, {
      env: { on_frame: function(){ onFrame.apply(null, arguments); } },
      wasi_snapshot_preview1: wasi
    });
  }).then(function(res){
    ex = res.instance.exports; mem = ex.memory; ex._initialize();
  });
}
var simdOk = (function(){
  try { return WebAssembly.validate(new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,123,3,2,1,0,10,10,1,8,0,65,0,253,15,253,98,11])); }
  catch (e) { return false; }
})();

// --- disegno: WebGL (YUV -> RGB sulla GPU) oppure conversione in WASM + putImageData ---
var cvgl = document.getElementById('cvgl'), cv2d = document.getElementById('cv2d');
var gl = null, glTex = [], glScale = null;
function initGL(){
  try { gl = cvgl.getContext('webgl', {antialias: false, depth: false, preserveDrawingBuffer: false}); } catch (e) { gl = null; }
  if (!gl) return false;
  var vs = 'attribute vec2 p;varying vec2 t;uniform vec2 s;void main(){t=vec2((p.x+1.0)*0.5,(1.0-p.y)*0.5);gl_Position=vec4(p,0.0,1.0);}';
  var fs = 'precision mediump float;varying vec2 t;uniform sampler2D Y,U,V;uniform vec2 sy,sc;void main(){' +
    'float y=1.1644*(texture2D(Y,vec2(t.x*sy.x,t.y)).r-0.0627);float u=texture2D(U,vec2(t.x*sc.x,t.y)).r-0.5;float v=texture2D(V,vec2(t.x*sc.x,t.y)).r-0.5;' +
    'gl_FragColor=vec4(y+1.7927*v,y-0.2132*u-0.5329*v,y+2.1124*u,1.0);}';
  function sh(type, src){ var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; }
  var pr = gl.createProgram();
  gl.attachShader(pr, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(pr);
  if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) { gl = null; return false; }
  gl.useProgram(pr);
  var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
  var loc = gl.getAttribLocation(pr, 'p'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  ['Y','U','V'].forEach(function(n, i){
    var tx = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, tx);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.uniform1i(gl.getUniformLocation(pr, n), i); glTex.push(tx);
  });
  glScale = [gl.getUniformLocation(pr, 'sy'), gl.getUniformLocation(pr, 'sc')];
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
  return true;
}
function drawGL(y, u, v, ly, lu, lv, w, h){
  if (cvgl.width !== w) { cvgl.width = w; cvgl.height = h; gl.viewport(0, 0, w, h); }
  var ch = (h + 1) >> 1, buf = mem.buffer;
  var planes = [[y, ly, h], [u, lu, ch], [v, lv, ch]];
  for (var i = 0; i < 3; i++) {
    gl.activeTexture(gl.TEXTURE0 + i);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, planes[i][1], planes[i][2], 0, gl.LUMINANCE, gl.UNSIGNED_BYTE,
      new Uint8Array(buf, planes[i][0], planes[i][1] * planes[i][2]));
  }
  gl.uniform2f(glScale[0], w / ly, 1); gl.uniform2f(glScale[1], ((w + 1) >> 1) / lu, 1);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}
var ctx2d = null;
function draw2D(y, u, v, ly, lu, lv, w, h){
  if (!ctx2d) ctx2d = cv2d.getContext('2d');
  if (cv2d.width !== w) { cv2d.width = w; cv2d.height = h; }
  var p = ex.to_rgba(y, u, v, ly, lu, lv, w, h);
  ctx2d.putImageData(new ImageData(new Uint8ClampedArray(mem.buffer, p, w * h * 4), w, h), 0, 0);
}
function show(which){ cvgl.style.display = which === 'gl' ? 'block' : 'none'; cv2d.style.display = which === '2d' ? 'block' : 'none'; }

// --- decodifica a pezzi, cedendo il controllo per non bloccare la pagina ---
var CHUNK = 16384;
function feedRange(data, from, to){
  var c = data.subarray(from, to), p = ex.in_buf(c.length);
  new Uint8Array(mem.buffer, p, c.length).set(c);
  return ex.feed(c.length);
}
async function bench(data, mode, label){
  if (ex.dec_init() !== 0) throw new Error('dec_init');
  var n = 0, cell = row(label, '...');
  onFrame = function(y, u, v, ly, lu, lv, w, h){
    n++;
    if (mode === 'gl') drawGL(y, u, v, ly, lu, lv, w, h);
    else if (mode === '2d') draw2D(y, u, v, ly, lu, lv, w, h);
  };
  show(mode);
  var busy = 0, t = performance.now(), last = t;
  for (var off = 0; off < data.length; off += CHUNK) {
    var a = performance.now();
    feedRange(data, off, Math.min(data.length, off + CHUNK));
    busy += performance.now() - a;
    if (performance.now() - last > 200) { last = performance.now(); cell.textContent = n + ' frame...'; await sleep(0); }
  }
  var a2 = performance.now(); ex.flush(); busy += performance.now() - a2;
  var fps = n / (busy / 1000);
  cell.textContent = fps.toFixed(0) + ' fps (' + n + ' frame)';
  return {n: n, fps: fps, cell: cell};
}

// --- riproduzione a velocita' reale, in loop ---
var playGen = 0;
function play(data, fps, mode){
  var my = ++playGen;
  var off = 0, decoded = 0, drawn = 0, dropped = 0, stalls = 0, target = 0;
  var cell = row('Riproduzione in tempo reale', '...');
  function pushNext(){
    var end = Math.min(data.length, off + 65536), c = data.subarray(off, end);
    var p = ex.in_buf(c.length);
    new Uint8Array(mem.buffer, p, c.length).set(c);
    ex.push(c.length); off = end;
  }
  function reset(){ ex.dec_init(); off = 0; }
  // un frame alla volta; a fine clip riparte dall'inizio
  function decodeOne(){
    for (var guard = 0; guard < 1000; guard++) {
      if (ex.left() === 0) {
        if (off >= data.length) { var n = ex.flush(); reset(); if (n > 0) return; }
        pushNext();
      }
      if (ex.next() > 0) return;
    }
  }
  reset();
  // disegno dentro la chiamata: dopo, la memoria del frame puo' essere riusata
  onFrame = function(y, u, v, ly, lu, lv, w, h){
    decoded++;
    if (decoded < target) { dropped++; return; }
    if (mode === 'gl') drawGL(y, u, v, ly, lu, lv, w, h); else draw2D(y, u, v, ly, lu, lv, w, h);
    drawn++;
  };
  show(mode);
  var start = performance.now(), statAt = start, statDrawn = 0;
  function tick(){
    if (my !== playGen) return;
    target = Math.floor((performance.now() - start) / 1000 * fps) + 1;
    var budget = performance.now() + 30;
    while (decoded < target && performance.now() < budget) decodeOne();
    if (target - decoded > fps) { start = performance.now() - decoded / fps * 1000; stalls++; }
    var now = performance.now();
    if (now - statAt >= 1000) {
      var rate = (drawn - statDrawn) / ((now - statAt) / 1000); statDrawn = drawn; statAt = now;
      cell.textContent = rate.toFixed(0) + ' fps mostrati su ' + fps.toFixed(0) + ', saltati ' + dropped + (stalls ? ', ritardi ' + stalls : '');
      cell.className = rate >= fps * 0.9 ? 'ok' : 'ko';
    }
    setTimeout(tick, 4);
  }
  tick();
}

async function run(){
  playGen++;
  out.innerHTML = '';
  row('WebAssembly SIMD', simdOk ? 'SI' : 'NO', simdOk ? 'ok' : 'ko');
  if (!simdOk) { row('Esito', 'questo browser non ha WASM SIMD, serve la versione senza SIMD', 'ko'); return; }
  var glOk = gl || initGL();
  row('WebGL', glOk ? 'SI' : 'NO', glOk ? 'ok' : 'ko');
  row('Pagina sicura (HTTPS)', window.isSecureContext ? 'SI' : 'NO (non serve)', 'na');
  var t = performance.now();
  try { if (!ex) await loadWasm(); }
  catch (e) { row('Decoder', 'errore: ' + e.message, 'ko'); return; }
  row('Decoder caricato', ((performance.now() - t) / 1000).toFixed(1) + ' s', 'ok');

  var sec = Math.max(3, Math.min(60, parseInt(document.getElementById('sec').value, 10) || 20));
  var vid = document.getElementById('vid').value.trim();
  var cell = row('Scarico ' + sec + ' s di H.264 da YouTube', '...');
  t = performance.now();
  var res;
  try { res = await fetch('/h264test?sec=' + sec + (vid ? '&v=' + encodeURIComponent(vid) : '')); }
  catch (e) { cell.textContent = 'errore di rete'; cell.className = 'ko'; return; }
  if (!res.ok) { cell.textContent = 'errore: ' + (await res.text()).slice(0, 160); cell.className = 'ko'; return; }
  var fmt = res.headers.get('X-Fmt') || '?';
  var tHead = (performance.now() - t) / 1000, tBody = performance.now();
  var reader = res.body.getReader(), parts = [], size = 0;
  while (true) {
    var r = await reader.read();
    if (r.done) break;
    parts.push(r.value); size += r.value.length;
    cell.textContent = (size / 1e6).toFixed(1) + ' MB';
  }
  var dl = (performance.now() - t) / 1000;
  var data = new Uint8Array(size), o = 0;
  parts.forEach(function(p){ data.set(p, o); o += p.length; });
  if (size < 1000) { cell.textContent = 'stream vuoto'; cell.className = 'ko'; return; }
  cell.textContent = (size / 1e6).toFixed(1) + ' MB in ' + dl.toFixed(1) + ' s'; cell.className = 'ok';
  row('di cui ricerca su YouTube (yt-dlp)', tHead.toFixed(1) + ' s', 'na');
  row('di cui trasferimento dati', ((performance.now() - tBody) / 1000).toFixed(1) + ' s', 'na');
  row('Formato YouTube', fmt, 'na');
  row('Peso del video', (size * 8 / sec / 1e6).toFixed(2) + ' Mbit/s (MJPEG di oggi: circa 40-50)', 'ok');
  await sleep(50);

  var b0 = await bench(data, 'none', 'Solo decodifica');
  var srcFps = b0.n / sec;
  var bGL = glOk ? await bench(data, 'gl', 'Decodifica + disegno WebGL') : null;
  var b2 = await bench(data, '2d', 'Decodifica + disegno senza WebGL');
  var best = bGL && bGL.fps > b2.fps ? bGL : b2, mode = best === bGL ? 'gl' : '2d';
  var ratio = best.fps / srcFps;
  row('Esito', (ratio >= 1.5 ? 'OK: margine x' : ratio >= 1.05 ? 'AL LIMITE: margine x' : 'TROPPO LENTO: x') + ratio.toFixed(1) +
      ' rispetto ai ' + srcFps.toFixed(0) + ' fps del video', ratio >= 1.5 ? 'ok' : ratio >= 1.05 ? 'na' : 'ko');
  play(data, srcFps, mode);
}
document.getElementById('go').onclick = function(){ run(); };

// aggiornamenti: stessa logica del pannello dell'app
(function(){
  var btn = document.getElementById('updbtn'), txt = document.getElementById('updtxt');
  btn.onclick = function(){
    if (btn.getAttribute('data-mode') === 'install') {
      btn.disabled = true;
      txt.textContent = 'installo, il server si riavvia...';
      fetch('/update/install').then(function(r){ return r.json(); }).then(function(j){
        if (!j.ok) { txt.textContent = 'errore: ' + j.error; btn.disabled = false; return; }
        txt.textContent = 'installata ' + j.version + ', ricarico...';
        var tries = 0;
        (function wait(){
          setTimeout(function(){
            fetch('/health').then(function(r){ if (r.ok) location.reload(); else throw 0; })
              .catch(function(){ if (++tries < 40) wait(); else txt.textContent = 'riavvio lento, ricarica la pagina'; });
          }, 1500);
        })();
      }).catch(function(){ txt.textContent = 'errore di rete'; btn.disabled = false; });
      return;
    }
    btn.disabled = true;
    txt.textContent = 'controllo...';
    fetch('/update/check').then(function(r){ return r.json(); }).then(function(j){
      btn.disabled = false;
      if (j.error) { txt.textContent = j.error; return; }
      if (j.newer) {
        txt.textContent = 'Nuova versione ' + j.latest + ' (hai ' + j.current + ')';
        btn.textContent = 'Installa';
        btn.setAttribute('data-mode', 'install');
      } else {
        txt.textContent = 'Versione ' + j.current + ', aggiornata';
      }
    }).catch(function(){ btn.disabled = false; txt.textContent = 'errore di rete'; });
  };
})();
</script></body></html>
"""

# Decoder H.264 (parser + decoder di FFmpeg 7.1, LGPL) compilato in WebAssembly
# con wasi-libc e SIMD. Riceve lo stream Annex-B da JS e chiama on_frame(y,u,v,...)
# per ogni frame. Sorgente in wasm/h264dec.c, si ricompila con wasm/build.sh.
# Compressione lzma + base64, decompresso una volta al primo uso.
H264_WASM_XZ_B64 = "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"
_h264_wasm_gz = None


def h264_wasm_gz():
    global _h264_wasm_gz
    if _h264_wasm_gz is None:
        import base64, gzip, lzma
        _h264_wasm_gz = gzip.compress(lzma.decompress(base64.b64decode(H264_WASM_XZ_B64)), 6)
    return _h264_wasm_gz


TEST_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test browser</title>
<style>
body{background:#111;color:#eee;font:22px/1.35 sans-serif;margin:0;padding:14px}
h1{font-size:26px;margin:0 0 8px}
.r{display:flex;justify-content:space-between;border-bottom:1px solid #333;padding:6px 0}
.ok{color:#4f4;font-weight:bold}.ko{color:#f55;font-weight:bold}.na{color:#fa0;font-weight:bold}
canvas{border:2px solid #555;margin:8px 0;width:160px;height:120px;image-rendering:pixelated}
#clock{font-size:30px;color:#6cf}
</style></head><body>
<h1>Test browser (WebCodecs)</h1>
<div id="clock"></div>
<div>Pagina senza tag video/audio. Provala anche in marcia: il contatore deve continuare a girare.</div>
<canvas id="cv" width="160" height="120"></canvas>
<div id="out"></div>
<script>
var H264="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", WEBP="UklGRs4BAABXRUJQVlA4IMIBAAAQCwCdASogACAAPtFapkwoJSOiKA1RABoJbACdMtU5n6b+AF9PaCcAf3n7Vd4A3AH6m/qr79m6H/6D2AP1V6a72PP2h9JC7R/vTFa5vx43Xicto3SYh41mZeE26jYWjcmAAP77bBvjYXMzvJhisCM79+M2BDpc6dzGGF/6H2uYEURvOfra96V3njC8fhCFgYuyPL+4Ef4zyMQfb7asr/Mqrq30O/Vzn/9IvYx5XpvGsHNflzeW+/+LAGcJsYId8wpUdJiYW0YLHLZLnvCxj0fxJpvFehfB5Wkn8qaH+Vflx3NPw3Ji5zFEQLRyQAOe+OB1EJtR79qVOgTrvzmvInBp4ss0WDw6C+AriIuGP7QbYyJPqMwt/2R0fs4ced9+C5a8pxK9VS+8qzNv84vj9bg1rj2+OE28ExCvHPLnjLfCg3YVM9exdRzRotb/8Ovnz9CJxnxbNgREEzjVXKewAuZzY/btf7sbzvBkA8h/TrEzFZhX/JgP2atsK5X01tJcoqiSlGsg9bztsMQgcFNZSUEFynUdWHfNc7Tu21ou9s9Z6e1siK/qSJP3o7viVAor/8LyneD80fUb+MXwDkyjoFSaAAA=", AVIF="AAAAIGZ0eXBhdmlmAAAAAGF2aWZtaWYxbWlhZk1BMUEAAAD5bWV0YQAAAAAAAAAvaGRscgAAAAAAAAAAcGljdAAAAAAAAAAAAAAAAFBpY3R1cmVIYW5kbGVyAAAAAA5waXRtAAAAAAABAAAAHmlsb2MAAAAARAAAAQABAAAAAQAAASEAAACcAAAAKGlpbmYAAAAAAAEAAAAaaW5mZQIAAAAAAQAAYXYwMUNvbG9yAAAAAGppcHJwAAAAS2lwY28AAAAUaXNwZQAAAAAAAAAgAAAAIAAAABBwaXhpAAAAAAMICAgAAAAMYXYxQ4EgAAAAAAATY29scm5jbHgAAgACAACAAAAAF2lwbWEAAAAAAAAAAQABBAECgwQAAACkbWRhdAoMIAAAAif+bXyQENACMosBFAA5AAAAssAAFVD4aMmepqqV2mRudBzeUZVYKeH4kozKCj//u5/g1jPgUMFuKFwqd5iviPVWqMW/4csgDZt583HdVYIVqA/MyIh52I94qYcDMrU/3IdFFk8PQh2VRBwsIiiKh3KeuPhxunDAi9b8XIyOwno1MtGJKkg4XKd4k8gP7IPBmc28+67J4A==";
var out=document.getElementById('out');
function row(name,val,cls){var d=document.createElement('div');d.className='r';
 d.innerHTML='<span>'+name+'</span><span class="'+cls+'">'+val+'</span>';out.appendChild(d);}
function yn(name,b,txt){row(name,txt||(b?'SI':'NO'),b?'ok':'ko');}
function b64(s){var a=atob(s),u=new Uint8Array(a.length);for(var i=0;i<a.length;i++)u[i]=a.charCodeAt(i);return u;}
var t0=Date.now();
setInterval(function(){document.getElementById('clock').textContent='Contatore: '+((Date.now()-t0)/1000).toFixed(1)+' s';},100);

yn('Pagina sicura (HTTPS)',!!window.isSecureContext);
yn('VideoDecoder',typeof VideoDecoder!=='undefined');
yn('AudioDecoder',typeof AudioDecoder!=='undefined');
yn('MediaSource (MSE)',typeof MediaSource!=='undefined');
yn('createImageBitmap',typeof createImageBitmap!=='undefined');
yn('AudioContext',!!(window.AudioContext||window.webkitAudioContext));
yn('OffscreenCanvas',typeof OffscreenCanvas!=='undefined');
row('User agent',(navigator.userAgent.match(/Chrom[a-z]+\/[\d.]+/)||['?'])[0],'na');

async function cfgVideo(codec,label){
 try{
  var r=await VideoDecoder.isConfigSupported({codec:codec,codedWidth:1280,codedHeight:720});
  var h=await VideoDecoder.isConfigSupported({codec:codec,codedWidth:1280,codedHeight:720,hardwareAcceleration:'prefer-hardware'});
  row(label,(r.supported?'SI':'NO')+' / hw '+(h.supported?'SI':'NO'),r.supported?'ok':'ko');
 }catch(e){row(label,'errore: '+e.message,'ko');}
}
async function cfgAudio(codec,label,extra){
 try{
  var c={codec:codec,sampleRate:44100,numberOfChannels:2};
  if(extra)for(var k in extra)c[k]=extra[k];
  var r=await AudioDecoder.isConfigSupported(c);
  row(label,r.supported?'SI':'NO',r.supported?'ok':'ko');
 }catch(e){row(label,'errore: '+e.message,'ko');}
}
async function img(label,b,type){
 try{
  var bl=new Blob([b64(b)],{type:type});
  var bm=await createImageBitmap(bl);
  yn(label,true,'SI ('+bm.width+'x'+bm.height+')');
 }catch(e){row(label,'NO','ko');}
}
async function realDecode(){
 if(typeof VideoDecoder==='undefined'){row('Decodifica H.264 reale','salto','na');return;}
 try{
  var drawn=false,errMsg='';
  var dec=new VideoDecoder({
   output:function(f){var cv=document.getElementById('cv');cv.getContext('2d').drawImage(f,0,0,cv.width,cv.height);drawn=true;f.close();},
   error:function(e){errMsg=e.message;}
  });
  dec.configure({codec:'avc1.42c01e',codedWidth:160,codedHeight:120});
  dec.decode(new EncodedVideoChunk({type:'key',timestamp:0,data:b64(H264)}));
  await dec.flush();
  if(drawn)row('Decodifica H.264 reale + canvas','SI (vedi riquadro sopra)','ok');
  else row('Decodifica H.264 reale + canvas','NO '+errMsg,'ko');
 }catch(e){row('Decodifica H.264 reale + canvas','NO: '+e.message,'ko');}
}
(async function(){
 if(typeof VideoDecoder!=='undefined'){
  await cfgVideo('avc1.42c01e','H.264 baseline');
  await cfgVideo('avc1.64001f','H.264 high 3.1 (720p)');
  await cfgVideo('avc1.640028','H.264 high 4.0 (1080p)');
  await cfgVideo('vp09.00.31.08','VP9');
  await cfgVideo('av01.0.05M.08','AV1');
 }
 if(typeof AudioDecoder!=='undefined'){
  await cfgAudio('mp4a.40.2','AAC-LC');
  await cfgAudio('opus','Opus');
  await cfgAudio('mp3','MP3');
 }
 await img('WebP',WEBP,'image/webp');
 await img('AVIF',AVIF,'image/avif');
 await realDecode();
 row('Test completato','','na');
})();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        if path not in ("/buf", "/status"):
            print("GET", path, flush=True)

        if path == "/home":
            q = (parse_qs(u.query).get("q") or [""])[0]
            recent = push_recent(q, recent_from_cookie(self.headers.get("Cookie")))
            cur = PAGE_URL.split("v=")[-1][:11] if "v=" in PAGE_URL else ""
            items = []
            seen = set()
            # collect queries to run in parallel
            shown_q = []
            for query in recent:
                if query == q or query in shown_q:
                    continue
                if len(shown_q) >= 3:
                    break
                shown_q.append(query)
            all_queries = ([q] if q else []) + shown_q
            # prefetch all searches + related + account in parallel
            with ThreadPoolExecutor(max_workers=6) as pool:
                search_futures = {query: pool.submit(search_yt, query) for query in all_queries}
                related_future = pool.submit(related_of, cur) if cur else None
                account_future = pool.submit(account_sections)
            if q:
                items.append("<h2>Ricerca: %s</h2>" % q.replace("<", ""))
                for vid, title, kind, url in search_futures[q].result():
                    if kind == "channel":
                        items.append(
                            '<a class="card" href="/channel?id=%s"><span>Canale: %s</span></a>'
                            % (vid, title.replace("<", ""))
                        )
                    else:
                        items.extend(cards([(vid, title)], seen, cur))
            for query in shown_q:
                items.append("<h2>%s</h2>" % query.replace("<", ""))
                for vid, title, kind, url in search_futures[query].result():
                    if kind == "channel":
                        continue
                    items.extend(cards([(vid, title)], seen, cur))
            if cur and related_future:
                items.append("<h2>Suggeriti per questo video</h2>")
                items.extend(cards(related_future.result(), seen, cur))
            for name, rows in account_future.result():
                if name.startswith("<"):
                    items.append(name)
                    continue
                if not rows:
                    continue
                items.append("<h2>%s</h2>" % name)
                items.extend(cards(rows, seen, cur))
            chips = "".join(
                '<a class="chip" href="/home?q=%s">%s</a>' % (quote(row), html.escape(row))
                for row in recent
            )
            body = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>home</title>
<style>
body{background:#111;color:#fff;font-family:sans-serif;margin:0}
form{display:flex;gap:8px;padding:12px;position:sticky;top:0;background:#111}
input{flex:1;font-size:18px;padding:8px}
button,a.back{font-size:18px;padding:8px 14px;color:#fff}
.chips{display:flex;gap:8px;flex-wrap:wrap;padding:0 12px 12px}
.chip,.chip:visited{color:#fff;background:#333;text-decoration:none;padding:6px 10px;border-radius:12px}
h2{grid-column:1/-1;font-size:16px;margin:8px 0 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;padding:12px}
.card,.card:visited{color:#fff;text-decoration:none}
.card img{width:100%%;aspect-ratio:16/9;object-fit:cover;background:#000;display:block}
.card span{display:block;padding:8px 0;font-size:15px}
</style>
<form action="/home"><input name="q" placeholder="cerca"><button type="submit">cerca</button><a class="back" href="%s" style="background:#fff;color:#000;text-decoration:none;padding:8px 14px;font-size:18px">player</a><a class="back" href="/cookies" style="background:#333;color:#fff;text-decoration:none;padding:8px 14px;font-size:18px">cookie</a></form>
<div class="chips">%s</div>
<div class="grid">%s</div>
</body>""" % (("/?watch=" + cur) if cur else "/home", chips, "".join(items) or "<p>nessuna ricerca recente</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", recent_cookie_header(recent))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/search":
            q = (parse_qs(u.query).get("q") or [""])[0]
            recent = push_recent(q, recent_from_cookie(self.headers.get("Cookie")))
            rows = search_yt(q) if q else []
            items = []
            for row in rows:
                vid, title = row[0], row[1]
                kind = row[2] if len(row) > 2 else 'video'
                safe = title.replace('<', '')
                if kind == 'channel':
                    items.append(
                        '<a class="hit" href="/channel?id=%s"><span>Canale: %s</span></a>' % (vid, safe)
                    )
                else:
                    items.append(
                        '<a class="hit" href="/watch?v=%s"><img src="https://i.ytimg.com/vi/%s/hqdefault.jpg"><span>%s</span></a>'
                        % (vid, vid, safe)
                    )
            body = """<!doctype html><meta charset="utf-8"><title>cerca</title>
<style>
body{background:#111;color:#fff;font-family:sans-serif;margin:0}
form{display:flex;gap:8px;padding:12px}
input{flex:1;font-size:18px;padding:8px}
button{font-size:18px;padding:8px 14px}
.hit{display:flex;gap:12px;color:#fff;text-decoration:none;padding:8px 12px;align-items:center}
.hit img{width:160px;height:90px;object-fit:cover;background:#000}
</style>
<form action="/search"><input name="q" value="%s" placeholder="cerca su YouTube"><button>cerca</button></form>
%s""" % (q.replace("<", ""), "".join(items))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", recent_cookie_header(recent))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/channel":
            cid = (parse_qs(u.query).get("id") or [""])[0]
            rows = channel_videos(cid) if cid else []
            items = []
            for vid, title in rows:
                items.append(
                    '<a class="card" href="/watch?v=%s"><img src="https://i.ytimg.com/vi/%s/hqdefault.jpg"><span>%s</span></a>'
                    % (vid, vid, title.replace("<", ""))
                )
            body = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>canale</title>
<style>
body{background:#111;color:#fff;font-family:sans-serif;margin:0}
.top{display:flex;gap:8px;padding:12px}
a.back{font-size:18px;padding:8px 14px;color:#fff;background:#222;text-decoration:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;padding:12px}
.card,.card:visited{color:#fff;text-decoration:none}
.card img{width:100%%;aspect-ratio:16/9;object-fit:cover;background:#000;display:block}
.card span{display:block;padding:8px 0;font-size:15px}
</style>
<div class="top"><a class="back" href="/home">home</a></div>
<div class="grid">%s</div>""" % ("".join(items) or "<p>nessun video</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/watch":
            vid = (parse_qs(u.query).get("v") or [""])[0]
            if vid:
                set_page("https://www.youtube.com/watch?v=" + vid)
            self.send_response(302)
            self.send_header("Location", "/?watch=" + vid)
            self.end_headers()
            return

        if path == "/next":
            nxt = next_of_current() or []
            body = json.dumps(nxt)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/log.txt":
            try:
                lines = open(os.path.expanduser("~/yt.log")).read().splitlines()
            except Exception:
                lines = ["log non trovato: ~/yt.log"]
            lines = [x for x in lines if "GET /log" not in x and "GET /buf" not in x and "GET /status" not in x and "GET /config" not in x and "GET /set" not in x]
            body = "\n".join(lines[-400:])
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body.encode())
            return

        if path == "/logs":
            try:
                lines = open(os.path.expanduser("~/yt.log")).read().splitlines()
            except Exception:
                lines = []
            lines = [x for x in lines if "GET /log" not in x and "GET /logs" not in x][-80:]
            body = "<pre style='background:#111;color:#ddd;padding:12px;white-space:pre-wrap'>%s</pre>" % "\n".join(lines).replace("<", "")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/buf":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(("%d/%d %.2f" % (len(state["v_q"]), V_CAP, state.get("rate") or 0)).encode())
            return

        if path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write((state.get("err") or "").encode("utf-8"))
            return

        if path == "/update/check":
            body = json.dumps(check_update())
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body.encode())
            return

        if path == "/update/install":
            try:
                new = install_update()
                out, code = {"ok": True, "version": new}, 200
            except Exception as e:
                out, code = {"ok": False, "error": str(e)}, 500
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())
            if code == 200:
                try:
                    self.wfile.flush()
                except Exception:
                    pass
                threading.Thread(target=restart_self, daemon=True).start()
            return

        if path == "/test":
            body = TEST_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/dur":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(str(int(state["duration"] or 0)).encode())
            return

        if path == "/wasmtest":
            body = WASMTEST_HTML.replace("__VER__", VERSION).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/h264.wasm":
            body = h264_wasm_gz()
            self.send_response(200)
            self.send_header("Content-Type", "application/wasm")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/h264test":
            # qualche secondo di H.264 originale di YouTube, senza ricodifica (Annex-B)
            vid = (q.get("v") or [""])[0].strip()
            url = ("https://www.youtube.com/watch?v=" + vid) if vid else PAGE_URL
            if not url:
                self.send_error(400, "apri prima un video nell'app o indica un id")
                return
            try:
                sec = max(3, min(60, int(float((q.get("sec") or ["20"])[0]))))
            except ValueError:
                sec = 20
            def h264_urls():
                p = subprocess.run(
                    yt_cmd("--no-playlist", "--user-agent", UA,
                           "-f", "bv*[vcodec^=avc1][height<=720]/bv*[vcodec^=avc1]",
                           "--print", "%(width)sx%(height)s %(fps)s fps %(vcodec)s formato %(format_id)s %(protocol)s",
                           "--print", "urls", url),
                    text=True, capture_output=True, timeout=90,
                )
                lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
                urls = [x for x in lines if x.startswith("http")]
                info = next((x for x in lines if not x.startswith("http")), "?")
                return urls, info, re.sub(r"https?://\S+", "<url>", p.stderr or "")[-300:]

            def h264_start(u):
                ff = subprocess.Popen(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                     "-user_agent", UA, "-headers", HEADERS, "-t", str(sec), "-i", u,
                     "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", "-"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                first = ff.stdout.read(65536)
                if first:
                    # stderr va svuotato in parallelo, se no ffmpeg puo' bloccarsi
                    threading.Thread(target=ff.stderr.read, daemon=True).start()
                    return ff, first, ""
                ff.wait()
                txt = ff.stderr.read().decode("utf-8", "replace")
                # gli indirizzi googlevideo sono lunghissimi e coprono il motivo dell'errore
                txt = re.sub(r"https?://\S+", "<url>", txt)
                txt = " | ".join(x.strip() for x in txt.splitlines() if x.strip())
                return None, b"", txt[:400]

            ff = first = None
            err = ""
            info = "?"
            for attempt in (1, 2):
                try:
                    urls, info, yerr = h264_urls()
                except Exception as e:
                    urls, yerr = [], str(e)
                if not urls:
                    err = "yt-dlp: nessun formato H.264. " + yerr
                    print("h264test tentativo %d:" % attempt, err, flush=True)
                    continue
                ff, first, ferr = h264_start(urls[0])
                if ff:
                    break
                err = "ffmpeg [" + info + "]: " + (ferr or "nessun dato")
                print("h264test tentativo %d:" % attempt, err, flush=True)
            if not ff:
                body = err.encode("utf-8", "replace")
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            print("h264test", info, "%ds" % sec, flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Fmt", info.encode("ascii", "replace").decode())
            self.end_headers()
            try:
                chunk = first
                while chunk:
                    self.wfile.write(chunk)
                    chunk = ff.stdout.read(65536)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                if ff.poll() is None:
                    ff.kill()
                ff.wait()
            return

        if path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if path == "/prime":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if path == "/ready":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if path in ("/", "/index.html"):
            cfg_changed = apply_settings(self.headers.get("Cookie"))
            watch = (q.get("watch") or [""])[0]
            tpos = (q.get("t") or ["0"])[0]
            if watch and watch not in PAGE_URL:
                set_page("https://www.youtube.com/watch?v=" + watch)
            try:
                if float(tpos) > 0:
                    state["seek_base"] = float(tpos)
            except ValueError:
                pass
            if not PAGE_URL:
                self.send_response(302)
                self.send_header("Location", "/home")
                self.end_headers()
                return
            if not state["duration"]:
                threading.Thread(target=fetch_duration, daemon=True).start()
            cancel_stop()
            try:
                want = float(tpos or 0)
            except ValueError:
                want = 0
            if cfg_changed or not (ffmpeg_alive() and abs(want - now_pos()) < 8):
                new_session(want)
            vid = PAGE_URL.split("v=")[-1][:11] if "v=" in PAGE_URL else ""
            body = HTML.replace("__DUR__", str(int(state["duration"] or 0))).replace("__FPS__", str(FPS)).replace("__START__", str(int(float(tpos or 0)))).replace("__CFG__", json.dumps(CFG)).replace("__OPTS__", json.dumps(SETTINGS_UI)).replace("__VER__", VERSION).replace("WATCHID", vid).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/ctrl":
            if "pos" in q:
                cancel_stop()
                want = float(q["pos"][0])
                apply_settings(self.headers.get("Cookie"))
                new_session(want)
                msg = "seek %.0f" % state["seek_base"]
            elif "pause" in q:
                if not state["paused"]:
                    state["seek_base"] = now_pos()
                    state["t_play"] = None
                    state["paused"] = True
                    msg = "paused"
                else:
                    state["paused"] = False
                    state["t_play"] = time.perf_counter()
                    msg = "play"
            else:
                msg = "ok"
            print(msg, flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(msg.encode())
            return

        if path == "/bye":
            arm_stop("pagina chiusa")
            self.send_response(204)
            self.end_headers()
            return

        if path.startswith("/stream.mp3"):
            try:
                with lock:
                    start_ff()
            except Exception as e:
                print("audio err", e, flush=True)
                self.send_error(500, str(e))
                return
            if not state["has_audio"]:
                self.send_error(404, "no audio")
                return
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            gen = state["gen"]
            try:
                while state["gen"] == gen:
                    if client_gone(self):
                        arm_stop("audio client chiuso")
                        return
                    if state["a_q"]:
                        self.wfile.write(state["a_q"].popleft())
                    elif state.get("ff_done") == gen:
                        return
                    else:
                        time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError, OSError):
                arm_stop("audio client via")
            return

        if path.startswith("/stream.mjpg"):
            try:
                with lock:
                    start_ff()
            except Exception as e:
                print("video err", e, flush=True)
                self.send_error(500, str(e))
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            gen = state["gen"]
            try:
                while state["gen"] == gen:
                    if client_gone(self):
                        arm_stop("video client chiuso")
                        return
                    if not state["v_q"]:
                        if state.get("ff_done") == gen:
                            return
                        time.sleep(0.01)
                        continue
                    jpg = state["v_q"].popleft()
                    hdr = (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpg)).encode()
                        + b"\r\n\r\n"
                    )
                    self.wfile.write(hdr + jpg + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                arm_stop("video client via")
            return

        if path == "/cookies":
            page = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>cookie</title>
<style>body{background:#111;color:#fff;font-family:sans-serif;padding:16px}a{color:#fff}input,button{font-size:18px;margin-top:12px}</style>
<p>Esporta i cookie di youtube.com in formato Netscape, poi caricali qui.</p>
<p>Dal browser normale: esporta e chiudi subito tutte le schede di YouTube, altrimenti il file muore.</p>
<form method="post" action="/cookies" enctype="multipart/form-data">
<input type="file" name="f" accept=".txt,text/plain" required>
<button>carica</button>
</form>
<p><a href="/home">home</a></p>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode())
            return

        if path == "/favicon.ico":
            self.send_error(404)
            return
        self.send_error(404)

    def do_POST(self):
        if urlparse(self.path).path != "/cookies":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        text = raw.decode("utf-8", "replace")
        if "filename=" in text:
            parts = text.split("\r\n\r\n", 1)
            if len(parts) == 2:
                text = parts[1].rsplit("\r\n--", 1)[0]
        if "youtube.com" not in text or "\t" not in text:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"file non valido: servono i cookie Netscape di youtube.com")
            return
        with open(COOKIES, "w") as f:
            f.write(text if text.endswith("\n") else text + "\n")
        state["err"] = ""
        print("cookie aggiornati", flush=True)
        self.send_response(302)
        self.send_header("Location", "/home")
        self.end_headers()

    def log_message(self, *a):
        pass


class QuietTLSServer(ThreadingHTTPServer):
    # handshake falliti (scanner, certificati rifiutati, connessioni chiuse) non
    # riempiono il log di traceback
    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve_https():
    # HTTPS facoltativo, su una porta separata: parte solo se trova il certificato.
    # Serve per le API del browser riservate alle pagine sicure (es. WebCodecs).
    cert = os.environ.get("YT_CERT") or os.path.expanduser("~/yt-cert/fullchain.pem")
    key = os.environ.get("YT_KEY") or os.path.expanduser("~/yt-cert/privkey.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        print("https spento: certificato non trovato in", os.path.dirname(cert), flush=True)
        return
    port = int(os.environ.get("YT_HTTPS_PORT") or 8443)
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv = QuietTLSServer(("0.0.0.0", port), Handler)
        # handshake nel thread della richiesta, non nel ciclo che accetta le connessioni
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    except Exception as e:
        print("https non avviato:", e, flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    print("https://0.0.0.0:%d/" % port, flush=True)


if __name__ == "__main__":
    fetch_duration()
    print("http://0.0.0.0:%d/  duration=%ss" % (PORT, state["duration"]))
    serve_https()
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        stop_ff()

#!/usr/bin/env python3
import html
import json
import os
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

VERSION = "11.24"
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
</style></head><body>
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
  var off = 0, decoded = 0, drawn = 0, dropped = 0, stalls = 0, pending = null;
  var cell = row('Riproduzione in tempo reale', '...');
  function reset(){ ex.dec_init(); off = 0; }
  reset();
  onFrame = function(y, u, v, ly, lu, lv, w, h){ decoded++; pending = [y, u, v, ly, lu, lv, w, h]; };
  show(mode);
  var start = performance.now(), statAt = start, statDrawn = 0, rate = 0;
  function tick(){
    if (my !== playGen) return;
    var target = Math.floor((performance.now() - start) / 1000 * fps);
    var got = 0, budget = performance.now() + 30;
    while (decoded < target && performance.now() < budget) {
      if (off >= data.length) { ex.flush(); reset(); }
      var before = decoded;
      feedRange(data, off, Math.min(data.length, off + 4096)); off += 4096;
      got += decoded - before;
    }
    if (got > 1) dropped += got - 1;
    if (target - decoded > fps) { start = performance.now() - decoded / fps * 1000; stalls++; }
    if (pending) {
      if (mode === 'gl') drawGL.apply(null, pending); else draw2D.apply(null, pending);
      drawn++; pending = null;
    }
    var now = performance.now();
    if (now - statAt >= 1000) {
      rate = (drawn - statDrawn) / ((now - statAt) / 1000); statDrawn = drawn; statAt = now;
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
            body = WASMTEST_HTML.encode("utf-8")
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
            try:
                p = subprocess.run(
                    yt_cmd("--no-playlist", "--user-agent", UA,
                           "-f", "bv*[vcodec^=avc1][height<=720]/bv*[vcodec^=avc1]",
                           "--print", "%(width)sx%(height)s %(fps)s fps %(vcodec)s",
                           "--print", "urls", url),
                    text=True, capture_output=True, timeout=90,
                )
            except Exception as e:
                self.send_error(502, "yt-dlp: %s" % e)
                return
            lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
            urls = [x for x in lines if x.startswith("http")]
            info = next((x for x in lines if not x.startswith("http")), "?")
            if not urls:
                print("h264test: nessun formato H.264", (p.stderr or "")[-300:], flush=True)
                self.send_error(502, "nessun formato H.264 per questo video")
                return
            print("h264test", info, "%ds" % sec, flush=True)
            ff = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                 "-user_agent", UA, "-headers", HEADERS, "-t", str(sec), "-i", urls[0],
                 "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Fmt", info.encode("ascii", "replace").decode())
            self.end_headers()
            try:
                while True:
                    chunk = ff.stdout.read(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
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

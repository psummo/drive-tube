#!/usr/bin/env python3
import html
import json
import os
from concurrent.futures import ThreadPoolExecutor
import select
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

VERSION = "11.22"
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
        peek = handler.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
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
var H264="AAAAAWdCwB7cKEflwEQAAAMABAAAAwAIPFi+AAAAAWjODyyAAAABBgX//1rcRem95tlIt5Ys2CDZI+7veDI2NCAtIGNvcmUgMTY0IHIzMTA4IDMxZTE5ZjkgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDIzIC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MCByZWY9MSBkZWJsb2NrPTE6MDowIGFuYWx5c2U9MHgxOjB4MTExIG1lPWhleCBzdWJtZT03IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAgbWl4ZWRfcmVmPTAgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0xIDh4OGRjdD0wIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zmc2V0PS0yIHRocmVhZHM9MyBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTAgd2VpZ2h0cD0wIGtleWludD0xIGtleWludF9taW49MSBzY2VuZWN1dD00MCBpbnRyYV9yZWZyZXNoPTAgcmM9Y3JmIG1idHJlZT0wIGNyZj0yMy4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD02OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFxPTE6MS4wMACAAAABZYiEBLyYoAA4o/D/yKDAFEUAAQDvAAh0tvRJtABDq/6P///eSVV9v8gguQAKEAAXBhIDgOICYwcQEwAKTQLCtWADgAQAVjBwAIAKwAARvglRCQ3Jo1/PfCRF+N4AEOlt6JNqwukq91lG3oAR3wLAMe/AAYeSlV9j+P+DDhAAEcABwAUEAAIGgAFAD2wEKCQVawzClMwf4dZb/e77+372wwAOH8TRrQBXgok/2lKMwNYoFeeW9yREJEMZGE5Sg///wWYoAAgH8QABGgwAAgIAAsMHIDYg5AbAYOAAgAjEHAAQARgRBSKAAajgY0AAyuH/+CKKAAKSHjgAD+vHAAEmj//+CsE0UAAQE+IAARAAaIAAICwAAg1wcgGxg5ANiDgAEQAEYwcAAiAAjHweZ/OMcSAGBpY87rX/zcmvFSUYPI1u2glA9B61/QH/2zt4f4//7+QgAECA8WCAAECQAAoANAsACGTQaVDsYpUrvC4y+3f4AGuRkDbsOhizDLANKpMAqgeS394DK7Tmm1/gcz+N/eKAAIAnErXwH/y4f4//7g9a75Q/+NgQEAC4EBAAHQABBMeADQwdAmAA+UnhDPXdFoEwGDgBACsYBwPFtZVCZHTkEACAFYEQVigAGowKGgpf5ENib5BoKuAIj+ZwUQfPAJVJu3Hv//UGHAAOCjsP6FbMvd48eA0+E3OuvmXmPfYMIBFXIQABoAYI0AGRMaEjt4r69nvOFkNjkAD544gxTxdVGfWqEkjGQw2KoLAAQGYAAYp1PGY1C6Eu3Nbc0y0wT09PFMFWZ+MwlhnpP9KR34f4//7/hfzf0eBym1a9zfb/AAsmCzfyuPihKe/6AsOi12wrZnJN/+A4oBhEAQUEXe2Ab1GwRI/HsuBsdq7Tmm1hIPFijMCZPz/AywAQAjCYK/7W+0oUBVAAuMeJqX1Ii/9QBNCj1kxKlK/kv9tqQDDuhzdexqqqiDGUwAAQEgAMBXEw9rV2N2NMjplpgnp6en/h8fYLwUQYvABeQzcLh3JJ43a8FYGDS4NuWJm6eYHAislgIpRwlZd2qU+AU9hMgADvCfSP1Oruv//D8RiIt3OpGTjqDDwAST2gGHW+c9++0P/D8AChoTpUFWr+5//wYLTPTIqZaYJ6enp/AKQw+4LAshGCjck3NB2Kjn8ABmWIrqK2yVpc6CEACsYRBaIeU1pu14KRBlFaS8sFYN2vgArGEQWiHlNabtQUiDKKsl5YIPG7QgEGiAAFQABAB5aTEHSYrLIA2IOQDY0//2HoYAtjFvIiELV2pdGP//DSCHwAKEhMkQRK+/nf/JnpkdMtME9PT0+GGFP+Cz4AgQjhBuCToG+FP/gArGEQWiHlNebtALdIeP2HqYz/4HD+CwihMEAARFNG4MqWBTUDtJ4HBOeWAaLMGqqAT7X/tb4IMAI+AwrIYvXkBO8zZv2fn+lIh+EPxsYt5EQpaO/2dI8HGU3nrF/vgDmAHQ1JHk48KOqR79HGEIABIA4QIAPwOwD8RNgCHCGrDAODRzawBBwY9YYB9O4aDpQAkhsinRkTRPDtf/x7PB4fG3OyWcIAQCWQEBAOHAIg5AbEa3GJO+ceEZ0UA4ACACMQdzTFTKcbdoSDacPWz/gGIcOxDgN7JnjHCBSIPeN8YQABkMITCABAyheDgBAwZGgHAAIAMDI0YHAbQ4umwDgBtAgVTZ/wDAdB2DbZNyOMdZBwCAfmV4aAwSEAYAIBsHIDYKuohdhrZBwAEAEYA/0RbAqsguJMqbpf8AwDQd1Gy6lI4QEHScB8fMWewDjQZ12fkM/+GAcOwOgQ7Ly8C4YtI2ul9hAgAOACTYQIY6cEAAYBw+uAIAARgDg2eGBwAgwRLBADAQZPD/hwDh2F8dIEHAKBaXhfOkbXS7CAAEAAAUScA4ABcHkHIDYmEAAneAUG3u8FgAEYAUGzwBwAEAEYg7soFxqzN2hI0DHvZ/wDIOHZMsj+Z+DSNkfyusIAAoBTTgOAMNg4ABMA4EwoBwAQKFwowOAyDn7sA4GQIduz/+A6CG3zY3WDFMg4EBcZV4QCJsBAAHAA5W5ZAbChQEtxiJe1lgAIAIwRBM9gT5AzcY0bTdq1i9hgABQ8gAFDQnSoKlX5z//gXRsBdG8h/O2/4P3vMwG/t6gNIeDjCnSHWT4QIJxYrfLktgJxG2HsaOZhD8MA4dgHHAFQWklyfhfqkl94RAAEAEoCBBABvBp+XgdGWWHEMeWeZZgHDBjIwBzAhqf/gMAMDh2lyA4IEmZOML+pCNFnP2nS4P0QgDR7A4ABugh/IDg7wY6NoIgwbahV/7w8zzAN0giCTItukApe8jwHqE9wtr/gRkABh3sSBvZNTdH//c8OgQdk/OEAAWAHPBgYAAgLOAAd3Z0AaDjYywQACBQuWeZ595WROsAQakABgKNhh/wDANB2fAqss1AcQCCsuDAAhoQDQYF2eOMEmBtyPPyILtgCSxlPAER1L/gGAaDvJnW4QII0cBhhnjPGnK+Af4RLs/b/8IAHDpQPAxmXwD4cekiHvCACAAIJ1gACAaEAQBWhRYy2LAA0DB0cAcAAhQGBlPljA4AIFCZYIANBRscP+AwABoOwTOdIEMAc9LzG6QUZ0vDQDABoQABsAAQEKNPZbCQqwfSuozxnjAKSMI5x3FBG7QsAAhQCAqOH/AMgKg7VsmZwYMyLrhAASqQIAAQGAAUANA1MsNB0Z5Z4M8GQyvYJAAbQFBcdn/kRAEGPv1AATOkaK3IAHwQAQPqrA/17DGMAcwBgSW4RAAEAxQAUIABAAEBJwA34MAV+gjbwEU/3hSfYIZHa78BUCz9UAO2EXYEHqe0AmMYDBj+7X/AAgGg7qfG2XwgIFIDwPyPDLDmCdsEjQY8bL+maCkUAAQFogAAgCPHAAN/w/9gsBM/0nRjDcIwpubMCaBmGQmdfh/HQeWq+sfx/ww3XY///+Y93v7/fg/umh5m8v0/CCHbISAAIAwAGSCPzSeB4Ef+tkZEGl5imKQZCAYA9zASYIlKooOyPsO2QxMdqoy2LIZgQIAYAFuCnek/8H1YtrB0CeOBop4gAIJEADpbLAANxWKAAb5biszBNl+xEMP18AiLaTrxQAedqSWW5/MAFEVUN5y/YQP0WG9wggoAaBwIAAQFQCLBAAFNokzPQkVcNlYm8eg5OEACEPuBgBDaImN+hEdeAibD7QlW01pBZkQACDDbgco//2CqISiAAiyIkudh5FcsgAEK+54cAEK+5mgnFAANxgABmDgAEQKQ=", WEBP="UklGRs4BAABXRUJQVlA4IMIBAAAQCwCdASogACAAPtFapkwoJSOiKA1RABoJbACdMtU5n6b+AF9PaCcAf3n7Vd4A3AH6m/qr79m6H/6D2AP1V6a72PP2h9JC7R/vTFa5vx43Xicto3SYh41mZeE26jYWjcmAAP77bBvjYXMzvJhisCM79+M2BDpc6dzGGF/6H2uYEURvOfra96V3njC8fhCFgYuyPL+4Ef4zyMQfb7asr/Mqrq30O/Vzn/9IvYx5XpvGsHNflzeW+/+LAGcJsYId8wpUdJiYW0YLHLZLnvCxj0fxJpvFehfB5Wkn8qaH+Vflx3NPw3Ji5zFEQLRyQAOe+OB1EJtR79qVOgTrvzmvInBp4ss0WDw6C+AriIuGP7QbYyJPqMwt/2R0fs4ced9+C5a8pxK9VS+8qzNv84vj9bg1rj2+OE28ExCvHPLnjLfCg3YVM9exdRzRotb/8Ovnz9CJxnxbNgREEzjVXKewAuZzY/btf7sbzvBkA8h/TrEzFZhX/JgP2atsK5X01tJcoqiSlGsg9bztsMQgcFNZSUEFynUdWHfNc7Tu21ou9s9Z6e1siK/qSJP3o7viVAor/8LyneD80fUb+MXwDkyjoFSaAAA=", AVIF="AAAAIGZ0eXBhdmlmAAAAAGF2aWZtaWYxbWlhZk1BMUEAAAD5bWV0YQAAAAAAAAAvaGRscgAAAAAAAAAAcGljdAAAAAAAAAAAAAAAAFBpY3R1cmVIYW5kbGVyAAAAAA5waXRtAAAAAAABAAAAHmlsb2MAAAAARAAAAQABAAAAAQAAASEAAACcAAAAKGlpbmYAAAAAAAEAAAAaaW5mZQIAAAAAAQAAYXYwMUNvbG9yAAAAAGppcHJwAAAAS2lwY28AAAAUaXNwZQAAAAAAAAAgAAAAIAAAABBwaXhpAAAAAAMICAgAAAAMYXYxQ4EgAAAAAAATY29scm5jbHgAAgACAACAAAAAF2lwbWEAAAAAAAAAAQABBAECgwQAAACkbWRhdAoMIAAAAif+bXyQENACMosBFAA5AAAAssAAFVD4aMmepqqV2mRudBzeUZVYKeH4kozKCj//u5/g1jPgUMFuKFwqd5iviPVWqMW/4csgDZt583HdVYIVqA/MyIh52I94qYcDMrU/3IdFFk8PQh2VRBwsIiiKh3KeuPhxunDAi9b8XIyOwno1MtGJKkg4XKd4k8gP7IPBmc28+67J4A==";
var out=document.getElementById('out');
function row(name,val,cls){var d=document.createElement('div');d.className='r';
 d.innerHTML='<span>'+name+'</span><span class="'+cls+'">'+val+'</span>';out.appendChild(d);}
function yn(name,b,txt){row(name,txt||(b?'SI':'NO'),b?'ok':'ko');}
function b64(s){var a=atob(s),u=new Uint8Array(a.length);for(var i=0;i<a.length;i++)u[i]=a.charCodeAt(i);return u;}
var t0=Date.now();
setInterval(function(){document.getElementById('clock').textContent='Contatore: '+((Date.now()-t0)/1000).toFixed(1)+' s';},100);

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


if __name__ == "__main__":
    fetch_duration()
    print("http://0.0.0.0:%d/  duration=%ss" % (PORT, state["duration"]))
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        stop_ff()

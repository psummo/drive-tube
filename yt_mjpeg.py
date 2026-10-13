#!/usr/bin/env python3
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
from urllib.parse import parse_qs, urlparse

def find_cfg():
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [
        os.environ.get("YT_CONF") or "",
        os.path.join(here, "yt_mjpeg.conf"),
        os.path.expanduser("~/yt_mjpeg.conf"),
        "/config/yt_mjpeg.conf",
    ]
    for path in cands:
        if path and os.path.isfile(path):
            return path
    return os.path.join(here, "yt_mjpeg.conf")


def load_cfg(path=None):
    path = path or find_cfg()
    cfg = {
        "PORT": 8090,
        "HEIGHT": 720,
        "FPS": 25,
        "Q": 5,
        "VIDEO_BUFFER_SEC": 15,
        "AUDIO_BUFFER_SEC": 8,
        "DURATION": 0,
        "SLACK_MS": -400,
    }
    try:
        for line in open(path):
            line = line.split("#", 1)[0].strip()
            if not line or "=" not in line:
                continue
            k, v = [x.strip() for x in line.split("=", 1)]
            if k in cfg:
                cfg[k] = int(v)
    except FileNotFoundError:
        pass
    return cfg

CFG = load_cfg()
PORT = CFG["PORT"]
HEIGHT = CFG["HEIGHT"]
FPS = CFG["FPS"]
Q = CFG["Q"]
VIDEO_BUFFER_SEC = CFG["VIDEO_BUFFER_SEC"]
AUDIO_BUFFER_SEC = CFG["AUDIO_BUFFER_SEC"]
DURATION = CFG["DURATION"]
SLACK_MS = CFG["SLACK_MS"]
CFG_PATH = find_cfg()
recent_mem = []
feed_cache = {}


def apply_saved():
    global HEIGHT, FPS, Q, VIDEO_BUFFER_SEC, AUDIO_BUFFER_SEC, DURATION, SLACK_MS, CFG
    CFG = load_cfg()
    HEIGHT = CFG["HEIGHT"]
    FPS = CFG["FPS"]
    Q = CFG["Q"]
    VIDEO_BUFFER_SEC = CFG["VIDEO_BUFFER_SEC"]
    AUDIO_BUFFER_SEC = CFG["AUDIO_BUFFER_SEC"]
    DURATION = CFG["DURATION"]
    SLACK_MS = CFG["SLACK_MS"]
    if DURATION:
        state["duration"] = DURATION
    new_session(now_pos())

LIMITS = {
    "PORT": (1, 65535),
    "HEIGHT": (144, 1080),
    "FPS": (5, 30),
    "Q": (2, 31),
    "VIDEO_BUFFER_SEC": (1, 120),
    "AUDIO_BUFFER_SEC": (1, 60),
    "DURATION": (0, 86400),
    "SLACK_MS": (-5000, 5000),
}


def save_cfg(key, value):
    if key not in LIMITS:
        raise ValueError("chiave sconosciuta")
    lo, hi = LIMITS[key]
    value = max(lo, min(hi, int(value)))
    lines = []
    try:
        lines = open(CFG_PATH).read().splitlines()
    except FileNotFoundError:
        lines = []
    found = False
    out = []
    for line in lines:
        raw = line.split("#", 1)[0].strip()
        if raw.startswith(key + " ") or raw.startswith(key + "=") or raw.startswith(key + " ="):
            out.append("%s = %s" % (key, value))
            found = True
        else:
            out.append(line)
    if not found:
        out.append("%s = %s" % (key, value))
    with open(CFG_PATH, "w") as fh:
        fh.write("\n".join(out).rstrip() + "\n")
    return value


def schedule_restart():
    def go():
        print("riavvio dopo modifica config", flush=True)
        stop_ff()
        script = os.path.expanduser("~/yt-refresh.sh")
        if os.path.isfile(script):
            subprocess.Popen(["bash", script], start_new_session=True)
        else:
            log = open(os.path.expanduser("~/yt.log"), "a")
            subprocess.Popen([sys.executable, os.path.abspath(__file__)] + sys.argv[1:], start_new_session=True, stdout=log, stderr=log)
        os._exit(0)
    threading.Timer(0.5, go).start()


def load_recent():
    rows = list(recent_mem)
    try:
        for line in open(CFG_PATH):
            raw = line.split("#", 1)[0].strip()
            if raw.startswith("RECENT="):
                q = raw.split("=", 1)[1].strip()
                if q and q not in rows:
                    rows.append(q)
    except FileNotFoundError:
        pass
    return rows[:10]


def push_recent(q):
    q = (q or "").strip().replace("\n", " ")
    if not q:
        return load_recent()
    rows = [q] + [x for x in load_recent() if x.lower() != q.lower()]
    rows = rows[:10]
    recent_mem[:] = rows
    try:
        kept = []
        for line in open(CFG_PATH):
            if not line.split("#", 1)[0].strip().startswith("RECENT="):
                kept.append(line if line.endswith("\n") else line + "\n")
        with open(CFG_PATH, "w") as fh:
            fh.writelines(kept)
            for row in rows:
                fh.write("RECENT=%s\n" % row)
        print("recent salvate", len(rows), flush=True)
    except Exception as e:
        print("recent non scritte nel conf:", e, flush=True)
        user = os.environ.get("USER") or "admin"
        print("esegui: sudo chown %s %s" % (user, CFG_PATH), flush=True)
    return rows

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
    fetch_duration()
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
    "duration": DURATION,
    "v_q": deque(),
    "a_q": deque(),
    "v_evt": threading.Event(),
    "a_evt": threading.Event(),
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


def pump_jpegs(gen, path):
    try:
        with open(path, "rb") as fh:
            buf = b""
            nbytes = 0
            t0 = time.perf_counter()
            while state["gen"] == gen:
                chunk = fh.read(65536)
                if not chunk:
                    break
                nbytes += len(chunk)
                now = time.perf_counter()
                if now - t0 >= 5:
                    print("video %.1f MB/s coda %d frame" % (nbytes / (now - t0) / 1e6, len(state["v_q"])), flush=True)
                    state["rate"] = nbytes / (now - t0) / 1e6
                    nbytes = 0
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
                    buf = buf[e + 2 :]
                    cap = 6000
                    while len(state["v_q"]) > cap:
                        state["v_q"].popleft()
                    state["v_evt"].set()
    except Exception as e:
        print("pump video", e, flush=True)


def pump_audio(gen, path):
    try:
        with open(path, "rb") as fh:
            n = 0
            t0 = time.perf_counter()
            while state["gen"] == gen:
                chunk = fh.read(4096)
                if not chunk:
                    break
                state["a_q"].append(chunk)
                n += len(chunk)
                if time.perf_counter() - t0 >= AUDIO_BUFFER_SEC and n > 40000:
                    state["a_evt"].set()
            if n:
                state["a_evt"].set()
            print("pump audio end (%d bytes)" % n, flush=True)
    except Exception as e:
        print("pump audio", e, flush=True)


def ffmpeg_cmd(vsrc, asrc, pos):
    if asrc and asrc == vsrc:
        return [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-user_agent", UA, "-headers", HEADERS,
            "-ss", str(pos), "-readrate", "16",
            "-reconnect", "1", "-reconnect_streamed", "1",
            "-i", vsrc,
            "-map", "0:v:0", "-an",
            "-vf", "scale=-2:%d" % HEIGHT, "-r", str(FPS), "-q:v", str(Q),
            "-f", "mjpeg", FIFO_V,
            "-map", "0:a:0", "-vn",
            "-c:a", "libmp3lame", "-b:a", "64k",
            "-f", "mp3", FIFO_A,
        ]
    if asrc:
        return [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-user_agent",
            UA,
            "-headers",
            HEADERS,
            "-ss",
            str(pos),
            "-readrate",
            "16",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-i",
            vsrc,
            "-user_agent",
            UA,
            "-headers",
            HEADERS,
            "-ss",
            str(pos),
            "-readrate",
            "16",
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-i",
            asrc,
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            "scale=-2:%d" % HEIGHT,
            "-r",
            str(FPS),
            "-q:v",
            str(Q),
            "-f",
            "mjpeg",
            FIFO_V,
            "-map",
            "1:a:0",
            "-vn",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "64k",
            "-f",
            "mp3",
            FIFO_A,
        ]
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-y",
        "-user_agent",
        UA,
        "-headers",
        HEADERS,
        "-ss",
        str(pos),
        "-readrate",
        "16",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-i",
        vsrc,
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "scale=-2:%d" % HEIGHT,
        "-r",
        str(FPS),
        "-q:v",
        str(Q),
        "-f",
        "mjpeg",
        FIFO_V,
    ]


def start_ff():
    if state["ff"] and state["ff"].poll() is None:
        return
    stop_ff()
    ensure_fifo(FIFO_V)
    pos = int(now_pos())
    urls = ytdlp_urls()
    vsrc = urls[0]
    asrc = urls[1] if len(urls) > 1 else urls[0]
    state["has_audio"] = True
    ensure_fifo(FIFO_A)
    print("ffmpeg @ %ds url=%d" % (pos, len(urls)), flush=True)
    gen_now = state["gen"]
    attempt = 0
    while True:
        if state["gen"] != gen_now:
            return
        attempt += 1
        state["err"] = "403 tentativo %d" % attempt
        state["ff"] = subprocess.Popen(ffmpeg_cmd(vsrc, asrc, pos))
        time.sleep(min(30, 2 * attempt))
        if state["ff"].poll() is None:
            state["err"] = ""
            break
        print("ffmpeg morto, retry %d" % attempt, flush=True)
        stop_ff()
        ensure_fifo(FIFO_V)
        try:
            urls = ytdlp_urls()
        except Exception as e:
            state["err"] = str(e)
            return
        vsrc = urls[0]
        asrc = urls[1] if len(urls) > 1 else urls[0]
        state["has_audio"] = True
        ensure_fifo(FIFO_A)
        if attempt >= 20:
            state["err"] = "403 dopo 20 tentativi"
            print(state["err"], flush=True)
            return
    state["seek_base"] = float(pos)
    state["t_play"] = time.perf_counter()
    state["paused"] = False
    gen = state["gen"]
    threading.Thread(target=pump_jpegs, args=(gen, FIFO_V), daemon=True).start()
    if asrc:
        threading.Thread(target=pump_audio, args=(gen, FIFO_A), daemon=True).start()


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
  <div id="ver" style="position:fixed;left:12px;bottom:12px;z-index:40;color:#aaa;font-size:13px">11.14b</div>
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
var playOrigin = 0;
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
    if (hud) hud.textContent = 'browser ' + frames.length + '  server ' + serverFrames + (rateMb ? '  ' + rateMb.toFixed(2) + ' MB/s' : '');
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
      if (err) {
        msg.textContent = err;
        go.style.display = 'block';
        ui(err);
        return;
      }
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
function stallPlay(){
  if (!stalled) {
    stalled = true;
    stopClock();
    if (actx && actx.state === 'running') actx.suspend();
  }
  showStatus('fermo, restano ' + (frames.length / FPS).toFixed(1) + 's di frame');
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
  if (!audioArm || !actx) { if (audioHold.length < 6) audioHold.push(buf); return; }
  var frameT = drawnT;
  if (audioCursor + 0.4 < frameT) {
    var late = frameT - audioCursor;
    if (buf.duration <= late) { audioCursor += buf.duration; return; }
    var cut = sliceBuf(buf, late);
    audioCursor += late;
    buf = cut;
    if (!buf) return;
  }
  if (audioCursor > frameT + 2) { if (audioHold.length < 6) audioHold.push(buf); return; }
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
    if (frames.length < FPS * 5) {
      stallPlay();
      showStatus('buffer ' + frames.length + ', fermo fino a 1 minuto');
      return;
    }
    if (stalled) {
      if (frames.length < FPS * 60) {
        showStatus('fermo a ' + frames.length + ', carico 1 minuto');
        return;
      }
      resumePlay();
      playOrigin = performance.now() - drawnT * 1000;
    }
    if (painting) return;
    var elapsed = (performance.now() - playOrigin) / 1000;
    if (drawnT + 1 / FPS > elapsed + 0.04) return;
    while (frames.length > 2 && drawnT + 2 / FPS < elapsed) {
      frames.shift();
      drawnT += 1 / FPS;
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
    if (SLACK_MS > 0) setTimeout(showVideo, SLACK_MS);
    else showVideo();
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
        if (err && (err.indexOf('403') === 0 || err.indexOf('Cookie') === 0)) { msg.textContent = err; ui(err); if (err.indexOf('dopo') >= 0) clearInterval(watch); }
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
      nextFrameAt = performance.now();
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
      nextFrameAt = performance.now();
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
        if (err && err.indexOf('403') === 0) {
          overlay.style.display = 'flex';
          msg.style.display = 'block';
          msg.textContent = err;
          ui(err);
        }
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
      var need = FPS * 60;
      showStatus(slow ? 'linea lenta: carico fino a 5000 frame' : 'caricamento');
      if (frames.length >= need) {
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
var FIELDS = [
  ['HEIGHT', 120, 'Altezza dei frame. Vale dal prossimo play.', 144, 1080],
  ['FPS', 1, 'Frame al secondo. 25 vuol dire un fotogramma ogni 40 ms.', 5, 30],
  ['Q', 1, 'Qualità JPEG. Più basso è più nitido e più pesante.', 2, 31],
  ['VIDEO_BUFFER_SEC', 1, 'Secondi di video tenuti in coda sul server.', 1, 120],
  ['AUDIO_BUFFER_SEC', 1, 'Secondi di audio tenuti in coda sul server.', 1, 60],
  ['DURATION', 30, 'Durata forzata in secondi. 0 = la legge da YouTube.', 0, 86400],
  ['SLACK_MS', 50, 'Scarto audio in millisecondi. Negativo = audio in anticipo.', -5000, 5000]
];
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
function drawCfg(){
  var box = document.getElementById('panel');
  box.innerHTML = '';
  FIELDS.forEach(function(f){
    var row = document.createElement('div');
    row.className = 'row';
    row.innerHTML = '<p><b>' + f[0] + '</b><small>' + f[2] + '</small></p><button type="button" data-d="-1">−</button><i>' + cfgNow[f[0]] + '</i><button type="button" data-d="1">+</button>';
    row.querySelectorAll('button').forEach(function(b){
      b.onclick = function(ev){
        ev.stopPropagation();
        var v = cfgNow[f[0]] + (b.getAttribute('data-d') === '1' ? f[1] : -f[1]);
        if (v < f[3]) v = f[3];
        if (v > f[4]) v = f[4];
        cfgNow[f[0]] = v;
        drawCfg();
      };
    });
    box.appendChild(row);
  });
  var save = document.createElement('button');
  save.id = 'savecfg';
  save.type = 'button';
  save.textContent = 'Salva';
  save.onclick = function(ev){
    ev.stopPropagation();
    var q = FIELDS.map(function(f){ return f[0] + '=' + cfgNow[f[0]]; }).join('&');
    box.innerHTML = '<p>salvo, svuoto il buffer...</p>';
    fetch('/save?' + q).then(function(r){ return r.text(); }).then(function(){ location.reload(); });
  };
  box.appendChild(save);
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        path = u.path
        if path not in ("/buf", "/status"):
            print("GET", path, flush=True)

        if path == "/home":
            q = (parse_qs(u.query).get("q") or [""])[0]
            recent = push_recent(q) if q else load_recent()
            cur = PAGE_URL.split("v=")[-1][:11] if "v=" in PAGE_URL else ""
            items = []
            seen = set()
            if q:
                items.append("<h2>Ricerca: %s</h2>" % q.replace("<", ""))
                for vid, title, kind, url in search_yt(q):
                    if kind == "channel":
                        items.append(
                            '<a class="card" href="/channel?id=%s"><span>Canale: %s</span></a>'
                            % (vid, title.replace("<", ""))
                        )
                    else:
                        items.extend(cards([(vid, title)], seen, cur))
            shown_q = []
            for query in recent:
                if query == q or query in shown_q:
                    continue
                if len(shown_q) >= 3:
                    break
                shown_q.append(query)
                items.append("<h2>%s</h2>" % query.replace("<", ""))
                for vid, title, kind, url in search_yt(query):
                    if kind == "channel":
                        continue
                    items.extend(cards([(vid, title)], seen, cur))
            if cur:
                items.append("<h2>Suggeriti per questo video</h2>")
                items.extend(cards(related_of(cur), seen, cur))
            for name, rows in account_sections():
                if name.startswith("<"):
                    items.append(name)
                    continue
                if not rows:
                    continue
                items.append("<h2>%s</h2>" % name)
                items.extend(cards(rows, seen, cur))
            chips = "".join(
                '<a class="chip" href="/home?q=%s">%s</a>' % (row.replace('"', ''), row.replace("<", ""))
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
<div class="chips" id="chips"></div>
<div class="grid">%s</div>
<script>
var key = 'yt-recent';
var q = new URLSearchParams(location.search).get('q') || '';
var rows = [];
try { rows = JSON.parse(localStorage.getItem(key) || '[]'); } catch (e) {}
if (q) rows = [q].concat(rows.filter(function(x){ return x.toLowerCase() !== q.toLowerCase(); })).slice(0, 10);
localStorage.setItem(key, JSON.stringify(rows));
document.getElementById('chips').innerHTML = rows.map(function(r){
  return '<a class="chip" href="/home?q=' + encodeURIComponent(r) + '">' + r.replace(/</g,'') + '</a>';
}).join('');
</script>
</body>""" % (("/?watch=" + cur) if cur else "/home", "".join(items) or "<p>nessuna ricerca recente</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/search":
            q = (parse_qs(u.query).get("q") or [""])[0]
            if q:
                push_recent(q)
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

        if path == "/config":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(json.dumps(CFG).encode())
            return

        if path == "/save":
            try:
                for key in LIMITS:
                    if key in q:
                        save_cfg(key, q[key][0])
            except Exception as e:
                self.send_response(400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(("non salvato: %s" % e).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"ok")
            apply_saved()
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
            self.wfile.write(("%d/%d %.2f" % (len(state["v_q"]), 6000, state.get("rate") or 0)).encode())
            return

        if path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write((state.get("err") or "").encode("utf-8"))
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
            fetch_duration()
            cancel_stop()
            try:
                want = float(tpos or 0)
            except ValueError:
                want = 0
            if not (ffmpeg_alive() and abs(want - now_pos()) < 8):
                new_session(want)
            vid = PAGE_URL.split("v=")[-1][:11] if "v=" in PAGE_URL else ""
            body = HTML.replace("__DUR__", str(int(state["duration"] or 0))).replace("__FPS__", str(FPS)).replace("__SLACK__", str(SLACK_MS)).replace("__START__", str(int(float(tpos or 0)))).replace("__CFG__", json.dumps(CFG)).replace("WATCHID", vid).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/ctrl":
            if "pos" in q:
                cancel_stop()
                want = float(q["pos"][0])
                if ffmpeg_alive() and abs(want - now_pos()) < 8:
                    msg = "tengo ffmpeg %.0f" % now_pos()
                else:
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
            state["a_evt"].wait(timeout=AUDIO_BUFFER_SEC + 12)
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
    print("conf", CFG_PATH, flush=True)
    fetch_duration()
    print("http://0.0.0.0:%d/  duration=%ss" % (PORT, state["duration"]))
    try:
        ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        stop_ff()

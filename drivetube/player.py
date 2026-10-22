"""Riproduzione per sessione: collega youtube (URL e info) a media (ffmpeg), gestisce avvio,
riavvio dopo errori/403, pausa, chiusura e pulizia delle sessioni."""
import os
import subprocess
import time
from collections import deque
from .context import (
    IDLE_CLOSE,
    MAX_PER_CODE,
    MAX_SESSIONS,
    OWNER,
    SESSIONS,
    SESSIONS_LOCK,
    Session,
    _bprint,
    _tls,
    bind,
    cur,
    lock,
    print,
    state,
    sthread,
)
from .settings import C
from .youtube import (
    FALLBACK_CLIENTS,
    StreamError,
    channel_of,
    channel_videos,
    net_with_client,
    related_of,
    stream_urls,
    video_info,
)
from .chunkproxy import px_forbidden, px_url
from .media import (
    ensure_fifo,
    ffmpeg_cmd,
    pump_audio,
    pump_h264,
    pump_jpegs,
    read_err,
    stop_ff,
    unblock_fifo,
)


def set_page(url):
    cur().url = url
    state['duration'] = 0
    state['seek_base'] = 0.0
    state['is_live'] = False
    state['title'] = ""
    sthread(fetch_duration).start()
    new_session(0)


def get_session(sid, who):
    drop = []
    with SESSIONS_LOCK:
        s = SESSIONS.get(sid)
        if s is not None and s.who != who:
            # stesso browser entrato con un altro codice: sessione nuova
            drop.append(SESSIONS.pop(sid))
            s = None
        if s is None:
            mine = sorted((x for x in SESSIONS.values() if x.who == who), key=lambda x: x.seen)
            while len(mine) >= MAX_PER_CODE:
                old = mine.pop(0)
                drop.append(SESSIONS.pop(old.sid))
            others = sorted(SESSIONS.values(), key=lambda x: x.seen)
            while len(others) >= MAX_SESSIONS:
                old = others.pop(0)
                drop.append(SESSIONS.pop(old.sid))
            s = Session(sid, who)
            SESSIONS[sid] = s
            _bprint("nuova sessione", s.tag, "(%d attive)" % len(SESSIONS), flush=True)
        s.seen = time.time()
    for d in drop:
        close_session(d)
    return s


def close_session(s):
    with SESSIONS_LOCK:
        if OWNER.get(s.who) == s.sid and s.sid not in SESSIONS:
            OWNER.pop(s.who, None)
    prev = getattr(_tls, "s", None)
    bind(s)
    try:
        with lock:
            state["gen"] += 1
            stop_ff()
        for path in (s.fifo_v, s.fifo_a):
            try:
                os.remove(path)
            except OSError:
                pass
        print("sessione chiusa", flush=True)
    finally:
        _tls.s = prev


def stop_all():
    with SESSIONS_LOCK:
        all_s = list(SESSIONS.values())
    for s in all_s:
        try:
            close_session(s)
        except Exception:
            pass


def janitor():
    while True:
        time.sleep(60)
        now = time.time()
        with SESSIONS_LOCK:
            old = [x for x in SESSIONS.values()
                   if now - x.seen > IDLE_CLOSE and not (x.state["ff"] and x.state["ff"].poll() is None)]
            for x in old:
                SESSIONS.pop(x.sid, None)
        for x in old:
            close_session(x)


def next_of_current():
    url = cur().url
    if not url or "v=" not in url:
        return []
    here = url.split("v=")[-1][:11]
    rows = [(vid, title) for vid, title in related_of(here) if vid != here]
    if len(rows) < 4:
        try:
            cid = channel_of(url)
            if cid:
                for vid, title in channel_videos(cid):
                    if vid != here and vid not in [x[0] for x in rows]:
                        rows.append((vid, title))
                    if len(rows) >= 8:
                        break
        except Exception as e:
            print("next canale", e, flush=True)
    return [{"id": vid, "title": title} for vid, title in rows[:8]]


def fetch_duration():
    url = cur().url
    if (state["duration"] or state.get("is_live")) and state.get("title") or not url:
        return
    info = video_info(url)
    if cur().url != url or info is None:
        return  # nel frattempo e' stato aperto un altro video, o yt-dlp non ha risposto
    if info["title"]:
        state["title"] = info["title"]
    if info["is_live"]:
        state["is_live"] = True
        print("live stream rilevato", flush=True)
    elif info["duration"]:
        state["duration"] = info["duration"]
        print("duration", state["duration"], flush=True)
    else:
        print("duration sconosciuta", flush=True)


def now_pos():
    if state["paused"] or state["t_play"] is None:
        return state["seek_base"]
    return max(0.0, state["seek_base"] + (time.perf_counter() - state["t_play"]))


def h264_height():
    return min(C("HEIGHT"), 720)


def ytdlp_format():
    if state["vmode"] == "h264":
        # solo H.264 (avc1), video e audio separati; mai sopra i 720p
        h = h264_height()
        return "bv*[vcodec^=avc1][height<=%d]+ba/bv*[vcodec^=avc1][height<=720]+ba" % h
    return "bv*[height<=720]+ba/bv*+ba/b"


def ytdlp_urls(net=None, quiet=False):
    # net: rete/client diversi da quelli salvati (ripiego dopo un 403);
    # quiet: in caso di errore non tocca state["err"] (il client lo leggerebbe
    # e, per esempio, passerebbe a MJPEG mentre si stanno provando altri client)
    try:
        return stream_urls(cur().url, ytdlp_format(), net)
    except StreamError as e:
        if quiet:
            raise
        if e.kind == "bot":
            state["err"] = "Verifica anti-bot di YouTube: controlla il PO token nella pagina admin."
        elif e.kind == "none":
            state["err"] = "Nessuno stream: video bloccato o non disponibile."
        elif state["vmode"] == "h264":
            # il client passa da solo a MJPEG quando vede questo errore
            state["err"] = "H264 non disponibile per questo video"
        else:
            state["err"] = "yt-dlp fallito. Controlla il log."
        raise RuntimeError(state["err"])


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
    sthread(later).start()


def cancel_stop():
    if state.get("hold_until"):
        print("ffmpeg tenuto aperto", flush=True)
    state["hold_until"] = 0


def spawn_ff(gen, pos, vsrc, asrc, askip=0):
    ensure_fifo(cur().fifo_v)
    ensure_fifo(cur().fifo_a)
    state["v_made"] = 0
    state["a_read"] = 0
    state["urls"] = (vsrc, asrc)
    pv = px_url(vsrc)
    pa = pv if asrc == vsrc else px_url(asrc)
    state["px_urls"] = (pv, pa)
    tail = deque(maxlen=12)
    p = subprocess.Popen(
        ffmpeg_cmd(pv, pa, pos),
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    state["ff"] = p
    print("ffmpeg %s @ %.1fs url=%d" % (state["vmode"], pos, 1 if asrc == vsrc else 2), flush=True)
    pump = pump_h264 if state["vmode"] == "h264" else pump_jpegs
    threads = [sthread(pump, gen, cur().fifo_v)]
    if asrc:
        threads.append(sthread(pump_audio, gen, cur().fifo_a, askip))
    for t in threads:
        t.start()
    sthread(read_err, p, tail).start()
    sthread(watch_ff, gen, p, pos, threads, tail).start()


def watch_ff(gen, p, pos, threads, tail):
    # Sorveglia ffmpeg. Se si chiude prima della fine del video (YouTube che
    # taglia la connessione, URL scaduto, 403) riparte dal punto esatto in cui
    # si era fermato, senza svuotare le code: il browser non se ne accorge.
    p.wait()
    unblock_fifo(cur().fifo_v)
    unblock_fifo(cur().fifo_a)
    for t in threads:
        while t.is_alive() and state["gen"] == gen:
            t.join(timeout=1)
    with lock:
        if state["gen"] != gen or state["ff"] is not p:
            return
        if state["vmode"] == "h264":
            # tempo dell'ultimo frame arrivato (vale anche tra un riavvio e l'altro)
            mx = state.get("v_maxpts")
            made = max(pos, mx + 1.0 / 30) if mx is not None else pos
        else:
            made = pos + state["v_made"] / float(C("FPS"))
        apos = pos + state["a_read"] * 8.0 / (C("AUDIO_KBPS") * 1000)
        dur = state["duration"] or 0
        rc = p.returncode
        is_live = state.get("is_live")
        if rc == 0 and not is_live and (not dur or made >= dur - 3 or (state["v_made"] == 0 and pos >= dur - 30)):
            state["ff_done"] = gen
            print("ffmpeg finito a %.0fs" % made, flush=True)
            return
        print("ffmpeg interrotto a %.1fs su %ds (codice %s, %d frame)" % (made, dur, rc, state["v_made"]), flush=True)
        for line in list(tail)[-3:]:
            print("  ff:", line[:240], flush=True)
        if made - pos >= 10:
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
        forbidden = any("403" in line for line in tail) or px_forbidden(state.get("px_urls"))
        cur_net = state.get("net")
    time.sleep(min(fails, 5) + 0.5)
    if forbidden or fails >= 2 or not urls:
        # 403: lo stesso URL verrebbe rifiutato di nuovo, se ne prendono di
        # nuovi subito; dal secondo 403 di fila si prova un altro client
        cands = [(cur_net, False)]
        if forbidden and fails >= 2:
            cands = [(net_with_client(c), True) for c in FALLBACK_CLIENTS[fails - 2:]]
        lst = None
        for net, alt in cands:
            try:
                lst = ytdlp_urls(net, quiet=alt)
            except Exception as e:
                print("riavvio fallito%s: %s" % (" con client " + net["client"] if alt else "", e), flush=True)
                continue
            if alt:
                print("403: provo il client %s" % net["client"], flush=True)
                state["net"] = net
            break
        if not lst:
            return
        state["err"] = ""
        urls = (lst[0], lst[1] if len(lst) > 1 else lst[0])
    with lock:
        if state["gen"] != gen or state["ff"] is not None:
            return
        askip = int(max(0.0, apos - made) * C("AUDIO_KBPS") * 1000 / 8)
        print("riparto da %.1fs (audio avanti %.2fs)" % (made, max(0.0, apos - made)), flush=True)
        spawn_ff(gen, made, urls[0], urls[1], askip)


def start_ff():
    # una sola partenza per sessione: un secondo /stream.mp3 (o un ffmpeg gia'
    # finito) non deve svuotare le code ne' rilanciare ffmpeg
    gen = state["gen"]
    if state.get("ff_gen") == gen:
        # un'altra richiesta (di solito /stream.h264) sta gia' avviando ffmpeg e
        # durante yt-dlp ha lasciato il lock: si aspetta che abbia finito. Prima
        # si tornava subito e /stream.mp3 rispondeva "no audio": al primo video
        # di ogni sessione si vedeva l'immagine senza suono
        t0 = time.time()
        while (state.get("ff_started") != gen and state.get("ff_gen") == gen
               and state["gen"] == gen and time.time() - t0 < 90):
            lock.release()
            time.sleep(0.05)
            lock.acquire()
        return
    stop_ff()
    state["ff_gen"] = gen
    state["fails"] = 0
    state["v_maxpts"] = None
    pos = int(now_pos())
    net = state.get("net")
    lock.release()
    try:
        urls = ytdlp_urls(net)
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
    state["ff_started"] = gen


def new_session(pos=None, vmode=None):
    with lock:
        if vmode in ("h264", "mjpeg"):
            state["vmode"] = vmode
        if pos is not None:
            state["seek_base"] = max(0.0, pos)
        state["t_play"] = None
        state["paused"] = False
        state["err"] = ""
        state["gen"] += 1
        stop_ff()

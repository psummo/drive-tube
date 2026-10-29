"""Server HTTP: pagine, API del player, pagina admin, avvio."""
import hmac
import html
import json
import os
import re
import secrets
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse
from .config import COOKIES, HEADERS, PORT, SETTINGS_UI, UA, VERSION, feed_cache
from .context import (
    MAX_PER_CODE,
    OWNER,
    SESSIONS,
    SESSIONS_LOCK,
    _bprint,
    _tls,
    bind,
    cur,
    in_session,
    lock,
    print,
    state,
    sthread,
)
from .settings import (
    C,
    apply_settings,
    cookie_value,
    push_recent,
    recent_cookie_header,
    recent_from_cookie,
)
from .access import (
    ACCESS,
    ACCESS_LOCK,
    ADMIN_PASS,
    access_save,
    admin_token,
    code_label,
    code_label_any,
    cookie_mode,
    home_feed,
    is_admin,
    login_blocked,
    login_failed,
    new_code,
    use_cookies,
    who_is,
)
from .tools import ytdlp_status, ytdlp_update
from .youtube import (
    COOKIE_CHECK,
    account_sections,
    channel_videos,
    cookies_check,
    cookies_youtube_only,
    related_of,
    search_yt,
    yt_cmd,
    yt_net_label,
)
from .potoken import (
    pot_install_begin,
    pot_install_plugin,
    pot_status,
    pot_supervisor,
    pot_test,
    yt_diagnose,
)
from .netutil import proxy_alive, QuietServer, client_gone
from .chunkproxy import start_chunk_proxy
from .media import V_CAP, ffmpeg_alive
from .player import (
    arm_stop,
    cancel_stop,
    close_session,
    fetch_duration,
    get_session,
    janitor,
    new_session,
    next_of_current,
    now_pos,
    set_page,
    start_ff,
    stop_all,
)
from .update import check_update, install_update, restart_self
from .report import delete_report, list_reports, read_report, save_report
from .pages import KEYTEST_HTML, CHANNEL_HTML, HOME_HTML, SEARCH_HTML, ADMIN_HTML, PLAYER_HTML, LOGIN_HTML, TEST_HTML, WASMTEST_HTML, h264_wasm_gz, page


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


def cookie_hdr(name, value, max_age=31536000, strict=False):
    return "%s=%s; Max-Age=%d; Path=/; SameSite=%s; HttpOnly" % (
        name, value, max_age, "Strict" if strict else "Lax")


# pagine HTML (senza accesso si va al login); il resto risponde 401
HTML_PATHS = {"/", "/index.html", "/home", "/search", "/channel", "/watch", "/test", "/wasmtest", "/keytest"}


class Handler(BaseHTTPRequestHandler):
    def end_headers(self):
        for k, v in getattr(self, "_extra", []):
            self.send_header(k, v)
        super().end_headers()

    def add_header(self, k, v):
        if not hasattr(self, "_extra"):
            self._extra = []
        self._extra.append((k, v))

    def redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.end_headers()

    def client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        hdr = self.headers.get("Cookie")
        if path == "/favicon.ico":
            self.send_error(404)
            return
        if path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if path == "/login":
            self.page_login()
            return
        if path == "/logout":
            self.add_header("Set-Cookie", cookie_hdr("dt_code", "", 0))
            self.add_header("Set-Cookie", cookie_hdr("dt_admin", "", 0, strict=True))
            self.redirect("/login")
            return
        if path == "/admin" or path.startswith("/admin/"):
            self.admin_get(u, path)
            return
        if self.enter(path):
            try:
                self._get(u, parse_qs(u.query), path)
            finally:
                _tls.s = None

    def enter(self, path):
        """Controlla l'accesso e lega la sessione del browser al thread."""
        hdr = self.headers.get("Cookie")
        who = who_is(hdr)
        page = path in HTML_PATHS or not path.startswith(("/stream", "/ctrl", "/buf", "/status", "/dur",
                                                           "/prime", "/ready", "/bye", "/next", "/h264", "/clog"))
        if not who:
            if page:
                self.redirect("/login")
            else:
                self.send_error(401, "serve il codice di accesso")
            return False
        self.who = who
        sid = cookie_value(hdr, "dt_sid")
        if not (len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)):
            sid = secrets.token_hex(16)
            self.add_header("Set-Cookie", cookie_hdr("dt_sid", sid))
        # un codice si usa da un solo browser alla volta: aprire una pagina lo
        # prende (e chiude l'altro); le richieste del player di un browser che
        # l'ha perso vengono rifiutate, cosi' i due schermi non se lo rubano
        with SESSIONS_LOCK:
            owner = OWNER.get(who)
            if owner and owner != sid and not page:
                busy = True
            else:
                busy = False
                OWNER[who] = sid
        if busy:
            body = b"in uso altrove"
            self.send_response(409)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        bind(get_session(sid, who))
        return True

    def _get(self, u, q, path):
        if path not in ("/buf", "/status", "/dur", "/prime", "/ready", "/clog"):
            print("GET", path, flush=True)

        if path == "/home":
            q = (parse_qs(u.query).get("q") or [""])[0]
            recent = push_recent(q, recent_from_cookie(self.headers.get("Cookie")))
            url_now = cur().url
            here = url_now.split("v=")[-1][:11] if "v=" in url_now else ""
            admin = self.who == "admin"
            items = []
            seen = set()
            # collect queries to run in parallel
            shown_q = []
            for query in recent:
                if query == q or query in shown_q:
                    continue
                if len(shown_q) >= 6:
                    break
                shown_q.append(query)
            all_queries = ([q] if q else []) + shown_q
            # ricerche, suggeriti e (con i cookie) account in parallelo, ognuno
            # nella sessione di chi chiede
            mine = use_cookies()
            show_personal = mine and home_feed() == "personal"
            with ThreadPoolExecutor(max_workers=8) as pool:
                search_futures = {query: pool.submit(in_session(search_yt), query) for query in all_queries}
                related_future = pool.submit(in_session(related_of), here) if here else None
                account_future = pool.submit(in_session(account_sections)) if show_personal else None
            # La ricerca appena fatta resta in cima, a parte. Tutto il resto
            # (ricerche recenti, suggeriti, canali, account) viene mescolato a
            # turno, cosi' i suggeriti finiscono in mezzo agli altri video.
            found_channels = []  # (cid, name)
            if q:
                items.append("<h2>Ricerca: %s</h2>" % q.replace("<", ""))
                for vid, title, kind, url in search_futures[q].result():
                    if kind == "channel":
                        items.append(
                            '<a class="card" href="/channel?id=%s"><span>Canale: %s</span></a>'
                            % (vid, title.replace("<", ""))
                        )
                        found_channels.append((url or vid, title))
                    else:
                        items.extend(cards([(vid, title)], seen, here))
            sources = []  # liste di (vid, titolo), da mescolare
            if here and related_future:
                sources.append(related_future.result())
            for query in shown_q:
                rows = []
                for vid, title, kind, url in search_futures[query].result():
                    if kind == "channel":
                        found_channels.append((url or vid, title))
                    else:
                        rows.append((vid, title))
                sources.append(rows)
            if found_channels:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    ch_futs = [pool.submit(in_session(channel_videos), cid, cname)
                               for cid, cname in found_channels[:4]]
                for fut in ch_futs:
                    try:
                        sources.append(fut.result()[:12])
                    except Exception:
                        pass
            for name, rows in (account_future.result() if account_future else []):
                sources.append(rows)
            mixed = []
            for i in range(max([len(s) for s in sources] or [0])):
                for s in sources:
                    if i < len(s):
                        mixed.append(s[i])
            if mixed:
                if q:
                    items.append("<h2>Per te</h2>")
                items.extend(cards(mixed, seen, here))
            chips = "".join(
                '<a class="chip" href="/home?q=%s">%s</a>' % (quote(row), html.escape(row))
                for row in recent
            )
            body = HOME_HTML % ('<a class="back" href="/admin" style="background:#333;color:#fff;text-decoration:none;padding:8px 14px;font-size:18px">admin</a>' if admin else "",
              chips, "".join(items) or "<p>nessuna ricerca recente</p>")
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
            body = SEARCH_HTML % (q.replace("<", ""), "".join(items))
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
            body = CHANNEL_HTML % ("".join(items) or "<p>nessun video</p>")
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

        if path == "/keytest":
            body = KEYTEST_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body)
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
            if state.get("is_live"):
                self.wfile.write(b"-1")
            else:
                self.wfile.write(str(int(state["duration"] or 0)).encode())
            return

        if path == "/title":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write((state.get("title") or "").encode("utf-8"))
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
            url = ("https://www.youtube.com/watch?v=" + vid) if vid else cur().url
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
            if watch and watch not in cur().url:
                set_page("https://www.youtube.com/watch?v=" + watch)
            try:
                if float(tpos) > 0:
                    state["seek_base"] = float(tpos)
            except ValueError:
                pass
            if not cur().url:
                self.send_response(302)
                self.send_header("Location", "/home")
                self.end_headers()
                return
            if not state["duration"]:
                sthread(fetch_duration).start()
            cancel_stop()
            try:
                want = float(tpos or 0)
            except ValueError:
                want = 0
            if cfg_changed or not (ffmpeg_alive() and abs(want - now_pos()) < 8):
                new_session(want)
            vid = cur().url.split("v=")[-1][:11] if "v=" in cur().url else ""
            body = PLAYER_HTML.replace("__DUR__", str(int(state["duration"] or 0))).replace("__FPS__", str(C("FPS"))).replace("__START__", str(int(float(tpos or 0)))).replace("__CFG__", json.dumps(cur().cfg)).replace("__OPTS__", json.dumps(SETTINGS_UI)).replace("__VER__", VERSION).replace("__VMODE__", str(int(cur().cfg.get("VMODE", 1)))).replace("__LIVE__", "1" if state.get("is_live") else "0").replace("__TITLE__", html.escape(state.get("title") or "drive-tube")).replace("WATCHID", vid).encode("utf-8")
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
                mode = (q.get("mode") or [""])[0]
                if mode not in ("h264", "mjpeg"):
                    mode = "h264" if cur().cfg.get("VMODE", 1) else "mjpeg"
                new_session(want, mode)
                msg = "seek %.0f %s" % (state["seek_base"], mode)
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

        if path == "/clog":
            # righe di diagnosi del player (stato audio ecc.), nel log del server
            m = (parse_qs(u.query).get("m") or [""])[0]
            m = re.sub(r"[\x00-\x1f]", " ", m)[:200]
            if m:
                print("player:", m, flush=True)
            self.send_response(204)
            self.end_headers()
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
                print("stream audio rifiutato: ffmpeg non avviato", flush=True)
                self.send_error(404, "no audio")
                return
            # un nuovo collegamento audio (anche un ricollegamento del player)
            # annulla lo spegnimento programmato dalla chiusura del precedente
            cancel_stop()
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            gen = state["gen"]
            sent = 0
            print("audio: player collegato (in coda %d KB)" % (sum(len(c) for c in list(state["a_q"])) // 1024), flush=True)
            try:
                while state["gen"] == gen:
                    if client_gone(self):
                        print("audio: player scollegato dopo %d KB" % (sent // 1024), flush=True)
                        arm_stop("audio client chiuso")
                        return
                    if state["a_q"]:
                        c = state["a_q"].popleft()
                        self.wfile.write(c)
                        sent += len(c)
                    elif state.get("ff_done") == gen:
                        print("audio: fine, inviati %d KB" % (sent // 1024), flush=True)
                        return
                    else:
                        time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError, OSError):
                print("audio: connessione persa dopo %d KB" % (sent // 1024), flush=True)
                arm_stop("audio client via")
            return

        if path.startswith("/stream.h264"):
            # record [lunghezza u32][pts f64][frame Annex-B], uno per frame
            if state["vmode"] != "h264":
                self.send_error(409, "sessione in modalita' %s" % state["vmode"])
                return
            try:
                with lock:
                    start_ff()
            except Exception as e:
                print("video h264 err", e, flush=True)
                self.send_error(500, str(e))
                return
            cancel_stop()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
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
                    out = []
                    while state["v_q"] and len(out) < 64:
                        out.append(state["v_q"].popleft())
                    self.wfile.write(b"".join(out))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                arm_stop("video client via")
            return

        if path.startswith("/stream.mjpg"):
            if state["vmode"] != "mjpeg":
                self.send_error(409, "sessione in modalita' %s" % state["vmode"])
                return
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

        # indirizzo sconosciuto: si torna alla home
        self.redirect("/home")

    # ---------- login ----------
    def page_login(self, msg="", status=200, admin=False):
        if admin:
            fill = {"__ACTION__": "/admin/login", "__TITLE__": "Admin", "__FIELD__": "password",
                    "__TYPE__": "password", "__PH__": "password admin", "__NOTE__": "",
                    "__LINK__": '<a href="/login">accesso con codice</a>'}
        else:
            note = "" if ACCESS["codes"] else "Nessun codice creato: il proprietario lo crea dalla pagina admin."
            fill = {"__ACTION__": "/login", "__TITLE__": "drive-tube", "__FIELD__": "code",
                    "__TYPE__": "text", "__PH__": "codice di accesso", "__NOTE__": note,
                    "__LINK__": '<a href="/admin">admin</a>'}
        fill["__MSG__"] = html.escape(msg)
        body = LOGIN_HTML
        for k, v in fill.items():
            body = body.replace(k, v)
        body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json_out(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- admin (GET) ----------
    def admin_get(self, u, path):
        hdr = self.headers.get("Cookie")
        if not ADMIN_PASS:
            body = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<body style='background:#111;color:#eee;font-family:sans-serif;padding:20px;line-height:1.5'>"
                    "<h1>Pagina admin spenta</h1><p>Per attivarla aggiungi la password admin al servizio sull'EC2:</p>"
                    "<pre style='background:#222;padding:10px'>sudo systemctl edit drive-tube</pre>"
                    "<p>e scrivi:</p><pre style='background:#222;padding:10px'>[Service]\nEnvironment=YT_ADMIN=la-tua-password</pre>"
                    "<p>poi <code>sudo systemctl restart drive-tube</code>.</p></body>").encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        if not is_admin(hdr):
            if path.startswith("/admin/api/") or path in ("/admin/log", "/admin/report"):
                self.json_out({"ok": False, "error": "accesso admin scaduto, ricarica la pagina"}, 403)
            else:
                self.page_login(admin=True)
            return
        if path == "/admin":
            body = (ADMIN_HTML.replace("__VER__", VERSION).replace("__MAXC__", str(MAX_PER_CODE))).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/admin/api/state":
            now = time.time()
            with SESSIONS_LOCK:
                sess = list(SESSIONS.values())
            rows = []
            for x in sorted(sess, key=lambda x: -x.seen):
                ff = x.state.get("ff")
                ff_alive = bool(ff and ff.poll() is None)
                rows.append({
                    "sid": x.sid, "who": x.who,
                    "playing": bool(x.url) and (ff_alive or now - x.seen < 30),
                    "vmode": x.state.get("vmode"), "ff": ff_alive,
                    "seen": now - x.seen, "err": x.state.get("err") or "",
                })
            with ACCESS_LOCK:
                codes = [dict(c) for c in ACCESS["codes"]]
            self.json_out({"ok": True, "version": VERSION, "codes": codes, "sessions": rows})
            return
        if path == "/admin/api/update/check":
            self.json_out(check_update())
            return
        if path == "/admin/api/cookies":
            st = os.stat(COOKIES) if os.path.isfile(COOKIES) else None
            self.json_out({"present": bool(st), "size": st.st_size if st else 0,
                           "mtime": int(st.st_mtime) if st else 0, "mode": cookie_mode(),
                           "feed": home_feed(), "check": dict(COOKIE_CHECK)})
            return
        if path == "/admin/api/pot":
            st = pot_status()
            n = ACCESS.get("yt") or {}
            st["net"] = {"label": yt_net_label(n), "client": n.get("client", ""),
                         "ipv4": bool(n.get("ipv4")), "proxy": n.get("proxy", ""),
                         "proxy_ok": proxy_alive(n["proxy"]) if n.get("proxy") else None}
            self.json_out(st)
            return
        if path == "/admin/api/ytdlp":
            self.json_out(ytdlp_status(force=bool(parse_qs(u.query).get("force"))))
            return
        if path == "/admin/api/reports":
            self.json_out({"reports": list_reports()})
            return
        if path == "/admin/report":
            rid = (parse_qs(u.query).get("id") or [""])[0]
            text = read_report(rid)
            if text is None:
                self.send_error(404)
                return
            body = text.encode("utf-8", "replace")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if parse_qs(u.query).get("dl"):
                self.send_header("Content-Disposition", 'attachment; filename="drive-tube-%s.txt"' % rid)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/admin/log":
            try:
                lines = open(os.path.expanduser("~/yt.log")).read().splitlines()
            except Exception:
                lines = ["log non trovato: ~/yt.log"]
            lines = [x for x in lines if "GET /admin" not in x]
            body = "\n".join(lines[-500:]).encode("utf-8", "replace")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.redirect("/admin")

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        hdr = self.headers.get("Cookie")
        n = int(self.headers.get("Content-Length") or 0)
        if n > (20000000 if path == "/admin/cookies" else 2000000):
            self.send_error(413)
            return
        raw = self.rfile.read(n)
        if path == "/login":
            self.post_login(raw)
        elif path == "/report":
            if not self.enter(path):
                return
            try:
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    data = {}
                data = data if isinstance(data, dict) else {}
                client = data.get("client") if isinstance(data.get("client"), dict) else {}
                self.json_out({"ok": True, "id": save_report(cur(), str(data.get("note") or ""), client)})
            except Exception as e:
                _bprint("segnalazione non salvata:", e, flush=True)
                self.json_out({"ok": False, "error": str(e)}, 500)
            finally:
                _tls.s = None
        elif path == "/admin/login":
            self.post_admin_login(raw)
        elif path == "/admin/cookies":
            if not ADMIN_PASS or not is_admin(hdr):
                self.send_error(403)
                return
            self.post_cookies(raw, anon=parse_qs(urlparse(self.path).query).get("anon") == ["1"])
        elif path.startswith("/admin/api/"):
            if not ADMIN_PASS or not is_admin(hdr):
                self.json_out({"ok": False, "error": "accesso admin scaduto, ricarica la pagina"}, 403)
                return
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                data = {}
            self.admin_api(path[len("/admin/api/"):], data if isinstance(data, dict) else {})
        else:
            self.send_error(404)

    def post_login(self, raw):
        ip = self.client_ip()
        if login_blocked(ip):
            self.page_login("Troppi tentativi sbagliati: riprova tra qualche minuto.", 429)
            return
        code = (parse_qs(raw.decode("utf-8", "replace")).get("code") or [""])[0].strip().lower()
        label = code_label(code)
        if not label:
            login_failed(ip)
            _bprint("accesso negato da", ip, flush=True)
            self.page_login("Codice non valido.", 403)
            return
        _bprint("accesso:", label, "da", ip, flush=True)
        self.add_header("Set-Cookie", cookie_hdr("dt_code", code))
        sid = cookie_value(self.headers.get("Cookie"), "dt_sid")
        if not (len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)):
            self.add_header("Set-Cookie", cookie_hdr("dt_sid", secrets.token_hex(16)))
        self.redirect("/home")

    def post_admin_login(self, raw):
        ip = self.client_ip()
        if not ADMIN_PASS:
            self.redirect("/admin")
            return
        if login_blocked(ip):
            self.page_login("Troppi tentativi sbagliati: riprova tra qualche minuto.", 429, admin=True)
            return
        pw = (parse_qs(raw.decode("utf-8", "replace")).get("password") or [""])[0]
        if not hmac.compare_digest(pw.encode(), ADMIN_PASS.encode()):
            login_failed(ip)
            _bprint("accesso admin negato da", ip, flush=True)
            self.page_login("Password sbagliata.", 403, admin=True)
            return
        _bprint("accesso admin da", ip, flush=True)
        self.add_header("Set-Cookie", cookie_hdr("dt_admin", admin_token(), 30 * 86400, strict=True))
        self.redirect("/admin")

    def admin_api(self, what, data):
        if what == "code/add":
            label = str(data.get("label") or "").strip()[:40]
            code = str(data.get("code") or "").strip().lower()
            if not label:
                self.json_out({"ok": False, "error": "manca il nome"})
                return
            if code and not (4 <= len(code) <= 40 and all(c.isalnum() or c in "-_" for c in code) and code.isascii()):
                self.json_out({"ok": False, "error": "codice: da 4 a 40 tra lettere, numeri, - e _"})
                return
            with ACCESS_LOCK:
                code = code or new_code()
                if any(c.get("code") == code for c in ACCESS["codes"]):
                    self.json_out({"ok": False, "error": "codice gia' usato"})
                    return
                if label.lower() == "admin" or any((c.get("label") or "").lower() == label.lower() for c in ACCESS["codes"]):
                    self.json_out({"ok": False, "error": "nome gia' usato, scegline un altro"})
                    return
                ACCESS["codes"].append({"label": label, "code": code, "enabled": True, "created": int(time.time())})
                access_save()
            _bprint("admin: nuovo codice per", label, flush=True)
            self.json_out({"ok": True, "code": code})
            return
        if what in ("code/toggle", "code/del"):
            code = str(data.get("code") or "")
            with ACCESS_LOCK:
                hit = [c for c in ACCESS["codes"] if c.get("code") == code]
                if not hit:
                    self.json_out({"ok": False, "error": "codice non trovato"})
                    return
                if what == "code/del":
                    ACCESS["codes"] = [c for c in ACCESS["codes"] if c.get("code") != code]
                else:
                    hit[0]["enabled"] = not hit[0].get("enabled", True)
                access_save()
                label = hit[0].get("label")
                off = what == "code/del" or not hit[0].get("enabled", True)
            if off:
                # chi usava quel codice viene chiuso subito
                with SESSIONS_LOCK:
                    gone = [x for x in SESSIONS.values() if x.who == label and not code_label_any(label)]
                    for x in gone:
                        SESSIONS.pop(x.sid, None)
                for x in gone:
                    close_session(x)
            _bprint("admin:", what, label, flush=True)
            self.json_out({"ok": True})
            return
        if what == "session/stop":
            sid = str(data.get("sid") or "")
            with SESSIONS_LOCK:
                x = SESSIONS.pop(sid, None)
            if x:
                close_session(x)
            self.json_out({"ok": True, "msg": "sessione chiusa" if x else "sessione gia' chiusa"})
            return
        if what == "report/del":
            ok = delete_report(str(data.get("id") or ""))
            self.json_out({"ok": ok, "msg": "segnalazione eliminata" if ok else "", "error": "" if ok else "non trovata"})
            return
        if what == "update/install":
            try:
                new = install_update()
            except Exception as e:
                self.json_out({"ok": False, "error": str(e)}, 500)
                return
            self.json_out({"ok": True, "version": new})
            threading.Thread(target=restart_self, daemon=True).start()
            return
        if what == "ytdlp/update":
            try:
                v = ytdlp_update()
            except Exception as e:
                _bprint("aggiornamento yt-dlp fallito:", e, flush=True)
                self.json_out({"ok": False, "error": str(e)}, 500)
                return
            _bprint("yt-dlp aggiornato a", v, flush=True)
            self.json_out({"ok": True, "version": v})
            return
        if what == "cookies/check":
            r = cookies_check()
            self.json_out({"ok": True, "check": r,
                           "msg": {True: "cookie validi: account riconosciuto", False: "cookie NON validi: riesportali"}.get(r["ok"], r["msg"])})
            return
        if what == "cookies/mode":
            mode = data.get("mode")
            if mode not in ("off", "admin", "all"):
                self.json_out({"ok": False, "error": "scelta non valida"})
                return
            with ACCESS_LOCK:
                ACCESS["cookies"] = mode
                access_save()
            feed_cache.clear()
            _bprint("admin: cookie YouTube per", mode, flush=True)
            self.json_out({"ok": True})
            return
        if what == "home_feed/mode":
            mode = data.get("mode")
            if mode not in ("personal", "related"):
                self.json_out({"ok": False, "error": "scelta non valida"})
                return
            with ACCESS_LOCK:
                ACCESS["home_feed"] = mode
                access_save()
            feed_cache.clear()
            _bprint("admin: home feed", mode, flush=True)
            self.json_out({"ok": True})
            return
        if what == "cookies/delete":
            try:
                os.remove(COOKIES)
            except FileNotFoundError:
                pass
            feed_cache.clear()
            _bprint("admin: cookie YouTube cancellati", flush=True)
            self.json_out({"ok": True, "msg": "cookie cancellati"})
            return
        if what == "pot/plugin":
            try:
                where = pot_install_plugin()
            except Exception as e:
                _bprint("plugin PO token non installato:", e, flush=True)
                self.json_out({"ok": False, "error": str(e)}, 500)
                return
            _bprint("plugin PO token installato in", where, flush=True)
            self.json_out({"ok": True, "path": where})
            return
        if what == "pot/server":
            if not pot_install_begin():
                self.json_out({"ok": False, "error": "installazione gia' in corso"}, 409)
                return
            _bprint("admin: installo il server PO token", flush=True)
            self.json_out({"ok": True})
            return
        if what == "pot/test":
            r = pot_test()
            r["net"] = yt_net_label(ACCESS.get("yt") or {})
            self.json_out(r)
            return
        if what == "yt/diagnose":
            self.json_out({"ok": True, "results": yt_diagnose()})
            return
        if what == "yt/net":
            cur_net = dict(ACCESS.get("yt") or {})
            if "client" in data:
                c = str(data.get("client") or "")
                if c and not re.fullmatch(r"[a-z_]{2,30}", c):
                    self.json_out({"ok": False, "error": "client non valido"})
                    return
                cur_net["client"] = c
            if "ipv4" in data:
                cur_net["ipv4"] = bool(data.get("ipv4"))
            if "proxy" in data:
                px = str(data.get("proxy") or "").strip()
                if px and not re.fullmatch(r"(https?|socks5h?|socks4a?)://\S+", px):
                    self.json_out({"ok": False, "error": "proxy non valido: usa http://, https:// o socks5://"})
                    return
                cur_net["proxy"] = px
            with ACCESS_LOCK:
                ACCESS["yt"] = cur_net
                access_save()
            feed_cache.clear()
            _bprint("admin: rete YouTube ->", yt_net_label(cur_net), flush=True)
            self.json_out({"ok": True, "msg": "salvato: " + yt_net_label(cur_net)})
            return
        if what == "restart":
            _bprint("admin: riavvio", flush=True)
            self.json_out({"ok": True})
            threading.Thread(target=restart_self, daemon=True).start()
            return
        self.json_out({"ok": False, "error": "azione sconosciuta"}, 404)

    def post_cookies(self, raw, anon=False):
        text = raw.decode("utf-8", "replace")
        if "filename=" in text:
            parts = text.split("\r\n\r\n", 1)
            if len(parts) == 2:
                text = parts[1].rsplit("\r\n--", 1)[0]
        if "youtube.com" not in text or "\t" not in text:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("file non valido: servono i cookie di youtube.com in formato Netscape".encode("utf-8"))
            return
        text, kept, dropped = cookies_youtube_only(text, anon)
        if not kept:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("nel file non ci sono cookie di youtube.com".encode("utf-8"))
            return
        fd = os.open(COOKIES, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        feed_cache.clear()
        _bprint("admin: cookie tenuti %d (youtube.com%s), scartati %d" % (kept, ", senza account" if anon else "", dropped), flush=True)
        with SESSIONS_LOCK:
            for x in SESSIONS.values():
                x.state["err"] = ""
        _bprint("admin: cookie YouTube caricati", flush=True)
        COOKIE_CHECK.update(t=0, ok=None, msg="")
        threading.Thread(target=cookies_check, daemon=True).start()  # verifica subito i nuovi cookie
        self.redirect("/admin")

    def log_message(self, *a):
        pass


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
        srv = QuietServer(("0.0.0.0", port), Handler)
        # handshake nel thread della richiesta, non nel ciclo che accetta le connessioni
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    except Exception as e:
        print("https non avviato:", e, flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _bprint("https://0.0.0.0:%d/" % port, flush=True)


def main():
    _bprint("http://0.0.0.0:%d/" % PORT, flush=True)
    print("codici attivi: %d, admin %s" % (
        sum(1 for c in ACCESS["codes"] if c.get("enabled", True)),
        "attivo" if ADMIN_PASS else "spento (manca YT_ADMIN)"), flush=True)
    threading.Thread(target=janitor, daemon=True).start()
    threading.Thread(target=pot_supervisor, daemon=True).start()
    start_chunk_proxy()
    serve_https()
    try:
        QuietServer(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        stop_all()

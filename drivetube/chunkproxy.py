"""Proxy locale che scarica un URL HTTP a pezzi da 10 MB (YouTube rallenta le richieste
lunghe) e riprende da solo se la connessione cade. Generico: non sa nulla di YouTube."""
import urllib.request
import re
import secrets
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from .config import UA
from .context import _tls, print
from .access import ACCESS
from .netutil import QuietServer


# ================= scaricamento a pezzi da YouTube =================
# ffmpeg scaricherebbe ogni stream con un'unica richiesta HTTP fino alla fine
# del file: YouTube dopo qualche MB rallenta o chiude la connessione, e con i
# video lunghi si vedono blocchi continui. Come fa yt-dlp, si scarica a pezzi
# da 10 MB: ffmpeg legge da questo piccolo proxy locale (solo 127.0.0.1), che
# chiede a YouTube un pezzo alla volta e, se una connessione cade, riprende
# dallo stesso byte senza che ffmpeg se ne accorga.
CHUNK = 10 << 20


_px = {}


_px_lock = threading.Lock()


_px_port = [0]


def px_url(url):
    """URL locale che serve `url` a pezzi; se il proxy non e' partito, `url`."""
    if not _px_port[0] or not url:
        return url
    tok = secrets.token_hex(8)
    now = time.time()
    with _px_lock:
        for k in [k for k, v in _px.items() if now - v["t"] > 6 * 3600]:
            _px.pop(k, None)
        _px[tok] = {"url": url, "t": now, "size": 0, "forbidden": False,
                    "s": getattr(_tls, "s", None), "slow": 0}
    return "http://127.0.0.1:%d/%s" % (_px_port[0], tok)


def px_forbidden(urls):
    """True se YouTube ha risposto 403 su uno di questi URL locali."""
    for u in urls or ():
        e = _px.get((u or "").rsplit("/", 1)[-1])
        if e and e["forbidden"]:
            return True
    return False


def _px_opener():
    # il proxy http(s) scelto in admin vale anche per lo stream (socks no:
    # urllib non lo gestisce)
    proxy = (ACCESS.get("yt") or {}).get("proxy", "")
    if proxy.startswith(("http://", "https://")):
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def _px_open(e, a, b):
    """Apre il pezzo a-b, con 3 tentativi; su 403 si ferma subito."""
    req = urllib.request.Request(e["url"], headers={
        "User-Agent": UA, "Referer": "https://www.youtube.com/",
        "Range": "bytes=%d-%d" % (a, b)})
    err = None
    for i in range(3):
        try:
            return _px_opener().open(req, timeout=20)
        except urllib.error.HTTPError as x:
            if x.code == 403:
                e["forbidden"] = True
                raise
            err = x
        except Exception as x:
            err = x
        time.sleep(1 + i)
    raise err


SLOW_KBPS = 150  # sotto questa velocita' un pezzo intero da YouTube e' sospetto


def px_speed(e, nbytes, secs):
    """Registra quando YouTube manda un pezzo molto piu' lentamente del normale:
    succede quando rallenta lo stream (di solito yt-dlp da aggiornare)."""
    if nbytes < (1 << 20) or secs <= 0:
        return
    kbps = nbytes / 1024.0 / secs
    if kbps < SLOW_KBPS:
        e["slow"] += 1
        print("stream youtube LENTO: %.0f KB/s (%.1f MB in %.0f s, %d volte) - prova ad aggiornare yt-dlp dall'admin"
              % (kbps, nbytes / 1048576.0, secs, e["slow"]), flush=True)


class ChunkProxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        e = _px.get(self.path.lstrip("/"))
        if not e:
            self.send_error(404)
            return
        _tls.s = e.get("s")  # le righe di log vanno alla sessione che ha aperto lo stream
        try:
            self._serve(e)
        finally:
            _tls.s = None

    def _serve(self, e):
        m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
        start = int(m.group(1)) if m else 0
        end = int(m.group(2)) if m and m.group(2) else None
        first = start + CHUNK - 1 if end is None else min(start + CHUNK - 1, end)
        try:
            r = _px_open(e, start, first)
        except urllib.error.HTTPError as x:
            print("stream youtube: HTTP %d" % x.code, flush=True)
            self.send_error(x.code)  # ffmpeg vede "403 Forbidden" e si riparte
            return
        except Exception as x:
            print("stream youtube:", x, flush=True)
            self.send_error(502)
            return
        mm = re.search(r"/(\d+)\s*$", r.headers.get("Content-Range", ""))
        if mm:
            e["size"] = int(mm.group(1))
        elif not e["size"]:
            mc = re.search(r"[?&]clen=(\d+)", e["url"])
            if mc:
                e["size"] = int(mc.group(1))
        total = e["size"]
        self.close_connection = True
        if not total:
            # dimensione sconosciuta: si passa la risposta cosi' com'e'
            self.send_response(200)
            self.send_header("Content-Type", r.headers.get("Content-Type", "application/octet-stream"))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    buf = r.read(65536)
                    if not buf:
                        break
                    self.wfile.write(buf)
            except Exception:
                pass
            r.close()
            return
        if start >= total:
            r.close()
            self.send_response(416)
            self.send_header("Content-Range", "bytes */%d" % total)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        last = total - 1 if end is None else min(end, total - 1)
        self.send_response(206 if m else 200)
        self.send_header("Content-Type", r.headers.get("Content-Type", "application/octet-stream"))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(last - start + 1))
        if m:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, last, total))
        self.send_header("Connection", "close")
        self.end_headers()
        pos = start
        piece_at, piece_from = time.time(), start
        while pos <= last:
            if r is None:
                piece_at, piece_from = time.time(), pos
                try:
                    r = _px_open(e, pos, min(pos + CHUNK - 1, last))
                except Exception as x:
                    print("stream youtube: pezzo da %d non riuscito: %s" % (pos, x), flush=True)
                    return  # ffmpeg si riconnette da qui (o riparte con URL nuovi)
            try:
                while pos <= last:
                    buf = r.read(65536)
                    if not buf:
                        break
                    if pos + len(buf) - 1 > last:
                        buf = buf[:last - pos + 1]
                    try:
                        self.wfile.write(buf)
                    except Exception:
                        r.close()
                        return  # ffmpeg ha chiuso (seek, pausa, fine)
                    pos += len(buf)
            except Exception as x:
                print("stream youtube: connessione caduta a %d, riprendo: %s" % (pos, x), flush=True)
            r.close()
            r = None
            px_speed(e, pos - piece_from, time.time() - piece_at)


def start_chunk_proxy():
    try:
        srv = QuietServer(("127.0.0.1", 0), ChunkProxy)
    except Exception as x:
        print("proxy stream non avviato (ffmpeg scarica direttamente):", x, flush=True)
        return
    _px_port[0] = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

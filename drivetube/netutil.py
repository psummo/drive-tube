"""Piccoli strumenti HTTP condivisi: server che non riempie il log, client
disconnesso, aperture di URL attraverso un proxy http(s) o SOCKS5 (per esempio
un tunnel verso la connessione di casa) e controllo che il proxy sia vivo."""
import http.client
import select
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .context import print


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


class QuietServer(ThreadingHTTPServer):
    # connessioni chiuse a meta' (pagina cambiata, scanner, handshake TLS falliti)
    # non riempiono il log di traceback
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


# ---- proxy: e' vivo? ----
_alive = {}            # url del proxy -> (quando, vivo)
_alive_lock = threading.Lock()
_last_state = {}       # url -> ultimo stato scritto nel log


def proxy_alive(url):
    """True se si riesce ad aprire una connessione TCP al proxy (controllo
    ripetuto al massimo ogni 15 secondi)."""
    if not url:
        return False
    now = time.time()
    with _alive_lock:
        hit = _alive.get(url)
        if hit and now - hit[0] < 15:
            return hit[1]
    u = urllib.parse.urlsplit(url)
    port = u.port or {"http": 80, "https": 443}.get(u.scheme, 1080)
    try:
        socket.create_connection((u.hostname, port), timeout=2).close()
        ok = True
    except OSError:
        ok = False
    with _alive_lock:
        _alive[url] = (now, ok)
        changed = _last_state.get(url) != ok
        _last_state[url] = ok
    if changed:
        if ok:
            print("proxy verso YouTube attivo:", u.scheme + "://" + (u.hostname or "") + ":" + str(port), flush=True)
        else:
            print("proxy verso YouTube NON raggiungibile (tunnel di casa spento?): uso l'IP del server", flush=True)
    return ok


def active_proxy(url):
    """Il proxy da usare adesso: quello scelto se e' vivo, altrimenti nessuno."""
    return url if url and proxy_alive(url) else ""


# ---- aprire un URL attraverso il proxy ----
def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise OSError("proxy SOCKS: connessione chiusa")
        buf += chunk
    return buf


def socks5_connect(proxy_host, proxy_port, host, port, timeout):
    """Connessione TCP a host:port attraverso un proxy SOCKS5 senza password.
    Il nome viene risolto dal proxy (come socks5h), cosi' anche il DNS passa da li'."""
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    try:
        s.sendall(b"\x05\x01\x00")
        if _recv_exact(s, 2) != b"\x05\x00":
            raise OSError("proxy SOCKS: serve un metodo di accesso non supportato")
        name = host.encode("idna")
        s.sendall(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + struct.pack(">H", port))
        head = _recv_exact(s, 4)
        if head[1] != 0:
            raise OSError("proxy SOCKS: connessione rifiutata (codice %d)" % head[1])
        skip = {1: 4, 4: 16}.get(head[3])
        if skip is None:
            skip = _recv_exact(s, 1)[0]
        _recv_exact(s, skip + 2)
        return s
    except Exception:
        s.close()
        raise


class _SocksHTTP(http.client.HTTPConnection):
    def __init__(self, host, port=None, proxy=None, **kw):
        super().__init__(host, port, **kw)
        self._proxy = proxy

    def connect(self):
        self.sock = socks5_connect(self._proxy[0], self._proxy[1], self.host, self.port, self.timeout)


class _SocksHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, port=None, proxy=None, **kw):
        super().__init__(host, port, context=ssl.create_default_context(), **kw)
        self._proxy = proxy

    def connect(self):
        sock = socks5_connect(self._proxy[0], self._proxy[1], self.host, self.port, self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def open_url(url, headers=None, timeout=20, proxy=""):
    """Come urllib.request.urlopen, ma attraverso `proxy` (http://, https://,
    socks5:// o socks5h://; vuoto = diretto). Solleva urllib.error.HTTPError
    per le risposte >= 400. La risposta ha .status, .headers, .read(), .close()."""
    headers = dict(headers or {})
    scheme = proxy.split("://", 1)[0].lower() if proxy else ""
    if scheme in ("", "http", "https"):
        handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
        return urllib.request.build_opener(*handlers).open(urllib.request.Request(url, headers=headers), timeout=timeout)
    if scheme not in ("socks5", "socks5h"):
        raise OSError("proxy %s non supportato per lo stream: usa socks5:// o http://" % scheme)
    p = urllib.parse.urlsplit(proxy)
    px = (p.hostname, p.port or 1080)
    for _ in range(5):  # segue i redirect, come urllib
        u = urllib.parse.urlsplit(url)
        cls = _SocksHTTPS if u.scheme == "https" else _SocksHTTP
        conn = cls(u.hostname, u.port, proxy=px, timeout=timeout)
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        conn.request("GET", path, headers=headers)
        r = conn.getresponse()
        if r.status in (301, 302, 303, 307, 308) and r.getheader("Location"):
            url = urllib.parse.urljoin(url, r.getheader("Location"))
            r.close()
            conn.close()
            continue
        if r.status >= 400:
            body = r.read(2000)
            conn.close()
            import io
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, io.BytesIO(body))
        return r
    raise OSError("troppi redirect")

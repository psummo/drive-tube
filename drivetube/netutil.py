"""Piccoli strumenti HTTP condivisi: server che non riempie il log, client disconnesso."""
import select
import socket
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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

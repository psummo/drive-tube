"""Accesso: codici, password admin, scelte salvate in access.json (cookie YouTube, feed, rete)."""
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from .config import COOKIES
from .context import _bprint, _tls
from .settings import cookie_value


def cookie_mode():
    """'off', 'admin' o 'all'. Mai impostato: 'all' se c'e' il file (come prima)."""
    m = ACCESS.get("cookies")
    if m in ("off", "admin", "all"):
        return m
    return "all" if os.path.isfile(COOKIES) else "off"


def home_feed():
    """'personal' o 'related'. Con personal (default) mostra iscrizioni/watch later/mi piace."""
    m = ACCESS.get("home_feed")
    return m if m in ("personal", "related") else "personal"


def use_cookies():
    # vale per la sessione legata al thread; senza sessione (richieste admin,
    # prova PO token) niente cookie
    if not os.path.isfile(COOKIES):
        return False
    m = cookie_mode()
    s = getattr(_tls, "s", None)
    if s is None:
        return False
    return m == "all" or (m == "admin" and s.who == "admin")


# ================= accesso =================
# Si entra solo con un codice valido (creati dalla pagina /admin) oppure come
# admin. La password admin sta nella variabile d'ambiente YT_ADMIN del servizio.
ACCESS_DIR = os.path.expanduser("~/.config/drive-tube")


ACCESS_FILE = os.path.join(ACCESS_DIR, "access.json")


ACCESS_LOCK = threading.Lock()


ADMIN_PASS = os.environ.get("YT_ADMIN", "")


CODE_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"   # niente 0/o, 1/l/i


LOGIN_FAILS = {}


def access_load():
    try:
        with open(ACCESS_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("codes"), list):
            return d
    except FileNotFoundError:
        pass
    except Exception as e:
        _bprint("access.json illeggibile:", e, flush=True)
    return {"codes": []}


def access_save():
    os.makedirs(ACCESS_DIR, mode=0o700, exist_ok=True)
    tmp = ACCESS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(ACCESS, f, indent=1)
    os.replace(tmp, ACCESS_FILE)


ACCESS = access_load()


def new_code():
    return "".join(secrets.choice(CODE_CHARS) for _ in range(10))


def code_label(code):
    if not code:
        return None
    for c in ACCESS["codes"]:
        if c.get("enabled", True) and hmac.compare_digest(str(c.get("code", "")), code):
            return c.get("label") or c["code"][:4]
    return None


def code_label_any(label):
    return any(c.get("enabled", True) and (c.get("label") or c["code"][:4]) == label for c in ACCESS["codes"])


def admin_token():
    if not ADMIN_PASS:
        return ""
    return hmac.new(ADMIN_PASS.encode(), b"drive-tube-admin", hashlib.sha256).hexdigest()


def is_admin(header):
    t = cookie_value(header, "dt_admin")
    return bool(ADMIN_PASS and t) and hmac.compare_digest(t, admin_token())


def who_is(header):
    """Chi fa la richiesta: 'admin', l'etichetta del codice, oppure None."""
    if is_admin(header):
        return "admin"
    return code_label(cookie_value(header, "dt_code"))


def login_blocked(ip):
    now = time.time()
    times = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 600]
    LOGIN_FAILS[ip] = times
    return len(times) >= 8


def login_failed(ip):
    LOGIN_FAILS.setdefault(ip, []).append(time.time())

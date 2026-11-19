"""Impostazioni del player e ricerche recenti, salvate nei cookie del browser."""
import json
from urllib.parse import parse_qs, quote, unquote, urlparse
from .config import DEFAULTS, SETTINGS
from .context import cur, print


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
    """Applica alla sessione corrente le impostazioni del cookie. True se sono cambiate."""
    s = cur()
    new = settings_from_cookie(header)
    if new == s.cfg:
        return False
    old, s.cfg = s.cfg, new
    print("impostazioni:", new, flush=True)
    # le impostazioni solo della pagina (diagnostica, doppio tocco) non chiedono
    # di riaprire lo stream
    ui_only = ("DIAG", "SKIP")
    return any(new.get(k) != (old or {}).get(k) for k in new if k not in ui_only)


def C(key):
    """Impostazione della sessione corrente."""
    return cur().cfg[key]


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


def watched_from_cookie(header):
    """Ultimi video YouTube guardati da questo browser (solo id), il piu' recente per primo."""
    rows = (cookie_value(header, "yt_seen") or "").split(".")
    return [x for x in rows if len(x) == 11][:30]


def push_watched(vid, rows):
    if not vid or len(vid) != 11:
        return rows
    return ([vid] + [x for x in rows if x != vid])[:30]


def watched_cookie_header(rows):
    return "yt_seen=%s; Max-Age=31536000; Path=/; SameSite=Lax; HttpOnly" % ".".join(rows)


def recent_cookie_header(rows):
    return "yt_recent=%s; Max-Age=31536000; Path=/; SameSite=Lax" % quote(json.dumps(rows, ensure_ascii=False), safe="")

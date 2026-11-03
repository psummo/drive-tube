"""Pagine web (frontend): HTML, CSS e JavaScript stanno in drivetube/web/, qui si caricano."""
import os


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")


def page(name):
    """Testo di una pagina in drivetube/web/ (letta una volta all'avvio)."""
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as f:
        return f.read()


PLAYER_HTML = page("player.html")


WASMTEST_HTML = page("wasmtest.html")


_h264_wasm_gz = None


def h264_wasm_gz():
    """Il decoder H.264 (WebAssembly, da wasm/build.sh), compresso per il browser."""
    global _h264_wasm_gz
    if _h264_wasm_gz is None:
        import gzip
        with open(os.path.join(WEB_DIR, "h264.wasm"), "rb") as f:
            _h264_wasm_gz = gzip.compress(f.read(), 6)
    return _h264_wasm_gz


TEST_HTML = page("test.html")
KEYTEST_HTML = page("keytest.html")
FRAMETEST_HTML = page("frametest.html")


ADMIN_HTML = page("admin.html")


LOGIN_HTML = page("login.html")


# pagine riempite dal server con l'operatore %: i "%%" sono "%" letterali
HOME_HTML = page("home.html").rstrip("\n")
SEARCH_HTML = page("search.html").rstrip("\n")
CHANNEL_HTML = page("channel.html").rstrip("\n")
LANDING_HTML = page("landing.html")
LIVE_HTML = page("live.html")
STREAMERS_HTML = page("streamers.html")
RAIPLAY_HTML = page("raiplay.html")

# barra in alto delle pagine di scelta (home, TV, YouTube, Twitch)
NAV_CSS = (".nav{display:flex;gap:6px;align-items:center;padding:10px 12px;background:#000;position:sticky;top:0;z-index:5;overflow-x:auto}"
           ".nav a,.nav a:visited{color:#ddd;text-decoration:none;font-size:18px;padding:9px 14px;border-radius:10px;white-space:nowrap}"
           ".nav a.on{background:#fff;color:#000}.nav .sp{flex:1}.nav a.sm{font-size:15px;color:#aaa}")


def nav(active, admin=False):
    items = [("home", "/home", "&#8962; Home"), ("live", "/live", "TV"), ("yt", "/youtube", "YouTube"),
             ("rai", "/raiplay", "RaiPlay"), ("tw", "/twitch", "Twitch"), ("k", "/kick", "Kick")]
    out = ['<nav class="nav">']
    for k, href, label in items:
        out.append('<a href="%s"%s>%s</a>' % (href, ' class="on"' if k == active else "", label))
    out.append('<span class="sp"></span>')
    if admin:
        out.append('<a class="sm" href="/admin">admin</a>')
    out.append('<a class="sm" href="/logout">esci</a></nav>')
    return "".join(out)


def with_nav(tpl, active, admin=False):
    return tpl.replace("__NAVCSS__", NAV_CSS).replace("__NAV__", nav(active, admin))


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


ADMIN_HTML = page("admin.html")


LOGIN_HTML = page("login.html")


# pagine riempite dal server con l'operatore %: i "%%" sono "%" letterali
HOME_HTML = page("home.html").rstrip("\n")
SEARCH_HTML = page("search.html").rstrip("\n")
CHANNEL_HTML = page("channel.html").rstrip("\n")

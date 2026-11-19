"""Pagine web (frontend): HTML, CSS e JavaScript stanno in drivetube/web/, qui si caricano."""
import os
from .recent import ROW_CSS


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

# Indicatore di caricamento al cambio pagina. Le pagine sono fatte dal server e
# alcune (Twitch/Kick "in diretta ora", YouTube, RaiPlay) ci mettono qualche
# secondo: senza un segno visibile non si capisce se il tocco e' arrivato.
# Al tocco su un link o all'invio di un modulo: barra animata in alto, voce
# toccata evidenziata e, dopo un attimo, una rotella al centro. Si toglie da
# solo quando si torna indietro (pagina dalla cache del browser).
# Attenzione: niente simboli di percentuale qui, alcune pagine passano dall'operatore %.
LOADER_CSS = ("#dtload{position:fixed;left:0;right:0;top:0;bottom:0;z-index:2147483000;pointer-events:none;display:none}"
              "#dtload.on{display:block}"
              "#dtload i{position:absolute;left:0;top:0;height:5px;width:40vw;background:#3ea6ff;"
              "box-shadow:0 0 8px #3ea6ff;animation:dtbar 1.1s linear infinite}"
              "#dtload b{position:absolute;left:50vw;top:50vh;width:64px;height:64px;margin:-32px 0 0 -32px;"
              "border:7px solid rgba(255,255,255,.25);border-top-color:#fff;border-radius:50vw;box-shadow:0 0 0 16px rgba(0,0,0,.6);"
              "opacity:0;animation:dtspin .8s linear infinite,dtshow .2s .35s forwards}"
              ".dtbusy{opacity:.55;outline:3px solid #3ea6ff;outline-offset:-3px}"
              "@keyframes dtbar{from{transform:translateX(-40vw)}to{transform:translateX(100vw)}}"
              "@keyframes dtspin{to{transform:rotate(360deg)}}"
              "@keyframes dtshow{to{opacity:1}}")

# Il loader resta acceso anche mentre la pagina aspetta le sue richieste
# (fetch/XMLHttpRequest): per esempio /youtube che chiede i suggerimenti a
# /home/more, o Twitch/Kick che chiedono chi e' in diretta. Lo fa solo nelle
# pagine con la barra in alto: il player ha richieste continue (stream, /ctrl).
# Una richiesta che non deve accenderlo (es. un controllo periodico) passa
# {dtQuiet:true} tra le opzioni di fetch.
def _loader_body(track):
    return ('<div id="dtload"><i></i><b></b></div><script>(function(){'
            'var L=document.getElementById("dtload"),busy=null,going=false,pend=0,tmo=0;'
            'function draw(){var v=going||pend>0;if((L.className=="on")!=v)L.className=v?"on":""}'
            'function unmark(){if(busy){busy.classList.remove("dtbusy");busy=null}}'
            'function off(){going=false;unmark();clearTimeout(tmo);draw()}'
            'function on(el){going=true;unmark();if(el&&el.classList){busy=el;el.classList.add("dtbusy")}'
            'clearTimeout(tmo);tmo=setTimeout(off,60000);draw()}'
            'window.dtLoading=on;'
            + ('function done(){pend=Math.max(0,pend-1);draw()}'
               'if(window.fetch){var F=window.fetch;window.fetch=function(u,o){'
               'if(o&&o.dtQuiet)return F.apply(this,arguments);'
               'pend++;draw();var r=F.apply(this,arguments);r.then(done,done);return r}}'
               'var XS=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.send=function(){'
               'pend++;draw();this.addEventListener("loadend",done);return XS.apply(this,arguments)};'
               if track else '') +
            'document.addEventListener("click",function(ev){'
            'if(ev.defaultPrevented||ev.button||ev.ctrlKey||ev.metaKey||ev.shiftKey||ev.altKey)return;'
            'var a=ev.target.closest&&ev.target.closest("a[href]");'
            'if(!a||(a.target&&a.target!="_self")||a.hasAttribute("download"))return;'
            'var h=a.getAttribute("href");if(!h||h.charAt(0)=="#"||/^javascript:/i.test(h))return;'
            'if(a.origin!==location.origin)return;'
            'if(a.pathname==location.pathname&&a.search==location.search&&a.hash)return;'
            'on(a)});'
            'document.addEventListener("submit",function(ev){'
            'var f=ev.target;if(ev.defaultPrevented||(f.target&&f.target!="_self"))return;'
            'on(f.querySelector("button,[type=submit]"))});'
            'window.addEventListener("pageshow",off);'
            '})();</script>')


LOADER_BODY = _loader_body(True)

LOADER_HTML = "<style>" + LOADER_CSS + "</style>" + _loader_body(False)

# barra in alto delle pagine di scelta (home, TV, YouTube, Twitch)
NAV_CSS = (".nav{display:flex;gap:6px;align-items:center;padding:10px 12px;background:#000;position:sticky;top:0;z-index:5;overflow-x:auto}"
           ".nav a,.nav a:visited{color:#ddd;text-decoration:none;font-size:18px;padding:9px 14px;border-radius:10px;white-space:nowrap}"
           ".nav a.on{background:#fff;color:#000}.nav .sp{flex:1}.nav a.sm{font-size:15px;color:#aaa}") + LOADER_CSS + ROW_CSS


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
    out.append(LOADER_BODY)
    return "".join(out)


def with_nav(tpl, active, admin=False):
    return tpl.replace("__NAVCSS__", NAV_CSS).replace("__NAV__", nav(active, admin))


# pagine senza la barra in alto: il caricamento si aggiunge in fondo
PLAYER_HTML += LOADER_HTML
SEARCH_HTML += LOADER_HTML
CHANNEL_HTML += LOADER_HTML

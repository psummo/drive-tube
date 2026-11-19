"""Righe "Continua a guardare" (video YouTube lasciati a meta') e "Ultimi visti"
(canali TV e dirette RaiPlay) in cima alle pagine.

Tutto sta nel browser, il server non scrive niente:
  cookie yt_cont        id~secondo~durata separati da "."  (id YouTube: niente "." ne' "~")
  cookie tv_seen        tv.<lista>.<canale> o rail.<canale> separati da "!"
  localStorage dt_names nome e copertina di ognuno, che il player conosce
Il server disegna le schede dai cookie; nomi e loghi li mette una riga di script
leggendo dt_names (nei cookie sarebbero troppo grandi: viaggiano a ogni richiesta)."""
import html
import re
from urllib.parse import quote
from .settings import cookie_value

CONT_MAX = 12
TV_MAX = 10

YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
TV_ID = re.compile(r"^(tv\.[A-Za-z0-9_-]+\.[0-9a-f]{8}|rail\.[a-z0-9]+)$")


def cont_from_cookie(header):
    """[(id, secondo, durata)] dal cookie yt_cont, il piu' recente per primo."""
    out, seen = [], set()
    for part in (cookie_value(header, "yt_cont") or "").split("."):
        bits = part.split("~")
        if len(bits) != 3 or not YT_ID.match(bits[0]) or bits[0] in seen:
            continue
        try:
            sec, dur = int(bits[1]), int(bits[2])
        except ValueError:
            continue
        seen.add(bits[0])
        out.append((bits[0], max(0, sec), max(0, dur)))
    return out[:CONT_MAX]


def cont_pos(header, vid):
    """Secondo a cui e' stato lasciato il video, o 0."""
    for v, sec, dur in cont_from_cookie(header):
        if v == vid:
            return sec
    return 0


def tv_from_cookie(header):
    out = []
    for w in (cookie_value(header, "tv_seen") or "").split("!"):
        if TV_ID.match(w) and w not in out:
            out.append(w)
    return out[:TV_MAX]


def _fmt(s):
    s = int(s)
    h, m = divmod(s // 60, 60)
    return "%d:%02d:%02d" % (h, m, s % 60) if h else "%d:%02d" % (m, s % 60)


# nomi e loghi da localStorage (li salva il player); senza, la scheda resta con la copertina
_NAMES_JS = ("<script>(function(){try{var m=JSON.parse(localStorage.getItem('dt_names')||'{}');"
             "var a=document.querySelectorAll('.rcard[data-w]');for(var i=0;i<a.length;i++){"
             "var x=m[a[i].getAttribute('data-w')];if(!x)continue;var s=a[i].querySelector('span');"
             "if(x.t&&s)s.textContent=x.t;if(x.p&&a[i].className.indexOf('rtv')!==-1&&!a[i].querySelector('img')){"
             "var r=a[i].querySelector('.rimg'),g=new Image();g.alt='';g.referrerPolicy='no-referrer';g.src=x.p;"
             "r.innerHTML='';r.appendChild(g);}}}catch(e){}})();</script>")


def _row(title, cards, cls=""):
    if not cards:
        return ""
    return ('<section class="rrow%s"><h2>%s</h2><div class="rscroll">%s</div></section>%s'
            % (cls, title, "".join(cards), _NAMES_JS))


def cont_html(header):
    cards = []
    for vid, sec, dur in cont_from_cookie(header):
        bar = ('<i style="width:%d%%"></i>' % max(2, min(100, sec * 100 // dur))) if dur else ""
        left = ("ancora " + _fmt(dur - sec)) if dur > sec else ("da " + _fmt(sec))
        cards.append('<a class="rcard" data-w="%s" href="/?watch=%s&amp;t=%d"><div class="rimg">'
                     '<img src="https://i.ytimg.com/vi/%s/mqdefault.jpg" loading="lazy" alt="">%s</div>'
                     '<span></span><small>%s</small></a>' % (vid, vid, sec, vid, bar, left))
    return _row("Continua a guardare", cards)


def tv_html(header):
    from .raiplay import LIVES
    rai = dict((k, n) for n, k in LIVES)
    cards = []
    for w in tv_from_cookie(header):
        name = rai.get(w[5:], "RaiPlay") if w.startswith("rail.") else ""
        cards.append('<a class="rcard rtv" data-w="%s" href="/watch?v=%s"><div class="rimg"><div class="ph">%s</div></div>'
                     '<span>%s</span></a>' % (w, quote(w), html.escape(name or "TV"), html.escape(name)))
    return _row("Ultimi visti", cards, " tvrow")


ROW_CSS = (".rrow{margin:4px 0 14px}.rrow h2{font-size:17px;margin:10px 0 8px}"
           ".rscroll{display:flex;gap:10px;overflow-x:auto;overflow-y:hidden;-webkit-overflow-scrolling:touch;"
           "scroll-snap-type:x proximity;padding-bottom:6px}"
           ".rcard,.rcard:visited{flex:0 0 220px;scroll-snap-align:start;color:#fff;text-decoration:none;"
           "background:#1c1c1c;border-radius:10px;overflow:hidden}"
           ".rimg{position:relative;aspect-ratio:16/9;background:#000}"
           ".rimg img{width:100%;height:100%;object-fit:cover;display:block}"
           ".rimg i{position:absolute;left:0;bottom:0;height:4px;background:#e11d48}"
           ".rcard span{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;"
           "font-size:15px;line-height:1.25;padding:6px 8px 0}"
           ".rcard small{display:block;color:#aaa;font-size:13px;padding:4px 8px 8px}"
           ".rcard.rtv{flex-basis:150px}.rcard.rtv .rimg img{object-fit:contain;padding:8px;box-sizing:border-box}"
           ".rcard.rtv .ph{height:100%;display:flex;align-items:center;justify-content:center;font-size:22px;"
           "font-weight:bold;color:#666;text-align:center}.rcard.rtv span{padding-bottom:8px;min-height:1.25em}")

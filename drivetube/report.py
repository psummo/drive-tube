"""Segnalazioni dal player: quando qualcosa non va, il browser manda il suo stato e
il server salva in un file di testo lo stato della sessione e le ultime righe di
log (gia' ripulite dai dati personali). Si leggono e scaricano dalla pagina admin."""
import json
import os
import re
import time
from collections import deque

from . import VERSION
from .context import LOG_RING, print
from .tools import ytdlp_status

REPORT_DIR = os.path.expanduser("~/.config/drive-tube/reports")
MAX_REPORTS = 50
_ID = re.compile(r"^\d{8}-\d{6}-[a-z0-9_-]{1,40}$")


def _clean(text, n=2000):
    return re.sub(r"[\x00-\x08\x0b-\x1f]", " ", str(text))[:n]


def save_report(s, note, client):
    """Salva una segnalazione della sessione `s`. Ritorna l'id."""
    os.makedirs(REPORT_DIR, exist_ok=True)
    who = re.sub(r"[^a-z0-9_-]", "", s.who.lower())[:40] or "utente"
    rid = time.strftime("%Y%m%d-%H%M%S-") + who
    st = s.state
    ff = st.get("ff")
    try:
        yt = ytdlp_status()
        ytv = "%s (ultima %s)%s" % (yt.get("installed") or "?", yt.get("latest") or "?",
                                   "  DA AGGIORNARE" if yt.get("newer") else "")
    except Exception as e:
        ytv = "non letto: %s" % e
    out = [
        "drive-tube - segnalazione",
        "quando: " + time.strftime("%Y-%m-%d %H:%M:%S"),
        "versione: " + VERSION,
        "yt-dlp: " + ytv,
        "sessione: " + s.tag,
        "nota: " + (_clean(note, 1000) or "-"),
        "",
        "== stato della sessione (server)",
        "formato: %s, posizione: %.1f s, in pausa: %s, durata: %s, live: %s" % (
            st.get("vmode"), float(st.get("seek_base") or 0), st.get("paused"), st.get("duration"), st.get("is_live")),
        "ffmpeg: %s, avvio n. %s, finito: %s, tentativi falliti: %s" % (
            "attivo" if ff and ff.poll() is None else ("fermo (codice %s)" % ff.returncode if ff else "mai partito"),
            st.get("gen"), st.get("ff_done") == st.get("gen"), st.get("fails")),
        "velocita' dal server: %.2f MB/s, coda video: %d, coda audio: %d" % (
            float(st.get("rate") or 0), len(st.get("v_q") or ()), len(st.get("a_q") or ())),
        "client YouTube di ripiego: %s" % ((st.get("net") or {}).get("client") or "-"),
        "errore: %s" % (st.get("err") or "-"),
        "",
        "== browser",
    ]
    for k in sorted((client or {}).keys()):
        if k == "log":
            continue
        out.append("%s: %s" % (_clean(k, 40), _clean(client[k], 400)))
    out += ["", "== ultime righe del player (browser)"]
    out += [_clean(x, 300) for x in ((client or {}).get("log") or [])[-150:]]
    out += ["", "== ultime righe di log di questa sessione"]
    out += list(getattr(s, "logs", ()))[-400:]
    out += ["", "== ultime righe di log del server (tutte le sessioni)"]
    out += list(LOG_RING)[-150:]
    path = os.path.join(REPORT_DIR, rid + ".txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    meta = {"id": rid, "t": time.time(), "who": s.who, "note": _clean(note, 200)}
    with open(path[:-4] + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f)
    # si tengono solo le piu' recenti
    for old in list_reports()[MAX_REPORTS:]:
        delete_report(old["id"])
    print("segnalazione salvata:", rid, flush=True)
    return rid


def list_reports():
    if not os.path.isdir(REPORT_DIR):
        return []
    out = []
    for fn in os.listdir(REPORT_DIR):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(REPORT_DIR, fn), encoding="utf-8") as f:
                out.append(json.load(f))
        except Exception:
            continue
    return sorted(out, key=lambda m: -m.get("t", 0))


def read_report(rid):
    if not _ID.match(rid or ""):
        return None
    try:
        with open(os.path.join(REPORT_DIR, rid + ".txt"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def delete_report(rid):
    if not _ID.match(rid or ""):
        return False
    gone = False
    for ext in (".txt", ".json"):
        try:
            os.remove(os.path.join(REPORT_DIR, rid + ext))
            gone = True
        except OSError:
            pass
    return gone

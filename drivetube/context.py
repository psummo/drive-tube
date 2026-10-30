"""Sessioni per browser e contesto per thread: ogni richiesta lavora sulla sua sessione
(state, lock, cur()), i thread avviati con sthread() la ereditano, e il log toglie i dati
personali e porta l'etichetta della sessione."""
import builtins
import os
import re
import threading
import time
from collections import deque
from .config import DEFAULTS, PORT, TMP_DIR


def in_session(fn):
    """La funzione, eseguita in un altro thread (es. ThreadPoolExecutor), lavora
    sulla sessione di chi la prepara."""
    sess = getattr(_tls, "s", None)

    def run(*a, **k):
        _tls.s = sess
        try:
            return fn(*a, **k)
        finally:
            _tls.s = None
    return run


# ================= sessioni =================
# Ogni browser ha la sua sessione: video, impostazioni, ffmpeg, code e file di
# scambio. Ogni richiesta HTTP lega la sua sessione al thread che la serve, e
# i thread avviati con sthread() ereditano la sessione di chi li avvia: cosi'
# state[...], lock e cur() si riferiscono sempre alla sessione giusta.
_tls = threading.local()


_bprint = builtins.print


# Privacy: nel log non finisce mai cosa guarda o cerca qualcuno. Indirizzi,
# id dei video, ricerche e titoli vengono tolti prima di scrivere.
_PRIV = [
    (re.compile(r"(?:https?://|www\.)\S+"), "<url>"),
    (re.compile(r"\b(?:youtube\.com|youtu\.be|googlevideo\.com)/\S*"), "<url>"),
    (re.compile(r"(\[youtube[^\]]*\]) [^:\s]+:"), r"\1 <video>:"),
    (re.compile(r"\bytsearch\d*:.*"), "ytsearch:<ricerca>"),
    (re.compile(r"\b(v|watch|list|q|search_query)=[^\s&]+"), r"\1=<...>"),
]


def privacy(text):
    for rx, sub in _PRIV:
        text = rx.sub(sub, text)
    return text


# ultime righe di log in memoria (gia' ripulite dai dati personali), per le
# segnalazioni dal player: tutte le sessioni insieme e, in Session.logs, ognuna
LOG_RING = deque(maxlen=3000)


def print(*a, **k):
    # nel log ogni riga di una sessione porta il suo nome (codice, non il video)
    s = getattr(_tls, "s", None)
    line = privacy(" ".join(str(x) for x in a))
    if s is not None:
        line = "[%s] %s" % (s.tag, line)
        s.logs.append(time.strftime("%H:%M:%S ") + line)
    LOG_RING.append(time.strftime("%H:%M:%S ") + line)
    k.pop("sep", None)
    _bprint(line, **k)


def new_state():
    return {
        "seek_base": 0.0,
        "t_play": None,
        "paused": False,
        "gen": 0,
        "ff": None,
        "has_audio": False,
        "duration": 0,
        "v_q": deque(),
        "a_q": deque(),
        "v_evt": threading.Event(),
        "a_evt": threading.Event(),
        "ff_gen": -1,
        "ff_done": -1,
        "vmode": "mjpeg",
        "v_maxpts": None,
        "v_made": 0,
        "a_read": 0,
        "fails": 0,
        "rate": 0,
        "is_live": False,
        "title": "",
        "net": None,  # client/rete che ha funzionato dopo un 403 (solo questa sessione)
    }


class Session:
    def __init__(self, sid, who):
        self.sid = sid
        self.who = who
        self.tag = "%s:%s" % (who, sid[:4])
        self.lock = threading.Lock()
        self.url = ""
        self.cfg = dict(DEFAULTS)
        self.created = self.seen = time.time()
        base = os.path.join(TMP_DIR, "yt_%d_%s" % (PORT, sid[:16]))
        self.fifo_v = base + "_v.fifo"
        self.fifo_a = base + "_a.fifo"
        self.state = new_state()
        self.logs = deque(maxlen=600)


SESSIONS = {}


SESSIONS_LOCK = threading.Lock()


OWNER = {}            # codice -> sid del browser che lo sta usando


MAX_PER_CODE = 1      # un codice = una sessione: chi lo apre altrove chiude questa


MAX_SESSIONS = 10     # sessioni in tutto


IDLE_CLOSE = 30 * 60  # una sessione ferma da 30 minuti (senza ffmpeg attivo) viene chiusa


def cur():
    s = getattr(_tls, "s", None)
    if s is None:
        raise RuntimeError("nessuna sessione legata a questo thread")
    return s


def bind(s):
    _tls.s = s


def sthread(target, *args):
    """Thread che lavora sulla stessa sessione di chi lo avvia."""
    s = getattr(_tls, "s", None)

    def run():
        _tls.s = s
        target(*args)
    return threading.Thread(target=run, daemon=True)


class _StateProxy:
    def __getitem__(self, k):
        return cur().state[k]

    def __setitem__(self, k, v):
        cur().state[k] = v

    def get(self, k, d=None):
        return cur().state.get(k, d)

    def __contains__(self, k):
        return k in cur().state


class _LockProxy:
    def acquire(self, *a, **k):
        return cur().lock.acquire(*a, **k)

    def release(self):
        return cur().lock.release()

    def __enter__(self):
        cur().lock.acquire()
        return self

    def __exit__(self, *a):
        cur().lock.release()


state = _StateProxy()


lock = _LockProxy()

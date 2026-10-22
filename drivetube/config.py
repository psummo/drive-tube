"""Configurazione: versione, porte, percorsi, user agent e impostazioni predefinite del player."""
import os


from . import VERSION  # una sola fonte: la riga VERSION di yt_mjpeg.py


UPDATE_URL = os.environ.get("YT_UPDATE_URL") or "https://raw.githubusercontent.com/psummo/drive-tube/main/yt_mjpeg.py"


PORT = int(os.environ.get("YT_PORT") or 8090)


# Impostazioni modificabili dal pannello dell'app. I default stanno qui nel codice;
# le scelte dell'utente vivono nel cookie "yt_cfg" del browser e, se manca o e'
# incompleto, si usano i default.
# (chiave, nome, spiegazione, default, [(valore, etichetta), ...])
SETTINGS = [
    ("VMODE", "Formato video", "H.264: il video arriva com'e' da YouTube e lo decodifica il browser, "
     "molti meno dati e fluido (consigliato). MJPEG: il server trasforma il video in immagini, "
     "piu' pesante per la rete; si usa da solo se H.264 non e' disponibile.", 1,
     [(1, "H.264"), (0, "MJPEG")]),
    ("HEIGHT", "Risoluzione", "Altezza del video. Piu' alta e' piu' nitida ma pesa di piu'. "
     "In H.264 al massimo 720p.", 720,
     [(360, "360p"), (480, "480p"), (720, "720p"), (1080, "1080p")]),
    ("FPS", "Fluidita'", "Fotogrammi al secondo (solo MJPEG: in H.264 si usano quelli del video).", 15,
     [(10, "10"), (15, "15"), (20, "20"), (25, "25"), (30, "30")]),
    ("Q", "Qualita' video", "Compressione JPEG, solo MJPEG: piu' e' alta, piu' dati servono.", 10,
     [(15, "Minima"), (10, "Bassa"), (6, "Media"), (3, "Alta")]),
    ("AUDIO_KBPS", "Qualita' audio", "Bitrate dell'MP3.", 64,
     [(32, "32k"), (48, "48k"), (64, "64k"), (96, "96k"), (128, "128k"), (192, "192k")]),
    ("AUDIO_BUFFER_SEC", "Buffer audio", "Secondi di audio caricati prima di partire.", 4,
     [(2, "2 s"), (4, "4 s"), (8, "8 s"), (15, "15 s")]),
]


SETTINGS_UI = [
    {"key": k, "label": n, "help": h, "options": [[v, l] for v, l in opts]}
    for k, n, h, d, opts in SETTINGS
]


DEFAULTS = {k: d for k, n, h, d, opts in SETTINGS}


feed_cache = {}


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


HEADERS = "User-Agent: " + UA + "\r\nReferer: https://www.youtube.com/\r\n"


# cookie YouTube facoltativi: dalla pagina admin si sceglie per chi usarli
COOKIES = os.path.expanduser("~/cookies.txt")


YTDLP_LOCAL = os.path.expanduser("~/.local/bin/yt-dlp")


YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp"


YTDLP_API = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"

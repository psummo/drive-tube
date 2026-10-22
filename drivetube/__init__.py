"""drive-tube: YouTube nel browser dell'auto senza <video> ne' <audio>.

Pacchetto del server. Il punto di avvio resta yt_mjpeg.py (servizio systemd
invariato), che tiene anche il numero di versione.
"""
import os
import re


def _read_version():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "yt_mjpeg.py")
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else "?"
    except OSError:
        return "?"


VERSION = _read_version()
APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

"""Aggiornamento dalla pagina admin: scarica l'ultima versione da GitHub e riavvia."""
import os
import re
import shutil
import sys
import time
import urllib.error
from . import APP_DIR
from .config import UPDATE_URL, VERSION
from .context import print
from .tools import _ver_key
from .player import stop_all


def fetch_remote_source():
    import urllib.request
    req = urllib.request.Request(UPDATE_URL + "?t=%d" % int(time.time()), headers={"User-Agent": "drive-tube"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode("utf-8")


def remote_version(src):
    import re
    m = re.search(r'^VERSION\s*=\s*"([^"]+)"', src, re.M)
    return m.group(1) if m else ""


def check_update():
    try:
        remote = remote_version(fetch_remote_source())
    except Exception as e:
        return {"current": VERSION, "error": "controllo fallito: %s" % e}
    if not remote:
        return {"current": VERSION, "error": "versione remota non trovata"}
    return {"current": VERSION, "latest": remote, "newer": _ver_key(remote) > _ver_key(VERSION)}


# archivio dell'ultima versione (si puo' cambiare per le prove)
ARCHIVE_URL = os.environ.get("YT_UPDATE_ARCHIVE") or "https://codeload.github.com/psummo/drive-tube/tar.gz/refs/heads/main"
BACKUP_DIR = os.path.join(APP_DIR, ".backup")


def install_update():
    """Scarica l'archivio dell'ultima versione, ne prende yt_mjpeg.py e drivetube/,
    controlla la sintassi di tutti i file, mette la versione attuale in .backup/
    e la sostituisce. Il riavvio lo fa restart_self()."""
    import io
    import tarfile
    import tempfile
    import urllib.request
    req = urllib.request.Request(ARCHIVE_URL, headers={"User-Agent": "drive-tube"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    tmp = tempfile.mkdtemp(prefix=".update-", dir=APP_DIR)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for m in tar.getmembers():
                parts = m.name.split("/", 1)
                if len(parts) < 2 or not (m.isfile() or m.isdir()):
                    continue
                rel = os.path.normpath(parts[1])
                if rel.startswith("..") or os.path.isabs(rel):
                    continue
                if rel != "yt_mjpeg.py" and rel != "drivetube" and not rel.startswith("drivetube" + os.sep):
                    continue
                dest = os.path.join(tmp, rel)
                if m.isdir():
                    os.makedirs(dest, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "wb") as f:
                    f.write(tar.extractfile(m).read())
        main = os.path.join(tmp, "yt_mjpeg.py")
        if not os.path.isfile(main) or not os.path.isfile(os.path.join(tmp, "drivetube", "web", "player.html")):
            raise RuntimeError("archivio incompleto")
        with open(main, encoding="utf-8") as f:
            remote = remote_version(f.read())
        if not remote:
            raise RuntimeError("versione remota non trovata")
        if _ver_key(remote) <= _ver_key(VERSION):
            raise RuntimeError("gia' aggiornato (%s)" % VERSION)
        for root, _, files in os.walk(tmp):
            for fn in files:
                if fn.endswith(".py"):
                    path = os.path.join(root, fn)
                    with open(path, encoding="utf-8") as f:
                        compile(f.read(), path, "exec")  # solo controllo della sintassi
        shutil.rmtree(BACKUP_DIR, ignore_errors=True)
        os.makedirs(BACKUP_DIR)
        shutil.copy2(os.path.join(APP_DIR, "yt_mjpeg.py"), os.path.join(BACKUP_DIR, "yt_mjpeg.py"))
        pkg = os.path.join(APP_DIR, "drivetube")
        if os.path.isdir(pkg):
            os.replace(pkg, os.path.join(BACKUP_DIR, "drivetube"))
        os.replace(os.path.join(tmp, "drivetube"), pkg)
        os.replace(main, os.path.join(APP_DIR, "yt_mjpeg.py"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("aggiornato %s -> %s, riavvio" % (VERSION, remote), flush=True)
    return remote


def restart_self():
    time.sleep(1.0)
    try:
        stop_all()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)

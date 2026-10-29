#!/usr/bin/env python3
# drive-tube - https://github.com/psummo/drive-tube - MIT License
# Solo per i passeggeri, mai per chi guida: ogni conseguenza dell'uso durante
# la guida e' responsabilita' esclusiva del conducente. Vedi README e LICENSE.
#
# Punto di avvio. Il codice sta nel pacchetto drivetube/ (backend) e le pagine
# in drivetube/web/ (frontend). Questa riga e' l'unica fonte della versione:
VERSION = "15.8"

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.environ.get("YT_UPDATE_ARCHIVE") or "https://codeload.github.com/psummo/drive-tube/tar.gz/refs/heads/main"


def bootstrap():
    """Se manca il pacchetto (aggiornamento dalla 14.x, che scaricava solo questo
    file) lo scarica da GitHub, una volta sola."""
    import io
    import tarfile
    import urllib.request
    print("drivetube/ mancante: lo scarico da GitHub", flush=True)
    req = urllib.request.Request(ARCHIVE, headers={"User-Agent": "drive-tube"})
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            parts = m.name.split("/", 1)
            if len(parts) < 2 or not parts[1].startswith("drivetube/"):
                continue
            rel = os.path.normpath(parts[1])
            if rel.startswith("..") or os.path.isabs(rel) or not (m.isfile() or m.isdir()):
                continue
            dest = os.path.join(HERE, rel)
            if m.isdir():
                os.makedirs(dest, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(tar.extractfile(m).read())


if __name__ == "__main__":
    if not os.path.isfile(os.path.join(HERE, "drivetube", "web", "player.html")):
        bootstrap()
    sys.path.insert(0, HERE)
    from drivetube.server import main
    main()

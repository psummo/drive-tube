# drive-tube
Player YouTube leggero per il browser dell'auto (MJPEG + Web Audio API).

Server Python single-file: yt-dlp + ffmpeg trasformano lo stream in MJPEG e MP3, il client li riproduce su canvas e AudioContext con l'audio come clock principale.

## Avvio
    python3 yt_mjpeg.py

Richiede `yt-dlp` e `ffmpeg`. Porta e qualità si configurano in `yt_mjpeg.conf`.

## Rilasci
Si pubblicano con un tag `vX.Y`: GitHub Actions crea la release con `yt_mjpeg.py` e `yt_mjpeg.conf`.

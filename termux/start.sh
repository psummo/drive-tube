#!/data/data/com.termux/files/usr/bin/bash
# Avvia drive-tube su Android (Termux): tiene sveglio il telefono, aggiorna da
# git, mostra l'indirizzo da aprire sulla Tesla e riparte se si ferma.
DIR="$HOME/drive-tube"
cd "$DIR" || exit 1
set -a; . "$HOME/.config/drive-tube/env"; set +a
command -v termux-wake-lock >/dev/null && termux-wake-lock
if pgrep -f "python.* yt_mjpeg[.]py" >/dev/null; then
  echo "drive-tube e' gia' in esecuzione."
else
  git pull --ff-only -q 2>/dev/null || true
  (
    while true; do
      python yt_mjpeg.py >> "$HOME/yt.log" 2>&1
      echo "$(date '+%H:%M:%S') drive-tube si e' fermato, riparto tra 5 secondi" >> "$HOME/yt.log"
      sleep 5
    done
  ) &
  sleep 4
fi
echo
echo "drive-tube e' attivo. Indirizzi (sulla Tesla apri quello dell'hotspot):"
python - <<PY
import sys; sys.path.insert(0, "$DIR")
from drivetube.netutil import local_ipv4, is_hotspot
rows = local_ipv4()
for name, ip in rows:
    print("  http://%s:${YT_PORT:-8090}/   (%s%s)" % (ip, name, "  <- HOTSPOT" if is_hotspot(name, ip) else ""))
if not any(is_hotspot(n, i) for n, i in rows):
    print("  (nessun hotspot trovato: accendilo e rilancia questo comando)")
PY
echo
echo "Log: $HOME/yt.log    Fermare: bash $DIR/termux/stop.sh"

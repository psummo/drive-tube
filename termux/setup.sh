#!/data/data/com.termux/files/usr/bin/bash
# drive-tube su Android, con Termux (da F-Droid, non dal Play Store).
#
#   curl -fsSL https://raw.githubusercontent.com/psummo/drive-tube/main/termux/setup.sh -o setup.sh
#   bash setup.sh
#
# Installa Python, ffmpeg, Node e yt-dlp, scarica drive-tube, chiede la password
# admin e prepara l'avvio automatico (con l'app Termux:Boot). Rilanciarlo
# aggiorna tutto.
set -e
DIR="$HOME/drive-tube"
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "Pacchetti (Python, ffmpeg, Node, git)"
pkg update -y
pkg install -y python ffmpeg nodejs git
# in alcune versioni di Termux pip e' un pacchetto a parte
python -m pip --version >/dev/null 2>&1 || pkg install -y python-pip
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libmp3lame || echo "ATTENZIONE: questo ffmpeg non ha libmp3lame (niente audio)" >&2

say "yt-dlp"
# "default" aggiunge yt-dlp-ejs, che risolve i controlli JavaScript di YouTube (con Node)
python -m pip install -U "yt-dlp[default]"

say "drive-tube"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only || echo "git pull non riuscito: uso la copia attuale"
else git clone https://github.com/psummo/drive-tube.git "$DIR"; fi

say "Password admin"
ENVF="$HOME/.config/drive-tube/env"
mkdir -p "$(dirname "$ENVF")"
if [ ! -s "$ENVF" ]; then
  read -r -s -p "Scegli una password admin (resta solo sul telefono): " PW; echo
  [ -n "$PW" ] || { echo "Password vuota." >&2; exit 1; }
  ( umask 077; printf "YT_ADMIN=%s\nYT_PORT=8090\n" "$PW" > "$ENVF" )
else
  echo "Tengo $ENVF"
fi

say "Avvio automatico"
mkdir -p "$HOME/.termux/boot" "$HOME/.shortcuts"
cat > "$HOME/.termux/boot/drive-tube" <<BOOT
#!/data/data/com.termux/files/usr/bin/sh
nohup bash "$DIR/termux/start.sh" > /dev/null 2>&1 &
BOOT
cp "$HOME/.termux/boot/drive-tube" "$HOME/.shortcuts/drive-tube"
chmod +x "$HOME/.termux/boot/drive-tube" "$HOME/.shortcuts/drive-tube"

say "Fatto"
cat <<TXT
Avvia ora con:   bash $DIR/termux/start.sh

Da fare una volta sul telefono:
 - Impostazioni > App > Termux > Batteria: "Senza restrizioni" (altrimenti Android lo chiude)
 - per farlo partire da solo all'accensione installa anche l'app Termux:Boot (F-Droid) e aprila una volta
 - per un pulsante sulla schermata home: app Termux:Widget, voce "drive-tube"
Poi accendi l'hotspot, collega la Tesla e apri sul suo browser l'indirizzo "hotspot" che compare all'avvio.
TXT

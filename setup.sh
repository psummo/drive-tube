#!/usr/bin/env bash
# drive-tube: easy standalone setup (Debian/Ubuntu, Fedora/RHEL/Amazon Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/psummo/drive-tube/main/setup.sh -o setup.sh
#   bash setup.sh
#
# Installs python3, git, ffmpeg, yt-dlp, Node.js 22 (user-local, no sudo) and Deno,
# clones the repo, asks for the admin password and creates the systemd service.
# Safe to run again: it updates what is already there.
# Optional env vars: YT_DIR (install folder), YT_PORT (default 8090).
set -euo pipefail

DIR="${YT_DIR:-$HOME/drive-tube}"
PORT="${YT_PORT:-8090}"
REPO="https://github.com/psummo/drive-tube.git"
NODE_LOCAL="$HOME/.local/share/drive-tube/node"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
say() { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }

say "System packages"
if command -v apt-get >/dev/null; then
  $SUDO apt-get update -y
  $SUDO apt-get install -y python3 git ffmpeg curl ca-certificates xz-utils unzip
elif command -v dnf >/dev/null; then
  # Amazon Linux / Fedora: ffmpeg may be missing from the default repos (see below)
  $SUDO dnf install -y python3 git curl ca-certificates xz unzip
  command -v ffmpeg >/dev/null || $SUDO dnf install -y ffmpeg-free || $SUDO dnf install -y ffmpeg || true
else
  echo "Unsupported distribution: install python3 git ffmpeg curl by hand, then re-run." >&2; exit 1
fi
if ! command -v ffmpeg >/dev/null; then
  say "ffmpeg not in the repos: installing a static build"
  case "$(uname -m)" in aarch64|arm64) A=arm64 ;; *) A=amd64 ;; esac
  curl -fsSL "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-${A}-static.tar.xz" | tar -xJ -C /tmp
  $SUDO install -m 755 /tmp/ffmpeg-*-static/ffmpeg /usr/local/bin/ffmpeg
  $SUDO install -m 755 /tmp/ffmpeg-*-static/ffprobe /usr/local/bin/ffprobe
fi
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libmp3lame || echo "WARNING: this ffmpeg has no libmp3lame (audio will not work)" >&2

say "yt-dlp"
case "$(uname -m)" in
  aarch64|arm64) YTF=yt-dlp_linux_aarch64 ;;
  *) YTF=yt-dlp_linux ;;
esac
$SUDO curl -fsSL "https://github.com/yt-dlp/yt-dlp/releases/latest/download/$YTF" -o /usr/local/bin/yt-dlp
$SUDO chmod a+rx /usr/local/bin/yt-dlp
yt-dlp --version

say "Node.js 22+ (needed by yt-dlp and the PO token provider)"
NODE_OK=0
if command -v node >/dev/null && [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 22 ]; then NODE_OK=1; fi
if [ "$NODE_OK" -eq 0 ] && [ ! -x "$NODE_LOCAL/bin/node" ]; then
  case "$(uname -m)" in aarch64|arm64) N=arm64 ;; *) N=x64 ;; esac
  BASE="https://nodejs.org/dist/latest-v22.x"
  FILE="$(curl -fsSL "$BASE/SHASUMS256.txt" | awk -v n="linux-$N.tar.xz" '$2 ~ n {print $2; exit}')"
  mkdir -p "$NODE_LOCAL"
  curl -fsSL "$BASE/$FILE" | tar -xJ -C "$NODE_LOCAL" --strip-components=1
fi
echo "node: $( ( command -v node >/dev/null && [ "$NODE_OK" -eq 1 ] && node --version ) || "$NODE_LOCAL/bin/node" --version )"

say "Deno (optional, yt-dlp uses it automatically when present)"
if ! command -v deno >/dev/null && [ ! -x "$HOME/.deno/bin/deno" ]; then
  curl -fsSL https://deno.land/install.sh | sh -s -- -y >/dev/null 2>&1 || echo "Deno not installed (fine: Node is enough)."
fi
[ -x "$HOME/.deno/bin/deno" ] && $SUDO ln -sf "$HOME/.deno/bin/deno" /usr/local/bin/deno || true

say "drive-tube"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull --ff-only || echo "pull failed (history rewritten?): run  git -C $DIR fetch && git -C $DIR reset --hard origin/main"
else git clone "$REPO" "$DIR"; fi

say "Admin password"
ENVF="$HOME/.config/drive-tube/env"
mkdir -p "$(dirname "$ENVF")"
if [ ! -s "$ENVF" ]; then
  read -r -s -p "Choose an admin password (stays on this machine only): " PW; echo
  [ -n "$PW" ] || { echo "Empty password." >&2; exit 1; }
  ( umask 077; printf "YT_ADMIN=%s\nYT_PORT=%s\n" "$PW" "$PORT" > "$ENVF" )
else
  echo "Keeping the existing $ENVF"
fi

say "systemd service"
$SUDO tee /etc/systemd/system/drive-tube.service >/dev/null <<UNIT
[Unit]
Description=drive-tube
After=network.target

[Service]
User=$(whoami)
WorkingDirectory=$DIR
EnvironmentFile=$ENVF
ExecStartPre=-$(command -v git) -C $DIR pull --ff-only
ExecStart=$(command -v python3) yt_mjpeg.py
Restart=always
StandardOutput=append:$HOME/yt.log
StandardError=append:$HOME/yt.log

[Install]
WantedBy=multi-user.target
UNIT
$SUDO systemctl daemon-reload
$SUDO systemctl enable --now drive-tube
$SUDO systemctl restart drive-tube

say "Done"
echo "Open  http://THIS-SERVER:$PORT/admin  (open port $PORT in the firewall / security group),"
echo "log in with the password you chose, create an access code, and use the PO token button"
echo "in the admin page to install the PO token provider. Log: $HOME/yt.log"

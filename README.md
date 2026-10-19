# drive-tube
A lightweight YouTube player for in-car browsers (built for the Tesla browser), which block `<video>` and `<audio>` while driving.

drive-tube is a single Python file. `yt-dlp` finds the YouTube stream and `ffmpeg` prepares it for the browser, which plays it on a canvas and through the Web Audio API, with no `<video>` or `<audio>` tags. Audio is the master clock and the video follows it.

Two video formats, selectable in the settings:
- **H.264** (default): the video arrives from YouTube as-is, with no re-encoding, and the browser decodes it with FFmpeg's decoder compiled to WebAssembly and draws it with WebGL. It needs about 0.5–2 Mbit/s and almost no server CPU. It works over plain http. It requires a browser with WebAssembly SIMD (Chrome 91 or later; the Tesla browser has it).
- **MJPEG**: the server turns the video into JPEG frames. It needs far more bandwidth (tens of Mbit/s) but works everywhere. It is used automatically when a video has no H.264 format or the browser lacks WebAssembly SIMD.

In both modes the audio is MP3 decoded by the browser.

## Requirements
A Linux machine with:
- `python3` and `git`
- `ffmpeg` with `libmp3lame` (most builds include it)
- `yt-dlp`
- **Node.js 22 or later** (or Deno): yt-dlp needs a JavaScript runtime for YouTube, and the PO token provider (see below) needs Node 22+

## Installation
Install the dependencies with your distribution's package manager:

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y python3 git ffmpeg curl

# Fedora / RHEL and derivatives
sudo dnf install -y python3 git ffmpeg curl
```
If your distribution doesn't ship `ffmpeg`, download a static Linux build and copy the binary to `/usr/local/bin`.

The `nodejs` package on Debian/Ubuntu is often too old: install Node 22+ from [NodeSource](https://github.com/nodesource/distributions). If only Node is installed (no Deno), drive-tube enables it for yt-dlp automatically (`--js-runtimes node`).

Install `yt-dlp` (standalone binary, no extra Python packages needed):
```bash
sudo curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp -o /usr/local/bin/yt-dlp
sudo chmod a+rx /usr/local/bin/yt-dlp
```
Later you can update it from the admin page (see below).

Download and run:
```bash
git clone https://github.com/psummo/drive-tube.git
cd drive-tube
YT_ADMIN='a-long-admin-password' python3 yt_mjpeg.py
```
The server listens on port **8090**: open `http://YOUR-SERVER:8090/`. If there is a firewall, open port 8090 for inbound traffic. To use another port: `YT_PORT=9000`.

## First setup: admin password and access codes
Nobody can use drive-tube without an **access code**: without one, every page redirects to the login. Codes are created, disabled and deleted from the **`/admin`** page, which is protected by the admin password in the `YT_ADMIN` environment variable. The password never lives in the code or on GitHub.

1. Start the server with `YT_ADMIN` set (see the systemd section below for a permanent setup).
2. Open `/admin`, log in with the password and create a code for yourself and one for each person you share the server with. Leave the code field empty to get a random 10-character code.
3. Open the app and enter your code. The browser remembers it.

Without `YT_ADMIN` the admin page stays off and explains how to enable it; without at least one code nobody can log in.

## Run as a service (systemd)
From the `drive-tube` folder, these commands create a service that runs as your user from the current path:
```bash
sudo tee /etc/systemd/system/drive-tube.service >/dev/null <<UNIT
[Unit]
Description=drive-tube
After=network.target

[Service]
User=$(whoami)
WorkingDirectory=$PWD
ExecStartPre=-$(command -v git) -C $PWD pull --ff-only
ExecStart=$(command -v python3) yt_mjpeg.py
Restart=always
StandardOutput=append:$HOME/yt.log
StandardError=append:$HOME/yt.log

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload && sudo systemctl enable --now drive-tube
```
Then add the admin password:
```bash
sudo systemctl edit drive-tube
```
and in the editor write:
```
[Service]
Environment=YT_ADMIN=a-long-admin-password
```
then `sudo systemctl restart drive-tube`.

Special characters in the password: in the shell, use single quotes (`YT_ADMIN='my!pass?'`), because bash interprets `!` even inside double quotes (`event not found`). In the systemd file `!` and `?` are fine as they are, but `%` must be written as `%%`, and a password with spaces needs double quotes around the whole assignment: `Environment="YT_ADMIN=my pass!"`.

The `ExecStartPre` line pulls the latest version every time the service starts. The log goes to `~/yt.log` and can also be read from the admin page. To stop or restart: `sudo systemctl stop|restart drive-tube`.

## Access, sessions and the admin page
- Each code has its own session (video, settings and ffmpeg are separate) and works on **one screen at a time**: opening it on another screen stops the first one, which shows a "Guarda qui" (watch here) button to take it back. Two screens with different videos need two codes. At most 10 sessions in total.
- Disabling or deleting a code immediately closes the sessions using it.
- After 8 wrong attempts from the same address within 10 minutes, login is blocked for a few minutes.
- Codes are stored in `~/.config/drive-tube/access.json`, readable only by your user.
- From the admin page you can see and close active sessions, check and install updates, restart the server, manage YouTube cookies and the PO token, update yt-dlp and read the log. The player itself has no update or log controls.
- **yt-dlp** (*Server* section) shows the installed and the latest version; the button downloads the latest stable release to `~/.local/bin/yt-dlp`, which from then on takes precedence over the system one. The same row shows the ffmpeg and JavaScript runtime versions.

Over plain http, codes and the admin password travel unencrypted. If you share the server, use HTTPS (see below).

## YouTube: cookies or PO token
From cloud server IPs, YouTube often asks for an anti-bot check. There are two ways to pass it, and they can be combined.

### Your account cookies (optional)
Export your youtube.com cookies in Netscape format and upload them from the admin page (*Cookie YouTube* section). They are saved to `~/cookies.txt`, readable only by your user. **Usa i cookie per** (use cookies for) chooses who uses them: *nessuno* (nobody), *solo admin* (admin only) or *tutti* (everyone). Whoever uses them acts with your account, and their home page also shows your subscriptions, "Watch later" and liked videos. If the file exists and you never made a choice, everyone uses it, as in older versions. Without cookies the home page shows only searches.

### PO token, no account needed
The [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) plugin generates the "PO tokens" YouTube checks in real browsers. It has two parts:

1. **A local server** (port 4416, bound to `127.0.0.1`). With Node 22+, once:
   ```bash
   git clone --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git ~/bgutil-ytdlp-pot-provider
   cd ~/bgutil-ytdlp-pot-provider/server && npm ci && npx tsc
   ```
   drive-tube finds `~/bgutil-ytdlp-pot-provider/server/build/main.js`, starts the server and restarts it if it stops. Alternatively use Docker: `docker run --name bgutil-provider -d --init --restart unless-stopped -p 127.0.0.1:4416:4416 brainicism/bgutil-ytdlp-pot-provider` (drive-tube uses it if it answers). Check the releases page for a version newer than 2.0.2.
2. **The yt-dlp plugin**: in the admin page, *PO token* section, **Installa / aggiorna il plugin** (install / update) downloads it to `~/.config/yt-dlp/plugins/`.

Then **Prova con un video** (test with a video) asks YouTube for a short video and shows whether the request passes and whether a token was generated. If YouTube still refuses, the server IP may be blocked: a PO token does not get around IP blocks.

If you share the server, the safest setup is PO token for everyone and cookies for nobody or admin only.

## HTTPS (optional)
The app works over plain http; HTTPS protects access codes and the admin password on the network. The server enables it on a separate port (**8443**) when it finds a certificate in `~/yt-cert/fullchain.pem` and `~/yt-cert/privkey.pem`; port 8090 keeps working.

You need a domain name pointing to the machine (a free [DuckDNS](https://www.duckdns.org) name works). Get a free Let's Encrypt certificate with certbot:

1. Open inbound ports **80** (used by Let's Encrypt for verification) and **8443**. On AWS EC2: *Security group → Inbound rules*.
2. Install certbot:
   ```bash
   sudo apt install -y certbot   # Debian / Ubuntu
   sudo dnf install -y certbot   # Fedora / RHEL / Amazon Linux
   ```
3. From the `drive-tube` folder, set your domain and run:
   ```bash
   DOM=yourname.duckdns.org
   sudo certbot certonly --standalone -d $DOM --agree-tos --register-unsafely-without-email --non-interactive \
     --deploy-hook "install -D -m 600 -o $(whoami) /etc/letsencrypt/live/$DOM/fullchain.pem $HOME/yt-cert/fullchain.pem && install -D -m 600 -o $(whoami) /etc/letsencrypt/live/$DOM/privkey.pem $HOME/yt-cert/privkey.pem && systemctl restart drive-tube"
   ```
   The deploy hook copies the certificate where the server reads it and restarts the service. It runs again on every automatic renewal (about every 60 days), as long as port 80 stays open and the machine is on.
4. Open `https://yourname.duckdns.org:8443/`. The log shows `https://0.0.0.0:8443/`; if it says `https spento`, the certificate was not found.

Port and paths can be changed with `YT_HTTPS_PORT`, `YT_CERT` and `YT_KEY`.

## Player settings
The gear icon (top left) opens the settings: video format (H.264 or MJPEG), resolution, frame rate and JPEG quality (these two for MJPEG only), audio quality and audio buffer. In H.264 the resolution is capped at 720p. **Salva e applica** (save and apply) stores them in **browser cookies**: there is no configuration file on the server. Without cookies the defaults in the code apply (`SETTINGS` in `yt_mjpeg.py`). Recent searches are stored in a cookie too.

Tapping the video shows the suggestions, the home button and a pause button in the middle; they hide after 5 seconds. While paused they stay visible.

## Updates
- **From the admin page**, *Server* section: the update button compares the version with `main`; installing downloads the file, checks its syntax, keeps a backup in `yt_mjpeg.py.bak` and restarts the server.
- **By hand**: `cd drive-tube && git pull`, then restart the service. If you had installed from the admin page, run `git checkout yt_mjpeg.py` first.

## Releases
Releases are published with a `vX.Y` tag: GitHub Actions creates the release with `yt_mjpeg.py` attached.

## Test pages
Available after login:
- `/test`: which features the browser offers (WebCodecs, WebAssembly, WebGL, image formats).
- `/wasmtest`: downloads a few seconds of the current video in H.264, measures how many frames per second the browser can decode and plays it in real time.

## WebAssembly decoder
The H.264 decoder is embedded in `yt_mjpeg.py`. The wrapper source is `wasm/h264dec.c`; `wasm/build.sh` rebuilds it (only clang/lld, cmake, ninja and git are needed) and embeds it back into the file.

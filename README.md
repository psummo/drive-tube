# drive-tube

> [!WARNING]
> **For passengers only. Never for the driver.**
> drive-tube is meant to entertain passengers, for example children in the back seat or on a rear screen. The driver must never watch, operate or be distracted by it while the vehicle is moving. Using it is entirely at your own risk: the driver alone is responsible for driving safely and for complying with the traffic laws of their country, and any accident, injury, damage, fine or other consequence arising from the use of this tool is the sole responsibility of the driver. The authors and contributors accept no liability of any kind.
>
> drive-tube is an independent open-source project, provided "as is" without warranty of any kind (see [LICENSE](LICENSE)). It is not affiliated with, endorsed by or connected to Tesla, Google or YouTube. You are responsible for respecting YouTube's terms of service and the rights of content owners.

A lightweight YouTube player for in-car browsers (built for the Tesla browser), so that passengers can keep watching while the car is moving: those browsers block `<video>` and `<audio>` while driving.

drive-tube is a small Python server with no dependencies beyond the standard library (see [Code layout](#code-layout)). `yt-dlp` finds the YouTube stream and `ffmpeg` prepares it for the browser, which plays it on a canvas and through the Web Audio API, with no `<video>` or `<audio>` tags. Audio is the master clock and the video follows it.

Two video formats, selectable in the settings:
- **H.264** (default): the video arrives from YouTube as-is, with no re-encoding, and the browser decodes it with FFmpeg's decoder compiled to WebAssembly and draws it with WebGL. It needs about 0.5–2 Mbit/s and almost no server CPU. It works over plain http. It requires a browser with WebAssembly SIMD (Chrome 91 or later; the Tesla browser has it).
- **MJPEG**: the server turns the video into JPEG frames. It needs far more bandwidth (tens of Mbit/s) but works everywhere. It is used automatically when a video has no H.264 format or the browser lacks WebAssembly SIMD.

In both modes the audio is MP3 decoded by the browser.

## Requirements
A Linux machine with:
- `python3` and `git`
- `ffmpeg` with `libmp3lame` (most builds include it)
- `yt-dlp`
- **Node.js 22 or later** (or Deno): yt-dlp needs a JavaScript runtime for YouTube, and the PO token provider (see below) needs Node 22+. If it's missing, the admin page can download it for you (no sudo needed)

## Easy setup (one script)
On a fresh Debian/Ubuntu or Fedora/RHEL/Amazon Linux machine, `setup.sh` does everything below for you: system packages (python3, git, ffmpeg), `yt-dlp`, Node.js 22 (user-local, no sudo), Deno, the clone of this repo, the admin password (asked once, stored in `~/.config/drive-tube/env` with mode 600, never in the code) and the systemd service.
```bash
curl -fsSL https://raw.githubusercontent.com/psummo/drive-tube/main/setup.sh -o setup.sh
bash setup.sh
```
Then open `http://YOUR-SERVER:8090/admin` (open the port in the firewall) and create your access codes. Running it again updates everything. Prefer to do it by hand? Follow the steps below.

## Installation
Install the dependencies with your distribution's package manager:

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y python3 git ffmpeg curl

# Fedora / RHEL and derivatives
sudo dnf install -y python3 git ffmpeg curl
```
If your distribution doesn't ship `ffmpeg`, download a static Linux build and copy the binary to `/usr/local/bin`.

The `nodejs` package on Debian/Ubuntu is often too old. You don't have to install Node yourself: the PO token button in the admin page (see below) downloads the latest official LTS build into `~/.local/share/drive-tube/node` when it can't find Node 22+. drive-tube also finds a Node installed with nvm, fnm, volta, asdf or in `/usr/local`, even though the systemd service doesn't load your shell's `PATH`, and uses the newest one. If only Node is available (no Deno), drive-tube enables it for yt-dlp automatically (`--js-runtimes node:PATH`).

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

## Privacy
drive-tube does not keep track of what people watch or search:
- the log never contains video ids, titles, searches or stream URLs (they are replaced by `<video>`, `<ricerca>`, `<url>` before writing), only technical events tagged with the access code's name;
- the admin page shows each session's code, mode and last activity, but not the video;
- nothing about videos is written to disk on the server. Recent searches and settings live in the viewer's own browser cookies.

## YouTube: cookies or PO token
From cloud server IPs, YouTube often asks for an anti-bot check. There are two ways to pass it, and they can be combined.

### Your account cookies (optional)
Export your youtube.com cookies in Netscape format and upload them from the admin page (*Cookie YouTube* section). Only the `youtube.com` cookies are kept (a full browser export also holds your mail, bank and other sites; everything else is discarded) and they are saved to `~/cookies.txt`, readable only by your user. **Carica senza account** (upload without account) also drops the account cookies (`SID`, `LOGIN_INFO`...) and keeps only the anonymous visitor ones: worth a try, but from a flagged IP YouTube usually wants a signed-in account. Prefer a secondary Google account: YouTube can suspend accounts used from servers. **Usa i cookie per** (use cookies for) chooses who uses them: *nessuno* (nobody), *solo admin* (admin only) or *tutti* (everyone). Whoever uses them acts with your account, and their home page also shows your subscriptions, "Watch later" and liked videos. If the file exists and you never made a choice, everyone uses it, as in older versions. Without cookies the home page shows only searches.

### PO token, no account needed
The [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) plugin generates the "PO tokens" YouTube checks in real browsers. It has two parts: a yt-dlp plugin and a small local server (port 4416, bound to `127.0.0.1`) that needs **Node.js 22+**.

In the admin page, *PO token* section, **Installa / aggiorna plugin e server** (install / update plugin and server) does everything: it finds Node 22+ (or downloads it, see above), downloads the plugin to `~/.config/yt-dlp/plugins/`, clones the latest server release into `~/bgutil-ytdlp-pot-provider`, runs `npm ci` and `tsc` (a few minutes) and starts it. Progress is shown on the page. From then on drive-tube starts the server at every launch and restarts it if it stops. Press the same button again to update. **Solo il plugin** (plugin only) updates just the plugin.

To do it by hand instead:
```bash
git clone --single-branch --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git ~/bgutil-ytdlp-pot-provider
cd ~/bgutil-ytdlp-pot-provider/server && npm ci && npx tsc
```
Or with Docker: `docker run --name bgutil-provider -d --init --restart unless-stopped -p 127.0.0.1:4416:4416 brainicism/bgutil-ytdlp-pot-provider` (drive-tube uses any server that answers on port 4416).

Then **Prova con un video** (test with a video) asks YouTube for a short video and shows whether the request passes and whether a token was generated. If YouTube still refuses, the server IP may be blocked: a PO token does not get around IP blocks.

If you share the server, the safest setup is PO token for everyone and cookies for nobody or admin only.

### When YouTube still says "Sign in to confirm you're not a bot"
That message usually means YouTube distrusts the server's IP address (cloud providers are flagged often), and a PO token alone does not fix it. The admin page's *Rete verso YouTube* (network to YouTube) section helps:
- **Trova una configurazione che funziona** (find a working setup) tries several combinations of YouTube player client (`tv`, `mweb`, `web_safari`, `web_embedded`, `android_vr`) and IPv4-only, and shows which pass. **Usa questa** (use this) saves one; it applies to every yt-dlp call (`--extractor-args youtube:player_client=...`, `-4`).
- **Proxy**: if nothing passes, route yt-dlp through a connection YouTube trusts, such as a SOCKS proxy on a home network (`socks5://host:port`, also `http://` and `https://`). The diagnosis uses the saved proxy too.
- Otherwise, cookies (above) for the admin only.

Settings are stored in `~/.config/drive-tube/access.json`.

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
The gear icon (top left) opens the settings: video format (H.264 or MJPEG), resolution, frame rate and JPEG quality (these two for MJPEG only), audio quality and audio buffer. In H.264 the resolution is capped at 720p. **Salva e applica** (save and apply) stores them in **browser cookies**: there is no configuration file on the server. Without cookies the defaults in the code apply (`SETTINGS` in `drivetube/config.py`). Recent searches are stored in a cookie too.

Tapping the video shows the suggestions, the home button, the picture-fit and full-screen buttons and a pause button in the middle; they hide after 5 seconds. While paused they stay visible.

The line at the top of the screen shows, in H.264 mode, how many seconds are buffered and the live frame rate: `28 fps (video 30)` means 28 frames shown in the last second out of the 30 the video has; `saltati N` (skipped) counts frames dropped because the decoder fell behind. On a slow connection the player adapts: every time it has to stop for loading, it waits for a longer buffer before carrying on (4, 12, 20, 28, then 30 seconds) and the next videos start with a bigger margin too. Frames are decoded a little ahead and shown on the screen refresh, following a smoothed copy of the audio clock, so they come out at even intervals. If the browser keeps the audio switched off (some only allow it after a tap), the player notices within a few seconds and asks for a tap on the screen, then carries on from the same point with sound. If the browser throttles its screen refresh (the tab goes to the background, or the screen slows down), the video keeps advancing from the internal timer instead of freezing while the audio clock runs on. On a device that cannot decode the video fast enough, the picture no longer freezes: it plays as fast as the device allows and, if it falls too far behind the sound, it resynchronises by jumping to the current position. If that happens often, choosing 480p in the settings decodes faster. The same line ends with the audio state (`audio running 96KB 12blk 12prog`: browser audio state, MP3 received, blocks decoded and scheduled), and audio events are also written to the server log as `player: ...` lines, so a playback problem on the car can be diagnosed from the admin page.

## Updates
- **From the admin page**, *Server* section: the update button compares the version with `main`; installing downloads the latest version from GitHub, checks the syntax of every file, moves the current `yt_mjpeg.py` and `drivetube/` to `.backup/` and restarts the server.
- **By hand**: `cd drive-tube && git pull`, then restart the service. If you had updated from the admin page, the folder no longer matches git: run `git fetch origin && git reset --hard origin/main` instead.
- **From 14.x to 15.0**: the 14.x updater only downloads `yt_mjpeg.py`. When the new `yt_mjpeg.py` starts and finds no `drivetube/` folder next to it, it downloads it from GitHub once, so updating from the admin page works. On a git checkout, running `git fetch origin && git reset --hard origin/main` and restarting the service is cleaner.

## Releases
Releases are published with a `vX.Y` tag: GitHub Actions checks the syntax and creates the release with a `drive-tube-vX.Y.tar.gz` archive (`yt_mjpeg.py`, `drivetube/`, README, LICENSE, `setup.sh`).

## Test pages
Available after login:
- `/test`: which features the browser offers (WebCodecs, WebAssembly, WebGL, image formats).
- `/wasmtest`: downloads a few seconds of the current video in H.264, measures how many frames per second the browser can decode and plays it in real time.
- `/keytest`: shows every event the browser receives from touches (press length, long press), keys (including media and volume keys), the scroll wheel, game controllers and the Media Session API (the system's "now playing" card and its next/previous/pause buttons). Each event is also written to the server log as `keytest: ...`, so a test in the car can be read later from the admin page. Use it to find out which steering-wheel buttons reach a web page.

## Code layout
`yt_mjpeg.py` is only the entry point (the systemd service runs it) and holds the version number. The server is the `drivetube/` package; the frontend is in `drivetube/web/`.

| Module | What it does |
|---|---|
| `drivetube/youtube.py` | **Getting data from YouTube** through yt-dlp: search, channels, personal feed, suggestions, a video's information and its stream URLs. It knows nothing about ffmpeg or the player, so it can be reused on its own. |
| `drivetube/media.py` | **ffmpeg**: the command line, the FIFO queues, reading MJPEG frames, demuxing H.264 from MPEG-TS, MP3 audio. It knows nothing about YouTube: it gets ready-made URLs. |
| `drivetube/chunkproxy.py` | Local proxy that downloads any HTTP URL in 10 MB pieces and resumes dropped connections. |
| `drivetube/player.py` | Playback for each browser session: ties `youtube` to `media`, handles start, restarts after errors or 403, pause and cleanup. |
| `drivetube/server.py` | HTTP server: pages, player API, admin page, start-up. |
| `drivetube/context.py` | Per-browser sessions, thread context and the privacy-aware log. |
| `drivetube/access.py`, `settings.py` | Access codes and admin password; player settings and recent searches in browser cookies. |
| `drivetube/tools.py`, `potoken.py` | yt-dlp and Node.js versions and updates; PO token provider and the network diagnosis. |
| `drivetube/update.py` | Update from the admin page. |
| `drivetube/config.py` | Version, port, paths and default settings. |
| `drivetube/web/` | **Frontend**: the player (`player.html`, with its CSS and JavaScript), home, search, channel, login, admin and test pages, and the H.264 decoder (`h264.wasm`). |

For example, a script that only needs YouTube search can do:
```python
import sys; sys.path.insert(0, "/path/to/drive-tube")
from drivetube.youtube import search_yt, video_info
print(search_yt("lofi"))
```
(`search_yt` and the other list functions read the access cookie settings for the current browser session; outside a session they run without cookies.)

## Repository contents
| File | What it is for |
|---|---|
| `yt_mjpeg.py` | Entry point and version number. |
| `drivetube/` | The app: backend modules and, in `web/`, the frontend (see [Code layout](#code-layout)). |
| `setup.sh` | One-script install (see [Easy setup](#easy-setup-one-script)). |
| `README.md` | This guide. |
| `LICENSE` | MIT license, with the passengers-only notice. |
| `wasm/h264dec.c` | Source of the H.264 decoder that runs in the browser: a thin C wrapper around FFmpeg's decoder. |
| `wasm/build.sh` | Script that rebuilds the decoder from FFmpeg's sources and copies it to `drivetube/web/h264.wasm`. |
| `.github/workflows/release.yml` | Publishes a GitHub release archive when a `vX.Y` tag is pushed. |

**You do not need the `wasm/` folder to use or host drive-tube.** The compiled decoder is already in `drivetube/web/h264.wasm` (served compressed to the browser as `/h264.wasm`). The folder is in the repository only so the decoder can be audited and rebuilt. Do this only if you want to change or update the decoder: `wasm/build.sh` needs clang/lld (15 or later), cmake, ninja, make, git and python3, downloads FFmpeg n7.1 and works in `wasm/.build/` (ignored by git).

## Changelog

### 15.5
- `/keytest` test H: turns the page into a full media source (silent `<audio>`, tone, title, artwork, moving position, every command) to check whether play/pause and next/previous from the car's media card and steering wheel reach the page while the "video limited" message is shown (it sits at the top and does not cover the browser's centre).

### 15.4
- Findings from `/keytest` in the car: the Tesla media card shows a page's Media Session (title, artwork) only after the page has played an `<audio>` element, and that same `<audio>` triggers the "video limited" message, which then stays until the page is reloaded. Media Session alone (with or without Web Audio, duration/position or extra commands) neither blocks nor appears in the card. Canvas drawing is never blocked. So drive-tube keeps away from media elements and does not use the Media Session.
- The player's `?ms=1` test now works like test E: title, thumbnail, real duration and position updated every second, and the media card commands wired to the player (play/pause/stop, seek to, ±10 s). Still off unless `?ms=1` is given.

### 15.3
- `/keytest`: three more tests to find what made the first version of the page trigger "video limited" while the reworked one does not: E repeats the first page's test exactly, F adds only duration/position (`setPositionState`) to the plain Media Session, G adds only the seek-to and skip-ad commands.

### 15.2
- `/keytest` reworked after the first test in the car: the log is always visible on the right, with the last event in large type. A canvas animation shows whether canvas drawing keeps going while the Tesla shows its "video limited" message. The Media Session check is split into four tests (Media Session only, Web Audio tone only, both, silent `<audio>` plus Media Session) to find what triggers the message. A fifth test opens the real player with `?ms=1`, which turns on the Media Session (title and thumbnail) for that test only.
- Finding so far: on the Tesla, setting the Media Session triggers "video limited to audio only" even without any `<audio>` or `<video>` element, so the player never uses it unless `?ms=1` is given.

### 15.1
- New `/keytest` page (linked from `/test`) to find out which car controls reach the browser: touches and long presses, keys, steering-wheel scroll wheel, controllers, and the Media Session API in two variants (Web Audio only, as drive-tube plays, and with a silent `<audio>` element).

### 15.0
- **Code split into modules**: the single 5,400-line file is now the `drivetube/` package. Getting data from YouTube (`youtube.py`) is separate from ffmpeg (`media.py`), the local download proxy and the player logic, and the frontend (HTML, CSS, JavaScript and the WebAssembly decoder) lives in `drivetube/web/` as plain files. `yt_mjpeg.py` stays the entry point, so the systemd service does not change. No change in behaviour: the same end-to-end test (playback in H.264 and MJPEG, audio, search, channels, suggestions, admin) passes before and after.
- The admin page update now downloads the whole package, checks every file and keeps the previous version in `.backup/`. Updating from 14.x works too (see [Updates](#updates)).

### 14.16
- **Picture fit**: a new button over the video switches between *Adatta* (whole picture, black bars), *Riempi* (fills the screen, crops the edges) and *Stira* (fills the screen, stretched). The choice is remembered in the browser. Where the browser allows it, *Schermo intero* switches to full screen.
- Clearer controls: the home button now reads "Home" with a bigger icon, the top buttons line up and have a border, the search button on the home page is readable (white on blue), and the suggestion strip is smaller and lower so it no longer covers the play/pause button.

### 14.15
- **Personal feed fixed**: subscriptions, watch later and liked videos were fetched in worker threads that lost the session, so yt-dlp ran without the account cookies and YouTube answered "The playlist does not exist". They now use the cookies like the rest of the app.

### 14.14
- **Fewer stalls, especially on long videos**: ffmpeg used to download each stream with a single HTTP request to the end of the file; YouTube slows down or drops such requests after a few MB, so long videos stalled for loading again and again. The stream now goes through a small local proxy (127.0.0.1 only) that fetches it from YouTube in 10 MB pieces, as yt-dlp does, and resumes from the same byte when a connection drops, without ffmpeg noticing. Seeking still works (byte ranges are passed through). An http(s) proxy set in the admin page now applies to the stream too.

### 14.13
- **403 on the stream**: when YouTube refuses the stream (HTTP 403) the player no longer retries the same rejected URL: it fetches fresh URLs right away and, if the 403 repeats, tries other YouTube clients in turn (`tv`, `web_safari`, `mweb`, `android_vr`). The client that works is kept for the rest of that session and written to the log, so you can save it in the admin page.
- **Network diagnosis checks the real stream**: *Trova una configurazione che funziona* used to check only that yt-dlp could read the video, which passes even when the stream itself is then refused. It now also downloads the first bytes of the stream, so a combination marked OK actually plays.

### 14.12
- **Mixed home**: the search you just made stays on top; everything else (recent searches, suggestions for the current video, channel videos and, with cookies, your personal feed) is now interleaved in a single grid instead of one section per source, so the suggestions land in between the other videos.

### 14.11
- **Tab title**: the player page now shows the video title as the browser tab title (fetched together with the duration, so it appears a moment after the page loads).
- **Idle sessions**: a session with no active ffmpeg and no activity is now closed after 30 minutes (was 3 hours), so the admin page no longer lists stale "home" sessions for hours. Settings live in browser cookies and a video can be resumed from its URL, so nothing is lost.

### 14.10
- **Suggestions now work on blocked IPs**: on datacenter IPs (e.g. AWS EC2) YouTube blocks the `youtube:tab` extractor, so playlists, mixes and channel pages fail with "The playlist does not exist", leaving the home page empty. The "suggested for this video" section no longer uses the `RD` mix playlist; it looks up the video's title and channel and finds similar videos through search, which still works. Channel sections on the home page fall back to search when the channel page is blocked.
- Note: the personal feed (subscriptions / watch later / liked) relies on `youtube:tab` and cannot be fetched from a blocked IP even with cookies. Use a proxy in the admin page's *Rete verso YouTube* section to restore it.

### 14.9
- **Home page**: shows up to 6 recent searches (was 3), and loads videos from channels found in search results as extra sections, so the home page is richer even in "related only" mode or without cookies.
- **Live streams**: detected automatically via yt-dlp; the progress bar and time display are replaced by a red LIVE indicator, seeking is disabled, the URL no longer updates with a timestamp, and ffmpeg reconnects automatically if the stream drops instead of marking it as finished.

### 14.8
- Search results doubled (16 instead of 8).
- Admin setting to choose between personal feed (subscriptions/watch later/liked) and related-only mode when cookies are active.

### 14.7
- MJPEG-only settings (FPS, JPEG quality) are hidden when in H.264 mode.
- Sidebar simplified: removed the tab bar since there is only one panel.

### 14.6
- Admin panel: sessions show "in riproduzione" only when ffmpeg is alive or the client was active in the last 30 seconds, not just because a URL was set.

## License
drive-tube is open source, released under the [MIT License](LICENSE). The embedded WebAssembly H.264 decoder is built from [FFmpeg](https://ffmpeg.org) n7.1 (LGPL 2.1 or later, configured without GPL parts); `wasm/build.sh` rebuilds it from the official FFmpeg sources.

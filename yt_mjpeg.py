#!/usr/bin/env python3
# drive-tube - https://github.com/psummo/drive-tube - MIT License
# Solo per i passeggeri, mai per chi guida: ogni conseguenza dell'uso durante
# la guida e' responsabilita' esclusiva del conducente. Vedi README e LICENSE.
import builtins
import hashlib
import hmac
import html
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
import secrets
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

VERSION = "14.10"
UPDATE_URL = "https://raw.githubusercontent.com/psummo/drive-tube/main/yt_mjpeg.py"

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


def cookie_value(header, name):
    for part in (header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return unquote(v)
    return ""


def settings_from_cookie(header):
    cfg = dict(DEFAULTS)
    try:
        data = json.loads(cookie_value(header, "yt_cfg") or "{}")
    except ValueError:
        data = {}
    if isinstance(data, dict):
        for k, n, h, d, opts in SETTINGS:
            v = data.get(k)
            if type(v) is int and v in [o[0] for o in opts]:
                cfg[k] = v
    return cfg


def apply_settings(header):
    """Applica alla sessione corrente le impostazioni del cookie. True se sono cambiate."""
    s = cur()
    new = settings_from_cookie(header)
    if new == s.cfg:
        return False
    s.cfg = new
    print("impostazioni:", new, flush=True)
    return True


def C(key):
    """Impostazione della sessione corrente."""
    return cur().cfg[key]


def recent_from_cookie(header):
    try:
        rows = json.loads(cookie_value(header, "yt_recent") or "[]")
    except ValueError:
        return []
    if not isinstance(rows, list):
        return []
    return [x for x in rows if isinstance(x, str) and x][:10]


def push_recent(q, rows):
    q = (q or "").strip().replace("\n", " ")
    if not q:
        return rows
    return ([q] + [x for x in rows if x.lower() != q.lower()])[:10]


def recent_cookie_header(rows):
    return "yt_recent=%s; Max-Age=31536000; Path=/; SameSite=Lax" % quote(json.dumps(rows, ensure_ascii=False), safe="")


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
_ytdlp_latest = {"t": 0, "v": "", "err": ""}


def ytdlp_bin():
    # la copia aggiornata dall'admin (nella home, scrivibile) ha la precedenza
    if os.path.isfile(YTDLP_LOCAL) and os.access(YTDLP_LOCAL, os.X_OK):
        return YTDLP_LOCAL
    return "yt-dlp"


def tool_version(cmd):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        out = (p.stdout or p.stderr or "").strip().splitlines()
        return out[0].strip() if out else ""
    except Exception:
        return ""


# ---- Node.js ----
# Il servizio systemd non legge ~/.bashrc: un Node installato con nvm, fnm, volta
# o in /usr/local non e' nel suo PATH. Lo cerchiamo nei posti soliti e usiamo il
# piu' recente; se manca (o e' troppo vecchio) drive-tube ne scarica uno ufficiale
# in ~/.local/share/drive-tube/node, senza sudo.
NODE_LOCAL = os.path.expanduser("~/.local/share/drive-tube/node")
NODE_MIN = 22
NODE_DIST = "https://nodejs.org/dist/"
_node_cache = {"t": 0.0, "path": "", "major": 0}


def _node_candidates():
    import glob
    h = os.path.expanduser("~")
    pats = [
        os.path.join(NODE_LOCAL, "bin", "node"),
        h + "/.nvm/versions/node/*/bin/node",
        h + "/.local/share/fnm/node-versions/*/installation/bin/node",
        h + "/.fnm/node-versions/*/installation/bin/node",
        h + "/.volta/bin/node",
        h + "/.volta/tools/image/node/*/bin/node",
        h + "/n/bin/node",
        h + "/.local/bin/node",
        h + "/.asdf/installs/nodejs/*/bin/node",
        "/usr/local/bin/node", "/usr/bin/node", "/opt/*/bin/node", "/snap/bin/node",
    ]
    out = []
    w = shutil.which("node")
    if w:
        out.append(w)
    for p in pats:
        out += sorted(glob.glob(p))
    seen, res = set(), []
    for p in out:
        r = os.path.realpath(p)
        if r not in seen and os.path.isfile(r) and os.access(r, os.X_OK):
            seen.add(r)
            res.append(p)
    return res


def find_node(refresh=False):
    """(percorso, versione principale) del Node piu' recente trovato, o ("", 0)."""
    if not refresh and time.time() - _node_cache["t"] < 120:
        return _node_cache["path"], _node_cache["major"]
    best, bmaj = "", 0
    for p in _node_candidates():
        m = re.search(r"v(\d+)", tool_version([p, "--version"]) or "")
        maj = int(m.group(1)) if m else 0
        if maj > bmaj:
            best, bmaj = p, maj
    _node_cache.update(t=time.time(), path=best, major=bmaj)
    return best, bmaj


def node_env(node):
    # PATH con la cartella di node davanti: npm e npx sono script "env node"
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(node) + os.pathsep + env.get("PATH", "")
    return env


def node_install(log=lambda m: None):
    """Scarica l'ultimo Node LTS ufficiale in NODE_LOCAL. Ritorna il percorso di node."""
    import platform
    import tarfile
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower())
    if not arch or not sys.platform.startswith("linux"):
        raise RuntimeError("non so scaricare Node per %s %s: installalo a mano" % (sys.platform, platform.machine()))
    ua = {"User-Agent": "drive-tube"}
    with urllib.request.urlopen(urllib.request.Request(NODE_DIST + "index.json", headers=ua), timeout=60) as r:
        rel = json.load(r)
    pick = next((x for x in rel if x.get("lts") and "linux-" + arch in x.get("files", [])), None)
    if not pick:
        raise RuntimeError("nessuna versione LTS di Node trovata per linux-%s" % arch)
    ver = pick["version"]
    name = "node-%s-linux-%s.tar.xz" % (ver, arch)
    base = NODE_DIST + ver + "/"
    log("scarico Node.js %s (senza sudo, in %s)" % (ver, NODE_LOCAL))
    with urllib.request.urlopen(urllib.request.Request(base + "SHASUMS256.txt", headers=ua), timeout=60) as r:
        sums = r.read().decode()
    m = re.search(r"^([0-9a-f]{64})\s+" + re.escape(name) + r"$", sums, re.M)
    if not m:
        raise RuntimeError("checksum di %s non trovato" % name)
    parent = os.path.dirname(NODE_LOCAL)
    os.makedirs(parent, exist_ok=True)
    tmp = os.path.join(parent, name + ".part")
    h = hashlib.sha256()
    with urllib.request.urlopen(urllib.request.Request(base + name, headers=ua), timeout=600) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            h.update(b)
            f.write(b)
    if h.hexdigest() != m.group(1):
        os.remove(tmp)
        raise RuntimeError("Node scaricato non valido (checksum diverso)")
    log("estraggo Node.js %s" % ver)
    work = NODE_LOCAL + ".new"
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    with tarfile.open(tmp) as t:
        root = os.path.realpath(work)
        for mem in t.getmembers():
            dest = os.path.realpath(os.path.join(work, mem.name))
            if not (dest == root or dest.startswith(root + os.sep)):
                raise RuntimeError("archivio di Node non valido")
        try:
            t.extractall(work, filter="fully_trusted")
        except TypeError:
            t.extractall(work)
    os.remove(tmp)
    inner = os.path.join(work, name[:-len(".tar.xz")])
    old = NODE_LOCAL + ".old"
    shutil.rmtree(old, ignore_errors=True)
    if os.path.exists(NODE_LOCAL):
        os.rename(NODE_LOCAL, old)
    os.rename(inner, NODE_LOCAL)
    shutil.rmtree(work, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)
    node, maj = find_node(refresh=True)
    if maj < NODE_MIN:
        raise RuntimeError("Node installato ma non funziona")
    return node


def ytdlp_status(force=False):
    inst = tool_version([ytdlp_bin(), "--version"])
    if force or time.time() - _ytdlp_latest["t"] > 600:
        try:
            req = urllib.request.Request(YTDLP_API, headers={"User-Agent": "drive-tube", "Accept": "application/vnd.github+json"})
            with urllib.request.urlopen(req, timeout=15) as r:
                tag = json.load(r).get("tag_name", "")
            _ytdlp_latest.update(t=time.time(), v=tag, err="")
        except Exception as e:
            _ytdlp_latest.update(t=time.time(), err="controllo fallito: %s" % e)
    latest = _ytdlp_latest["v"]
    js = ""
    deno = shutil.which("deno")
    node, _ = find_node()
    if deno:
        js = "deno %s" % tool_version([deno, "--version"])
    elif node:
        js = "node %s (%s)" % (tool_version([node, "--version"]), node)
    ff = tool_version(["ffmpeg", "-version"])
    return {
        "installed": inst or "non trovato", "latest": latest, "error": _ytdlp_latest["err"],
        "newer": bool(inst and latest and _ver_key(latest) > _ver_key(inst)),
        "path": shutil.which(ytdlp_bin()) or ytdlp_bin(),
        "js": js or "nessuno (yt-dlp potrebbe non funzionare con YouTube)",
        "ffmpeg": " ".join(ff.split()[:3]) if ff else "non trovato",
    }


def ytdlp_update():
    os.makedirs(os.path.dirname(YTDLP_LOCAL), exist_ok=True)
    tmp = YTDLP_LOCAL + ".new"
    req = urllib.request.Request(YTDLP_URL, headers={"User-Agent": "drive-tube"})
    with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    os.chmod(tmp, 0o755)
    v = tool_version([tmp, "--version"])
    if not v or not v[0].isdigit():
        os.remove(tmp)
        raise RuntimeError("il file scaricato non funziona")
    os.replace(tmp, YTDLP_LOCAL)
    feed_cache.clear()
    _ytdlp_latest["t"] = 0
    return v


# ================= PO token (bgutil) =================
# Senza cookie, dagli IP dei server YouTube chiede spesso la verifica anti-bot.
# Il plugin bgutil per yt-dlp ottiene i "PO token" che YouTube controlla nei
# browser veri; li genera un piccolo server locale (porta 4416). Se trova il
# server compilato in ~/bgutil-ytdlp-pot-provider, drive-tube lo avvia e lo tiene
# acceso da solo; se gira gia' (per esempio in Docker) lo usa e basta.
POT_HOME = os.path.expanduser("~/bgutil-ytdlp-pot-provider")
POT_MAIN = os.path.join(POT_HOME, "server", "build", "main.js")
POT_PLUGIN_DIR = os.path.expanduser("~/.config/yt-dlp/plugins")
POT_ZIP = os.path.join(POT_PLUGIN_DIR, "bgutil-ytdlp-pot-provider.zip")
POT_ZIP_URL = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/releases/latest/download/bgutil-ytdlp-pot-provider.zip"
POT_PING = "http://127.0.0.1:4416/ping"
POT_TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
POT_REPO = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"
POT_FALLBACK_TAG = "2.0.2"
_pot = {"proc": None, "last_start": 0.0, "install": {}}


def pot_ping():
    try:
        with urllib.request.urlopen(POT_PING, timeout=2) as r:
            data = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return data if isinstance(data, dict) else {"ok": True}
    except Exception:
        return None


def pot_plugin_files():
    out = []
    for base in (POT_PLUGIN_DIR, os.path.expanduser("~/yt-dlp-plugins")):
        try:
            out += [os.path.join(base, n) for n in os.listdir(base) if "bgutil" in n.lower()]
        except OSError:
            pass
    return out


def pot_server_log(proc):
    n = 0
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if line and n < 50:
                _bprint("[pot]", line[:200], flush=True)
                n += 1
    except Exception:
        pass


def pot_start(force=False):
    # avvia il server bgutil se e' compilato e non risponde gia'
    p = _pot["proc"]
    alive = bool(p and p.poll() is None)
    node, _ = find_node()
    if alive or (_pot["install"].get("running") and not force) or not os.path.isfile(POT_MAIN) or not node:
        return False
    if not force and time.time() - _pot["last_start"] <= 60:
        return False
    if pot_ping() is not None:
        return False
    _pot["last_start"] = time.time()
    _bprint("avvio il server PO token (bgutil)", flush=True)
    _pot["proc"] = subprocess.Popen(
        [node, "build/main.js"], cwd=os.path.dirname(os.path.dirname(POT_MAIN)),
        env=node_env(node), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    threading.Thread(target=pot_server_log, args=(_pot["proc"],), daemon=True).start()
    return True


def pot_supervisor():
    # tiene acceso il server bgutil (lo riavvia se si ferma)
    while True:
        try:
            pot_start()
        except Exception as e:
            _bprint("server PO token:", e, flush=True)
        time.sleep(20)


def pot_latest_tag():
    # ultima versione X.Y.Z del server, letta dai tag del repository
    try:
        out = subprocess.run(["git", "ls-remote", "--tags", POT_REPO], capture_output=True,
                             text=True, timeout=60).stdout
        tags = re.findall(r"refs/tags/(\d+)\.(\d+)\.(\d+)$", out, re.M)
        if tags:
            return ".".join(max(tags, key=lambda t: tuple(int(x) for x in t)))
    except Exception:
        pass
    return POT_FALLBACK_TAG


def pot_install_server():
    """Scarica, compila e avvia il server bgutil (gira in un thread)."""
    st = _pot["install"]

    def step(msg):
        st["step"] = msg
        st["log"].append(msg)
        _bprint("PO token:", msg, flush=True)

    env = None

    def run(cmd, cwd=None, timeout=900):
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
        tail = [l for l in ((p.stdout or "") + (p.stderr or "")).splitlines() if l.strip()][-12:]
        if p.returncode != 0:
            st["log"].extend(tail)
            raise RuntimeError("%s non riuscito (codice %d)" % (" ".join(cmd[:2]), p.returncode))

    try:
        node, maj = find_node(refresh=True)
        if maj >= NODE_MIN:
            step("uso Node.js v%d: %s" % (maj, node))
        else:
            step("Node.js %s: ne scarico uno recente" % ("v%d e' troppo vecchio" % maj if maj else "non trovato"))
            node = node_install(step)
        nbin = os.path.dirname(node)
        npm, npx = os.path.join(nbin, "npm"), os.path.join(nbin, "npx")
        if not (os.path.isfile(npm) and os.path.isfile(npx)):
            step("accanto a %s non ci sono npm e npx: ne scarico uno completo" % node)
            node = node_install(step)
            nbin = os.path.dirname(node)
            npm, npx = os.path.join(nbin, "npm"), os.path.join(nbin, "npx")
        env = node_env(node)
        if not shutil.which("git"):
            raise RuntimeError("manca il comando git")
        step("installo il plugin di yt-dlp")
        pot_install_plugin()
        tag = pot_latest_tag()
        if os.path.isdir(os.path.join(POT_HOME, ".git")):
            step("aggiorno il server alla versione %s" % tag)
            run(["git", "fetch", "--depth", "1", "origin", "tag", tag], cwd=POT_HOME, timeout=300)
            run(["git", "checkout", "-f", tag], cwd=POT_HOME)
        else:
            if os.path.exists(POT_HOME):
                raise RuntimeError("%s esiste ma non e' una copia git: spostala o cancellala" % POT_HOME)
            step("scarico il server, versione %s" % tag)
            run(["git", "clone", "--depth", "1", "--branch", tag, POT_REPO, POT_HOME], timeout=300)
        srv = os.path.join(POT_HOME, "server")
        step("installo le dipendenze (npm ci, qualche minuto)")
        run([npm, "ci", "--no-audit", "--no-fund"], cwd=srv)
        step("compilo (tsc)")
        run([npx, "tsc"], cwd=srv)
        if not os.path.isfile(POT_MAIN):
            raise RuntimeError("compilazione finita ma manca %s" % POT_MAIN)
        p = _pot["proc"]
        if p and p.poll() is None:
            step("fermo il server vecchio")
            p.terminate()
            try:
                p.wait(10)
            except Exception:
                p.kill()
        step("avvio il server")
        pot_start(force=True)
        for _ in range(30):
            if pot_ping() is not None:
                break
            time.sleep(1)
        if pot_ping() is None:
            raise RuntimeError("il server compilato non risponde sulla porta 4416: guarda il log")
        step("fatto: server %s attivo" % tag)
        st["ok"] = True
    except Exception as e:
        st["error"] = str(e)
        st["log"].append("ERRORE: %s" % e)
        _bprint("PO token: installazione fallita:", e, flush=True)
    finally:
        st["running"] = False


def pot_install_begin():
    st = _pot["install"]
    if st.get("running"):
        return False
    _pot["install"] = {"running": True, "step": "inizio", "log": [], "error": "", "ok": False}
    threading.Thread(target=pot_install_server, daemon=True).start()
    return True


def pot_status():
    ping = pot_ping()
    p = _pot["proc"]
    node, _ = find_node()
    return {
        "plugin": pot_plugin_files(),
        "server": ping is not None,
        "server_version": (ping or {}).get("version", ""),
        "managed": bool(p and p.poll() is None),
        "built": os.path.isfile(POT_MAIN),
        "node": tool_version([node, "--version"]) if node else "",
        "install": _pot["install"],
    }


def pot_install_plugin():
    import zipfile
    os.makedirs(POT_PLUGIN_DIR, exist_ok=True)
    tmp = POT_ZIP + ".new"
    req = urllib.request.Request(POT_ZIP_URL, headers={"User-Agent": "drive-tube"})
    with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    try:
        with zipfile.ZipFile(tmp) as z:
            if not any(n.startswith("yt_dlp_plugins/") for n in z.namelist()):
                raise RuntimeError("lo zip scaricato non contiene un plugin di yt-dlp")
    except zipfile.BadZipFile:
        os.remove(tmp)
        raise RuntimeError("il file scaricato non e' uno zip valido")
    os.replace(tmp, POT_ZIP)
    return POT_ZIP


def pot_test(net=None):
    """Chiede a yt-dlp i formati di un video breve e riporta le righe utili."""
    try:
        p = subprocess.run(
            yt_cmd("-v", "--simulate", "--no-playlist", "-f", "bv*[height<=360]+ba/b",
                   "--user-agent", UA, POT_TEST_VIDEO, net=net),
            capture_output=True, text=True, timeout=120,
        )
    except Exception as e:
        return {"ok": False, "token": False, "error": "yt-dlp non partito: %s" % e,
                "lines": ["yt-dlp non partito: %s" % e]}
    keep, err = [], ""
    for line in (p.stderr or "").splitlines() + (p.stdout or "").splitlines():
        low = line.lower()
        if ("[pot" in low or "po token" in low or "error" in low or "sign in" in low
                or "js runtime" in low or "jsc" in low or "challenge" in low or "player_client" in low):
            keep.append(re.sub(r"https?://\S+", "<url>", line.strip())[:220])
        if line.startswith("ERROR:"):
            err = re.sub(r"https?://\S+", "<url>", line[6:].strip())
            err = re.sub(r"^\[youtube\] [\w-]+: ", "", err)[:160]
    tokens = any("generating a" in x.lower() and "po token" in x.lower() for x in keep)
    return {"ok": p.returncode == 0, "token": tokens, "error": err,
            "lines": keep[-25:] or ["nessuna riga utile nell'output"]}


def yt_diagnose():
    """Prova le combinazioni di client e IPv4 (con il proxy salvato) e dice quali passano."""
    proxy = (ACCESS.get("yt") or {}).get("proxy", "")
    nets = [{"client": c, "ipv4": v4, "proxy": proxy} for c, v4 in YT_VARIANTS]
    with ThreadPoolExecutor(max_workers=3) as ex:
        res = list(ex.map(pot_test, nets))
    out = []
    for n, r in zip(nets, res):
        out.append({"client": n["client"], "ipv4": n["ipv4"], "label": yt_net_label(n),
                    "ok": r["ok"], "token": r["token"], "error": r.get("error", "")})
        _bprint("diagnosi YouTube:", yt_net_label(n), "->", "OK" if r["ok"] else (r.get("error") or "errore"), flush=True)
    return out


# Cookie che identificano l'account Google: senza questi resta solo il "visitatore"
COOKIE_ACCOUNT = re.compile(r"SID|LOGIN_INFO")


def cookies_youtube_only(text, anon=False):
    """Tiene solo i cookie di youtube.com (un export del browser contiene anche
    banca, posta, ecc.), uno per nome (il piu' recente). Con anon=True toglie
    anche quelli dell'account. Ritorna (testo, tenuti, scartati)."""
    best, dropped = {}, 0
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        p = line.split("\t")
        if len(p) != 7:
            dropped += 1
            continue
        dom = p[0][len("#HttpOnly_"):] if p[0].startswith("#HttpOnly_") else p[0]
        if not (dom == "youtube.com" or dom.endswith(".youtube.com")) or (anon and COOKIE_ACCOUNT.search(p[5])):
            dropped += 1
            continue
        key = (dom.lstrip("."), p[2], p[5])
        exp = int(p[4]) if p[4].isdigit() else 0
        if key in best:
            dropped += 1
            if exp <= best[key][0]:
                continue
        best[key] = (exp, line)
    out = "# Netscape HTTP Cookie File\n" + "".join(v[1] + "\n" for v in best.values())
    return out, len(best), dropped


def cookie_mode():
    """'off', 'admin' o 'all'. Mai impostato: 'all' se c'e' il file (come prima)."""
    m = ACCESS.get("cookies")
    if m in ("off", "admin", "all"):
        return m
    return "all" if os.path.isfile(COOKIES) else "off"


def home_feed():
    """'personal' o 'related'. Con personal (default) mostra iscrizioni/watch later/mi piace."""
    m = ACCESS.get("home_feed")
    return m if m in ("personal", "related") else "personal"


def use_cookies():
    # vale per la sessione legata al thread; senza sessione (richieste admin,
    # prova PO token) niente cookie
    if not os.path.isfile(COOKIES):
        return False
    m = cookie_mode()
    s = getattr(_tls, "s", None)
    if s is None:
        return False
    return m == "all" or (m == "admin" and s.who == "admin")


def in_session(fn):
    """La funzione, eseguita in un altro thread (es. ThreadPoolExecutor), lavora
    sulla sessione di chi la prepara."""
    sess = getattr(_tls, "s", None)

    def run(*a, **k):
        _tls.s = sess
        try:
            return fn(*a, **k)
        finally:
            _tls.s = None
    return run


# Rete verso YouTube, scelta dalla pagina admin: client di YouTube da usare,
# solo IPv4, proxy. Dagli IP dei server YouTube a volte blocca un client o gli
# indirizzi IPv6 ma non gli altri; la diagnosi in admin prova le combinazioni.
YT_VARIANTS = [
    ("", False), ("", True),
    ("tv", False), ("tv", True),
    ("mweb", False), ("web_safari", False), ("web_embedded", False), ("android_vr", True),
]


def yt_net(net=None):
    n = ACCESS.get("yt") if net is None else net
    n = n if isinstance(n, dict) else {}
    out = []
    if n.get("ipv4"):
        out.append("-4")
    if n.get("client"):
        out += ["--extractor-args", "youtube:player_client=" + n["client"]]
    if n.get("proxy"):
        out += ["--proxy", n["proxy"]]
    return out


def yt_net_label(n):
    t = ("client " + n["client"]) if n.get("client") else "client predefiniti"
    if n.get("ipv4"):
        t += ", solo IPv4"
    if n.get("proxy"):
        t += ", proxy"
    return t


def yt_cmd(*args, net=None):
    cmd = [ytdlp_bin()]
    if use_cookies():
        cmd += ["--cookies", COOKIES]
    cmd += yt_net(net)
    # yt-dlp attiva da solo solo Deno: se c'e' soltanto Node va abilitato,
    # altrimenti alcune richieste a YouTube falliscono
    if not shutil.which("deno"):
        node, _ = find_node()
        if node:
            cmd += ["--js-runtimes", "node:" + node]
    cmd += list(args)
    return cmd

def set_page(url):
    cur().url = url
    state['duration'] = 0
    state['seek_base'] = 0.0
    state['is_live'] = False
    sthread(fetch_duration).start()
    new_session(0)


def search_yt(q):
    ck = ("s", q, use_cookies())
    hit = feed_cache.get(ck)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    p = subprocess.run(
        yt_cmd('--flat-playlist', '--print', '%(id)s\t%(title)s\t%(channel_id)s\t%(url)s', 'ytsearch16:' + q),
        text=True, capture_output=True,
    )
    rows = []
    for line in p.stdout.splitlines():
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        vid, title = parts[0].strip(), parts[1].strip()
        url = parts[3].strip() if len(parts) > 3 else ''
        kind = 'channel' if vid.startswith('UC') or '/channel/' in url or '/@' in url else 'video'
        rows.append((vid, title, kind, url))
    feed_cache[ck] = (time.time(), rows)
    return rows


def flat_rows(url, limit=8, start=1):
    end = start + limit - 1
    key = (url, start, end, use_cookies())
    hit = feed_cache.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        p = subprocess.run(
            yt_cmd(
                "--flat-playlist",
                "--playlist-start", str(start),
                "--playlist-end", str(end),
                "--print", "%(id)s\t%(title)s",
                url,
            ),
            text=True, capture_output=True, timeout=30,
        )
    except Exception as e:
        print("elenco non riuscito:", e, flush=True)
        return []
    rows = []
    for line in p.stdout.splitlines():
        if "\t" not in line:
            continue
        vid, title = line.split("\t", 1)
        vid = vid.strip()
        if len(vid) != 11:
            continue
        rows.append((vid, title.strip()))
    feed_cache[key] = (time.time(), rows)
    if not rows:
        err = (p.stderr or "")[-200:]
        print("elenco vuoto:", err, flush=True)
    return rows


def video_meta(vid):
    """(titolo, canale) di un video, da usare per cercare contenuti simili."""
    ck = ("m", vid, use_cookies())
    hit = feed_cache.get(ck)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    p = subprocess.run(
        yt_cmd("--print", "%(title)s\t%(channel)s", "--no-playlist",
               "https://www.youtube.com/watch?v=" + vid),
        text=True, capture_output=True,
    )
    title = chan = ""
    for line in p.stdout.splitlines():
        parts = line.split("\t")
        if parts and parts[0].strip() and parts[0].strip() != "NA":
            title = parts[0].strip()
            chan = parts[1].strip() if len(parts) > 1 and parts[1].strip() != "NA" else ""
            break
    feed_cache[ck] = (time.time(), (title, chan))
    return title, chan


def videos_from_search(query, exclude=None, limit=12):
    """Solo i video (niente canali) da una ricerca, escludendo alcuni id."""
    exclude = exclude or set()
    rows = []
    for r in search_yt(query):
        vid, title = r[0], r[1]
        kind = r[2] if len(r) > 2 else 'video'
        if kind == 'video' and vid and vid not in exclude:
            exclude.add(vid)
            rows.append((vid, title))
        if len(rows) >= limit:
            break
    return rows


def related_of(vid):
    # Su IP datacenter YouTube blocca playlist/mix e pagine canale (youtube:tab);
    # la ricerca invece passa. Cerchiamo video simili dal canale e dal titolo.
    if not vid:
        return []
    ck = ("r", vid, use_cookies())
    hit = feed_cache.get(ck)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    title, chan = video_meta(vid)
    seen = {vid}
    rows = []
    for query in ([chan] if chan else []) + ([title] if title else []):
        rows += videos_from_search(query, seen, 12 - len(rows))
        if len(rows) >= 12:
            break
    feed_cache[ck] = (time.time(), rows)
    return rows


def account_sections():
    jobs = [
        ("Iscrizioni", "https://www.youtube.com/feed/subscriptions", 1, 15),
        ("Iscrizioni", "https://www.youtube.com/feed/subscriptions", 16, 15),
        ("Iscrizioni", "https://www.youtube.com/feed/subscriptions", 31, 15),
        ("Più tardi", "https://www.youtube.com/playlist?list=WL", 1, 12),
        ("Mi piace", "https://www.youtube.com/playlist?list=LL", 1, 12),
    ]
    out = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {pool.submit(flat_rows, url, n, start): name for name, url, start, n in jobs}
        for fut in futs:
            name = futs[fut]
            try:
                rows = fut.result()
            except Exception as e:
                print("elenco home non riuscito:", e, flush=True)
                rows = []
            got = out.setdefault(name, [])
            have = set(v for v, t in got)
            for vid, title in rows:
                if vid not in have:
                    got.append((vid, title))
                    have.add(vid)
    return [(name, out.get(name) or []) for name in ("Iscrizioni", "Più tardi", "Mi piace")]


def cards(rows, seen, cur):
    html = []
    for vid, title in rows:
        if not vid or vid == cur or vid in seen:
            continue
        seen.add(vid)
        html.append(
            '<a class="card" href="/watch?v=%s"><img src="https://i.ytimg.com/vi/%s/hqdefault.jpg"><span>%s</span></a>'
            % (vid, vid, title.replace("<", ""))
        )
    return html


def channel_videos(cid, name=None):
    if cid.startswith('http'):
        url = cid.rstrip('/') + '/videos'
    elif cid.startswith('@'):
        url = 'https://www.youtube.com/' + cid + '/videos'
    else:
        url = 'https://www.youtube.com/channel/' + cid + '/videos'
    rows = flat_rows(url, 24)
    # pagina canale bloccata dall'IP (youtube:tab): ripieghiamo sulla ricerca
    if not rows and name:
        rows = videos_from_search(name, None, 12)
    return rows
# ================= sessioni =================
# Ogni browser ha la sua sessione: video, impostazioni, ffmpeg, code e file di
# scambio. Ogni richiesta HTTP lega la sua sessione al thread che la serve, e
# i thread avviati con sthread() ereditano la sessione di chi li avvia: cosi'
# state[...], lock e cur() si riferiscono sempre alla sessione giusta.
_tls = threading.local()
_bprint = builtins.print


# Privacy: nel log non finisce mai cosa guarda o cerca qualcuno. Indirizzi,
# id dei video, ricerche e titoli vengono tolti prima di scrivere.
_PRIV = [
    (re.compile(r"(?:https?://|www\.)\S+"), "<url>"),
    (re.compile(r"\b(?:youtube\.com|youtu\.be|googlevideo\.com)/\S*"), "<url>"),
    (re.compile(r"(\[youtube[^\]]*\]) [^:\s]+:"), r"\1 <video>:"),
    (re.compile(r"\bytsearch\d*:.*"), "ytsearch:<ricerca>"),
    (re.compile(r"\b(v|watch|list|q|search_query)=[^\s&]+"), r"\1=<...>"),
]


def privacy(text):
    for rx, sub in _PRIV:
        text = rx.sub(sub, text)
    return text


def print(*a, **k):
    # nel log ogni riga di una sessione porta il suo nome (codice, non il video)
    s = getattr(_tls, "s", None)
    line = privacy(" ".join(str(x) for x in a))
    if s is not None:
        line = "[%s] %s" % (s.tag, line)
    k.pop("sep", None)
    _bprint(line, **k)


def new_state():
    return {
        "seek_base": 0.0,
        "t_play": None,
        "paused": False,
        "gen": 0,
        "ff": None,
        "has_audio": False,
        "duration": 0,
        "v_q": deque(),
        "a_q": deque(),
        "v_evt": threading.Event(),
        "a_evt": threading.Event(),
        "ff_gen": -1,
        "ff_done": -1,
        "vmode": "mjpeg",
        "v_maxpts": None,
        "v_made": 0,
        "a_read": 0,
        "fails": 0,
        "rate": 0,
        "is_live": False,
    }


class Session:
    def __init__(self, sid, who):
        self.sid = sid
        self.who = who
        self.tag = "%s:%s" % (who, sid[:4])
        self.lock = threading.Lock()
        self.url = ""
        self.cfg = dict(DEFAULTS)
        self.created = self.seen = time.time()
        base = "/tmp/yt_%d_%s" % (PORT, sid[:16])
        self.fifo_v = base + "_v.fifo"
        self.fifo_a = base + "_a.fifo"
        self.state = new_state()


SESSIONS = {}
SESSIONS_LOCK = threading.Lock()
OWNER = {}            # codice -> sid del browser che lo sta usando
MAX_PER_CODE = 1      # un codice = una sessione: chi lo apre altrove chiude questa
MAX_SESSIONS = 10     # sessioni in tutto
IDLE_CLOSE = 3 * 3600  # una sessione ferma da 3 ore viene chiusa


def cur():
    s = getattr(_tls, "s", None)
    if s is None:
        raise RuntimeError("nessuna sessione legata a questo thread")
    return s


def bind(s):
    _tls.s = s


def sthread(target, *args):
    """Thread che lavora sulla stessa sessione di chi lo avvia."""
    s = getattr(_tls, "s", None)

    def run():
        _tls.s = s
        target(*args)
    return threading.Thread(target=run, daemon=True)


class _StateProxy:
    def __getitem__(self, k):
        return cur().state[k]

    def __setitem__(self, k, v):
        cur().state[k] = v

    def get(self, k, d=None):
        return cur().state.get(k, d)

    def __contains__(self, k):
        return k in cur().state


class _LockProxy:
    def acquire(self, *a, **k):
        return cur().lock.acquire(*a, **k)

    def release(self):
        return cur().lock.release()

    def __enter__(self):
        cur().lock.acquire()
        return self

    def __exit__(self, *a):
        cur().lock.release()


state = _StateProxy()
lock = _LockProxy()


def get_session(sid, who):
    drop = []
    with SESSIONS_LOCK:
        s = SESSIONS.get(sid)
        if s is not None and s.who != who:
            # stesso browser entrato con un altro codice: sessione nuova
            drop.append(SESSIONS.pop(sid))
            s = None
        if s is None:
            mine = sorted((x for x in SESSIONS.values() if x.who == who), key=lambda x: x.seen)
            while len(mine) >= MAX_PER_CODE:
                old = mine.pop(0)
                drop.append(SESSIONS.pop(old.sid))
            others = sorted(SESSIONS.values(), key=lambda x: x.seen)
            while len(others) >= MAX_SESSIONS:
                old = others.pop(0)
                drop.append(SESSIONS.pop(old.sid))
            s = Session(sid, who)
            SESSIONS[sid] = s
            _bprint("nuova sessione", s.tag, "(%d attive)" % len(SESSIONS), flush=True)
        s.seen = time.time()
    for d in drop:
        close_session(d)
    return s


def close_session(s):
    with SESSIONS_LOCK:
        if OWNER.get(s.who) == s.sid and s.sid not in SESSIONS:
            OWNER.pop(s.who, None)
    prev = getattr(_tls, "s", None)
    bind(s)
    try:
        with lock:
            state["gen"] += 1
            stop_ff()
        for path in (s.fifo_v, s.fifo_a):
            try:
                os.remove(path)
            except OSError:
                pass
        print("sessione chiusa", flush=True)
    finally:
        _tls.s = prev


def stop_all():
    with SESSIONS_LOCK:
        all_s = list(SESSIONS.values())
    for s in all_s:
        try:
            close_session(s)
        except Exception:
            pass


def janitor():
    while True:
        time.sleep(60)
        now = time.time()
        with SESSIONS_LOCK:
            old = [x for x in SESSIONS.values()
                   if now - x.seen > IDLE_CLOSE and not (x.state["ff"] and x.state["ff"].poll() is None)]
            for x in old:
                SESSIONS.pop(x.sid, None)
        for x in old:
            close_session(x)


def next_of_current():
    url = cur().url
    if not url or "v=" not in url:
        return []
    here = url.split("v=")[-1][:11]
    rows = [(vid, title) for vid, title in related_of(here) if vid != here]
    if len(rows) < 4:
        try:
            p = subprocess.run(
                yt_cmd("--print", "%(channel_id)s", "--no-playlist", url),
                text=True, capture_output=True, timeout=20,
            )
            cid = ""
            for line in p.stdout.splitlines():
                line = line.strip()
                if line.startswith("UC"):
                    cid = line
                    break
            if cid:
                for vid, title in channel_videos(cid):
                    if vid != here and vid not in [x[0] for x in rows]:
                        rows.append((vid, title))
                    if len(rows) >= 8:
                        break
        except Exception as e:
            print("next canale", e, flush=True)
    return [{"id": vid, "title": title} for vid, title in rows[:8]]


def fetch_duration():
    url = cur().url
    if state["duration"] or state.get("is_live") or not url:
        return
    p = subprocess.run(
        yt_cmd("--print", "%(duration)s\t%(is_live)s\t%(live_status)s", "--no-playlist", url),
        text=True,
        capture_output=True,
    )
    if cur().url != url:
        return  # nel frattempo e' stato aperto un altro video
    for line in reversed(p.stdout.splitlines()):
        parts = line.strip().split("\t")
        dur_s = parts[0] if parts else ""
        live_s = parts[1].strip().lower() if len(parts) > 1 else ""
        live_status = parts[2].strip().lower() if len(parts) > 2 else ""
        print("fetch_duration raw:", repr(line.strip()), flush=True)
        if live_s in ("true", "1") or live_status == "is_live":
            state["is_live"] = True
            print("live stream rilevato", flush=True)
            return
        try:
            state["duration"] = int(float(dur_s))
            print("duration", state["duration"], flush=True)
            return
        except ValueError:
            continue
    print("duration sconosciuta", p.stderr[-400:], flush=True)


def now_pos():
    if state["paused"] or state["t_play"] is None:
        return state["seek_base"]
    return max(0.0, state["seek_base"] + (time.perf_counter() - state["t_play"]))


def h264_height():
    return min(C("HEIGHT"), 720)


def ytdlp_format():
    if state["vmode"] == "h264":
        # solo H.264 (avc1), video e audio separati; mai sopra i 720p
        h = h264_height()
        return "bv*[vcodec^=avc1][height<=%d]+ba/bv*[vcodec^=avc1][height<=720]+ba" % h
    return "bv*[height<=720]+ba/bv*+ba/b"


def ytdlp_urls():
    p = subprocess.run(
        yt_cmd(
            "-g",
            "--no-playlist",
            "--user-agent",
            UA,
            "-f",
            ytdlp_format(),
            cur().url,
        ),
        text=True,
        capture_output=True,
    )
    if p.returncode != 0:
        err = p.stderr or ""
        print(err, flush=True)
        if "Sign in to confirm" in err or "not a bot" in err:
            state["err"] = "Verifica anti-bot di YouTube: controlla il PO token nella pagina admin."
        elif state["vmode"] == "h264":
            # il client passa da solo a MJPEG quando vede questo errore
            state["err"] = "H264 non disponibile per questo video"
        else:
            state["err"] = "yt-dlp fallito. Controlla il log."
        raise RuntimeError(state["err"])
    lines = [x for x in p.stdout.strip().splitlines() if x.startswith("http")]
    if not lines:
        state["err"] = "Nessuno stream: video bloccato o non disponibile."
        raise RuntimeError(state["err"])
    return lines


def client_gone(handler):
    try:
        ready, _, _ = select.select([handler.connection], [], [], 0)
        if not ready:
            return False
        peek = socket.socket.recv(handler.connection, 1, socket.MSG_PEEK | socket.MSG_DONTWAIT)
        return peek == b""
    except BlockingIOError:
        return False
    except Exception:
        return True


def arm_stop(reason):
    print(reason, "- ffmpeg resta aperto 8s", flush=True)
    state["hold_until"] = time.time() + 8
    def later():
        time.sleep(8)
        with lock:
            due = state.get("hold_until") or 0
            if not due or time.time() + 0.3 < due:
                return
            state["hold_until"] = 0
            state["gen"] += 1
            stop_ff()
            print("ffmpeg spento, nessuno in ascolto", flush=True)
    sthread(later).start()


def cancel_stop():
    if state.get("hold_until"):
        print("ffmpeg tenuto aperto", flush=True)
    state["hold_until"] = 0


def ffmpeg_alive():
    p = state.get("ff")
    return bool(p and p.poll() is None)


def stop_ff():
    p = state["ff"]
    if p and p.poll() is None:
        p.kill()
        try:
            p.wait(timeout=2)
        except Exception:
            pass
    state["ff"] = None
    state["rate"] = 0
    state["v_q"].clear()
    state["a_q"].clear()
    state["v_evt"].clear()
    state["a_evt"].clear()


def ensure_fifo(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    os.mkfifo(path)


def unblock_fifo(path):
    # se un pump e' fermo su open() perche' ffmpeg e' morto prima di aprire
    # l'uscita, apro e chiudo il lato scrittura: il pump riceve EOF ed esce
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
        os.close(fd)
    except OSError:
        pass


V_CAP = 6000


def pump_jpegs(gen, path):
    try:
        with open(path, "rb") as fh:
            buf = b""
            nbytes = 0
            idle = 0.0
            t0 = time.perf_counter()
            while state["gen"] == gen:
                if len(state["v_q"]) >= V_CAP:
                    # coda piena: smetto di leggere e ffmpeg si ferma da solo.
                    # Prima qui si scartavano frame e il video saltava avanti.
                    time.sleep(0.1)
                    idle += 0.1
                    continue
                chunk = fh.read(65536)
                if not chunk:
                    break
                nbytes += len(chunk)
                now = time.perf_counter()
                if now - t0 >= 5:
                    state["rate"] = nbytes / max(0.5, now - t0 - idle) / 1e6
                    print("video %.1f MB/s coda %d frame" % (state["rate"], len(state["v_q"])), flush=True)
                    nbytes = 0
                    idle = 0.0
                    t0 = now
                buf += chunk
                while True:
                    s = buf.find(b"\xff\xd8")
                    e = buf.find(b"\xff\xd9", s + 2) if s != -1 else -1
                    if s == -1 or e == -1:
                        if s > 0:
                            buf = buf[s:]
                        break
                    state["v_q"].append(buf[s : e + 2])
                    state["v_made"] += 1
                    buf = buf[e + 2 :]
                    state["v_evt"].set()
    except Exception as e:
        print("pump video", e, flush=True)


class TSVideo:
    """Estrae i frame video (PES) da uno stream MPEG-TS: (pts in secondi, dati Annex-B)."""

    def __init__(self):
        self.buf = b""
        self.pid = None
        self.cur = None
        self.pts = None

    def feed(self, chunk):
        out = []
        buf = self.buf + chunk
        i, n = 0, len(buf)
        while n - i >= 188:
            if buf[i] != 0x47:
                j = buf.find(b"\x47", i + 1)
                if j < 0:
                    i = n
                    break
                i = j
                continue
            b1, b2, b3 = buf[i + 1], buf[i + 2], buf[i + 3]
            pusi = b1 & 0x40
            pid = ((b1 & 0x1F) << 8) | b2
            afc = (b3 >> 4) & 3
            p = i + 4
            if afc & 2:
                p += 1 + buf[i + 4]
            end = i + 188
            i = end
            if not (afc & 1) or p >= end:
                continue
            if self.pid is not None and pid != self.pid:
                continue
            if pusi and buf[p:p + 3] == b"\x00\x00\x01" and (buf[p + 3] & 0xF0) == 0xE0:
                if self.pid is None:
                    self.pid = pid
                if self.cur is not None:
                    out.append((self.pts, bytes(self.cur)))
                flags, hlen = buf[p + 7], buf[p + 8]
                pts = None
                if flags & 0x80:
                    t = buf[p + 9:p + 14]
                    pts = (((t[0] >> 1) & 7) << 30 | t[1] << 22 | (t[2] >> 1) << 15
                           | t[3] << 7 | t[4] >> 1) / 90000.0
                self.pts = pts
                self.cur = bytearray(buf[p + 9 + hlen:end])
            elif self.cur is not None and pid == self.pid:
                self.cur += buf[p:end]
        self.buf = buf[i:]
        return out

    def finish(self):
        out = [(self.pts, bytes(self.cur))] if self.cur is not None else []
        self.cur = None
        return out


def pump_h264(gen, path):
    # legge l'MPEG-TS di ffmpeg (video H.264 in copia, tempi originali con -copyts)
    # e mette in coda un record per frame: [lunghezza u32][pts f64][dati Annex-B]
    try:
        ts = TSVideo()
        last = None
        with open(path, "rb") as fh:
            nbytes = 0
            idle = 0.0
            t0 = time.perf_counter()
            while state["gen"] == gen:
                if len(state["v_q"]) >= V_CAP:
                    time.sleep(0.1)
                    idle += 0.1
                    continue
                chunk = fh.read(65536)
                if not chunk:
                    break
                nbytes += len(chunk)
                now = time.perf_counter()
                if now - t0 >= 5:
                    state["rate"] = nbytes / max(0.5, now - t0 - idle) / 1e6
                    print("video h264 %.2f MB/s coda %d frame" % (state["rate"], len(state["v_q"])), flush=True)
                    nbytes = 0
                    idle = 0.0
                    t0 = now
                for pts, data in ts.feed(chunk):
                    last = h264_push(pts, data, last)
            if state["gen"] == gen:
                for pts, data in ts.finish():
                    last = h264_push(pts, data, last)
    except Exception as e:
        print("pump h264", e, flush=True)


def h264_push(pts, data, last):
    if pts is None:
        pts = (last or 0.0) + 1.0 / 30
    state["v_q"].append(struct.pack("<Id", len(data), pts) + data)
    state["v_made"] += 1
    mx = state.get("v_maxpts")
    if mx is None or pts > mx:
        state["v_maxpts"] = pts
    state["v_evt"].set()
    return pts


def pump_audio(gen, path, skip=0):
    try:
        with open(path, "rb") as fh:
            n = 0
            while state["gen"] == gen:
                chunk = fh.read(4096)
                if not chunk:
                    break
                n += len(chunk)
                state["a_read"] = n
                if skip > 0:
                    if len(chunk) <= skip:
                        skip -= len(chunk)
                        continue
                    chunk = chunk[skip:]
                    skip = 0
                state["a_q"].append(chunk)
                state["a_evt"].set()
            print("pump audio end (%d bytes)" % n, flush=True)
    except Exception as e:
        print("pump audio", e, flush=True)


def ffmpeg_cmd(vsrc, asrc, pos):
    net = [
        "-user_agent", UA, "-headers", HEADERS,
        "-ss", "%.3f" % pos, "-readrate", "16",
        "-reconnect", "1", "-reconnect_streamed", "1",
        "-reconnect_on_network_error", "1",
    ]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"] + net + ["-i", vsrc]
    if state["vmode"] == "h264":
        # video in copia, nessuna ricodifica: parte dal keyframe prima di pos e,
        # con -copyts, ogni frame tiene il suo tempo originale (il client scarta
        # i frame prima di pos dopo averli decodificati)
        vout = [
            "-map", "0:v:0", "-an", "-c:v", "copy",
            "-f", "mpegts", "-muxdelay", "0", "-muxpreload", "0", cur().fifo_v,
        ]
    else:
        vout = [
            "-map", "0:v:0", "-an",
            "-vf", "scale=-2:%d" % C("HEIGHT"), "-r", str(C("FPS")), "-q:v", str(C("Q")),
            "-f", "mjpeg", cur().fifo_v,
        ]
    if state["vmode"] == "h264":
        vout = ["-copyts"] + vout
    if not asrc:
        return cmd + vout
    if asrc == vsrc:
        amap = "0:a:0"
    else:
        cmd += net + ["-i", asrc]
        amap = "1:a:0"
    # niente tag ID3 ne' header Xing: lo stream audio si puo' riprendere a meta'
    aout = [
        "-map", amap, "-vn",
        "-c:a", "libmp3lame", "-b:a", "%dk" % C("AUDIO_KBPS"), "-ar", "48000",
        "-id3v2_version", "0", "-write_xing", "0",
        "-f", "mp3", cur().fifo_a,
    ]
    return cmd + vout + aout


def read_err(p, tail):
    n = 0
    try:
        for raw in p.stderr:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            tail.append(line)
            n += 1
            if n <= 20:
                print("ff:", line[:240], flush=True)
    except Exception:
        pass


def spawn_ff(gen, pos, vsrc, asrc, askip=0):
    ensure_fifo(cur().fifo_v)
    ensure_fifo(cur().fifo_a)
    state["v_made"] = 0
    state["a_read"] = 0
    state["urls"] = (vsrc, asrc)
    tail = deque(maxlen=12)
    p = subprocess.Popen(
        ffmpeg_cmd(vsrc, asrc, pos),
        stdin=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    state["ff"] = p
    print("ffmpeg %s @ %.1fs url=%d" % (state["vmode"], pos, 1 if asrc == vsrc else 2), flush=True)
    pump = pump_h264 if state["vmode"] == "h264" else pump_jpegs
    threads = [sthread(pump, gen, cur().fifo_v)]
    if asrc:
        threads.append(sthread(pump_audio, gen, cur().fifo_a, askip))
    for t in threads:
        t.start()
    sthread(read_err, p, tail).start()
    sthread(watch_ff, gen, p, pos, threads, tail).start()


def watch_ff(gen, p, pos, threads, tail):
    # Sorveglia ffmpeg. Se si chiude prima della fine del video (YouTube che
    # taglia la connessione, URL scaduto, 403) riparte dal punto esatto in cui
    # si era fermato, senza svuotare le code: il browser non se ne accorge.
    p.wait()
    unblock_fifo(cur().fifo_v)
    unblock_fifo(cur().fifo_a)
    for t in threads:
        while t.is_alive() and state["gen"] == gen:
            t.join(timeout=1)
    with lock:
        if state["gen"] != gen or state["ff"] is not p:
            return
        if state["vmode"] == "h264":
            # tempo dell'ultimo frame arrivato (vale anche tra un riavvio e l'altro)
            mx = state.get("v_maxpts")
            made = max(pos, mx + 1.0 / 30) if mx is not None else pos
        else:
            made = pos + state["v_made"] / float(C("FPS"))
        apos = pos + state["a_read"] * 8.0 / (C("AUDIO_KBPS") * 1000)
        dur = state["duration"] or 0
        rc = p.returncode
        is_live = state.get("is_live")
        if rc == 0 and not is_live and (not dur or made >= dur - 3 or (state["v_made"] == 0 and pos >= dur - 30)):
            state["ff_done"] = gen
            print("ffmpeg finito a %.0fs" % made, flush=True)
            return
        print("ffmpeg interrotto a %.1fs su %ds (codice %s, %d frame)" % (made, dur, rc, state["v_made"]), flush=True)
        for line in list(tail)[-3:]:
            print("  ff:", line[:240], flush=True)
        if made - pos >= 10:
            state["fails"] = 0
        else:
            state["fails"] = state.get("fails", 0) + 1
        fails = state["fails"]
        if fails >= 6:
            state["err"] = "403 dopo %d tentativi: YouTube interrompe lo stream" % fails
            print(state["err"], flush=True)
            return
        urls = state.get("urls")
        state["ff"] = None
    time.sleep(min(fails, 5) + 0.5)
    if fails >= 2 or not urls:
        try:
            lst = ytdlp_urls()
        except Exception as e:
            print("riavvio fallito:", e, flush=True)
            return
        urls = (lst[0], lst[1] if len(lst) > 1 else lst[0])
    with lock:
        if state["gen"] != gen or state["ff"] is not None:
            return
        askip = int(max(0.0, apos - made) * C("AUDIO_KBPS") * 1000 / 8)
        print("riparto da %.1fs (audio avanti %.2fs)" % (made, max(0.0, apos - made)), flush=True)
        spawn_ff(gen, made, urls[0], urls[1], askip)


def start_ff():
    # una sola partenza per sessione: un secondo /stream.mp3 (o un ffmpeg gia'
    # finito) non deve svuotare le code ne' rilanciare ffmpeg
    gen = state["gen"]
    if state.get("ff_gen") == gen:
        # un'altra richiesta (di solito /stream.h264) sta gia' avviando ffmpeg e
        # durante yt-dlp ha lasciato il lock: si aspetta che abbia finito. Prima
        # si tornava subito e /stream.mp3 rispondeva "no audio": al primo video
        # di ogni sessione si vedeva l'immagine senza suono
        t0 = time.time()
        while (state.get("ff_started") != gen and state.get("ff_gen") == gen
               and state["gen"] == gen and time.time() - t0 < 90):
            lock.release()
            time.sleep(0.05)
            lock.acquire()
        return
    stop_ff()
    state["ff_gen"] = gen
    state["fails"] = 0
    state["v_maxpts"] = None
    pos = int(now_pos())
    lock.release()
    try:
        urls = ytdlp_urls()
    except Exception:
        lock.acquire()
        if state["gen"] == gen:
            state["ff_gen"] = -1
        raise
    lock.acquire()
    if state["gen"] != gen:
        return
    vsrc = urls[0]
    asrc = urls[1] if len(urls) > 1 else urls[0]
    state["has_audio"] = True
    state["seek_base"] = float(pos)
    state["t_play"] = time.perf_counter()
    state["paused"] = False
    state["err"] = ""
    spawn_ff(gen, pos, vsrc, asrc)
    state["ff_started"] = gen


def new_session(pos=None, vmode=None):
    with lock:
        if vmode in ("h264", "mjpeg"):
            state["vmode"] = vmode
        if pos is not None:
            state["seek_base"] = max(0.0, pos)
        state["t_play"] = None
        state["paused"] = False
        state["err"] = ""
        state["gen"] += 1
        stop_ff()


HTML = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>yt</title>
<style>
  html,body{margin:0;height:100%;background:#000;color:#fff;font-family:sans-serif}
  #screen,#hscreen{width:100%;height:100%;object-fit:contain;background:#000;display:none}
  #overlay{
    position:fixed;inset:0;display:flex;align-items:center;justify-content:center;
    background:rgba(0,0,0,.72);z-index:3;color:#fff;flex-direction:column;
  }
  #msg{font-size:28px;margin-bottom:24px;text-align:center;white-space:pre-line;line-height:1.35}
  #hud{position:fixed;top:14px;left:0;right:0;text-align:center;z-index:7;font-size:20px;color:#fff;text-shadow:0 1px 4px #000;pointer-events:none}
  #go{
    width:160px;height:160px;border-radius:50%;background:#fff;color:#000;border:0;
    font-size:72px;display:none;
  }
  #prog{position:fixed;left:12px;right:12px;bottom:24px;height:28px;background:transparent;z-index:4}
  #track{position:absolute;left:0;right:0;top:11px;height:6px;background:#333}
  #fill{display:block;height:100%;width:0;background:#fff}
  #time{position:fixed;right:12px;bottom:40px;font-size:14px;z-index:4}
  #log{display:none}
  #menu,#homebtn{
    position:fixed;top:12px;z-index:30;width:48px;height:48px;border:0;border-radius:8px;
    background:rgba(0,0,0,.55);color:#fff;font-size:26px;
  }
  #menu{left:12px}
  #homebtn{right:12px;display:none;text-decoration:none;text-align:center;line-height:48px}
  #side{
    position:fixed;top:0;left:0;bottom:0;width:440px;max-width:94vw;background:#141414;z-index:20;
    transform:translateX(-110%);transition:transform .2s;display:flex;flex-direction:column;
    pointer-events:none;
  }
  #side.on{transform:translateX(0);pointer-events:auto}
  #panel{flex:1;overflow:auto;padding:14px 14px 24px}
  #panel-title{font-size:22px;font-weight:bold;margin:0 0 10px;padding-bottom:10px;border-bottom:1px solid #333}
  .row{display:flex;align-items:center;gap:8px;padding:10px 0;border-bottom:1px solid #333}
  .row p{margin:0;flex:1}
  .row b{display:block}
  .row small{color:#aaa}
  .row i{min-width:64px;text-align:center;font-style:normal;font-size:20px}
  .row button{width:40px;height:40px;border:0;border-radius:8px;background:#333;color:#fff;font-size:22px}
  .row button.wide{width:auto;padding:0 14px;font-size:16px}
  .row2{padding:10px 0;border-bottom:1px solid #333}
  .row2 p{margin:0 0 8px}
  .row2 b{display:block}
  .row2 small{color:#aaa}
  .opts{display:flex;flex-wrap:wrap;gap:6px}
  .opts button{min-width:56px;height:40px;padding:0 12px;border:0;border-radius:8px;background:#333;color:#fff;font-size:16px}
  .opts button.on{background:#fff;color:#000}
  #resetcfg{width:100%;height:44px;margin-top:10px;border:0;border-radius:8px;background:#333;color:#fff;font-size:16px}
  #logbox{white-space:pre-wrap;font:12px/1.4 monospace;color:#ddd}
  #nexts a, #nexts a:visited, #nexts a:hover{color:#fff;text-decoration:none}
  #savecfg{width:100%;height:48px;margin-top:16px;border:0;border-radius:8px;background:#fff;color:#000;font-size:18px}
  #freeze{position:fixed;inset:0;width:100%;height:100%;display:none;z-index:2;background:#000}
  #pp{
    position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:11;display:none;
    width:150px;height:150px;border-radius:50%;border:3px solid rgba(255,255,255,.85);
    background:rgba(0,0,0,.55);color:#fff;font-size:64px;line-height:1;padding:0;
  }
</style>
<img id="v" alt="">
<canvas id="screen"></canvas>
<canvas id="hscreen"></canvas>
<canvas id="freeze"></canvas>
<div id="hud"></div>
<div id="overlay">
  <div id="msg">Loading...</div>
  <div id="ver" style="position:fixed;left:12px;bottom:12px;z-index:40;color:#aaa;font-size:13px">__VER__</div>
<button id="go" type="button">&#9654;</button>
</div>
<button id="pp" type="button">&#10074;&#10074;</button>
<div id="prog"><div id="track"><i id="fill"></i></div></div>
<div id="time"></div>
<button id="menu" type="button">&#9881;</button>
<a id="homebtn" href="/home">&#8962;</a>
<div id="side">
  <div id="panel"></div>
</div>
<div id="log"></div>
<div id="nexts" style="display:none;position:fixed;left:8px;right:8px;bottom:58px;z-index:12;gap:8px;overflow:auto;background:rgba(0,0,0,.55);padding:8px"></div>
<script>
var DURATION = __DUR__;
var FPS = __FPS__;
var VID = 'WATCHID';
var IS_LIVE = __LIVE__ === 1;
var BUF_SEC = 15;
var BUF_CAP = 6000;
var rateMb = 0;
var serverFrames = 0;
var syncOrigin = 0;
var playOrigin = 0;
var stalled = false;
var startAt = __START__;
var pos = startAt;
var playing = false;
var tick = null;
var gen = 0;
var warmV = null;
var warmA = null;
var screen = document.getElementById('screen');
var vAbort = null;
var frames = [];
var playTimer = null;
var vUrl = null;
var freezeC = document.getElementById('freeze');
var overlay = document.getElementById('overlay');
var actx = null;
var audioAbort = null;
var audioNext = 0;
var audioOn = false;
var audioCursor = 0;
var msg = document.getElementById('msg');
var go = document.getElementById('go');
var ppLabel = 'pausa';
var audioBase = 0;
var shownFrames = 0;
var drawnT = 0;
var painting = false;
var paintGen = 0;
var frameBase = 0;
var vEnded = false;
// il codice e' stato aperto su un altro schermo: qui ci si ferma
var kickedShown = false;
function showKicked(){
  if (kickedShown) return;
  kickedShown = true;
  gen++;
  if (fillWait) clearInterval(fillWait);
  stopVideo();
  stopAudio();
  stopClock();
  overlay.style.display = 'flex';
  msg.style.display = 'block';
  go.style.display = 'none';
  msg.innerHTML = 'Questo codice è in uso su un altro schermo.<br><br>' +
    '<button id="takeback" type="button" style="font-size:24px;padding:12px 22px;border:0;border-radius:10px;background:#fff;color:#000">Guarda qui</button>';
  document.getElementById('takeback').onclick = function(ev){ ev.stopPropagation(); location.reload(); };
}
function ck(r){
  if (r && r.status === 409) { showKicked(); throw new Error('in uso altrove'); }
  return r;
}
function statusLine(){
  if (vmode === 'h264' && hs) {
    return 'H.264' + (rateMb ? ' ' + rateMb.toFixed(2) + ' MB/s' : '') + NL + 'video pronto ' + Math.max(0, hAhead()).toFixed(1) + ' s';
  }
  var slow = rateMb > 0 && rateMb < 1;
  var head = slow ? ('Linea lenta ' + rateMb.toFixed(2) + ' MB/s') : (rateMb ? (rateMb.toFixed(2) + ' MB/s') : 'misuro la linea');
  return head + '\\n' + 'browser ' + frames.length + ' frame' + '\\n' + 'server ' + serverFrames + ' frame';
}
function showStatus(extra){
  overlay.style.display = 'flex';
  msg.style.display = 'block';
  go.style.display = 'none';
  msg.textContent = statusLine() + (extra ? '\\n' + extra : '');
}
var rateAt = 0;
function pollRate(){
  var now = Date.now();
  if (now - rateAt < 1000) return;
  rateAt = now;
  fetch('/buf').then(ck).then(function(r){ return r.text(); }).then(function(t){
    var p = t.split(' ');
    var bits = (p[0] || '').split('/');
    serverFrames = parseInt(bits[0], 10) || 0;
    var v = parseFloat(p[1]);
    if (!isNaN(v) && v > 0) rateMb = v;
    var hud = document.getElementById('hud');
    if (hud) {
      var desync = '';
      if (actx && audioArm && drawnT > 0) {
        var ear = earTime();
        var diff = ear - drawnT;
        if (Math.abs(diff) > 0.15) desync = (diff > 0 ? '  audio avanti ' : '  video avanti ') + Math.abs(diff).toFixed(1) + 's';
        else desync = '  sync ok';
      }
      var fps = '';
      if (vmode === 'h264' && hs && hs.started && !userPaused && !stalled) {
        fps = '  ' + hs.fps.toFixed(0) + ' fps (video ' + (1 / hs.dur).toFixed(0) + ')' + (hs.lateNow ? '  saltati ' + hs.lateNow : '');
      }
      var au = '  audio ' + (actx ? actx.state : 'assente') + ' ' + aDiag.kb.toFixed(0) + 'KB ' + aDiag.blk + 'blk ' + aDiag.sched + 'prog' + (aDiag.err ? ' ' + aDiag.err + 'err' : '') + (audioHold.length ? ' attesa ' + audioHold.length : '');
      hud.textContent = (vmode === 'h264' && hs ? 'H.264 pronto ' + Math.max(0, hAhead()).toFixed(0) + ' s' : 'browser ' + frames.length) + fps + '  server ' + serverFrames + (rateMb ? '  ' + rateMb.toFixed(2) + ' MB/s' : '') + desync + au;
    }
  }).catch(function(){});
}
function ui(line){
  try { fetch('/clog?m=' + encodeURIComponent(String(line).slice(0, 200))).catch(function(){}); } catch (e) {}
  var box = document.getElementById('log');
  var row = document.createElement('div');
  row.textContent = line;
  box.appendChild(row);
  while (box.childNodes.length > 8) box.removeChild(box.childNodes[1]);
}

function fmt(s){
  s = Math.max(0, Math.floor(s));
  var m = Math.floor(s/60), r = s%60;
  return m + ':' + (r<10?'0':'') + r;
}
function draw(){
  if (IS_LIVE) {
    document.getElementById('prog').style.display = 'none';
    document.getElementById('time').textContent = '\\u25cf LIVE';
    document.getElementById('time').style.color = '#f00';
    return;
  }
  var p = DURATION ? Math.min(100, (pos/DURATION)*100) : 0;
  document.getElementById('fill').style.width = p + '%';
  document.getElementById('time').textContent = fmt(pos) + ' / ' + fmt(DURATION);
}
function pollDur(){
  if (DURATION || IS_LIVE) return;
  fetch('/dur').then(function(r){ return r.text(); }).then(function(t){
    var d = parseInt(t, 10) || 0;
    if (d === -1) { IS_LIVE = true; draw(); }
    else if (d > 0) { DURATION = d; draw(); }
    else setTimeout(pollDur, 1500);
  }).catch(function(){ setTimeout(pollDur, 3000); });
}
pollDur();
function startClock(){
  playing = true;
  if (tick) clearInterval(tick);
  tick = setInterval(function(){
    if (!playing) return;
    if (!IS_LIVE && DURATION && pos > DURATION) pos = DURATION;
    draw();
    if (!IS_LIVE) history.replaceState(null, '', '/?watch=' + VID + '&t=' + Math.floor(pos));
  }, 1000);
}
function stopClock(){ playing = false; }
function stopWarm(){
  if (warmV) { warmV.abort(); warmV = null; }
  if (warmA) { warmA.abort(); warmA = null; }
}
function preload(auto){
  var my = ++gen;
  stopWarm();
  overlay.style.display = 'flex';
  msg.style.display = 'block';
  msg.textContent = 'Loading...';
  go.style.display = 'none';
  screen.style.display = 'none';
  stopVideo();
  stopAudio();
  stopClock();
  draw();
  fetch('/prime').catch(function(){});
  function poll(){
    if (my !== gen) return;
    fetch('/status').then(ck).then(function(r){ return r.text(); }).then(function(err){
      if (my !== gen) return;
      if (err && (err.indexOf('Verifica') === 0 || err.indexOf('yt-dlp') === 0 || err.indexOf('Nessuno stream') === 0 || err.indexOf('403 dopo') === 0)) {
        msg.textContent = err;
        go.style.display = 'block';
        ui(err);
        return;
      }
      if (err) { setTimeout(poll, 800); return; }
      fetch('/ready').then(function(r){ return r.text(); }).then(function(t){
        if (my !== gen) return;
        if (t === 'ok') { msg.textContent = 'In attesa del play'; go.style.display = 'block'; }
        else setTimeout(poll, 800);
      });
    }).catch(function(){ if (my === gen) setTimeout(poll, 1000); });
  }
  poll();
}
function stopVideo(){
  hStop();
  paintGen++;
  painting = false;
  if (vAbort) { vAbort.abort(); vAbort = null; }
  if (playTimer) { clearInterval(playTimer); playTimer = null; }
  frames = [];
  vEnded = false;
  if (vUrl) { URL.revokeObjectURL(vUrl); vUrl = null; }
}
function findBytes(u8, a, b, from){
  for (var i = from || 0; i < u8.length - 1; i++)
    if (u8[i] === a && u8[i + 1] === b) return i;
  return -1;
}
function startVideoBuf(){
  stopVideo();
  var ctrl = new AbortController();
  vAbort = ctrl;
  var my = gen;
  var pending = new Uint8Array(0);
  var started = false;
  screen.style.display = 'none';
  fetch('/stream.mjpg?t=' + Date.now(), {signal: ctrl.signal}).then(ck).then(function(res){
    var reader = res.body.getReader();
    function read(){
      return reader.read().then(function(r){
        if (r.done && !ctrl.signal.aborted && my === gen) { vEnded = true; return; }
        if (r.done || ctrl.signal.aborted || my !== gen) return;
        pending = concatU8(pending, r.value);
        while (true) {
          var s = findBytes(pending, 0xff, 0xd8, 0);
          var e = s < 0 ? -1 : findBytes(pending, 0xff, 0xd9, s + 2);
          if (s < 0 || e < 0) {
            if (s > 0) pending = pending.slice(s);
            break;
          }
          frames.push(pending.slice(s, e + 2));
          pending = pending.slice(e + 2);
        }
        if (pending.length > 1500000) pending = pending.slice(-2000);
        if (frames.length >= BUF_CAP) {
          return new Promise(function(ok){ setTimeout(ok, 200); }).then(read);
        }
        return read();
      });
    }
    return read();
  }).catch(function(){});
}
var audioUrl = '';
function stallPlay(reason){
  if (!stalled) {
    stalled = true;
    stopClock();
    if (actx && actx.state === 'running') actx.suspend();
  }
  showStatus(reason || ('fermo, restano ' + (frames.length / FPS).toFixed(1) + 's di frame'));
}
function resumePlay(){
  stalled = false;
  overlay.style.display = 'none';
  if (!userPaused) startClock();
}
function sliceBuf(buf, skipSec){
  var rate = buf.sampleRate;
  var skip = Math.floor(skipSec * rate);
  var len = buf.length - skip;
  if (len < 200) return null;
  var out = actx.createBuffer(buf.numberOfChannels, len, rate);
  for (var c = 0; c < buf.numberOfChannels; c++) out.getChannelData(c).set(buf.getChannelData(c).subarray(skip));
  return out;
}
function scheduleBuf(buf){
  if (!buf) return;
  if (!audioArm || !actx) { if (audioHold.length < 120) { audioHold.push(buf); if (actx) audioNext = actx.currentTime + audioHold.reduce(function(s,b){ return s + b.duration; }, 0); } return; }
  var frameT = drawnT;
  if (audioCursor + 0.4 < frameT) {
    var late = frameT - audioCursor;
    if (buf.duration <= late) { audioCursor += buf.duration; return; }
    var cut = sliceBuf(buf, late);
    audioCursor += late;
    buf = cut;
    if (!buf) return;
  }
  var when = syncOrigin + audioCursor;
  if (when < actx.currentTime - 0.2) { audioCursor += buf.duration; return; }
  var src = actx.createBufferSource();
  src.buffer = buf;
  src.connect(actx.destination);
  // un blocco gia' in ritardo non si sposta avanti (si sovrapporrebbe al
  // successivo): si salta la parte gia' passata, cosi' i blocchi restano attaccati
  var skip = 0;
  if (when < actx.currentTime + 0.02) { skip = actx.currentTime + 0.02 - when; when += skip; }
  // finito di suonare, il blocco esce dalla lista: senza questo tutto l'audio
  // decodificato restava in memoria fino al salto successivo (circa 1 GB in 45 minuti)
  src.onended = function(){
    var i = audioSources.indexOf(src);
    if (i >= 0) audioSources.splice(i, 1);
    src.disconnect();
  };
  try {
    src.start(when, Math.min(skip, Math.max(0, buf.duration - 0.001))); audioSources.push(src);
    aDiag.sched++;
    if (!aDiag.firstSched) { aDiag.firstSched = true; ui('audio: primo blocco tra ' + (when - actx.currentTime).toFixed(2) + 's, contesto ' + actx.state); }
  } catch (e) { ui('audio: start fallito (' + e + ')'); }
  audioCursor += buf.duration;
  audioNext = syncOrigin + audioCursor;
}
function flushHeldAudio(){
  if (!audioArm || !audioHold.length) return;
  var pending = audioHold;
  audioHold = [];
  for (var i = 0; i < pending.length; i++) scheduleBuf(pending[i]);
}
function earTime(){
  if (!actx || !audioArm) return drawnT;
  return actx.currentTime - syncOrigin;
}
function holdEar(){
  var ear = earTime();
  if (!actx || userPaused || stalled) return ear;
  if (ear > drawnT + 0.12) {
    if (actx.state === 'running') actx.suspend();
    ear = earTime();
  } else if (actx.state === 'suspended' && drawnT >= ear - 0.03) {
    actx.resume();
    ear = earTime();
  }
  return ear;
}
function audioMustWait(){
  if (!actx || !audioOn || !audioArm) return false;
  flushHeldAudio();
  if (audioCursor + 0.2 < drawnT) return true;
  return false;
}
function showVideo(){
  screen.style.display = 'block';
  freezeC.style.display = 'none';
  overlay.style.display = 'none';
  stalled = false;
  startClock();
  draw();
  shownFrames = 0;
  drawnT = 0;
  frameBase = pos;
  painting = false;
  paintGen++;
  if (playTimer) clearInterval(playTimer);
  playTimer = setInterval(function(){
    if (userPaused) {
      if (actx && actx.state === 'running') actx.suspend();
      var hud = document.getElementById('hud');
      if (hud) hud.textContent = 'pausa, buffer ' + frames.length + ' frame';
      return;
    }
    pollRate();
    if (frames.length < FPS * 5 && !vEnded) {
      stallPlay();
      showStatus('buffer ' + frames.length + ', carico');
      return;
    }
    if (actx && audioArm && audioOn && !userPaused) {
      var ear = earTime();
      if (ear > drawnT + 0.5 && frames.length > FPS * 2) {
        overlay.style.display = 'flex';
        msg.style.display = 'block';
        msg.textContent = 'riallineamento in corso';
        go.style.display = 'none';
        while (frames.length > 2 && drawnT + 1/FPS < ear - 0.1) {
          frames.shift();
          drawnT += 1/FPS;
          shownFrames++;
        }
        pos = frameBase + drawnT;
        draw();
        if (Math.abs(ear - drawnT) < 0.3) overlay.style.display = 'none';
        return;
      } else if (overlay.style.display === 'flex' && msg.textContent === 'riallineamento in corso') {
        overlay.style.display = 'none';
      }
    }
    if (stalled) {
      if (frames.length < FPS * 10 && !vEnded) {
        showStatus('fermo a ' + frames.length + ', carico buffer');
        return;
      }
      resumePlay();
      playOrigin = performance.now() - drawnT * 1000;
      // durante lo stallo actx era sospeso: orologio audio e video fermi insieme,
      // quindi l'audio gia' programmato e' ancora allineato. Basta ripartire.
      if (actx && audioOn && actx.state === 'suspended' && earTime() <= drawnT + 0.12) actx.resume();
    }
    if (audioHold.length && audioArm) flushHeldAudio();
    if (painting) return;
    var elapsed = (performance.now() - playOrigin) / 1000;
    if (drawnT + 1 / FPS > elapsed + 0.04) return;
    while (frames.length > 2 && drawnT + 2 / FPS < elapsed) {
      frames.shift();
      drawnT += 1 / FPS;
    }
    if (!frames.length) {
      if (vEnded) {
        clearInterval(playTimer);
        playTimer = null;
        stopClock();
        draw();
        showNext(true);
      }
      return;
    }
    var jpg = frames.shift();
    shownFrames++;
    var token = ++paintGen;
    painting = true;
    createImageBitmap(new Blob([jpg], {type: 'image/jpeg'})).then(function(bmp){
      painting = false;
      if (token !== paintGen) { bmp.close && bmp.close(); return; }
      if (screen.width !== bmp.width) {
        screen.width = bmp.width;
        screen.height = bmp.height;
      }
      screen.getContext('2d').drawImage(bmp, 0, 0);
      bmp.close && bmp.close();
      drawnT += 1 / FPS;
      pos = frameBase + drawnT;
      draw();
      if (!userPaused && !stalled) holdEar();
    }).catch(function(){ painting = false; });
  }, 1000 / FPS);
  syncOrigin = actx ? actx.currentTime : 0;
  audioArm = true;
  audioCursor = 0;
  playOrigin = performance.now();
  flushHeldAudio();
}
function hold(){
  stopClock();
  if (screen.width) {
    freezeC.width = screen.width;
    freezeC.height = screen.height;
    freezeC.getContext('2d').drawImage(screen, 0, 0);
    freezeC.style.display = 'block';
  }
}
function ensureCtx(){
  if (!actx) {
    var AC = window.AudioContext || window.webkitAudioContext;
    try { actx = new AC({sampleRate: 48000}); } catch (e) { actx = new AC(); }
    aDiag.ctx = actx.state;
    ui('audio creato: ' + actx.state + ', ' + actx.sampleRate + ' Hz');
    actx.onstatechange = function(){ aDiag.ctx = actx.state; ui('audio: ' + actx.state + ' a ' + actx.currentTime.toFixed(2) + 's'); };
  }
  // un suono muto fatto partire dentro il tocco sblocca l'audio anche nei
  // browser piu' severi
  try {
    var b = actx.createBuffer(1, 1, actx.sampleRate), s = actx.createBufferSource();
    s.buffer = b; s.connect(actx.destination); s.start(0);
  } catch (e) {}
  if (actx.state !== 'suspended') return Promise.resolve();
  // resume() che non si risolve mai (succede) non deve bloccare l'avvio
  return Promise.race([
    actx.resume().catch(function(e){ ui('audio resume rifiutato: ' + e); }),
    new Promise(function(ok){ setTimeout(ok, 1500); })
  ]);
}
// diagnostica audio, mostrata in alto e mandata al log del server
var aDiag = {ctx: '-', kb: 0, blk: 0, err: 0, sched: 0, firstData: false, firstSched: false};
// audio che dovrebbe suonare ma il browser lo tiene spento: si riaccende al
// primo tocco sullo schermo (riaccenderlo fuori da un tocco puo' non funzionare)
function audioShouldRun(){
  return actx && audioOn && audioArm && playTimer && !userPaused && !stalled && actx.state === 'suspended';
}
['pointerdown', 'touchend', 'click'].forEach(function(evn){
  document.addEventListener(evn, function(){
    if (audioShouldRun()) { actx.resume(); ui('audio riacceso dal tocco (' + evn + ')'); }
  }, true);
});
var audioHold = [];
var audioSources = [];
var audioArm = null;
function stopAudio(){
  audioOn = false;
  audioArm = null;
  if (audioAbort) { audioAbort.abort(); audioAbort = null; }
  audioNext = 0;
  audioCursor = 0;
  audioHold = [];
  for (var i = 0; i < audioSources.length; i++) {
    try { audioSources[i].stop(); } catch (e) {}
  }
  audioSources = [];
  // il contesto resta acceso: fermarlo qui obbligava a riaccenderlo fuori da un
  // tocco, e alcuni browser (Tesla compreso) a volte non lo permettono: il video
  // restava fermo sul primo frame finche' non si toccava la barra
}
function concatU8(a, b){
  var c = new Uint8Array(a.length + b.length);
  c.set(a, 0); c.set(b, a.length);
  return c;
}
// --- MP3 a pezzi senza buchi ---
// Divide lo stream nei suoi frame reali (lunghezza calcolata dall'intestazione,
// niente falsi inizi dentro i dati) e decodifica blocchi di circa un secondo.
// Ogni blocco porta con se' gli ultimi MP3_PRE frame del blocco precedente:
// servono al decoder (serbatoio di bit e sovrapposizione tra frame) e il loro
// suono viene scartato. L'ultimo frame di ogni blocco non si usa e diventa il
// primo del blocco dopo, cosi' anche la fine del blocco e' pulita.
var MP3_SR = [[44100, 48000, 32000], [22050, 24000, 16000], [11025, 12000, 8000]];
var MP3_BR1 = [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320];
var MP3_BR2 = [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160];
var MP3_PRE = 4, MP3_BLOCK = 40;
function mp3Frame(u8, i){
  if (i + 4 > u8.length || u8[i] !== 0xFF || (u8[i + 1] & 0xE0) !== 0xE0) return null;
  var ver = (u8[i + 1] >> 3) & 3, layer = (u8[i + 1] >> 1) & 3;
  if (ver === 1 || layer !== 1) return null;
  var bri = u8[i + 2] >> 4, sri = (u8[i + 2] >> 2) & 3, pad = (u8[i + 2] >> 1) & 1;
  if (bri === 0 || bri === 15 || sri === 3) return null;
  var v = ver === 3 ? 0 : (ver === 2 ? 1 : 2);
  var sr = MP3_SR[v][sri], br = (v === 0 ? MP3_BR1 : MP3_BR2)[bri] * 1000;
  return {len: Math.floor((v === 0 ? 144 : 72) * br / sr) + pad, sr: sr, spf: v === 0 ? 1152 : 576};
}
// primo frame valido da i in poi, confermato dal frame successivo
function mp3Sync(u8, i){
  for (; i + 4 <= u8.length; i++) {
    var f = mp3Frame(u8, i);
    if (!f) continue;
    if (i + f.len + 4 > u8.length) return i;
    if (mp3Frame(u8, i + f.len)) return i;
  }
  return -1;
}
// stato: {pend: byte non ancora usati, pre: byte dei frame da riusare, npre}
function mp3Chunker(){ return {pend: new Uint8Array(0), pre: new Uint8Array(0), npre: 0, synced: false, sr: 0, spf: 1152}; }
// aggiunge byte; ritorna un blocco pronto da decodificare oppure null
// blocco: {bytes, dropHead (s), dropTail (s)}
function mp3Take(st, chunk, last){
  if (chunk && chunk.length) st.pend = st.pend.length ? concatU8(st.pend, chunk) : chunk;
  var u = st.pend, o = 0;
  if (!st.synced) {
    o = mp3Sync(u, 0);
    if (o < 0) { if (u.length > 8192) st.pend = u.slice(-4); return null; }
    st.synced = true;
  }
  var starts = [];
  while (true) {
    var f = mp3Frame(u, o);
    if (!f) {
      if (o + 4 > u.length) break;
      var s = mp3Sync(u, o + 1);
      if (s < 0) break;
      o = s;
      continue;
    }
    if (o + f.len > u.length) break;
    st.sr = f.sr; st.spf = f.spf;
    starts.push(o);
    o += f.len;
    if (!last && starts.length > MP3_BLOCK) break;
  }
  var n = starts.length;
  if (!last && n <= MP3_BLOCK) {
    if (starts.length) st.pend = u.subarray(starts[0]);
    return null;
  }
  if (!n) return null;
  // usati: tutti i frame tranne l'ultimo (salvo a fine stream)
  var useEnd = last ? o : starts[n - 1];
  var body = u.subarray(starts[0], o);
  var bytes = st.pre.length ? concatU8(st.pre, body) : body.slice();
  var fdur = st.spf / st.sr;
  var blk = {bytes: bytes, dropHead: st.npre * fdur, dropTail: last ? 0 : fdur, sr: st.sr};
  // prossimo prefisso: ultimi MP3_PRE frame usati
  var usedStarts = last ? starts : starts.slice(0, n - 1);
  var k = Math.min(MP3_PRE, usedStarts.length);
  st.pre = u.slice(usedStarts[usedStarts.length - k], useEnd);
  st.npre = k;
  st.pend = last ? new Uint8Array(0) : u.subarray(useEnd);
  return blk;
}
// taglia testa e coda del suono decodificato (in secondi)
function mp3Trim(ctx, buf, head, tail){
  var rate = buf.sampleRate;
  var a = Math.round(head * rate), b = buf.length - Math.round(tail * rate);
  if (b - a < 64) return null;
  if (a === 0 && b === buf.length) return buf;
  var out = ctx.createBuffer(buf.numberOfChannels, b - a, rate);
  for (var c = 0; c < buf.numberOfChannels; c++) out.getChannelData(c).set(buf.getChannelData(c).subarray(a, b));
  return out;
}

function playAudioStream(url){
  stopAudio();
  var ctrl = new AbortController();
  audioAbort = ctrl;
  audioOn = true;
  var pending = new Uint8Array(0);
  var started = false;
  var my = gen;
  function armVideo(){
    if (started || my !== gen) return;
    started = true;
    showVideo();
  }
  return ensureCtx().then(function(){
    audioNext = actx.currentTime + 0.25;
    ui('audio: chiedo lo stream, contesto ' + actx.state);
    return fetch(url, {signal: ctrl.signal});
  }).then(ck).then(function(res){
    ui('audio: risposta ' + res.status + ' ' + (res.headers.get('Content-Type') || '') + (res.body ? '' : ' (senza body)'));
    if (!res.ok) { ui('audio: stream mp3 rifiutato (' + res.status + ')'); armVideo(); return; }
    aDiag.kb = 0; aDiag.blk = 0; aDiag.err = 0; aDiag.sched = 0; aDiag.firstData = false; aDiag.firstSched = false;
    var reader = res.body.getReader();
    var st = mp3Chunker();
    function decodeBlk(blk){
      var b = blk.bytes;
      return actx.decodeAudioData(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength)).then(function(buf){
        if (ctrl.signal.aborted || my !== gen) return;
        aDiag.blk++;
        var t = mp3Trim(actx, buf, blk.dropHead, blk.dropTail);
        if (t) scheduleBuf(t);
      }).catch(function(e){
        aDiag.err++;
        if (aDiag.err <= 2) ui('audio: decodifica fallita (' + (e && e.message ? e.message : e) + ')');
      });
    }
    // decodifica in ordine tutti i blocchi pronti
    function drain(chunk, last){
      var blk = mp3Take(st, chunk, last);
      if (!blk) return Promise.resolve();
      return decodeBlk(blk).then(function(){
        if (last || ctrl.signal.aborted || my !== gen) return;
        return drain(null, false);
      });
    }
    function read(){
      if (actx && audioNext - actx.currentTime > 8) {
        return new Promise(function(ok){ setTimeout(ok, 300); }).then(read);
      }
      return reader.read().then(function(r){
        if (ctrl.signal.aborted || my !== gen) return;
        if (r.done) return drain(null, true);
        aDiag.kb += r.value.length / 1024;
        if (!aDiag.firstData) { aDiag.firstData = true; ui('audio: primi dati mp3, contesto ' + actx.state); }
        return drain(r.value, false).then(read);
      });
    }
    return read();
  }).catch(function(e){
    if (!ctrl.signal.aborted && my === gen) ui('audio: stream interrotto (' + (e && e.message ? e.message : e) + ', ' + aDiag.kb.toFixed(0) + ' KB)');
  });
}
function startBoth(){
  stopWarm();
  var t = Date.now();
  if (startAt) pos = startAt;
  overlay.style.display = 'flex';
  msg.style.display = 'block';
  msg.textContent = 'Caricamento buffer 0';
  go.style.display = 'none';
  fetch('/ctrl?pos=' + (startAt || pos || 0)).then(function(){
    audioUrl = '/stream.mp3?t=' + t;
    playAudioStream(audioUrl);
    startVideoBuf();
  });
    var watch = setInterval(function(){
      fetch('/status').then(function(r){ return r.text(); }).then(function(err){
        if (err && (err.indexOf('Verifica') === 0 || err.indexOf('yt-dlp') === 0 || err.indexOf('Nessuno stream') === 0 || err.indexOf('403 dopo') === 0)) { msg.textContent = err; ui(err); clearInterval(watch); }
      });
      fetch('/buf').then(function(r){ return r.text(); }).then(function(t){
        var parts = t.split(' ');
        var n = parseInt(parts[0], 10) || 0;
        var rate = parseFloat(parts[1]) || 0;
        var slow = rate > 0 && rate < 1;
        var need = slow ? FPS * 12 : FPS * 4;
        msg.style.display = 'block';
        msg.textContent = (slow ? 'Linea lenta ' + rate.toFixed(1) + ' MB/s — ' : '') + 'Caricamento buffer ' + parts[0];
        if (n >= need && go.style.display === 'none') {
          go.style.display = 'block';
          ui('pronto');
        }
      });
    }, 1000);
}
var userPaused = false;
var clickAt = 0;
function showNext(on){
  document.getElementById('nexts').style.display = on ? 'flex' : 'none';
  if (on) loadNext();
}
// comandi sopra il video: suggerimenti, home e pulsante pausa/play al centro
var chromeOn = false, ctlTimer = null;
var ppBtn = document.getElementById('pp');
function ctlGlyph(){ ppBtn.innerHTML = userPaused ? '&#9654;' : '&#10074;&#10074;'; }
function showControls(autoHideMs){
  chromeOn = true;
  showNext(true);
  document.getElementById('homebtn').style.display = 'block';
  ctlGlyph();
  ppBtn.style.display = 'block';
  if (ctlTimer) { clearTimeout(ctlTimer); ctlTimer = null; }
  if (!userPaused) ctlTimer = setTimeout(hideControls, autoHideMs || 5000);
}
function hideControls(){
  if (ctlTimer) { clearTimeout(ctlTimer); ctlTimer = null; }
  if (userPaused) return;
  chromeOn = false;
  showNext(false);
  document.getElementById('homebtn').style.display = 'none';
  ppBtn.style.display = 'none';
}
ppBtn.onclick = function(ev){
  ev.stopPropagation();
  togglePlay();
};
function togglePlay(){
  var now = Date.now();
  if (now - clickAt < 700) return;
  clickAt = now;
  if (go.style.display !== 'none') { startBoth(); return; }
  fetch('/ctrl?pause=1').then(ck).then(function(r){ return r.text(); }).then(function(t){
    if (t === 'paused') {
      if (hs && hs.started && !stalled) hs.holdT = hClock();
      userPaused = true;
      stopClock();
      if (actx && actx.state === 'running') actx.suspend();
      showControls();
      ui('pausa');
    } else {
      userPaused = false;
      showControls(2500);
      if (actx && actx.state === 'suspended') actx.resume();
      flushHeldAudio();
      startClock();
      playOrigin = performance.now() - drawnT * 1000;
      if (hs && hs.holdT != null && !stalled) { playOrigin = performance.now() - (hs.holdT - hs.X) * 1000; hs.holdT = null; hs.clk = null; }
      ui('ripresa, buffer ' + frames.length);
    }
  }).catch(function(){
    if (!userPaused && hs && hs.started && !stalled) hs.holdT = hClock();
    userPaused = !userPaused;
    if (userPaused) {
      stopClock();
      if (actx && actx.state === 'running') actx.suspend();
      showControls();
    } else {
      showControls(2500);
      if (actx && actx.state === 'suspended') actx.resume();
      flushHeldAudio();
      startClock();
      playOrigin = performance.now() - drawnT * 1000;
      if (hs && hs.holdT != null && !stalled) { playOrigin = performance.now() - (hs.holdT - hs.X) * 1000; hs.holdT = null; hs.clk = null; }
    }
  });
}
// ================= H.264 =================
// Il server manda il video H.264 originale di YouTube, un record per frame:
// [lunghezza u32][pts f64 in secondi][frame Annex-B]. Il browser lo decodifica
// con FFmpeg compilato in WebAssembly e lo disegna con WebGL. L'orologio e'
// l'audio: si mostra sempre il frame del momento in cui e' l'audio.
// I frame tra il keyframe e il punto di partenza (pts < X) si decodificano
// ma non si mostrano. Il video resta compresso in memoria (pochi MB) e si
// decodifica un frame alla volta, quando serve.
var VMODE_CFG = __VMODE__;
var vmode = VMODE_CFG ? 'h264' : 'mjpeg';
var mjCanvas = screen;
var hCanvas = document.getElementById('hscreen');
var hs = null;
var hWasm = null, hMem = null, hReady = null;
var hOnFrame = function(){};
var H_START = 3, H_STALL = 0.5, H_RESUME = 4, H_AHEAD = 120;
// Linea lenta: a ogni blocco per caricamento si aspetta un buffer piu' lungo
// prima di ripartire (4, 12, 20, 28, poi 30 s), e anche i video successivi
// partono con piu' margine. Vale per tutta la pagina.
var hStalls = 0;
function hResumeNeed(){ return Math.min(30, H_RESUME + 8 * hStalls); }
function hStartNeed(){ return Math.min(20, H_START + 6 * hStalls); }
var NL = String.fromCharCode(10);

function setVmode(m){
  vmode = m;
  mjCanvas.style.display = 'none';
  hCanvas.style.display = 'none';
  screen = m === 'h264' ? hCanvas : mjCanvas;
}
function hDv(){ return new DataView(hMem.buffer); }
var hWasi = {
  environ_get: function(){ return 0; },
  environ_sizes_get: function(a, b){ hDv().setUint32(a, 0, true); hDv().setUint32(b, 0, true); return 0; },
  clock_time_get: function(id, prec, o){ hDv().setBigUint64(o, BigInt(Math.round(performance.now() * 1e6)), true); return 0; },
  fd_close: function(){ return 0; }, fd_fdstat_get: function(){ return 52; }, fd_fdstat_set_flags: function(){ return 52; },
  fd_prestat_get: function(){ return 8; }, fd_prestat_dir_name: function(){ return 52; },
  fd_read: function(){ return 52; }, fd_seek: function(){ return 52; }, path_open: function(){ return 52; },
  fd_write: function(fd, iovs, n, nw){ var t = 0; for (var i = 0; i < n; i++) t += hDv().getUint32(iovs + i * 8 + 4, true); hDv().setUint32(nw, t, true); return 0; },
  proc_exit: function(c){ throw new Error('exit ' + c); }
};
function hLoad(){
  if (hReady) return hReady;
  var ok = false;
  try {
    ok = typeof WebAssembly === 'object' && WebAssembly.validate(new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,123,3,2,1,0,10,10,1,8,0,65,0,253,15,253,98,11]));
  } catch (e) { ok = false; }
  if (!ok) { hReady = Promise.reject(new Error('WebAssembly SIMD assente')); hReady.catch(function(){}); return hReady; }
  hReady = fetch('/h264.wasm').then(function(r){
    if (!r.ok) throw new Error('decoder HTTP ' + r.status);
    return r.arrayBuffer();
  }).then(function(buf){
    return WebAssembly.instantiate(buf, {
      env: { on_frame: function(){ hOnFrame.apply(null, arguments); } },
      wasi_snapshot_preview1: hWasi
    });
  }).then(function(res){
    hWasm = res.instance.exports; hMem = hWasm.memory; hWasm._initialize();
    if (hWasm.dec_init() !== 0) throw new Error('dec_init');
    return true;
  });
  hReady.catch(function(e){ ui('decoder H.264 non disponibile: ' + e.message); });
  return hReady;
}

// --- disegno: WebGL (YUV -> RGB sulla GPU) o, senza WebGL, conversione in WASM ---
var hGL = null, hGLtried = false, hTex = [], hScale = null, h2d = null;
function hInitGL(){
  hGLtried = true;
  try { hGL = hCanvas.getContext('webgl', {antialias: false, depth: false, preserveDrawingBuffer: true}); } catch (e) { hGL = null; }
  if (!hGL) return;
  var gl = hGL;
  var vs = 'attribute vec2 p;varying vec2 t;void main(){t=vec2((p.x+1.0)*0.5,(1.0-p.y)*0.5);gl_Position=vec4(p,0.0,1.0);}';
  var fs = 'precision mediump float;varying vec2 t;uniform sampler2D Y,U,V;uniform vec2 sy,sc;void main(){' +
    'float y=1.1644*(texture2D(Y,vec2(t.x*sy.x,t.y)).r-0.0627);float u=texture2D(U,vec2(t.x*sc.x,t.y)).r-0.5;float v=texture2D(V,vec2(t.x*sc.x,t.y)).r-0.5;' +
    'gl_FragColor=vec4(y+1.7927*v,y-0.2132*u-0.5329*v,y+2.1124*u,1.0);}';
  function sh(type, src){ var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; }
  var pr = gl.createProgram();
  gl.attachShader(pr, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(pr);
  if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) { hGL = null; return; }
  gl.useProgram(pr);
  var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
  var loc = gl.getAttribLocation(pr, 'p'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  ['Y','U','V'].forEach(function(n, i){
    var tx = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, tx);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.uniform1i(gl.getUniformLocation(pr, n), i); hTex.push(tx);
  });
  hScale = [gl.getUniformLocation(pr, 'sy'), gl.getUniformLocation(pr, 'sc')];
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
}
function hDrawPlanes(f){
  var gl = hGL, w = f.w, h = f.h, ch = (h + 1) >> 1;
  if (hCanvas.width !== w || hCanvas.height !== h) { hCanvas.width = w; hCanvas.height = h; gl.viewport(0, 0, w, h); }
  var planes = [[f.y, f.ly, h], [f.u, f.lu, ch], [f.v, f.lv, ch]];
  for (var i = 0; i < 3; i++) {
    gl.activeTexture(gl.TEXTURE0 + i);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, planes[i][1], planes[i][2], 0, gl.LUMINANCE, gl.UNSIGNED_BYTE, planes[i][0]);
  }
  gl.uniform2f(hScale[0], w / f.ly, 1); gl.uniform2f(hScale[1], ((w + 1) >> 1) / f.lu, 1);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}
function hDraw(y, u, v, ly, lu, lv, w, h){
  if (!hGLtried) hInitGL();
  if (hGL) {
    var gl = hGL;
    if (hCanvas.width !== w || hCanvas.height !== h) { hCanvas.width = w; hCanvas.height = h; gl.viewport(0, 0, w, h); }
    var ch = (h + 1) >> 1, buf = hMem.buffer;
    var planes = [[y, ly, h], [u, lu, ch], [v, lv, ch]];
    for (var i = 0; i < 3; i++) {
      gl.activeTexture(gl.TEXTURE0 + i);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, planes[i][1], planes[i][2], 0, gl.LUMINANCE, gl.UNSIGNED_BYTE,
        new Uint8Array(buf, planes[i][0], planes[i][1] * planes[i][2]));
    }
    gl.uniform2f(hScale[0], w / ly, 1); gl.uniform2f(hScale[1], ((w + 1) >> 1) / lu, 1);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    return;
  }
  if (!h2d) h2d = hCanvas.getContext('2d');
  if (hCanvas.width !== w || hCanvas.height !== h) { hCanvas.width = w; hCanvas.height = h; }
  var p = hWasm.to_rgba(y, u, v, ly, lu, lv, w, h);
  h2d.putImageData(new ImageData(new Uint8ClampedArray(hMem.buffer, p, w * h * 4), w, h), 0, 0);
}

// --- sessione ---
function hNew(X){
  hWasm.dec_init();
  hs = {
    gen: gen, X: X, pk: [], lastPts: -1, outPts: -1e9, shownPts: -1e9, dur: 1 / 30,
    target: X, ended: false, flushed: false, failed: 0, started: false, drawn: 0,
    late: 0, abort: null, lastT: -1, frozenAt: 0, resumeTried: false, askedTap: false,
    fpsAt: 0, fpsDrawn: 0, fpsLate: 0, fps: 0, lateNow: 0, q: [], clk: null,
    startedAt: 0, audioRetry: 0, drewAt: 0, lagSince: 0, resyncAt: 0
  };
  hOnFrame = function(y, u, v, ly, lu, lv, w, h, pts){
    if (!hs) return;
    var prev = hs.outPts;
    hs.outPts = pts;
    if (pts > prev && pts - prev > 0.004 && pts - prev < 0.2) hs.dur = pts - prev;
    if (pts < hs.X - hs.dur / 2) return;            // prima del punto di partenza
    if (pts <= hs.shownPts + 0.001) return;          // gia' mostrato (ffmpeg ripartito)
    if (hs.started && hGL) {
      // tutti i frame finiscono in coda; hDue sceglie quello giusto per l'orologio
      // e salta i vecchi. La coda e' limitata (memoria) tenendo i piu' recenti.
      var ch = (h + 1) >> 1, buf = hMem.buffer;
      hs.q.push({pts: pts, w: w, h: h, ly: ly, lu: lu, lv: lv,
        y: new Uint8Array(buf, y, ly * h).slice(), u: new Uint8Array(buf, u, lu * ch).slice(),
        v: new Uint8Array(buf, v, lv * ch).slice()});
      while (hs.q.length > 12) { hs.q.shift(); hs.late++; }
      return;
    }
    hDraw(y, u, v, ly, lu, lv, w, h);
    hShown(pts);
  };
}
function hShown(pts){
  hs.shownPts = pts;
  hs.drawn++;
  drawnT = Math.max(0, pts - hs.X);
  pos = pts;
}
// disegna il frame piu' recente gia' dovuto. Chiamata sia dal refresh dello
// schermo (requestAnimationFrame, fluido) sia dal timer hTick: se il browser
// ferma requestAnimationFrame (scheda in secondo piano, schermo che rallenta)
// il video andrebbe comunque avanti, mentre l'orologio audio continua a correre.
function hDue(){
  if (!hs || !hs.started || !hs.q.length || userPaused || stalled) return;
  var T = hClock(), f = null;
  while (hs.q.length && hs.q[0].pts <= T + 0.008) {
    if (f) hs.late++;
    f = hs.q.shift();
  }
  // nessun frame dovuto: se tutta la coda e' nel futuro si aspetta; se invece
  // siamo rimasti indietro (tutti prima di T) si mostra comunque il piu' recente
  if (!f) return;
  hDrawPlanes(f);
  hShown(f.pts);
  hs.drewAt = performance.now();
}
// video rimasto troppo indietro rispetto all'audio e non recupera (dispositivo
// che non regge la decodifica): si risincronizza saltando alla posizione audio
// con un nuovo keyframe dal server. Con un minimo di distanza tra due salti.
function hMaybeResync(T){
  if (!hs.started || hs.shownPts < 0) return false;
  var lag = T - hs.shownPts;
  if (lag < 2.5) { hs.lagSince = 0; return false; }
  var now = performance.now();
  if (!hs.lagSince) { hs.lagSince = now; return false; }
  if (now - hs.lagSince < 1500) return false;
  if (now - (hs.resyncAt || 0) < 6000) return false;
  hs.resyncAt = now; hs.lagSince = 0;
  ui('video indietro ' + lag.toFixed(1) + ' s, risincronizzo');
  beginAt(Math.floor(T));
  return true;
}
function hPresent(me){
  if (!hs || hs !== me) return;
  requestAnimationFrame(function(){ hPresent(me); });
  hDue();
}
function hStop(){
  if (!hs) return;
  if (hs.abort) hs.abort.abort();
  hs = null;
  hOnFrame = function(){};
}
function hStream(){
  var my = gen, me = hs, ctrl = new AbortController();
  me.abort = ctrl;
  fetch('/stream.h264?t=' + Date.now(), {signal: ctrl.signal}).then(ck).then(function(res){
    if (!res.ok) { me.failed = res.status; return; }
    var reader = res.body.getReader();
    var pend = new Uint8Array(0);
    function read(){
      if (my !== gen || hs !== me) return;
      if (me.lastPts - Math.max(me.X, me.shownPts) > H_AHEAD) {
        return new Promise(function(ok){ setTimeout(ok, 500); }).then(read);
      }
      return reader.read().then(function(r){
        if (my !== gen || hs !== me) return;
        if (r.done) { me.ended = true; return; }
        pend = pend.length ? concatU8(pend, r.value) : r.value;
        var off = 0;
        while (pend.length - off >= 12) {
          var dv = new DataView(pend.buffer, pend.byteOffset + off, 12);
          var n = dv.getUint32(0, true), pts = dv.getFloat64(4, true);
          if (pend.length - off - 12 < n) break;
          me.pk.push({pts: pts, data: pend.subarray(off + 12, off + 12 + n)});
          if (pts > me.lastPts) me.lastPts = pts;
          off += 12 + n;
        }
        pend = off < pend.length ? pend.slice(off) : new Uint8Array(0);
        return read();
      });
    }
    return read();
  }).catch(function(){});
}
// decodifica pacchetti finche' esce un frame; false se non c'e' altro da decodificare
function hDecodeNext(){
  while (hs.pk.length) {
    var p = hs.pk.shift();
    var ptr = hWasm.in_buf(p.data.length);
    new Uint8Array(hMem.buffer, ptr, p.data.length).set(p.data);
    if (hWasm.decode(p.data.length, p.pts) > 0) return true;
  }
  if (hs.ended && !hs.flushed) { hs.flushed = true; return hWasm.flush() > 0; }
  return false;
}
// momento del video che deve essere sullo schermo adesso
function hRawClock(){
  // l'orologio dell'audio vale solo se l'audio e' davvero in programma: senza
  // dati audio (stream che non arriva) il video va per conto suo
  if (actx && audioOn && audioArm && actx.state !== 'closed' && aDiag.sched > 0) return hs.X + (actx.currentTime - syncOrigin);
  return hs.X + (performance.now() - playOrigin) / 1000;
}
// L'orologio dell'audio avanza a scatti (blocchi di 10-40 ms in alcuni browser):
// usato direttamente, i frame uscivano a intervalli irregolari e il video
// sembrava meno fluido dei suoi fps. Qui si segue performance.now() e lo si
// corregge piano verso l'audio; se l'audio si ferma (pausa, stallo) ci si ferma.
function hClock(){
  if (!hs.started) { hs.clk = null; return hs.X + drawnT; }
  // fermi (pausa o caricamento): l'orologio resta dove si e' fermato. Prima si
  // usava l'ultimo frame mostrato, che poteva essere indietro rispetto
  // all'audio: alla ripartenza il buffer sembrava subito finito e il player
  // alternava fermo/riparti decine di volte al secondo
  if (userPaused || stalled) { hs.clk = null; return hs.holdT != null ? hs.holdT : hs.X + drawnT; }
  var raw = hRawClock(), now = performance.now();
  var c = hs.clk;
  if (!c || (actx && audioOn && audioArm && aDiag.sched > 0 && actx.state !== 'running')) {
    hs.clk = c = {raw: raw, rawAt: now, t: raw, at: now};
    return raw;
  }
  if (raw !== c.raw) { c.raw = raw; c.rawAt = now; }
  else if (now - c.rawAt > 150) { c.at = now; return c.t; }   // audio fermo: niente corsa in avanti
  var t = c.t + (now - c.at) / 1000;
  var err = raw - t;
  if (Math.abs(err) > 0.12) t = raw; else t += err * 0.08;
  c.t = t; c.at = now;
  return t;
}
function hAhead(){ return hs.lastPts - Math.max(hs.X, hs.shownPts); }

// preparazione: decodifica dal keyframe fino al primo frame da mostrare
function hPreroll(budgetMs){
  var until = performance.now() + budgetMs;
  while (hs.shownPts < hs.X - hs.dur && performance.now() < until) {
    if (!hDecodeNext()) break;
  }
  return hs.shownPts >= hs.X - hs.dur;
}
function showVideoH(){
  if (actx && audioOn && actx.state === 'suspended') actx.resume();
  screen.style.display = 'block';
  freezeC.style.display = 'none';
  overlay.style.display = 'none';
  stalled = false;
  hs.started = true;
  hs.startedAt = performance.now();
  drawnT = Math.max(0, hs.shownPts - hs.X);
  pos = hs.X + drawnT;
  startClock();
  draw();
  if (playTimer) clearInterval(playTimer);
  playTimer = setInterval(hTick, 10);
  var me = hs;
  requestAnimationFrame(function(){ hPresent(me); });
  // l'MP3 parte esattamente da X: orologio audio = X adesso. Il primo frame
  // (pts >= X - mezzo frame) e' gia' sullo schermo, gli altri seguono l'audio.
  syncOrigin = actx ? actx.currentTime : 0;
  audioArm = true;
  audioCursor = 0;
  playOrigin = performance.now();
  flushHeldAudio();
}
function hFinish(){
  clearInterval(playTimer);
  playTimer = null;
  stopClock();
  if (actx && actx.state === 'running') actx.suspend();
  draw();
  showNext(true);
}
// fotogrammi mostrati nell'ultimo secondo e fps del video originale
// l'audio non arriva (0 byte dopo qualche secondo di video): si ricollega lo
// stream mp3 senza toccare l'orologio; i blocchi gia' passati vengono saltati
function hAudioWatch(){
  if (!audioOn || !actx || aDiag.kb > 0 || !hs.startedAt) return;
  if (performance.now() - hs.startedAt < 4000 * (hs.audioRetry + 1)) return;
  if (hs.audioRetry >= 3) return;
  hs.audioRetry++;
  ui('audio: nessun dato dopo ' + Math.round((performance.now() - hs.startedAt) / 1000) + 's, ricollego (' + hs.audioRetry + ')');
  var keepOrigin = syncOrigin;
  audioUrl = '/stream.mp3?t=' + Date.now() + '&r=' + hs.audioRetry;
  playAudioStream(audioUrl);
  syncOrigin = keepOrigin;
  audioArm = true;
  audioCursor = 0;
}
function hFps(){
  var now = performance.now();
  if (!hs.fpsAt) { hs.fpsAt = now; hs.fpsDrawn = hs.drawn; hs.fpsLate = hs.late; return; }
  var dt = (now - hs.fpsAt) / 1000;
  if (dt < 1) return;
  hs.fps = (hs.drawn - hs.fpsDrawn) / dt;
  hs.lateNow = hs.late - hs.fpsLate;
  hs.fpsAt = now; hs.fpsDrawn = hs.drawn; hs.fpsLate = hs.late;
}
// video fermo pur essendo in riproduzione (orologio audio bloccato): prima si
// prova a riaccendere l'audio, poi si chiede un tocco, che riparte da qui
function hWatch(T){
  if (T > hs.lastT + 0.01) { hs.lastT = T; hs.frozenAt = 0; return; }
  var now = performance.now();
  if (!hs.frozenAt) { hs.frozenAt = now; return; }
  var f = now - hs.frozenAt;
  if (f > 1500 && !hs.resumeTried && actx && actx.state !== 'running') {
    hs.resumeTried = true;
    actx.resume();
  }
  if (f > 3500 && !hs.askedTap && !hs.tapShown && actx && actx.state !== 'running') {
    hs.askedTap = true;
    hs.tapShown = true;
    ui('video fermo (audio ' + (actx ? actx.state : 'assente') + '): tocca per ripartire');
    overlay.style.display = 'flex';
    msg.style.display = 'block';
    msg.textContent = 'Tocca lo schermo per avviare';
    go.style.display = 'block';
  }
}
// un tocco sullo schermo mentre si aspetta: si riaccende l'audio e si riparte
// da dove si era (audio e video erano fermi insieme, restano allineati)
function hTapResume(){
  if (!hs || !hs.askedTap) return false;
  hs.askedTap = false; hs.frozenAt = 0; hs.resumeTried = false;
  overlay.style.display = 'none';
  go.style.display = 'none';
  if (actx && actx.state !== 'running') actx.resume();
  return true;
}
function hTick(){
  if (!hs || hs.gen !== gen) return;
  if (userPaused) {
    hs.frozenAt = 0; hs.fpsAt = 0;
    if (actx && actx.state === 'running') actx.suspend();
    var hud0 = document.getElementById('hud');
    if (hud0) hud0.textContent = 'pausa, video pronto ' + hAhead().toFixed(0) + ' s';
    return;
  }
  pollRate();
  var ahead = hs.lastPts - hClock();
  if (!stalled && !hs.ended && ahead < H_STALL) {
    hs.frozenAt = 0; hs.fpsAt = 0;
    hs.holdT = hClock();
    hStalls++;
    ui('caricamento: fermo ' + hStalls + ', riparto con ' + hResumeNeed() + ' s pronti');
    stallPlay('carico il video, ' + Math.max(0, ahead).toFixed(1) + ' / ' + hResumeNeed() + ' s');
    return;
  }
  if (stalled) {
    if (!hs.ended && ahead < hResumeNeed()) {
      hs.frozenAt = 0; hs.fpsAt = 0;
      showStatus('linea lenta: carico il video, ' + Math.max(0, ahead).toFixed(1) + ' / ' + hResumeNeed() + ' s');
      return;
    }
    resumePlay();
    playOrigin = performance.now() - ((hs.holdT != null ? hs.holdT : hs.X + drawnT) - hs.X) * 1000;
    hs.holdT = null;
    hs.clk = null;
    // audio e video erano fermi insieme: l'audio programmato e' ancora allineato
    if (actx && audioOn && actx.state === 'suspended') actx.resume();
  }
  if (audioHold.length && audioArm) flushHeldAudio();
  var T = hClock();
  hs.target = T;
  hFps();
  hAudioWatch();
  // decodifica in anticipo fino a poco oltre l'orologio, entro un budget di
  // tempo; la coda la svuota hDue mostrando il frame giusto. Se siamo indietro
  // (outPts < T) decodifica a tutta per rincorrere; non si blocca mai sulla coda.
  var until = performance.now() + 25;
  if (hGL) {
    while (hs.q.length < 8 && hs.outPts <= T + hs.dur * 3 && performance.now() < until) {
      if (!hDecodeNext()) break;
    }
  } else {
    while (hs.outPts + hs.dur <= T + hs.dur * 0.5 && performance.now() < until) {
      if (!hDecodeNext()) break;
    }
  }
  // se requestAnimationFrame non disegna da oltre 50 ms (scheda in secondo
  // piano, schermo lento), ci pensa il timer: il video non resta mai fermo
  // mentre l'audio corre
  if (performance.now() - (hs.drewAt || 0) > 50) hDue();
  if (hs.ended && hs.flushed && !hs.pk.length && !hs.q.length && T > hs.outPts + hs.dur) { hFinish(); return; }
  if (hMaybeResync(T)) return;   // la risincronizzazione ricrea la sessione
  hWatch(T);
}

// avvio di uno stream H.264 a partire da pos (chiamata da beginAt)
function beginH264(my, t0){
  hNew(pos);
  setVmode('h264');
  audioUrl = '/stream.mp3?t=' + Date.now();
  playAudioStream(audioUrl);
  hStream();
  var blocked = false;
  fillWait = setInterval(function(){
    if (my !== gen || blocked) { if (blocked) clearInterval(fillWait); return; }
    pollRate();
    fetch('/status').then(ck).then(function(r){ return r.text(); }).then(function(err){
      if (my !== gen || blocked) return;
      if (err && (err.indexOf('H264') === 0 || err.indexOf('403 dopo') === 0)) {
        blocked = true;
        hFallback(err);
        return;
      }
      if (err && (err.indexOf('Verifica') === 0 || err.indexOf('yt-dlp') === 0 || err.indexOf('Nessuno stream') === 0)) {
        blocked = true;
        clearInterval(fillWait);
        stopVideo();
        stopAudio();
        overlay.style.display = 'flex';
        msg.style.display = 'block';
        go.style.display = 'none';
        msg.textContent = err;
        ui(err);
      }
    });
    if (blocked || !hs) return;
    if (hs.failed) { blocked = true; hFallback('stream H.264 rifiutato (' + hs.failed + ')'); return; }
    var first = hPreroll(120);
    var ahead = hAhead();
    var audioReady = !audioOn || audioHold.length > 0;
    var elapsed = Date.now() - t0;
    if (!hs.pk.length && hs.lastPts < 0 && elapsed > 25000) {
      showStatus('nessun dato video. controllo cookie nel log');
      return;
    }
    showStatus('caricamento ' + Math.max(0, ahead).toFixed(1) + ' / ' + hStartNeed() + ' s' + (first && ahead >= hStartNeed() && !audioReady ? ', audio' : ''));
    if (first && (ahead >= hStartNeed() || hs.ended) && (audioReady || elapsed > 8000)) {
      clearInterval(fillWait);
      showVideoH();
    }
  }, 200);
}
function hFallback(why){
  clearInterval(fillWait);
  var x = hs ? hs.X : pos;
  ui('H.264 non disponibile (' + why + '), uso MJPEG');
  setVmode('mjpeg');
  beginAt(x);
}
setVmode(vmode);
if (VMODE_CFG) hLoad();

var fillWait = null;
function beginAt(sec){
  pos = Math.floor(sec || 0);
  startAt = pos;
  history.replaceState(null, '', '/?watch=' + VID + '&t=' + pos);
  if (fillWait) clearInterval(fillWait);
  var my = ++gen;
  stopVideo();
  stopAudio();
  stopClock();
  showStatus('apro lo stream a ' + pos + 's');
  var t0 = Date.now();
  if (vmode === 'h264') {
    hLoad().then(function(){
      if (my !== gen) return;
      return fetch('/ctrl?pos=' + pos + '&mode=h264').then(ck).then(function(){
        if (my === gen) beginH264(my, t0);
      });
    }).catch(function(e){
      if (my !== gen || kickedShown) return;
      ui('uso MJPEG: ' + (e && e.message ? e.message : e));
      setVmode('mjpeg');
      beginAt(pos);
    });
    return;
  }
  setVmode('mjpeg');
  fetch('/ctrl?pos=' + pos + '&mode=mjpeg').then(ck).then(function(){
    if (my !== gen) return;
    audioUrl = '/stream.mp3?t=' + Date.now();
    playAudioStream(audioUrl);
    startVideoBuf();
    var blocked = false;
    fillWait = setInterval(function(){
      if (my !== gen || blocked) { if (blocked) clearInterval(fillWait); return; }
      pollRate();
      fetch('/status').then(ck).then(function(r){ return r.text(); }).then(function(err){
        if (my !== gen) return;
        if (err && (err.indexOf('Verifica') === 0 || err.indexOf('yt-dlp') === 0 || err.indexOf('Nessuno stream') === 0 || err.indexOf('403 dopo') === 0)) {
          blocked = true;
          clearInterval(fillWait);
          stopVideo();
          stopAudio();
          overlay.style.display = 'flex';
          msg.style.display = 'block';
          go.style.display = 'none';
          msg.textContent = err;
          ui(err);
        }
      });
      if (blocked) return;
      if (!rateMb && Date.now() - t0 < 8000) {
        showStatus('misuro la linea');
        return;
      }
      if (!frames.length && Date.now() - t0 > 12000) {
        showStatus('nessun frame. controllo cookie nel log');
        return;
      }
      var slow = rateMb > 0 && rateMb < 1;
      var need = slow ? FPS * 12 : FPS * 5;
      var audioReady = !audioOn || audioHold.length > 0;
      var elapsed_ms = Date.now() - t0;
      showStatus(slow ? 'linea lenta: carico ' + need + ' frame' : 'caricamento' + (frames.length >= need && !audioReady ? ' audio' : ''));
      if (frames.length >= need && (audioReady || elapsed_ms > 6000)) {
        clearInterval(fillWait);
        showVideo();
      }
    }, 300);
  });
}
go.onclick = function(){
  if (hTapResume()) { ensureCtx(); return; }
  ensureCtx().then(function(){ beginAt(pos); });
};
mjCanvas.onclick = stageClick;
hCanvas.onclick = stageClick;
overlay.onclick = function(ev){
  if (ev.target === go) return;
  if (hTapResume()) { ensureCtx(); return; }
  stageClick(ev);
};
var cfgNow = __CFG__;
var SETTINGS = __OPTS__;
function stageClick(ev){
  if (ev.target.closest && ev.target.closest('#side, #menu, #homebtn, #nexts, #prog, #pp')) return;
  if (go.style.display !== 'none') return;
  if (userPaused) { showControls(); return; }
  if (chromeOn) hideControls(); else showControls();
}
function saveCfgCookie(){
  var o = {};
  SETTINGS.forEach(function(f){ o[f.key] = cfgNow[f.key]; });
  document.cookie = 'yt_cfg=' + encodeURIComponent(JSON.stringify(o)) + '; max-age=31536000; path=/; samesite=lax';
}
function drawCfg(){
  var box = document.getElementById('panel');
  box.innerHTML = '<p id="panel-title">Impostazioni</p>';
  SETTINGS.forEach(function(f){
    if (vmode === 'h264' && (f.key === 'FPS' || f.key === 'Q')) return;
    var row = document.createElement('div');
    row.className = 'row2';
    row.innerHTML = '<p><b>' + f.label + '</b><small>' + f.help + '</small></p><div class="opts"></div>';
    var opts = row.querySelector('.opts');
    f.options.forEach(function(o){
      var b = document.createElement('button');
      b.type = 'button';
      b.textContent = o[1];
      if (cfgNow[f.key] === o[0]) b.className = 'on';
      b.onclick = function(ev){ ev.stopPropagation(); cfgNow[f.key] = o[0]; drawCfg(); };
      opts.appendChild(b);
    });
    box.appendChild(row);
  });
  var save = document.createElement('button');
  save.id = 'savecfg';
  save.type = 'button';
  save.textContent = 'Salva e applica';
  save.onclick = function(ev){
    ev.stopPropagation();
    saveCfgCookie();
    box.innerHTML = '<p>salvo, riparto con le nuove impostazioni...</p>';
    location.reload();
  };
  box.appendChild(save);
  var reset = document.createElement('button');
  reset.id = 'resetcfg';
  reset.type = 'button';
  reset.textContent = 'Ripristina i default';
  reset.onclick = function(ev){
    ev.stopPropagation();
    document.cookie = 'yt_cfg=; max-age=0; path=/';
    location.reload();
  };
  box.appendChild(reset);
}
document.getElementById('menu').addEventListener('click', function(ev){
  ev.preventDefault();
  ev.stopPropagation();
  var side = document.getElementById('side');
  var open = side.className.indexOf('on') === -1;
  side.className = open ? 'on' : '';
  if (open) drawCfg();
}, true);
function seekAt(ev){
  if (!DURATION || IS_LIVE) return;
  var x = ev.clientX;
  if (ev.changedTouches && ev.changedTouches[0]) x = ev.changedTouches[0].clientX;
  var r = document.getElementById('prog').getBoundingClientRect();
  var sec = Math.floor(Math.max(0, Math.min(DURATION, ((x - r.left) / r.width) * DURATION)));
  pos = sec;
  draw();
  ensureCtx().then(function(){ beginAt(sec); });
}
document.getElementById('prog').onclick = seekAt;
document.getElementById('prog').ontouchend = function(ev){ ev.preventDefault(); seekAt(ev); };
draw();
preload();
window.addEventListener('pagehide', function(){
  fetch('/bye', {keepalive: true}).catch(function(){});
});
function loadNext(){
  fetch('/next').then(function(r){ return r.json(); }).then(function(list){
    var box = document.getElementById('nexts');
    box.innerHTML = '';
    (list || []).forEach(function(n){
      var a = document.createElement('a');
      a.href = '/watch?v=' + n.id;
      a.style.cssText = 'width:160px;background:#222;display:block;flex:0 0 auto';
      a.innerHTML = '<img src="https://i.ytimg.com/vi/' + n.id + '/hqdefault.jpg" style="width:100%;display:block"><span style="display:block;font-size:12px;padding:4px">' + (n.title || '').replace(/</g,'') + '</span>';
      box.appendChild(a);
    });
  }).catch(function(){});
}
loadNext();
</script>
"""


def _ver_key(v):
    nums = []
    for part in v.replace("-", ".").split("."):
        digits = "".join(c for c in part if c.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)


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


def install_update():
    import py_compile
    import shutil
    import tempfile
    src = fetch_remote_source()
    remote = remote_version(src)
    if not remote:
        raise RuntimeError("versione remota non trovata")
    if _ver_key(remote) <= _ver_key(VERSION):
        raise RuntimeError("gia' aggiornato (%s)" % VERSION)
    me = os.path.abspath(__file__)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=os.path.dirname(me), encoding="utf-8")
    try:
        tmp.write(src)
        tmp.close()
        py_compile.compile(tmp.name, doraise=True)
        shutil.copy2(me, me + ".bak")
        os.replace(tmp.name, me)
    finally:
        if os.path.exists(tmp.name):
            os.remove(tmp.name)
    print("aggiornato %s -> %s, riavvio" % (VERSION, remote), flush=True)
    return remote


def restart_self():
    time.sleep(1.0)
    try:
        stop_all()
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


WASMTEST_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test H.264 WASM</title>
<style>
body{background:#111;color:#eee;font:20px/1.35 sans-serif;margin:0;padding:14px}
h1{font-size:24px;margin:0 0 8px}
.r{display:flex;justify-content:space-between;gap:12px;border-bottom:1px solid #333;padding:6px 0}
.r span:last-child{text-align:right}
.ok{color:#4f4;font-weight:bold}.ko{color:#f55;font-weight:bold}.na{color:#fa0;font-weight:bold}
canvas{display:none;width:100%;max-width:960px;background:#000;margin:8px 0}
#clock{font-size:28px;color:#6cf}
input,button{font-size:20px;background:#222;color:#fff;border:1px solid #555;padding:4px 8px}
button{background:#2a5;border:0;padding:8px 18px}
#bar{display:flex;flex-wrap:wrap;align-items:center;gap:10px 16px;background:#1b1b1b;border:1px solid #333;padding:8px 10px;margin:0 0 10px}
#bar a{color:#6cf;text-decoration:none}
#bar .sp{flex:1}
#updtxt{color:#aaa;font-size:17px}
</style></head><body>
<div id="bar">
  <a href="/">&larr; App</a>
  <a href="/test">Test browser</a>
  <span class="sp"></span>
  <span id="updtxt">Versione __VER__</span>
</div>
<h1>Test H.264 in WebAssembly</h1>
<div id="clock"></div>
<div>Video YouTube (id, vuoto = quello aperto nell'app) <input id="vid" size="13">
 secondi <input id="sec" value="20" size="3"> <button id="go">Avvia</button></div>
<canvas id="cvgl"></canvas><canvas id="cv2d"></canvas>
<div id="out"></div>
<script>
var out = document.getElementById('out');
var t0 = Date.now();
setInterval(function(){ document.getElementById('clock').textContent = 'Contatore: ' + ((Date.now() - t0) / 1000).toFixed(1) + ' s'; }, 100);
function row(name, val, cls){
  var d = document.createElement('div'); d.className = 'r';
  d.innerHTML = '<span>' + name + '</span><span class="' + (cls || 'na') + '">' + val + '</span>';
  out.appendChild(d); return d.lastChild;
}
function sleep(ms){ return new Promise(function(ok){ setTimeout(ok, ms); }); }

// --- WASI minimo: il decoder non usa file ne' rete ---
var mem = null, ex = null, onFrame = function(){};
function dv(){ return new DataView(mem.buffer); }
var wasi = {
  environ_get: function(){ return 0; },
  environ_sizes_get: function(a, b){ dv().setUint32(a, 0, true); dv().setUint32(b, 0, true); return 0; },
  clock_time_get: function(id, prec, o){ dv().setBigUint64(o, BigInt(Math.round(performance.now() * 1e6)), true); return 0; },
  fd_close: function(){ return 0; }, fd_fdstat_get: function(){ return 52; }, fd_fdstat_set_flags: function(){ return 52; },
  fd_prestat_get: function(){ return 8; }, fd_prestat_dir_name: function(){ return 52; },
  fd_read: function(){ return 52; }, fd_seek: function(){ return 52; }, path_open: function(){ return 52; },
  fd_write: function(fd, iovs, n, nw){ var t = 0; for (var i = 0; i < n; i++) t += dv().getUint32(iovs + i * 8 + 4, true); dv().setUint32(nw, t, true); return 0; },
  proc_exit: function(c){ throw new Error('exit ' + c); }
};
function loadWasm(){
  return fetch('/h264.wasm').then(function(r){
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.arrayBuffer();
  }).then(function(buf){
    return WebAssembly.instantiate(buf, {
      env: { on_frame: function(){ onFrame.apply(null, arguments); } },
      wasi_snapshot_preview1: wasi
    });
  }).then(function(res){
    ex = res.instance.exports; mem = ex.memory; ex._initialize();
  });
}
var simdOk = (function(){
  try { return WebAssembly.validate(new Uint8Array([0,97,115,109,1,0,0,0,1,5,1,96,0,1,123,3,2,1,0,10,10,1,8,0,65,0,253,15,253,98,11])); }
  catch (e) { return false; }
})();

// --- disegno: WebGL (YUV -> RGB sulla GPU) oppure conversione in WASM + putImageData ---
var cvgl = document.getElementById('cvgl'), cv2d = document.getElementById('cv2d');
var gl = null, glTex = [], glScale = null;
function initGL(){
  try { gl = cvgl.getContext('webgl', {antialias: false, depth: false, preserveDrawingBuffer: false}); } catch (e) { gl = null; }
  if (!gl) return false;
  var vs = 'attribute vec2 p;varying vec2 t;uniform vec2 s;void main(){t=vec2((p.x+1.0)*0.5,(1.0-p.y)*0.5);gl_Position=vec4(p,0.0,1.0);}';
  var fs = 'precision mediump float;varying vec2 t;uniform sampler2D Y,U,V;uniform vec2 sy,sc;void main(){' +
    'float y=1.1644*(texture2D(Y,vec2(t.x*sy.x,t.y)).r-0.0627);float u=texture2D(U,vec2(t.x*sc.x,t.y)).r-0.5;float v=texture2D(V,vec2(t.x*sc.x,t.y)).r-0.5;' +
    'gl_FragColor=vec4(y+1.7927*v,y-0.2132*u-0.5329*v,y+2.1124*u,1.0);}';
  function sh(type, src){ var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; }
  var pr = gl.createProgram();
  gl.attachShader(pr, sh(gl.VERTEX_SHADER, vs)); gl.attachShader(pr, sh(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(pr);
  if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) { gl = null; return false; }
  gl.useProgram(pr);
  var b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
  var loc = gl.getAttribLocation(pr, 'p'); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  ['Y','U','V'].forEach(function(n, i){
    var tx = gl.createTexture(); gl.activeTexture(gl.TEXTURE0 + i); gl.bindTexture(gl.TEXTURE_2D, tx);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.uniform1i(gl.getUniformLocation(pr, n), i); glTex.push(tx);
  });
  glScale = [gl.getUniformLocation(pr, 'sy'), gl.getUniformLocation(pr, 'sc')];
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
  return true;
}
function drawGL(y, u, v, ly, lu, lv, w, h){
  if (cvgl.width !== w) { cvgl.width = w; cvgl.height = h; gl.viewport(0, 0, w, h); }
  var ch = (h + 1) >> 1, buf = mem.buffer;
  var planes = [[y, ly, h], [u, lu, ch], [v, lv, ch]];
  for (var i = 0; i < 3; i++) {
    gl.activeTexture(gl.TEXTURE0 + i);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.LUMINANCE, planes[i][1], planes[i][2], 0, gl.LUMINANCE, gl.UNSIGNED_BYTE,
      new Uint8Array(buf, planes[i][0], planes[i][1] * planes[i][2]));
  }
  gl.uniform2f(glScale[0], w / ly, 1); gl.uniform2f(glScale[1], ((w + 1) >> 1) / lu, 1);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}
var ctx2d = null;
function draw2D(y, u, v, ly, lu, lv, w, h){
  if (!ctx2d) ctx2d = cv2d.getContext('2d');
  if (cv2d.width !== w) { cv2d.width = w; cv2d.height = h; }
  var p = ex.to_rgba(y, u, v, ly, lu, lv, w, h);
  ctx2d.putImageData(new ImageData(new Uint8ClampedArray(mem.buffer, p, w * h * 4), w, h), 0, 0);
}
function show(which){ cvgl.style.display = which === 'gl' ? 'block' : 'none'; cv2d.style.display = which === '2d' ? 'block' : 'none'; }

// --- decodifica a pezzi, cedendo il controllo per non bloccare la pagina ---
var CHUNK = 16384;
function feedRange(data, from, to){
  var c = data.subarray(from, to), p = ex.in_buf(c.length);
  new Uint8Array(mem.buffer, p, c.length).set(c);
  return ex.feed(c.length);
}
async function bench(data, mode, label){
  if (ex.dec_init() !== 0) throw new Error('dec_init');
  var n = 0, cell = row(label, '...');
  onFrame = function(y, u, v, ly, lu, lv, w, h){
    n++;
    if (mode === 'gl') drawGL(y, u, v, ly, lu, lv, w, h);
    else if (mode === '2d') draw2D(y, u, v, ly, lu, lv, w, h);
  };
  show(mode);
  var busy = 0, t = performance.now(), last = t;
  for (var off = 0; off < data.length; off += CHUNK) {
    var a = performance.now();
    feedRange(data, off, Math.min(data.length, off + CHUNK));
    busy += performance.now() - a;
    if (performance.now() - last > 200) { last = performance.now(); cell.textContent = n + ' frame...'; await sleep(0); }
  }
  var a2 = performance.now(); ex.flush(); busy += performance.now() - a2;
  var fps = n / (busy / 1000);
  cell.textContent = fps.toFixed(0) + ' fps (' + n + ' frame)';
  return {n: n, fps: fps, cell: cell};
}

// --- riproduzione a velocita' reale, in loop ---
var playGen = 0;
function play(data, fps, mode){
  var my = ++playGen;
  var off = 0, decoded = 0, drawn = 0, dropped = 0, stalls = 0, target = 0;
  var cell = row('Riproduzione in tempo reale', '...');
  function pushNext(){
    var end = Math.min(data.length, off + 65536), c = data.subarray(off, end);
    var p = ex.in_buf(c.length);
    new Uint8Array(mem.buffer, p, c.length).set(c);
    ex.push(c.length); off = end;
  }
  function reset(){ ex.dec_init(); off = 0; }
  // un frame alla volta; a fine clip riparte dall'inizio
  function decodeOne(){
    for (var guard = 0; guard < 1000; guard++) {
      if (ex.left() === 0) {
        if (off >= data.length) { var n = ex.flush(); reset(); if (n > 0) return; }
        pushNext();
      }
      if (ex.next() > 0) return;
    }
  }
  reset();
  // disegno dentro la chiamata: dopo, la memoria del frame puo' essere riusata
  onFrame = function(y, u, v, ly, lu, lv, w, h){
    decoded++;
    if (decoded < target) { dropped++; return; }
    if (mode === 'gl') drawGL(y, u, v, ly, lu, lv, w, h); else draw2D(y, u, v, ly, lu, lv, w, h);
    drawn++;
  };
  show(mode);
  var start = performance.now(), statAt = start, statDrawn = 0;
  function tick(){
    if (my !== playGen) return;
    target = Math.floor((performance.now() - start) / 1000 * fps) + 1;
    var budget = performance.now() + 30;
    while (decoded < target && performance.now() < budget) decodeOne();
    if (target - decoded > fps) { start = performance.now() - decoded / fps * 1000; stalls++; }
    var now = performance.now();
    if (now - statAt >= 1000) {
      var rate = (drawn - statDrawn) / ((now - statAt) / 1000); statDrawn = drawn; statAt = now;
      cell.textContent = rate.toFixed(0) + ' fps mostrati su ' + fps.toFixed(0) + ', saltati ' + dropped + (stalls ? ', ritardi ' + stalls : '');
      cell.className = rate >= fps * 0.9 ? 'ok' : 'ko';
    }
    setTimeout(tick, 4);
  }
  tick();
}

async function run(){
  playGen++;
  out.innerHTML = '';
  row('WebAssembly SIMD', simdOk ? 'SI' : 'NO', simdOk ? 'ok' : 'ko');
  if (!simdOk) { row('Esito', 'questo browser non ha WASM SIMD, serve la versione senza SIMD', 'ko'); return; }
  var glOk = gl || initGL();
  row('WebGL', glOk ? 'SI' : 'NO', glOk ? 'ok' : 'ko');
  row('Pagina sicura (HTTPS)', window.isSecureContext ? 'SI' : 'NO (non serve)', 'na');
  var t = performance.now();
  try { if (!ex) await loadWasm(); }
  catch (e) { row('Decoder', 'errore: ' + e.message, 'ko'); return; }
  row('Decoder caricato', ((performance.now() - t) / 1000).toFixed(1) + ' s', 'ok');

  var sec = Math.max(3, Math.min(60, parseInt(document.getElementById('sec').value, 10) || 20));
  var vid = document.getElementById('vid').value.trim();
  var cell = row('Scarico ' + sec + ' s di H.264 da YouTube', '...');
  t = performance.now();
  var res;
  try { res = await fetch('/h264test?sec=' + sec + (vid ? '&v=' + encodeURIComponent(vid) : '')); }
  catch (e) { cell.textContent = 'errore di rete'; cell.className = 'ko'; return; }
  if (!res.ok) { cell.textContent = 'errore: ' + (await res.text()).slice(0, 160); cell.className = 'ko'; return; }
  var fmt = res.headers.get('X-Fmt') || '?';
  var tHead = (performance.now() - t) / 1000, tBody = performance.now();
  var reader = res.body.getReader(), parts = [], size = 0;
  while (true) {
    var r = await reader.read();
    if (r.done) break;
    parts.push(r.value); size += r.value.length;
    cell.textContent = (size / 1e6).toFixed(1) + ' MB';
  }
  var dl = (performance.now() - t) / 1000;
  var data = new Uint8Array(size), o = 0;
  parts.forEach(function(p){ data.set(p, o); o += p.length; });
  if (size < 1000) { cell.textContent = 'stream vuoto'; cell.className = 'ko'; return; }
  cell.textContent = (size / 1e6).toFixed(1) + ' MB in ' + dl.toFixed(1) + ' s'; cell.className = 'ok';
  row('di cui ricerca su YouTube (yt-dlp)', tHead.toFixed(1) + ' s', 'na');
  row('di cui trasferimento dati', ((performance.now() - tBody) / 1000).toFixed(1) + ' s', 'na');
  row('Formato YouTube', fmt, 'na');
  row('Peso del video', (size * 8 / sec / 1e6).toFixed(2) + ' Mbit/s (MJPEG di oggi: circa 40-50)', 'ok');
  await sleep(50);

  var b0 = await bench(data, 'none', 'Solo decodifica');
  var srcFps = b0.n / sec;
  var bGL = glOk ? await bench(data, 'gl', 'Decodifica + disegno WebGL') : null;
  var b2 = await bench(data, '2d', 'Decodifica + disegno senza WebGL');
  var best = bGL && bGL.fps > b2.fps ? bGL : b2, mode = best === bGL ? 'gl' : '2d';
  var ratio = best.fps / srcFps;
  row('Esito', (ratio >= 1.5 ? 'OK: margine x' : ratio >= 1.05 ? 'AL LIMITE: margine x' : 'TROPPO LENTO: x') + ratio.toFixed(1) +
      ' rispetto ai ' + srcFps.toFixed(0) + ' fps del video', ratio >= 1.5 ? 'ok' : ratio >= 1.05 ? 'na' : 'ko');
  play(data, srcFps, mode);
}
document.getElementById('go').onclick = function(){ run(); };
</script></body></html>
"""

# Decoder H.264 (parser + decoder di FFmpeg 7.1, LGPL) compilato in WebAssembly
# con wasi-libc e SIMD. Riceve lo stream Annex-B da JS e chiama on_frame(y,u,v,...)
# per ogni frame. Sorgente in wasm/h264dec.c, si ricompila con wasm/build.sh.
# Compressione lzma + base64, decompresso una volta al primo uso.
H264_WASM_XZ_B64 = "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"
_h264_wasm_gz = None


def h264_wasm_gz():
    global _h264_wasm_gz
    if _h264_wasm_gz is None:
        import base64, gzip, lzma
        _h264_wasm_gz = gzip.compress(lzma.decompress(base64.b64decode(H264_WASM_XZ_B64)), 6)
    return _h264_wasm_gz


TEST_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test browser</title>
<style>
body{background:#111;color:#eee;font:22px/1.35 sans-serif;margin:0;padding:14px}
h1{font-size:26px;margin:0 0 8px}
.r{display:flex;justify-content:space-between;border-bottom:1px solid #333;padding:6px 0}
.ok{color:#4f4;font-weight:bold}.ko{color:#f55;font-weight:bold}.na{color:#fa0;font-weight:bold}
canvas{border:2px solid #555;margin:8px 0;width:160px;height:120px;image-rendering:pixelated}
#clock{font-size:30px;color:#6cf}
</style></head><body>
<h1>Test browser (WebCodecs)</h1>
<div id="clock"></div>
<div>Pagina senza tag video/audio. Provala anche in marcia: il contatore deve continuare a girare.</div>
<canvas id="cv" width="160" height="120"></canvas>
<div id="out"></div>
<script>
var H264="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", WEBP="UklGRs4BAABXRUJQVlA4IMIBAAAQCwCdASogACAAPtFapkwoJSOiKA1RABoJbACdMtU5n6b+AF9PaCcAf3n7Vd4A3AH6m/qr79m6H/6D2AP1V6a72PP2h9JC7R/vTFa5vx43Xicto3SYh41mZeE26jYWjcmAAP77bBvjYXMzvJhisCM79+M2BDpc6dzGGF/6H2uYEURvOfra96V3njC8fhCFgYuyPL+4Ef4zyMQfb7asr/Mqrq30O/Vzn/9IvYx5XpvGsHNflzeW+/+LAGcJsYId8wpUdJiYW0YLHLZLnvCxj0fxJpvFehfB5Wkn8qaH+Vflx3NPw3Ji5zFEQLRyQAOe+OB1EJtR79qVOgTrvzmvInBp4ss0WDw6C+AriIuGP7QbYyJPqMwt/2R0fs4ced9+C5a8pxK9VS+8qzNv84vj9bg1rj2+OE28ExCvHPLnjLfCg3YVM9exdRzRotb/8Ovnz9CJxnxbNgREEzjVXKewAuZzY/btf7sbzvBkA8h/TrEzFZhX/JgP2atsK5X01tJcoqiSlGsg9bztsMQgcFNZSUEFynUdWHfNc7Tu21ou9s9Z6e1siK/qSJP3o7viVAor/8LyneD80fUb+MXwDkyjoFSaAAA=", AVIF="AAAAIGZ0eXBhdmlmAAAAAGF2aWZtaWYxbWlhZk1BMUEAAAD5bWV0YQAAAAAAAAAvaGRscgAAAAAAAAAAcGljdAAAAAAAAAAAAAAAAFBpY3R1cmVIYW5kbGVyAAAAAA5waXRtAAAAAAABAAAAHmlsb2MAAAAARAAAAQABAAAAAQAAASEAAACcAAAAKGlpbmYAAAAAAAEAAAAaaW5mZQIAAAAAAQAAYXYwMUNvbG9yAAAAAGppcHJwAAAAS2lwY28AAAAUaXNwZQAAAAAAAAAgAAAAIAAAABBwaXhpAAAAAAMICAgAAAAMYXYxQ4EgAAAAAAATY29scm5jbHgAAgACAACAAAAAF2lwbWEAAAAAAAAAAQABBAECgwQAAACkbWRhdAoMIAAAAif+bXyQENACMosBFAA5AAAAssAAFVD4aMmepqqV2mRudBzeUZVYKeH4kozKCj//u5/g1jPgUMFuKFwqd5iviPVWqMW/4csgDZt583HdVYIVqA/MyIh52I94qYcDMrU/3IdFFk8PQh2VRBwsIiiKh3KeuPhxunDAi9b8XIyOwno1MtGJKkg4XKd4k8gP7IPBmc28+67J4A==";
var out=document.getElementById('out');
function row(name,val,cls){var d=document.createElement('div');d.className='r';
 d.innerHTML='<span>'+name+'</span><span class="'+cls+'">'+val+'</span>';out.appendChild(d);}
function yn(name,b,txt){row(name,txt||(b?'SI':'NO'),b?'ok':'ko');}
function b64(s){var a=atob(s),u=new Uint8Array(a.length);for(var i=0;i<a.length;i++)u[i]=a.charCodeAt(i);return u;}
var t0=Date.now();
setInterval(function(){document.getElementById('clock').textContent='Contatore: '+((Date.now()-t0)/1000).toFixed(1)+' s';},100);

yn('Pagina sicura (HTTPS)',!!window.isSecureContext);
yn('VideoDecoder',typeof VideoDecoder!=='undefined');
yn('AudioDecoder',typeof AudioDecoder!=='undefined');
yn('MediaSource (MSE)',typeof MediaSource!=='undefined');
yn('createImageBitmap',typeof createImageBitmap!=='undefined');
yn('AudioContext',!!(window.AudioContext||window.webkitAudioContext));
yn('OffscreenCanvas',typeof OffscreenCanvas!=='undefined');
row('User agent',(navigator.userAgent.match(/Chrom[a-z]+\/[\d.]+/)||['?'])[0],'na');

async function cfgVideo(codec,label){
 try{
  var r=await VideoDecoder.isConfigSupported({codec:codec,codedWidth:1280,codedHeight:720});
  var h=await VideoDecoder.isConfigSupported({codec:codec,codedWidth:1280,codedHeight:720,hardwareAcceleration:'prefer-hardware'});
  row(label,(r.supported?'SI':'NO')+' / hw '+(h.supported?'SI':'NO'),r.supported?'ok':'ko');
 }catch(e){row(label,'errore: '+e.message,'ko');}
}
async function cfgAudio(codec,label,extra){
 try{
  var c={codec:codec,sampleRate:44100,numberOfChannels:2};
  if(extra)for(var k in extra)c[k]=extra[k];
  var r=await AudioDecoder.isConfigSupported(c);
  row(label,r.supported?'SI':'NO',r.supported?'ok':'ko');
 }catch(e){row(label,'errore: '+e.message,'ko');}
}
async function img(label,b,type){
 try{
  var bl=new Blob([b64(b)],{type:type});
  var bm=await createImageBitmap(bl);
  yn(label,true,'SI ('+bm.width+'x'+bm.height+')');
 }catch(e){row(label,'NO','ko');}
}
async function realDecode(){
 if(typeof VideoDecoder==='undefined'){row('Decodifica H.264 reale','salto','na');return;}
 try{
  var drawn=false,errMsg='';
  var dec=new VideoDecoder({
   output:function(f){var cv=document.getElementById('cv');cv.getContext('2d').drawImage(f,0,0,cv.width,cv.height);drawn=true;f.close();},
   error:function(e){errMsg=e.message;}
  });
  dec.configure({codec:'avc1.42c01e',codedWidth:160,codedHeight:120});
  dec.decode(new EncodedVideoChunk({type:'key',timestamp:0,data:b64(H264)}));
  await dec.flush();
  if(drawn)row('Decodifica H.264 reale + canvas','SI (vedi riquadro sopra)','ok');
  else row('Decodifica H.264 reale + canvas','NO '+errMsg,'ko');
 }catch(e){row('Decodifica H.264 reale + canvas','NO: '+e.message,'ko');}
}
(async function(){
 if(typeof VideoDecoder!=='undefined'){
  await cfgVideo('avc1.42c01e','H.264 baseline');
  await cfgVideo('avc1.64001f','H.264 high 3.1 (720p)');
  await cfgVideo('avc1.640028','H.264 high 4.0 (1080p)');
  await cfgVideo('vp09.00.31.08','VP9');
  await cfgVideo('av01.0.05M.08','AV1');
 }
 if(typeof AudioDecoder!=='undefined'){
  await cfgAudio('mp4a.40.2','AAC-LC');
  await cfgAudio('opus','Opus');
  await cfgAudio('mp3','MP3');
 }
 await img('WebP',WEBP,'image/webp');
 await img('AVIF',AVIF,'image/avif');
 await realDecode();
 row('Test completato','','na');
})();
</script></body></html>"""


ADMIN_HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>drive-tube admin</title>
<style>
body{background:#111;color:#eee;font:17px/1.4 sans-serif;margin:0;padding:12px 14px 40px}
h1{font-size:22px;margin:0}
h2{font-size:18px;margin:26px 0 8px;border-bottom:1px solid #333;padding-bottom:6px}
a{color:#6cf;text-decoration:none}
#bar{display:flex;flex-wrap:wrap;gap:10px 18px;align-items:center;background:#1b1b1b;border:1px solid #333;padding:10px 12px}
#bar .sp{flex:1}
table{width:100%;border-collapse:collapse;font-size:16px}
th,td{text-align:left;padding:7px 6px;border-bottom:1px solid #2a2a2a;vertical-align:middle}
th{color:#aaa;font-weight:normal}
code{font:16px monospace;background:#222;padding:2px 6px;border-radius:4px;user-select:all}
input{font-size:17px;background:#222;color:#fff;border:1px solid #555;padding:6px 8px;border-radius:6px}
button{font-size:16px;background:#345;color:#fff;border:0;padding:7px 12px;border-radius:6px;cursor:pointer}
button.go{background:#2a5}
button.warn{background:#933}
button:disabled{opacity:.5}
.off{color:#777}
.ok{color:#4f4}.ko{color:#f66}.na{color:#aaa}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
#log{white-space:pre-wrap;font:12px/1.4 monospace;background:#0b0b0b;border:1px solid #333;padding:8px;max-height:60vh;overflow:auto;display:none}
#msg{min-height:22px;color:#fc6}
small{color:#999}
</style></head><body>
<div id="bar">
  <h1>drive-tube admin</h1>
  <span class="na">versione __VER__</span>
  <span class="sp"></span>
  <a href="/home">App</a>
  <a href="/test">Test browser</a>
  <a href="/wasmtest">Test H.264</a>
  <a href="/logout">Esci</a>
</div>
<div id="msg"></div>

<h2>Codici di accesso</h2>
<small>Chi ha un codice attivo puo' usare l'app. Un codice si usa da un solo schermo alla volta: aprendolo su un altro, il primo si ferma.</small>
<table id="codes"><thead><tr><th>Nome</th><th>Codice</th><th>Stato</th><th></th></tr></thead><tbody></tbody></table>
<div class="row">
  <input id="newlabel" placeholder="nome (es. Marco)" size="16">
  <input id="newcode" placeholder="codice (vuoto = casuale)" size="20">
  <button class="go" id="addcode">Aggiungi</button>
</div>

<h2>Sessioni attive</h2>
<table id="sess"><thead><tr><th>Chi</th><th>Stato</th><th>Modo</th><th>ffmpeg</th><th>Ultima attivita'</th><th></th></tr></thead><tbody></tbody></table>

<h2>Server</h2>
<div class="row">
  <span id="updtxt" class="na">Versione __VER__</span>
  <button id="updbtn">Controlla aggiornamenti</button>
  <button class="warn" id="restart">Riavvia il server</button>
</div>
<div class="row">
  <span id="ytdtxt" class="na">yt-dlp: controllo...</span>
  <button id="ytdbtn" disabled>Aggiorna yt-dlp</button>
</div>
<div class="row"><small id="tools"></small></div>

<h2>Cookie YouTube (facoltativi)</h2>
<small>Con i cookie del tuo account YouTube le richieste passano piu' facilmente e nella home compaiono iscrizioni, "guarda piu' tardi" e "mi piace". Chi li usa agisce con il tuo account.</small>
<div class="row"><span id="ckstate" class="na">controllo...</span></div>
<div class="row">
  <span>Usa i cookie per:</span>
  <button data-ck="off">nessuno</button>
  <button data-ck="admin">solo admin</button>
  <button data-ck="all">tutti</button>
</div>
<div class="row" id="feedrow" style="display:none">
  <span>Home mostra:</span>
  <button data-feed="personal">iscrizioni e mi piace</button>
  <button data-feed="related">solo correlati</button>
</div>
<form class="row" method="post" action="/admin/cookies" enctype="multipart/form-data">
  <input type="file" name="f" accept=".txt,text/plain" required>
  <button type="submit">Carica cookie (Netscape)</button>
  <button type="submit" formaction="/admin/cookies?anon=1">Carica senza account (solo visitatore)</button>
  <button type="button" class="warn" id="ckdel">Cancella i cookie</button>
</form>

<h2>PO token (verifica anti-bot di YouTube)</h2>
<small>Senza cookie, i token del plugin bgutil fanno passare le richieste a YouTube. Servono il plugin e il suo server locale (Node.js 22+): il primo pulsante installa e avvia tutti e due.</small>
<div class="row"><span id="potplug" class="na">plugin: controllo...</span></div>
<div class="row"><span id="potsrv" class="na">server: controllo...</span></div>
<div class="row">
  <button class="go" id="potsrvbtn">Installa / aggiorna plugin e server</button>
  <button id="potinst">Solo il plugin</button>
  <button id="pottest">Prova con un video</button>
</div>
<div id="potout" style="white-space:pre-wrap;font:12px/1.4 monospace;background:#0b0b0b;border:1px solid #333;padding:8px;display:none"></div>

<h2>Rete verso YouTube</h2>
<small>Se YouTube risponde "Sign in to confirm you're not a bot" anche con il PO token, di solito blocca l'indirizzo del server. A volte basta un altro client di YouTube o solo IPv4: la diagnosi prova le combinazioni e puoi usare quella che passa. Se non passa niente, serve un proxy su una connessione di casa (o i cookie).</small>
<div class="row"><span id="netcur" class="na">impostazione: controllo...</span></div>
<div class="row">
  <button class="go" id="netdiag">Trova una configurazione che funziona</button>
  <button id="netreset">Torna ai valori predefiniti</button>
</div>
<table id="netres" style="display:none"><thead><tr><th>Configurazione</th><th>Risultato</th><th></th></tr></thead><tbody></tbody></table>
<div class="row">
  <input id="netproxy" placeholder="proxy, es. socks5://casa.example:1080 (vuoto = nessuno)" size="44">
  <button id="netproxybtn">Salva proxy</button>
</div>

<h2>Log</h2>
<div class="row"><button id="logbtn">Mostra il log</button></div>
<div id="log"></div>

<script>
function $(id){ return document.getElementById(id); }
function esc(t){ return String(t == null ? '' : t).replace(/[&<>"]/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]; }); }
function say(t, bad){ $('msg').textContent = t || ''; $('msg').style.color = bad ? '#f66' : '#fc6'; }
function api(path, data){
  return fetch('/admin/api/' + path, {
    method: data === undefined ? 'GET' : 'POST',
    headers: {'Content-Type': 'application/json'},
    body: data === undefined ? undefined : JSON.stringify(data)
  }).then(function(r){
    return r.json().catch(function(){ return {ok: false, error: 'HTTP ' + r.status}; });
  });
}
function ago(s){
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + ' s fa';
  if (s < 3600) return Math.round(s / 60) + ' min fa';
  return (s / 3600).toFixed(1) + ' ore fa';
}
function render(st){
  var cb = $('codes').querySelector('tbody');
  cb.innerHTML = '';
  if (!st.codes.length) cb.innerHTML = '<tr><td colspan="4" class="na">nessun codice: aggiungine uno qui sotto</td></tr>';
  st.codes.forEach(function(c){
    var tr = document.createElement('tr');
    var on = c.enabled !== false;
    tr.innerHTML = '<td' + (on ? '' : ' class="off"') + '>' + esc(c.label) + '</td><td><code>' + esc(c.code) + '</code></td>' +
      '<td class="' + (on ? 'ok' : 'ko') + '">' + (on ? 'attivo' : 'disattivato') + '</td>' +
      '<td><button data-a="toggle">' + (on ? 'Disattiva' : 'Attiva') + '</button> <button class="warn" data-a="del">Elimina</button></td>';
    tr.querySelector('[data-a=toggle]').onclick = function(){ api('code/toggle', {code: c.code}).then(done); };
    tr.querySelector('[data-a=del]').onclick = function(){
      if (!confirm("Eliminare il codice di " + c.label + "? Chi lo usa non potrà più entrare.")) return;
      api('code/del', {code: c.code}).then(done);
    };
    cb.appendChild(tr);
  });
  var sb = $('sess').querySelector('tbody');
  sb.innerHTML = '';
  if (!st.sessions.length) sb.innerHTML = '<tr><td colspan="6" class="na">nessuna sessione</td></tr>';
  st.sessions.forEach(function(x){
    var tr = document.createElement('tr');
    tr.innerHTML = '<td>' + esc(x.who) + ' <small>' + esc(x.sid.slice(0, 4)) + '</small></td>' +
      '<td>' + (x.playing ? 'in riproduzione' : '<span class="na">home</span>') +
      (x.err ? ' <small class="ko">' + esc(x.err) + '</small>' : '') + '</td>' +
      '<td>' + esc(x.vmode) + '</td><td class="' + (x.ff ? 'ok' : 'na') + '">' + (x.ff ? 'attivo' : 'fermo') + '</td>' +
      '<td>' + ago(x.seen) + '</td><td><button class="warn">Chiudi</button></td>';
    tr.querySelector('button').onclick = function(){ api('session/stop', {sid: x.sid}).then(done); };
    sb.appendChild(tr);
  });
}
function load(){ return api('state').then(function(st){ if (st.ok === false) { say(st.error, true); return; } render(st); }); }
function done(r){
  if (r && r.ok === false) say(r.error, true); else say(r && r.msg ? r.msg : '');
  load();
}
$('addcode').onclick = function(){
  var label = $('newlabel').value.trim();
  if (!label) { say('scrivi un nome per il codice', true); return; }
  api('code/add', {label: label, code: $('newcode').value.trim()}).then(function(r){
    if (r.ok) { $('newlabel').value = ''; $('newcode').value = ''; say('codice per ' + label + ': ' + r.code); load(); }
    else say(r.error, true);
  });
};
$('updbtn').onclick = function(){
  var btn = $('updbtn'), txt = $('updtxt');
  if (btn.getAttribute('data-mode') === 'install') {
    btn.disabled = true;
    txt.textContent = 'installo, il server si riavvia...';
    api('update/install', {}).then(function(j){
      if (!j.ok) { txt.textContent = 'errore: ' + j.error; btn.disabled = false; return; }
      txt.textContent = 'installata ' + j.version + ', ricarico...';
      waitRestart(txt);
    });
    return;
  }
  btn.disabled = true;
  txt.textContent = 'controllo...';
  api('update/check').then(function(j){
    btn.disabled = false;
    if (j.error) { txt.textContent = j.error; return; }
    if (j.newer) {
      txt.textContent = 'Nuova versione ' + j.latest + ' (hai ' + j.current + ')';
      btn.textContent = 'Installa';
      btn.setAttribute('data-mode', 'install');
    } else txt.textContent = 'Versione ' + j.current + ', aggiornata';
  });
};
function waitRestart(txt){
  var tries = 0;
  (function wait(){
    setTimeout(function(){
      fetch('/health').then(function(r){ if (r.ok) location.reload(); else throw 0; })
        .catch(function(){ if (++tries < 40) wait(); else txt.textContent = 'riavvio lento, ricarica la pagina'; });
    }, 1500);
  })();
}
$('restart').onclick = function(){
  if (!confirm('Riavviare il server? Tutti i video in corso si fermano.')) return;
  $('updtxt').textContent = 'riavvio...';
  api('restart', {}).then(function(){ waitRestart($('updtxt')); });
};
$('logbtn').onclick = function(){
  var box = $('log');
  fetch('/admin/log').then(function(r){ return r.text(); }).then(function(t){
    box.style.display = 'block';
    box.textContent = t;
    box.scrollTop = box.scrollHeight;
    $('logbtn').textContent = 'Aggiorna il log';
  });
};
function loadYtdlp(force){
  $('ytdtxt').textContent = 'yt-dlp: controllo...';
  return api('ytdlp' + (force ? '?force=1' : '')).then(function(j){
    var t = 'yt-dlp ' + j.installed + (j.latest ? ' (ultima ' + j.latest + ')' : '');
    if (j.error) t += ' - ' + j.error;
    $('ytdtxt').textContent = t;
    $('ytdtxt').className = j.newer ? 'ko' : 'ok';
    $('ytdbtn').disabled = false;
    $('ytdbtn').textContent = j.newer ? 'Aggiorna yt-dlp' : 'Controlla di nuovo';
    $('ytdbtn').setAttribute('data-mode', j.newer ? 'update' : 'check');
    $('tools').textContent = 'in uso: ' + j.path + ' · ' + j.ffmpeg + ' · JavaScript: ' + j.js;
  });
}
$('ytdbtn').onclick = function(){
  var btn = $('ytdbtn');
  if (btn.getAttribute('data-mode') !== 'update') { loadYtdlp(true); return; }
  btn.disabled = true;
  $('ytdtxt').textContent = 'scarico yt-dlp...';
  api('ytdlp/update', {}).then(function(j){
    if (!j.ok) { say('aggiornamento yt-dlp fallito: ' + j.error, true); btn.disabled = false; return; }
    say('yt-dlp aggiornato a ' + j.version);
    loadYtdlp(false);
  });
};
function loadPot(){
  return api('pot').then(function(j){
    $('potplug').textContent = j.plugin.length ? 'plugin installato: ' + j.plugin.join(', ') : 'plugin non installato';
    $('potplug').className = j.plugin.length ? 'ok' : 'ko';
    var t = j.server ? 'server attivo' + (j.server_version ? ' (versione ' + j.server_version + ')' : '') + (j.managed ? ', avviato da drive-tube' : '')
      : (j.built ? 'server compilato ma non risponde' + (j.node ? '' : ': manca node') : 'server non installato in ~/bgutil-ytdlp-pot-provider');
    $('potsrv').textContent = t + (j.node ? ' · node ' + j.node : ' · node non installato');
    $('potsrv').className = j.server ? 'ok' : 'ko';
    var ins = j.install || {}, out = $('potout'), b = $('potsrvbtn');
    b.disabled = !!ins.running;
    if (ins.log && ins.log.length) {
      out.style.display = 'block';
      out.style.color = ins.error ? '#f88' : (ins.ok ? '#8f8' : '#ccc');
      out.textContent = (ins.running ? 'installazione in corso: ' + ins.step + '...' + String.fromCharCode(10, 10) : '') + ins.log.join(String.fromCharCode(10));
    }
    clearTimeout(loadPot.t);
    if (ins.running) loadPot.t = setTimeout(loadPot, 2000);
  });
}
$('potsrvbtn').onclick = function(){
  $('potsrvbtn').disabled = true;
  api('pot/server', {}).then(function(j){
    if (!j.ok) { say(j.error, true); $('potsrvbtn').disabled = false; return; }
    say(''); loadPot();
  });
};
$('potinst').onclick = function(){
  var b = $('potinst'); b.disabled = true; say('scarico il plugin...');
  api('pot/plugin', {}).then(function(j){
    b.disabled = false;
    if (!j.ok) { say('plugin non installato: ' + j.error, true); return; }
    say('plugin installato in ' + j.path); loadPot();
  });
};
function loadNet(){
  return api('pot').then(function(j){
    var n = j.net || {};
    $('netcur').textContent = 'in uso: ' + (n.label || 'client predefiniti');
    if (document.activeElement !== $('netproxy')) $('netproxy').value = n.proxy || '';
  });
}
$('netdiag').onclick = function(){
  var b = $('netdiag'), t = $('netres'), tb = t.querySelector('tbody');
  b.disabled = true; t.style.display = 'table';
  tb.innerHTML = '<tr><td colspan="3" class="na">provo ' + 8 + ' combinazioni, serve circa un minuto...</td></tr>';
  api('yt/diagnose', {}).then(function(j){
    b.disabled = false; tb.innerHTML = '';
    if (!j.ok) { tb.innerHTML = '<tr><td colspan="3" class="ko">' + esc(j.error) + '</td></tr>'; return; }
    var any = false;
    j.results.forEach(function(r){
      var tr = document.createElement('tr');
      any = any || r.ok;
      tr.innerHTML = '<td>' + esc(r.label) + '</td><td class="' + (r.ok ? 'ok' : 'ko') + '">' +
        (r.ok ? 'funziona' + (r.token ? ' (con PO token)' : '') : esc(r.error || 'rifiutato')) + '</td><td>' +
        (r.ok ? '<button class="go">Usa questa</button>' : '') + '</td>';
      var btn = tr.querySelector('button');
      if (btn) btn.onclick = function(){ api('yt/net', {client: r.client, ipv4: r.ipv4}).then(function(x){ done(x); loadNet(); }); };
      tb.appendChild(tr);
    });
    if (!any) say('nessuna combinazione passa: YouTube blocca l\u0027indirizzo del server. Serve un proxy di casa o i cookie.', true);
  });
};
$('netreset').onclick = function(){ api('yt/net', {client: '', ipv4: false}).then(function(x){ done(x); loadNet(); }); };
$('netproxybtn').onclick = function(){ api('yt/net', {proxy: $('netproxy').value.trim()}).then(function(x){ done(x); loadNet(); }); };
loadNet();
$('pottest').onclick = function(){
  var b = $('pottest'), out = $('potout'); b.disabled = true;
  out.style.display = 'block'; out.textContent = 'chiedo a YouTube un video di prova, puo\u0027 servire un minuto...';
  api('pot/test', {}).then(function(j){
    b.disabled = false;
    out.textContent = (j.ok ? (j.token ? 'OK: video raggiungibile e PO token generato' : 'OK: video raggiungibile (nessun PO token usato)') : 'ERRORE: YouTube ha rifiutato la richiesta') + ' [' + (j.net || '') + ']' + String.fromCharCode(10, 10) + (j.lines || []).join(String.fromCharCode(10));
    out.style.color = j.ok ? '#8f8' : '#f88';
  });
};
function loadCk(){
  return api('cookies').then(function(j){
    var names = {off: 'nessuno', admin: 'solo admin', all: 'tutti'};
    $('ckstate').textContent = (j.present ? 'file caricato il ' + new Date(j.mtime * 1000).toLocaleString() : 'nessun file caricato') +
      ' · in uso per: ' + names[j.mode] + (j.present || j.mode === 'off' ? '' : ' (ma manca il file)');
    $('ckstate').className = j.present && j.mode !== 'off' ? 'ok' : 'na';
    document.querySelectorAll('[data-ck]').forEach(function(b){ b.className = b.getAttribute('data-ck') === j.mode ? 'go' : ''; });
    var show = j.present && j.mode !== 'off';
    $('feedrow').style.display = show ? '' : 'none';
    document.querySelectorAll('[data-feed]').forEach(function(b){ b.className = b.getAttribute('data-feed') === j.feed ? 'go' : ''; });
  });
}
document.querySelectorAll('[data-ck]').forEach(function(b){
  b.onclick = function(){ api('cookies/mode', {mode: b.getAttribute('data-ck')}).then(function(r){ done(r); loadCk(); }); };
});
document.querySelectorAll('[data-feed]').forEach(function(b){
  b.onclick = function(){ api('home_feed/mode', {mode: b.getAttribute('data-feed')}).then(function(r){ done(r); loadCk(); }); };
});
$('ckdel').onclick = function(){
  if (!confirm('Cancellare il file dei cookie YouTube dal server?')) return;
  api('cookies/delete', {}).then(function(r){ done(r); loadCk(); });
};
load();
loadYtdlp(false);
loadPot();
loadCk();
setInterval(load, 5000);
</script>
</body></html>
"""

LOGIN_HTML = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>drive-tube</title>
<style>
body{background:#111;color:#fff;font-family:sans-serif;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
form{display:flex;flex-direction:column;gap:16px;width:min(420px,90vw)}
h1{font-size:30px;margin:0 0 6px}
input{font-size:28px;padding:12px;background:#222;color:#fff;border:1px solid #555;border-radius:10px;text-align:center;letter-spacing:2px}
button{font-size:26px;padding:14px;background:#fff;color:#000;border:0;border-radius:10px}
.msg{color:#f88;min-height:24px;font-size:20px}
.note{color:#999;font-size:16px}
a{color:#888;font-size:15px;text-align:center}
</style>
<form method="post" action="__ACTION__">
<h1>__TITLE__</h1>
<div class="msg">__MSG__</div>
<input name="__FIELD__" type="__TYPE__" autocomplete="off" autocapitalize="off" autofocus required placeholder="__PH__">
<button type="submit">Entra</button>
<div class="note">__NOTE__</div>
__LINK__
</form>"""


# ================= accesso =================
# Si entra solo con un codice valido (creati dalla pagina /admin) oppure come
# admin. La password admin sta nella variabile d'ambiente YT_ADMIN del servizio.
ACCESS_DIR = os.path.expanduser("~/.config/drive-tube")
ACCESS_FILE = os.path.join(ACCESS_DIR, "access.json")
ACCESS_LOCK = threading.Lock()
ADMIN_PASS = os.environ.get("YT_ADMIN", "")
CODE_CHARS = "abcdefghjkmnpqrstuvwxyz23456789"   # niente 0/o, 1/l/i
LOGIN_FAILS = {}


def access_load():
    try:
        with open(ACCESS_FILE) as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("codes"), list):
            return d
    except FileNotFoundError:
        pass
    except Exception as e:
        _bprint("access.json illeggibile:", e, flush=True)
    return {"codes": []}


def access_save():
    os.makedirs(ACCESS_DIR, mode=0o700, exist_ok=True)
    tmp = ACCESS_FILE + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(ACCESS, f, indent=1)
    os.replace(tmp, ACCESS_FILE)


ACCESS = access_load()


def new_code():
    return "".join(secrets.choice(CODE_CHARS) for _ in range(10))


def code_label(code):
    if not code:
        return None
    for c in ACCESS["codes"]:
        if c.get("enabled", True) and hmac.compare_digest(str(c.get("code", "")), code):
            return c.get("label") or c["code"][:4]
    return None


def code_label_any(label):
    return any(c.get("enabled", True) and (c.get("label") or c["code"][:4]) == label for c in ACCESS["codes"])


def admin_token():
    if not ADMIN_PASS:
        return ""
    return hmac.new(ADMIN_PASS.encode(), b"drive-tube-admin", hashlib.sha256).hexdigest()


def is_admin(header):
    t = cookie_value(header, "dt_admin")
    return bool(ADMIN_PASS and t) and hmac.compare_digest(t, admin_token())


def who_is(header):
    """Chi fa la richiesta: 'admin', l'etichetta del codice, oppure None."""
    if is_admin(header):
        return "admin"
    return code_label(cookie_value(header, "dt_code"))


def login_blocked(ip):
    now = time.time()
    times = [t for t in LOGIN_FAILS.get(ip, []) if now - t < 600]
    LOGIN_FAILS[ip] = times
    return len(times) >= 8


def login_failed(ip):
    LOGIN_FAILS.setdefault(ip, []).append(time.time())


def cookie_hdr(name, value, max_age=31536000, strict=False):
    return "%s=%s; Max-Age=%d; Path=/; SameSite=%s; HttpOnly" % (
        name, value, max_age, "Strict" if strict else "Lax")


# pagine HTML (senza accesso si va al login); il resto risponde 401
HTML_PATHS = {"/", "/index.html", "/home", "/search", "/channel", "/watch", "/test", "/wasmtest"}


class Handler(BaseHTTPRequestHandler):
    def end_headers(self):
        for k, v in getattr(self, "_extra", []):
            self.send_header(k, v)
        super().end_headers()

    def add_header(self, k, v):
        if not hasattr(self, "_extra"):
            self._extra = []
        self._extra.append((k, v))

    def redirect(self, where):
        self.send_response(302)
        self.send_header("Location", where)
        self.end_headers()

    def client_ip(self):
        return self.client_address[0] if self.client_address else "?"

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        hdr = self.headers.get("Cookie")
        if path == "/favicon.ico":
            self.send_error(404)
            return
        if path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return
        if path == "/login":
            self.page_login()
            return
        if path == "/logout":
            self.add_header("Set-Cookie", cookie_hdr("dt_code", "", 0))
            self.add_header("Set-Cookie", cookie_hdr("dt_admin", "", 0, strict=True))
            self.redirect("/login")
            return
        if path == "/admin" or path.startswith("/admin/"):
            self.admin_get(u, path)
            return
        if self.enter(path):
            try:
                self._get(u, parse_qs(u.query), path)
            finally:
                _tls.s = None

    def enter(self, path):
        """Controlla l'accesso e lega la sessione del browser al thread."""
        hdr = self.headers.get("Cookie")
        who = who_is(hdr)
        page = path in HTML_PATHS or not path.startswith(("/stream", "/ctrl", "/buf", "/status", "/dur",
                                                           "/prime", "/ready", "/bye", "/next", "/h264", "/clog"))
        if not who:
            if page:
                self.redirect("/login")
            else:
                self.send_error(401, "serve il codice di accesso")
            return False
        self.who = who
        sid = cookie_value(hdr, "dt_sid")
        if not (len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)):
            sid = secrets.token_hex(16)
            self.add_header("Set-Cookie", cookie_hdr("dt_sid", sid))
        # un codice si usa da un solo browser alla volta: aprire una pagina lo
        # prende (e chiude l'altro); le richieste del player di un browser che
        # l'ha perso vengono rifiutate, cosi' i due schermi non se lo rubano
        with SESSIONS_LOCK:
            owner = OWNER.get(who)
            if owner and owner != sid and not page:
                busy = True
            else:
                busy = False
                OWNER[who] = sid
        if busy:
            body = b"in uso altrove"
            self.send_response(409)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return False
        bind(get_session(sid, who))
        return True

    def _get(self, u, q, path):
        if path not in ("/buf", "/status", "/dur", "/prime", "/ready", "/clog"):
            print("GET", path, flush=True)

        if path == "/home":
            q = (parse_qs(u.query).get("q") or [""])[0]
            recent = push_recent(q, recent_from_cookie(self.headers.get("Cookie")))
            url_now = cur().url
            here = url_now.split("v=")[-1][:11] if "v=" in url_now else ""
            admin = self.who == "admin"
            items = []
            seen = set()
            # collect queries to run in parallel
            shown_q = []
            for query in recent:
                if query == q or query in shown_q:
                    continue
                if len(shown_q) >= 6:
                    break
                shown_q.append(query)
            all_queries = ([q] if q else []) + shown_q
            # ricerche, suggeriti e (con i cookie) account in parallelo, ognuno
            # nella sessione di chi chiede
            mine = use_cookies()
            show_personal = mine and home_feed() == "personal"
            with ThreadPoolExecutor(max_workers=8) as pool:
                search_futures = {query: pool.submit(in_session(search_yt), query) for query in all_queries}
                related_future = pool.submit(in_session(related_of), here) if here else None
                account_future = pool.submit(in_session(account_sections)) if show_personal else None
            # raccogli canali dai risultati di ricerca per caricare i loro video
            found_channels = []  # (cid, name)
            if q:
                items.append("<h2>Ricerca: %s</h2>" % q.replace("<", ""))
                for vid, title, kind, url in search_futures[q].result():
                    if kind == "channel":
                        items.append(
                            '<a class="card" href="/channel?id=%s"><span>Canale: %s</span></a>'
                            % (vid, title.replace("<", ""))
                        )
                        found_channels.append((url or vid, title))
                    else:
                        items.extend(cards([(vid, title)], seen, here))
            for query in shown_q:
                items.append("<h2>%s</h2>" % query.replace("<", ""))
                for vid, title, kind, url in search_futures[query].result():
                    if kind == "channel":
                        items.append(
                            '<a class="card" href="/channel?id=%s"><span>Canale: %s</span></a>'
                            % (vid, title.replace("<", ""))
                        )
                        found_channels.append((url or vid, title))
                    else:
                        items.extend(cards([(vid, title)], seen, here))
            if here and related_future:
                items.append("<h2>Suggeriti per questo video</h2>")
                items.extend(cards(related_future.result(), seen, here))
            for name, rows in (account_future.result() if account_future else []):
                if rows:
                    items.append("<h2>%s</h2>" % name)
                    items.extend(cards(rows, seen, here))
            # carica video dai canali trovati nelle ricerche
            if found_channels:
                ch_limit = 4  # massimo canali da espandere
                with ThreadPoolExecutor(max_workers=4) as pool:
                    ch_futs = []
                    for cid, cname in found_channels[:ch_limit]:
                        ch_futs.append((cname, pool.submit(in_session(channel_videos), cid, cname)))
                    for cname, fut in ch_futs:
                        try:
                            ch_rows = fut.result()
                            if ch_rows:
                                items.append("<h2>%s</h2>" % cname.replace("<", ""))
                                items.extend(cards(ch_rows[:12], seen, here))
                        except Exception:
                            pass
            chips = "".join(
                '<a class="chip" href="/home?q=%s">%s</a>' % (quote(row), html.escape(row))
                for row in recent
            )
            body = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>home</title>
<style>
body{background:#111;color:#fff;font-family:sans-serif;margin:0}
form{display:flex;gap:8px;padding:12px;position:sticky;top:0;background:#111}
input{flex:1;font-size:18px;padding:8px}
button,a.back{font-size:18px;padding:8px 14px;color:#fff}
.chips{display:flex;gap:8px;flex-wrap:wrap;padding:0 12px 12px}
.chip,.chip:visited{color:#fff;background:#333;text-decoration:none;padding:6px 10px;border-radius:12px}
h2{grid-column:1/-1;font-size:16px;margin:8px 0 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;padding:12px}
.card,.card:visited{color:#fff;text-decoration:none}
.card img{width:100%%;aspect-ratio:16/9;object-fit:cover;background:#000;display:block}
.card span{display:block;padding:8px 0;font-size:15px}
</style>
<form action="/home"><input name="q" placeholder="cerca"><button type="submit">cerca</button>%s<a class="back" href="/logout" style="background:#333;color:#fff;text-decoration:none;padding:8px 14px;font-size:18px">esci</a></form>
<div class="chips">%s</div>
<div class="grid">%s</div>
</body>""" % ('<a class="back" href="/admin" style="background:#333;color:#fff;text-decoration:none;padding:8px 14px;font-size:18px">admin</a>' if admin else "",
              chips, "".join(items) or "<p>nessuna ricerca recente</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", recent_cookie_header(recent))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/search":
            q = (parse_qs(u.query).get("q") or [""])[0]
            recent = push_recent(q, recent_from_cookie(self.headers.get("Cookie")))
            rows = search_yt(q) if q else []
            items = []
            for row in rows:
                vid, title = row[0], row[1]
                kind = row[2] if len(row) > 2 else 'video'
                safe = title.replace('<', '')
                if kind == 'channel':
                    items.append(
                        '<a class="hit" href="/channel?id=%s"><span>Canale: %s</span></a>' % (vid, safe)
                    )
                else:
                    items.append(
                        '<a class="hit" href="/watch?v=%s"><img src="https://i.ytimg.com/vi/%s/hqdefault.jpg"><span>%s</span></a>'
                        % (vid, vid, safe)
                    )
            body = """<!doctype html><meta charset="utf-8"><title>cerca</title>
<style>
body{background:#111;color:#fff;font-family:sans-serif;margin:0}
form{display:flex;gap:8px;padding:12px}
input{flex:1;font-size:18px;padding:8px}
button{font-size:18px;padding:8px 14px}
.hit{display:flex;gap:12px;color:#fff;text-decoration:none;padding:8px 12px;align-items:center}
.hit img{width:160px;height:90px;object-fit:cover;background:#000}
</style>
<form action="/search"><input name="q" value="%s" placeholder="cerca su YouTube"><button>cerca</button></form>
%s""" % (q.replace("<", ""), "".join(items))
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Set-Cookie", recent_cookie_header(recent))
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/channel":
            cid = (parse_qs(u.query).get("id") or [""])[0]
            rows = channel_videos(cid) if cid else []
            items = []
            for vid, title in rows:
                items.append(
                    '<a class="card" href="/watch?v=%s"><img src="https://i.ytimg.com/vi/%s/hqdefault.jpg"><span>%s</span></a>'
                    % (vid, vid, title.replace("<", ""))
                )
            body = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>canale</title>
<style>
body{background:#111;color:#fff;font-family:sans-serif;margin:0}
.top{display:flex;gap:8px;padding:12px}
a.back{font-size:18px;padding:8px 14px;color:#fff;background:#222;text-decoration:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;padding:12px}
.card,.card:visited{color:#fff;text-decoration:none}
.card img{width:100%%;aspect-ratio:16/9;object-fit:cover;background:#000;display:block}
.card span{display:block;padding:8px 0;font-size:15px}
</style>
<div class="top"><a class="back" href="/home">home</a></div>
<div class="grid">%s</div>""" % ("".join(items) or "<p>nessun video</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/watch":
            vid = (parse_qs(u.query).get("v") or [""])[0]
            if vid:
                set_page("https://www.youtube.com/watch?v=" + vid)
            self.send_response(302)
            self.send_header("Location", "/?watch=" + vid)
            self.end_headers()
            return

        if path == "/next":
            nxt = next_of_current() or []
            body = json.dumps(nxt)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body.encode("utf-8"))
            return

        if path == "/buf":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(("%d/%d %.2f" % (len(state["v_q"]), V_CAP, state.get("rate") or 0)).encode())
            return

        if path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write((state.get("err") or "").encode("utf-8"))
            return

        if path == "/test":
            body = TEST_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/dur":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if state.get("is_live"):
                self.wfile.write(b"-1")
            else:
                self.wfile.write(str(int(state["duration"] or 0)).encode())
            return

        if path == "/wasmtest":
            body = WASMTEST_HTML.replace("__VER__", VERSION).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/h264.wasm":
            body = h264_wasm_gz()
            self.send_response(200)
            self.send_header("Content-Type", "application/wasm")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/h264test":
            # qualche secondo di H.264 originale di YouTube, senza ricodifica (Annex-B)
            vid = (q.get("v") or [""])[0].strip()
            url = ("https://www.youtube.com/watch?v=" + vid) if vid else cur().url
            if not url:
                self.send_error(400, "apri prima un video nell'app o indica un id")
                return
            try:
                sec = max(3, min(60, int(float((q.get("sec") or ["20"])[0]))))
            except ValueError:
                sec = 20
            def h264_urls():
                p = subprocess.run(
                    yt_cmd("--no-playlist", "--user-agent", UA,
                           "-f", "bv*[vcodec^=avc1][height<=720]/bv*[vcodec^=avc1]",
                           "--print", "%(width)sx%(height)s %(fps)s fps %(vcodec)s formato %(format_id)s %(protocol)s",
                           "--print", "urls", url),
                    text=True, capture_output=True, timeout=90,
                )
                lines = [x.strip() for x in p.stdout.splitlines() if x.strip()]
                urls = [x for x in lines if x.startswith("http")]
                info = next((x for x in lines if not x.startswith("http")), "?")
                return urls, info, re.sub(r"https?://\S+", "<url>", p.stderr or "")[-300:]

            def h264_start(u):
                ff = subprocess.Popen(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                     "-user_agent", UA, "-headers", HEADERS, "-t", str(sec), "-i", u,
                     "-map", "0:v:0", "-c:v", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "h264", "-"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                first = ff.stdout.read(65536)
                if first:
                    # stderr va svuotato in parallelo, se no ffmpeg puo' bloccarsi
                    threading.Thread(target=ff.stderr.read, daemon=True).start()
                    return ff, first, ""
                ff.wait()
                txt = ff.stderr.read().decode("utf-8", "replace")
                # gli indirizzi googlevideo sono lunghissimi e coprono il motivo dell'errore
                txt = re.sub(r"https?://\S+", "<url>", txt)
                txt = " | ".join(x.strip() for x in txt.splitlines() if x.strip())
                return None, b"", txt[:400]

            ff = first = None
            err = ""
            info = "?"
            for attempt in (1, 2):
                try:
                    urls, info, yerr = h264_urls()
                except Exception as e:
                    urls, yerr = [], str(e)
                if not urls:
                    err = "yt-dlp: nessun formato H.264. " + yerr
                    print("h264test tentativo %d:" % attempt, err, flush=True)
                    continue
                ff, first, ferr = h264_start(urls[0])
                if ff:
                    break
                err = "ffmpeg [" + info + "]: " + (ferr or "nessun dato")
                print("h264test tentativo %d:" % attempt, err, flush=True)
            if not ff:
                body = err.encode("utf-8", "replace")
                self.send_response(502)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.end_headers()
                self.wfile.write(body)
                return
            print("h264test", info, "%ds" % sec, flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Fmt", info.encode("ascii", "replace").decode())
            self.end_headers()
            try:
                chunk = first
                while chunk:
                    self.wfile.write(chunk)
                    chunk = ff.stdout.read(65536)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                if ff.poll() is None:
                    ff.kill()
                ff.wait()
            return

        if path == "/prime":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if path == "/ready":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(b"ok")
            return

        if path in ("/", "/index.html"):
            cfg_changed = apply_settings(self.headers.get("Cookie"))
            watch = (q.get("watch") or [""])[0]
            tpos = (q.get("t") or ["0"])[0]
            if watch and watch not in cur().url:
                set_page("https://www.youtube.com/watch?v=" + watch)
            try:
                if float(tpos) > 0:
                    state["seek_base"] = float(tpos)
            except ValueError:
                pass
            if not cur().url:
                self.send_response(302)
                self.send_header("Location", "/home")
                self.end_headers()
                return
            if not state["duration"]:
                sthread(fetch_duration).start()
            cancel_stop()
            try:
                want = float(tpos or 0)
            except ValueError:
                want = 0
            if cfg_changed or not (ffmpeg_alive() and abs(want - now_pos()) < 8):
                new_session(want)
            vid = cur().url.split("v=")[-1][:11] if "v=" in cur().url else ""
            body = HTML.replace("__DUR__", str(int(state["duration"] or 0))).replace("__FPS__", str(C("FPS"))).replace("__START__", str(int(float(tpos or 0)))).replace("__CFG__", json.dumps(cur().cfg)).replace("__OPTS__", json.dumps(SETTINGS_UI)).replace("__VER__", VERSION).replace("__VMODE__", str(int(cur().cfg.get("VMODE", 1)))).replace("__LIVE__", "1" if state.get("is_live") else "0").replace("WATCHID", vid).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/ctrl":
            if "pos" in q:
                cancel_stop()
                want = float(q["pos"][0])
                apply_settings(self.headers.get("Cookie"))
                mode = (q.get("mode") or [""])[0]
                if mode not in ("h264", "mjpeg"):
                    mode = "h264" if cur().cfg.get("VMODE", 1) else "mjpeg"
                new_session(want, mode)
                msg = "seek %.0f %s" % (state["seek_base"], mode)
            elif "pause" in q:
                if not state["paused"]:
                    state["seek_base"] = now_pos()
                    state["t_play"] = None
                    state["paused"] = True
                    msg = "paused"
                else:
                    state["paused"] = False
                    state["t_play"] = time.perf_counter()
                    msg = "play"
            else:
                msg = "ok"
            print(msg, flush=True)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(msg.encode())
            return

        if path == "/clog":
            # righe di diagnosi del player (stato audio ecc.), nel log del server
            m = (parse_qs(u.query).get("m") or [""])[0]
            m = re.sub(r"[\x00-\x1f]", " ", m)[:200]
            if m:
                print("player:", m, flush=True)
            self.send_response(204)
            self.end_headers()
            return
        if path == "/bye":
            arm_stop("pagina chiusa")
            self.send_response(204)
            self.end_headers()
            return

        if path.startswith("/stream.mp3"):
            try:
                with lock:
                    start_ff()
            except Exception as e:
                print("audio err", e, flush=True)
                self.send_error(500, str(e))
                return
            if not state["has_audio"]:
                print("stream audio rifiutato: ffmpeg non avviato", flush=True)
                self.send_error(404, "no audio")
                return
            # un nuovo collegamento audio (anche un ricollegamento del player)
            # annulla lo spegnimento programmato dalla chiusura del precedente
            cancel_stop()
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            gen = state["gen"]
            sent = 0
            print("audio: player collegato (in coda %d KB)" % (sum(len(c) for c in list(state["a_q"])) // 1024), flush=True)
            try:
                while state["gen"] == gen:
                    if client_gone(self):
                        print("audio: player scollegato dopo %d KB" % (sent // 1024), flush=True)
                        arm_stop("audio client chiuso")
                        return
                    if state["a_q"]:
                        c = state["a_q"].popleft()
                        self.wfile.write(c)
                        sent += len(c)
                    elif state.get("ff_done") == gen:
                        print("audio: fine, inviati %d KB" % (sent // 1024), flush=True)
                        return
                    else:
                        time.sleep(0.02)
            except (BrokenPipeError, ConnectionResetError, OSError):
                print("audio: connessione persa dopo %d KB" % (sent // 1024), flush=True)
                arm_stop("audio client via")
            return

        if path.startswith("/stream.h264"):
            # record [lunghezza u32][pts f64][frame Annex-B], uno per frame
            if state["vmode"] != "h264":
                self.send_error(409, "sessione in modalita' %s" % state["vmode"])
                return
            try:
                with lock:
                    start_ff()
            except Exception as e:
                print("video h264 err", e, flush=True)
                self.send_error(500, str(e))
                return
            cancel_stop()
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            gen = state["gen"]
            try:
                while state["gen"] == gen:
                    if client_gone(self):
                        arm_stop("video client chiuso")
                        return
                    if not state["v_q"]:
                        if state.get("ff_done") == gen:
                            return
                        time.sleep(0.01)
                        continue
                    out = []
                    while state["v_q"] and len(out) < 64:
                        out.append(state["v_q"].popleft())
                    self.wfile.write(b"".join(out))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                arm_stop("video client via")
            return

        if path.startswith("/stream.mjpg"):
            if state["vmode"] != "mjpeg":
                self.send_error(409, "sessione in modalita' %s" % state["vmode"])
                return
            try:
                with lock:
                    start_ff()
            except Exception as e:
                print("video err", e, flush=True)
                self.send_error(500, str(e))
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            gen = state["gen"]
            try:
                while state["gen"] == gen:
                    if client_gone(self):
                        arm_stop("video client chiuso")
                        return
                    if not state["v_q"]:
                        if state.get("ff_done") == gen:
                            return
                        time.sleep(0.01)
                        continue
                    jpg = state["v_q"].popleft()
                    hdr = (
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(jpg)).encode()
                        + b"\r\n\r\n"
                    )
                    self.wfile.write(hdr + jpg + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                arm_stop("video client via")
            return

        # indirizzo sconosciuto: si torna alla home
        self.redirect("/home")

    # ---------- login ----------
    def page_login(self, msg="", status=200, admin=False):
        if admin:
            fill = {"__ACTION__": "/admin/login", "__TITLE__": "Admin", "__FIELD__": "password",
                    "__TYPE__": "password", "__PH__": "password admin", "__NOTE__": "",
                    "__LINK__": '<a href="/login">accesso con codice</a>'}
        else:
            note = "" if ACCESS["codes"] else "Nessun codice creato: il proprietario lo crea dalla pagina admin."
            fill = {"__ACTION__": "/login", "__TITLE__": "drive-tube", "__FIELD__": "code",
                    "__TYPE__": "text", "__PH__": "codice di accesso", "__NOTE__": note,
                    "__LINK__": '<a href="/admin">admin</a>'}
        fill["__MSG__"] = html.escape(msg)
        body = LOGIN_HTML
        for k, v in fill.items():
            body = body.replace(k, v)
        body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def json_out(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------- admin (GET) ----------
    def admin_get(self, u, path):
        hdr = self.headers.get("Cookie")
        if not ADMIN_PASS:
            body = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                    "<body style='background:#111;color:#eee;font-family:sans-serif;padding:20px;line-height:1.5'>"
                    "<h1>Pagina admin spenta</h1><p>Per attivarla aggiungi la password admin al servizio sull'EC2:</p>"
                    "<pre style='background:#222;padding:10px'>sudo systemctl edit drive-tube</pre>"
                    "<p>e scrivi:</p><pre style='background:#222;padding:10px'>[Service]\nEnvironment=YT_ADMIN=la-tua-password</pre>"
                    "<p>poi <code>sudo systemctl restart drive-tube</code>.</p></body>").encode("utf-8")
            self.send_response(503)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(body)
            return
        if not is_admin(hdr):
            if path.startswith("/admin/api/") or path == "/admin/log":
                self.json_out({"ok": False, "error": "accesso admin scaduto, ricarica la pagina"}, 403)
            else:
                self.page_login(admin=True)
            return
        if path == "/admin":
            body = (ADMIN_HTML.replace("__VER__", VERSION).replace("__MAXC__", str(MAX_PER_CODE))).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if path == "/admin/api/state":
            now = time.time()
            with SESSIONS_LOCK:
                sess = list(SESSIONS.values())
            rows = []
            for x in sorted(sess, key=lambda x: -x.seen):
                ff = x.state.get("ff")
                ff_alive = bool(ff and ff.poll() is None)
                rows.append({
                    "sid": x.sid, "who": x.who,
                    "playing": bool(x.url) and (ff_alive or now - x.seen < 30),
                    "vmode": x.state.get("vmode"), "ff": ff_alive,
                    "seen": now - x.seen, "err": x.state.get("err") or "",
                })
            with ACCESS_LOCK:
                codes = [dict(c) for c in ACCESS["codes"]]
            self.json_out({"ok": True, "version": VERSION, "codes": codes, "sessions": rows})
            return
        if path == "/admin/api/update/check":
            self.json_out(check_update())
            return
        if path == "/admin/api/cookies":
            st = os.stat(COOKIES) if os.path.isfile(COOKIES) else None
            self.json_out({"present": bool(st), "size": st.st_size if st else 0,
                           "mtime": int(st.st_mtime) if st else 0, "mode": cookie_mode(),
                           "feed": home_feed()})
            return
        if path == "/admin/api/pot":
            st = pot_status()
            n = ACCESS.get("yt") or {}
            st["net"] = {"label": yt_net_label(n), "client": n.get("client", ""),
                         "ipv4": bool(n.get("ipv4")), "proxy": n.get("proxy", "")}
            self.json_out(st)
            return
        if path == "/admin/api/ytdlp":
            self.json_out(ytdlp_status(force=bool(parse_qs(u.query).get("force"))))
            return
        if path == "/admin/log":
            try:
                lines = open(os.path.expanduser("~/yt.log")).read().splitlines()
            except Exception:
                lines = ["log non trovato: ~/yt.log"]
            lines = [x for x in lines if "GET /admin" not in x]
            body = "\n".join(lines[-500:]).encode("utf-8", "replace")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.redirect("/admin")

    # ---------- POST ----------
    def do_POST(self):
        path = urlparse(self.path).path
        hdr = self.headers.get("Cookie")
        n = int(self.headers.get("Content-Length") or 0)
        if n > (20000000 if path == "/admin/cookies" else 2000000):
            self.send_error(413)
            return
        raw = self.rfile.read(n)
        if path == "/login":
            self.post_login(raw)
        elif path == "/admin/login":
            self.post_admin_login(raw)
        elif path == "/admin/cookies":
            if not ADMIN_PASS or not is_admin(hdr):
                self.send_error(403)
                return
            self.post_cookies(raw, anon=parse_qs(urlparse(self.path).query).get("anon") == ["1"])
        elif path.startswith("/admin/api/"):
            if not ADMIN_PASS or not is_admin(hdr):
                self.json_out({"ok": False, "error": "accesso admin scaduto, ricarica la pagina"}, 403)
                return
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                data = {}
            self.admin_api(path[len("/admin/api/"):], data if isinstance(data, dict) else {})
        else:
            self.send_error(404)

    def post_login(self, raw):
        ip = self.client_ip()
        if login_blocked(ip):
            self.page_login("Troppi tentativi sbagliati: riprova tra qualche minuto.", 429)
            return
        code = (parse_qs(raw.decode("utf-8", "replace")).get("code") or [""])[0].strip().lower()
        label = code_label(code)
        if not label:
            login_failed(ip)
            _bprint("accesso negato da", ip, flush=True)
            self.page_login("Codice non valido.", 403)
            return
        _bprint("accesso:", label, "da", ip, flush=True)
        self.add_header("Set-Cookie", cookie_hdr("dt_code", code))
        sid = cookie_value(self.headers.get("Cookie"), "dt_sid")
        if not (len(sid) == 32 and all(c in "0123456789abcdef" for c in sid)):
            self.add_header("Set-Cookie", cookie_hdr("dt_sid", secrets.token_hex(16)))
        self.redirect("/home")

    def post_admin_login(self, raw):
        ip = self.client_ip()
        if not ADMIN_PASS:
            self.redirect("/admin")
            return
        if login_blocked(ip):
            self.page_login("Troppi tentativi sbagliati: riprova tra qualche minuto.", 429, admin=True)
            return
        pw = (parse_qs(raw.decode("utf-8", "replace")).get("password") or [""])[0]
        if not hmac.compare_digest(pw.encode(), ADMIN_PASS.encode()):
            login_failed(ip)
            _bprint("accesso admin negato da", ip, flush=True)
            self.page_login("Password sbagliata.", 403, admin=True)
            return
        _bprint("accesso admin da", ip, flush=True)
        self.add_header("Set-Cookie", cookie_hdr("dt_admin", admin_token(), 30 * 86400, strict=True))
        self.redirect("/admin")

    def admin_api(self, what, data):
        if what == "code/add":
            label = str(data.get("label") or "").strip()[:40]
            code = str(data.get("code") or "").strip().lower()
            if not label:
                self.json_out({"ok": False, "error": "manca il nome"})
                return
            if code and not (4 <= len(code) <= 40 and all(c.isalnum() or c in "-_" for c in code) and code.isascii()):
                self.json_out({"ok": False, "error": "codice: da 4 a 40 tra lettere, numeri, - e _"})
                return
            with ACCESS_LOCK:
                code = code or new_code()
                if any(c.get("code") == code for c in ACCESS["codes"]):
                    self.json_out({"ok": False, "error": "codice gia' usato"})
                    return
                if label.lower() == "admin" or any((c.get("label") or "").lower() == label.lower() for c in ACCESS["codes"]):
                    self.json_out({"ok": False, "error": "nome gia' usato, scegline un altro"})
                    return
                ACCESS["codes"].append({"label": label, "code": code, "enabled": True, "created": int(time.time())})
                access_save()
            _bprint("admin: nuovo codice per", label, flush=True)
            self.json_out({"ok": True, "code": code})
            return
        if what in ("code/toggle", "code/del"):
            code = str(data.get("code") or "")
            with ACCESS_LOCK:
                hit = [c for c in ACCESS["codes"] if c.get("code") == code]
                if not hit:
                    self.json_out({"ok": False, "error": "codice non trovato"})
                    return
                if what == "code/del":
                    ACCESS["codes"] = [c for c in ACCESS["codes"] if c.get("code") != code]
                else:
                    hit[0]["enabled"] = not hit[0].get("enabled", True)
                access_save()
                label = hit[0].get("label")
                off = what == "code/del" or not hit[0].get("enabled", True)
            if off:
                # chi usava quel codice viene chiuso subito
                with SESSIONS_LOCK:
                    gone = [x for x in SESSIONS.values() if x.who == label and not code_label_any(label)]
                    for x in gone:
                        SESSIONS.pop(x.sid, None)
                for x in gone:
                    close_session(x)
            _bprint("admin:", what, label, flush=True)
            self.json_out({"ok": True})
            return
        if what == "session/stop":
            sid = str(data.get("sid") or "")
            with SESSIONS_LOCK:
                x = SESSIONS.pop(sid, None)
            if x:
                close_session(x)
            self.json_out({"ok": True, "msg": "sessione chiusa" if x else "sessione gia' chiusa"})
            return
        if what == "update/install":
            try:
                new = install_update()
            except Exception as e:
                self.json_out({"ok": False, "error": str(e)}, 500)
                return
            self.json_out({"ok": True, "version": new})
            threading.Thread(target=restart_self, daemon=True).start()
            return
        if what == "ytdlp/update":
            try:
                v = ytdlp_update()
            except Exception as e:
                _bprint("aggiornamento yt-dlp fallito:", e, flush=True)
                self.json_out({"ok": False, "error": str(e)}, 500)
                return
            _bprint("yt-dlp aggiornato a", v, flush=True)
            self.json_out({"ok": True, "version": v})
            return
        if what == "cookies/mode":
            mode = data.get("mode")
            if mode not in ("off", "admin", "all"):
                self.json_out({"ok": False, "error": "scelta non valida"})
                return
            with ACCESS_LOCK:
                ACCESS["cookies"] = mode
                access_save()
            feed_cache.clear()
            _bprint("admin: cookie YouTube per", mode, flush=True)
            self.json_out({"ok": True})
            return
        if what == "home_feed/mode":
            mode = data.get("mode")
            if mode not in ("personal", "related"):
                self.json_out({"ok": False, "error": "scelta non valida"})
                return
            with ACCESS_LOCK:
                ACCESS["home_feed"] = mode
                access_save()
            feed_cache.clear()
            _bprint("admin: home feed", mode, flush=True)
            self.json_out({"ok": True})
            return
        if what == "cookies/delete":
            try:
                os.remove(COOKIES)
            except FileNotFoundError:
                pass
            feed_cache.clear()
            _bprint("admin: cookie YouTube cancellati", flush=True)
            self.json_out({"ok": True, "msg": "cookie cancellati"})
            return
        if what == "pot/plugin":
            try:
                where = pot_install_plugin()
            except Exception as e:
                _bprint("plugin PO token non installato:", e, flush=True)
                self.json_out({"ok": False, "error": str(e)}, 500)
                return
            _bprint("plugin PO token installato in", where, flush=True)
            self.json_out({"ok": True, "path": where})
            return
        if what == "pot/server":
            if not pot_install_begin():
                self.json_out({"ok": False, "error": "installazione gia' in corso"}, 409)
                return
            _bprint("admin: installo il server PO token", flush=True)
            self.json_out({"ok": True})
            return
        if what == "pot/test":
            r = pot_test()
            r["net"] = yt_net_label(ACCESS.get("yt") or {})
            self.json_out(r)
            return
        if what == "yt/diagnose":
            self.json_out({"ok": True, "results": yt_diagnose()})
            return
        if what == "yt/net":
            cur_net = dict(ACCESS.get("yt") or {})
            if "client" in data:
                c = str(data.get("client") or "")
                if c and not re.fullmatch(r"[a-z_]{2,30}", c):
                    self.json_out({"ok": False, "error": "client non valido"})
                    return
                cur_net["client"] = c
            if "ipv4" in data:
                cur_net["ipv4"] = bool(data.get("ipv4"))
            if "proxy" in data:
                px = str(data.get("proxy") or "").strip()
                if px and not re.fullmatch(r"(https?|socks5h?|socks4a?)://\S+", px):
                    self.json_out({"ok": False, "error": "proxy non valido: usa http://, https:// o socks5://"})
                    return
                cur_net["proxy"] = px
            with ACCESS_LOCK:
                ACCESS["yt"] = cur_net
                access_save()
            feed_cache.clear()
            _bprint("admin: rete YouTube ->", yt_net_label(cur_net), flush=True)
            self.json_out({"ok": True, "msg": "salvato: " + yt_net_label(cur_net)})
            return
        if what == "restart":
            _bprint("admin: riavvio", flush=True)
            self.json_out({"ok": True})
            threading.Thread(target=restart_self, daemon=True).start()
            return
        self.json_out({"ok": False, "error": "azione sconosciuta"}, 404)

    def post_cookies(self, raw, anon=False):
        text = raw.decode("utf-8", "replace")
        if "filename=" in text:
            parts = text.split("\r\n\r\n", 1)
            if len(parts) == 2:
                text = parts[1].rsplit("\r\n--", 1)[0]
        if "youtube.com" not in text or "\t" not in text:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("file non valido: servono i cookie di youtube.com in formato Netscape".encode("utf-8"))
            return
        text, kept, dropped = cookies_youtube_only(text, anon)
        if not kept:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("nel file non ci sono cookie di youtube.com".encode("utf-8"))
            return
        fd = os.open(COOKIES, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        feed_cache.clear()
        _bprint("admin: cookie tenuti %d (youtube.com%s), scartati %d" % (kept, ", senza account" if anon else "", dropped), flush=True)
        with SESSIONS_LOCK:
            for x in SESSIONS.values():
                x.state["err"] = ""
        _bprint("admin: cookie YouTube caricati", flush=True)
        self.redirect("/admin")

    def log_message(self, *a):
        pass


class QuietServer(ThreadingHTTPServer):
    # connessioni chiuse a meta' (pagina cambiata, scanner, handshake TLS falliti)
    # non riempiono il log di traceback
    daemon_threads = True

    def handle_error(self, request, client_address):
        if isinstance(sys.exc_info()[1], (ssl.SSLError, ConnectionError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def serve_https():
    # HTTPS facoltativo, su una porta separata: parte solo se trova il certificato.
    # Serve per le API del browser riservate alle pagine sicure (es. WebCodecs).
    cert = os.environ.get("YT_CERT") or os.path.expanduser("~/yt-cert/fullchain.pem")
    key = os.environ.get("YT_KEY") or os.path.expanduser("~/yt-cert/privkey.pem")
    if not (os.path.exists(cert) and os.path.exists(key)):
        print("https spento: certificato non trovato in", os.path.dirname(cert), flush=True)
        return
    port = int(os.environ.get("YT_HTTPS_PORT") or 8443)
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        srv = QuietServer(("0.0.0.0", port), Handler)
        # handshake nel thread della richiesta, non nel ciclo che accetta le connessioni
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    except Exception as e:
        print("https non avviato:", e, flush=True)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _bprint("https://0.0.0.0:%d/" % port, flush=True)


if __name__ == "__main__":
    _bprint("http://0.0.0.0:%d/" % PORT, flush=True)
    print("codici attivi: %d, admin %s" % (
        sum(1 for c in ACCESS["codes"] if c.get("enabled", True)),
        "attivo" if ADMIN_PASS else "spento (manca YT_ADMIN)"), flush=True)
    threading.Thread(target=janitor, daemon=True).start()
    threading.Thread(target=pot_supervisor, daemon=True).start()
    serve_https()
    try:
        QuietServer(("0.0.0.0", PORT), Handler).serve_forever()
    finally:
        stop_all()

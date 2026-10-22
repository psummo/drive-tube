"""Programmi esterni: yt-dlp (versione e aggiornamento) e Node.js (ricerca e installazione)."""
import urllib.request
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
from .config import YTDLP_API, YTDLP_LOCAL, YTDLP_URL, feed_cache


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


def _ver_key(v):
    nums = []
    for part in v.replace("-", ".").split("."):
        digits = "".join(c for c in part if c.isdigit())
        nums.append(int(digits) if digits else 0)
    return tuple(nums)

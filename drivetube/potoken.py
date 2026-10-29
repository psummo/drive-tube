"""PO token (bgutil) e diagnosi della rete verso YouTube."""
import urllib.request
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from .config import UA
from .context import _bprint
from .access import ACCESS
from .tools import NODE_MIN, find_node, node_env, node_install, tool_version
from .netutil import active_proxy, open_url
from .youtube import YT_VARIANTS, yt_cmd, yt_net_label


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
    ok = p.returncode == 0
    if ok:
        # --simulate controlla solo l'estrazione: lo stream vero puo' dare 403
        sok, serr = stream_probe(net)
        keep.append("stream: " + ("scaricabile" if sok else serr))
        if not sok:
            ok, err = False, "stream rifiutato (" + serr + ")"
    return {"ok": ok, "token": tokens, "error": err,
            "lines": keep[-25:] or ["nessuna riga utile nell'output"]}


def stream_probe(net=None):
    """Prende l'URL dello stream e ne scarica i primi KB, come fara' ffmpeg."""
    try:
        p = subprocess.run(
            yt_cmd("-g", "--no-playlist", "-f", "bv*[vcodec^=avc1][height<=360]/b",
                   "--user-agent", UA, POT_TEST_VIDEO, net=net),
            capture_output=True, text=True, timeout=120,
        )
        urls = [x for x in p.stdout.splitlines() if x.startswith("http")]
        if not urls:
            return False, "nessun URL"
        proxy = active_proxy(((net if net is not None else ACCESS.get("yt")) or {}).get("proxy", ""))
        r = open_url(urls[0], {"User-Agent": UA, "Range": "bytes=0-65535"}, 20, proxy)
        r.read(1024)
        r.close()
        return True, ""
    except urllib.error.HTTPError as e:
        return False, "HTTP %d" % e.code
    except Exception as e:
        return False, str(e)[:80]


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

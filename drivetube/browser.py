"""Cookie di YouTube presi da un browser vero sul server: Chromium comandato da
Playwright (Node) apre YouTube, accetta il banner dei cookie, apre un video e
restituisce i cookie, compresi quelli che le pagine web non possono leggere. Si
installa dalla pagina admin; Chromium ha bisogno di alcune librerie di sistema
(una volta: sudo .../playwright/cli.js install-deps chromium)."""
import glob
import json
import os
import subprocess
import time

from .config import COOKIES, UA
from .context import print
from .tools import find_node, node_env
from .youtube import COOKIE_CHECK, cookies_write, cookies_youtube_only, run_yt

BROWSER_DIR = os.path.expanduser("~/.local/share/drive-tube/browser")
PLAYWRIGHT = "playwright@1.56.0"
SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "browser_cookies.js")
TEST_VIDEO = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


def _browsers_path():
    return os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or os.path.expanduser("~/.cache/ms-playwright")


def browser_status():
    """Cosa c'e' gia': Node, il pacchetto playwright, Chromium."""
    node, major = find_node()
    pkg = os.path.join(BROWSER_DIR, "node_modules", "playwright", "package.json")
    have_pw = os.path.isfile(pkg)
    have_chromium = bool(glob.glob(os.path.join(_browsers_path(), "chromium*")))
    return {"node": bool(node), "playwright": have_pw, "chromium": have_chromium,
            "ready": bool(node) and have_pw and have_chromium,
            "deps_cmd": 'sudo "%s" %s install-deps chromium' % (
                node or "node", os.path.join(BROWSER_DIR, "node_modules", "playwright", "cli.js"))}


def _npm(node):
    # npm accanto a node (installazione ufficiale, nvm, ~/.local/share/drive-tube/node)
    base = os.path.dirname(os.path.dirname(os.path.realpath(node)))
    for cand in (os.path.join(base, "lib", "node_modules", "npm", "bin", "npm-cli.js"),
                 os.path.join(base, "node_modules", "npm", "bin", "npm-cli.js")):
        if os.path.isfile(cand):
            return [node, cand]
    npm = os.path.join(os.path.dirname(node), "npm")
    return [npm] if os.path.isfile(npm) else ["npm"]


def browser_install():
    """Installa playwright in BROWSER_DIR e scarica Chromium (qualche minuto)."""
    node, major = find_node(refresh=True)
    if not node:
        raise RuntimeError("manca Node: installalo dalla sezione PO token")
    os.makedirs(BROWSER_DIR, exist_ok=True)
    env = node_env(node)
    p = subprocess.run(_npm(node) + ["install", "--no-audit", "--no-fund", "--prefix", BROWSER_DIR, PLAYWRIGHT],
                       capture_output=True, text=True, timeout=900, env=env)
    if p.returncode != 0:
        raise RuntimeError("npm non riuscito: " + (p.stderr or p.stdout).strip()[-300:])
    cli = os.path.join(BROWSER_DIR, "node_modules", "playwright", "cli.js")
    p = subprocess.run([node, cli, "install", "chromium"], capture_output=True, text=True, timeout=1200, env=env)
    if p.returncode != 0:
        raise RuntimeError("download di Chromium non riuscito: " + (p.stderr or p.stdout).strip()[-300:])
    print("browser per i cookie installato", flush=True)
    return browser_status()


def _netscape(cookies):
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        dom = c.get("domain") or ""
        exp = c.get("expires") or 0
        lines.append("\t".join([
            ("#HttpOnly_" if c.get("httpOnly") else "") + dom,
            "TRUE" if dom.startswith(".") else "FALSE",
            c.get("path") or "/",
            "TRUE" if c.get("secure") else "FALSE",
            str(int(exp)) if exp and exp > 0 else "0",
            c.get("name") or "",
            c.get("value") or "",
        ]))
    return "\n".join(lines) + "\n"


def browser_cookies():
    """Apre YouTube nel browser del server, salva i cookie ottenuti in ~/cookies.txt
    (solo youtube.com, senza account) e prova un video con quei cookie."""
    st = browser_status()
    if not st["ready"]:
        raise RuntimeError("prima installa il browser (pulsante qui sopra)")
    node, _ = find_node()
    env = dict(node_env(node), DT_BROWSER_DIR=BROWSER_DIR, DT_UA=UA)
    t0 = time.time()
    p = subprocess.run([node, SCRIPT], capture_output=True, text=True, timeout=180, env=env)
    try:
        res = json.loads(p.stdout or "{}")
    except ValueError:
        res = {"ok": False, "error": (p.stderr or p.stdout or "nessuna risposta").strip()[-300:]}
    if not res.get("ok"):
        err = res.get("error") or (p.stderr or "").strip()[-300:]
        if any(k in err for k in ("error while loading shared libraries", "missing dependencies",
                                  "Missing libraries", "install-deps", "cannot open shared object")):
            err = ("a Chromium mancano librerie di sistema. Via SSH lancia una volta: " + st["deps_cmd"] +
                   "  (dettagli: " + err[:300] + ")")
        raise RuntimeError("il browser non e' riuscito: " + err)
    text, kept, dropped = cookies_youtube_only(_netscape(res.get("cookies") or []))
    if not kept:
        raise RuntimeError("YouTube non ha dato cookie (passi: %s)" % ", ".join(res.get("steps", [])))
    cookies_write(text)
    COOKIE_CHECK.update(t=time.time(), ok=None, msg="cookie da visitatore presi dal browser")
    names = sorted({line.split("\t")[5] for line in text.splitlines() if line.count("\t") == 6})
    print("cookie YouTube presi dal browser: %d (%s) in %.0f s" % (kept, ", ".join(names), time.time() - t0), flush=True)
    # prova: yt-dlp con questi cookie supera la verifica anti-bot?
    t = run_yt("--simulate", "--no-playlist", "-f", "b", TEST_VIDEO, cookies=True,
               capture_output=True, text=True, timeout=150)
    err = t.stderr or ""
    if t.returncode == 0:
        verdict = "prova video riuscita: YouTube accetta le richieste con questi cookie"
    elif "Sign in to confirm" in err or "not a bot" in err:
        verdict = "prova video: YouTube chiede ancora la verifica anti-bot"
    else:
        verdict = "prova video non riuscita: " + (err.strip().splitlines()[-1][:160] if err.strip() else "errore")
    print(verdict, flush=True)
    return {"ok": True, "kept": kept, "names": names, "steps": res.get("steps", []), "verdict": verdict,
            "video_ok": t.returncode == 0}

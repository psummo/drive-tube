"""Cookie di YouTube presi da un browser vero sul server: Chromium comandato da
Playwright (Node) apre YouTube, accetta il banner dei cookie, apre un video e
restituisce i cookie, compresi quelli che le pagine web non possono leggere. Si
installa dalla pagina admin; Chromium ha bisogno di alcune librerie di sistema
(una volta: sudo .../playwright/cli.js install-deps chromium)."""
import glob
import json
import os
import queue
import subprocess
import threading
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


# lavoro in corso, letto dalla pagina admin mentre va avanti
JOB = {"running": False, "steps": [], "result": None, "error": "", "t": 0.0}
_job_lock = threading.Lock()


def _step(text):
    JOB["steps"].append("%3.0f s  %s" % (time.time() - JOB["t"], text))
    print("browser cookie:", text, flush=True)


def browser_cookies_start():
    """Avvia browser_cookies() in background. False se ce n'e' gia' uno in corso."""
    with _job_lock:
        if JOB["running"]:
            return False
        JOB.update(running=True, steps=[], result=None, error="", t=time.time())

    def run():
        try:
            JOB["result"] = browser_cookies()
        except Exception as e:
            JOB["error"] = str(e)
            _step("ERRORE: " + str(e)[:300])
        finally:
            JOB["running"] = False
    threading.Thread(target=run, daemon=True).start()
    return True


def browser_job():
    return {"running": JOB["running"], "steps": list(JOB["steps"]), "result": JOB["result"],
            "error": JOB["error"], "age": time.time() - JOB["t"] if JOB["t"] else None}


def browser_cookies():
    """Apre YouTube nel browser del server, salva i cookie ottenuti in ~/cookies.txt
    (solo youtube.com, senza account) e prova un video con quei cookie."""
    st = browser_status()
    if not st["ready"]:
        raise RuntimeError("prima installa il browser (pulsante qui sopra)")
    node, _ = find_node()
    env = dict(node_env(node), DT_BROWSER_DIR=BROWSER_DIR, DT_UA=UA)
    t0 = time.time()
    if not JOB["t"]:
        JOB["t"] = t0
    p = subprocess.Popen([node, SCRIPT], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    errlines = []

    def read_err():
        for line in p.stderr:
            line = line.rstrip()
            if line.startswith("PASSO: "):
                _step(line[7:])
            elif line:
                errlines.append(line)
    rt = threading.Thread(target=read_err, daemon=True)
    rt.start()
    try:
        stdout, _ = p.communicate(timeout=180)
    except subprocess.TimeoutExpired:
        p.kill()
        raise RuntimeError("il browser non ha finito entro 3 minuti (YouTube non risponde?)")
    rt.join(timeout=5)
    try:
        res = json.loads(stdout or "{}")
    except ValueError:
        res = {"ok": False, "error": ("\n".join(errlines) or stdout or "nessuna risposta").strip()[-300:]}
    if not res.get("ok"):
        err = res.get("error") or "\n".join(errlines).strip()[-300:]
        if any(k in err for k in ("error while loading shared libraries", "missing dependencies",
                                  "Missing libraries", "install-deps", "cannot open shared object")):
            err = ("a Chromium mancano librerie di sistema. Via SSH lancia una volta: " + st["deps_cmd"] +
                   "  (dettagli: " + err[:300] + ")")
        raise RuntimeError("il browser non e' riuscito: " + err)
    text, kept, dropped = cookies_youtube_only(_netscape(res.get("cookies") or []))
    if not kept:
        raise RuntimeError("YouTube non ha dato cookie (passi: %s)" % ", ".join(res.get("steps", [])))
    cookies_write(text)
    _step("salvati %d cookie, ora provo un video con yt-dlp" % kept)
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
    _step(verdict)
    return {"ok": True, "kept": kept, "names": names, "steps": res.get("steps", []), "verdict": verdict,
            "video_ok": t.returncode == 0}


# ================= login a un account Google nel browser del server =================
# Una sessione interattiva (browser_session.js) con profilo persistente: dalla pagina
# admin si vede la schermata, si clicca e si scrive. Finito l'accesso, i cookie
# dell'account vanno a yt-dlp; poi il profilo li rinnova da solo (account_refresh).

PROFILE_DIR = os.path.join(BROWSER_DIR, "profile")
LOGIN_FILE = os.path.join(BROWSER_DIR, "login.json")
SHOT = os.path.join(BROWSER_DIR, "shot.jpg")
SESSION_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts", "browser_session.js")
LOGIN_URL = os.environ.get("DT_LOGIN_URL") or \
    "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F"
YT_HOME = os.environ.get("DT_YT_BASE") or "https://www.youtube.com"
REFRESH_EVERY = 6 * 3600
IDLE_CLOSE_LOGIN = 15 * 60


class BrowserSession:
    """Un Chromium con il profilo persistente, comandato riga per riga."""

    def __init__(self):
        node, _ = find_node()
        if not node or not browser_status()["ready"]:
            raise RuntimeError("prima installa il browser (pulsante qui sopra)")
        os.makedirs(PROFILE_DIR, mode=0o700, exist_ok=True)
        env = dict(node_env(node), DT_BROWSER_DIR=BROWSER_DIR, DT_PROFILE=PROFILE_DIR, DT_UA=UA)
        self.p = subprocess.Popen([node, SESSION_SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, env=env, bufsize=1)
        self.q = queue.Queue()
        self.lock = threading.Lock()
        self.seen = time.time()
        threading.Thread(target=self._read, daemon=True).start()
        first = self._get(90)
        if not first.get("ok"):
            self.close()
            err = first.get("error") or "il browser non e' partito"
            if any(k in err for k in ("shared libraries", "missing dependencies", "Missing libraries")):
                err = "a Chromium mancano librerie di sistema. Via SSH lancia una volta: " + browser_status()["deps_cmd"]
            raise RuntimeError(err)

    def _read(self):
        for line in self.p.stdout:
            try:
                self.q.put(json.loads(line))
            except ValueError:
                pass
        self.q.put({"ok": False, "error": "il browser si e' chiuso"})

    def _get(self, timeout):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return {"ok": False, "error": "il browser non risponde"}

    def cmd(self, timeout=90, **c):
        with self.lock:
            self.seen = time.time()
            if self.p.poll() is not None:
                return {"ok": False, "error": "il browser si e' chiuso"}
            try:
                self.p.stdin.write(json.dumps(c) + "\n")
                self.p.stdin.flush()
            except OSError:
                return {"ok": False, "error": "il browser si e' chiuso"}
            return self._get(timeout)

    def alive(self):
        return self.p.poll() is None

    def close(self):
        try:
            if self.alive():
                self.cmd(timeout=15, cmd="close")
        except Exception:
            pass
        try:
            self.p.wait(timeout=10)
        except Exception:
            self.p.kill()


_login = {"s": None}
_login_lock = threading.Lock()


def _account_names(text):
    return sorted({line.split("\t")[5] for line in text.splitlines() if line.count("\t") == 6})


def _has_account(names):
    return any(n in ("SID", "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO") for n in names)


def login_state():
    s = _login["s"]
    info = {}
    try:
        with open(LOGIN_FILE) as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    return {"active": bool(s and s.alive()), "logged": bool(info.get("t")), "since": info.get("t"),
            "last_refresh": info.get("refresh"), "refresh_msg": info.get("refresh_msg", "")}


def _save_login(**kw):
    info = {}
    try:
        with open(LOGIN_FILE) as f:
            info = json.load(f)
    except (OSError, ValueError):
        pass
    info.update(kw)
    with open(LOGIN_FILE, "w") as f:
        json.dump(info, f)


def _session():
    s = _login["s"]
    if not (s and s.alive()):
        raise RuntimeError("nessun accesso in corso: premi \"Accedi con un account Google\"")
    return s


def login_start():
    with _login_lock:
        if _login["s"] and _login["s"].alive():
            _login["s"].close()
        _login["s"] = BrowserSession()
    r = _login["s"].cmd(cmd="goto", url=LOGIN_URL, wait=2500)
    print("login Google nel browser del server: aperto", flush=True)
    if not r.get("ok"):
        raise RuntimeError("pagina di accesso non aperta: " + r.get("error", ""))
    return r


def login_shot():
    r = _session().cmd(cmd="shot", path=SHOT, timeout=30)
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "schermata non riuscita"))
    with open(SHOT, "rb") as f:
        return f.read(), r


def login_click(x, y):
    return _session().cmd(cmd="click", x=int(x), y=int(y))


def login_type(text):
    # il testo (anche la password) va solo alla pagina: mai nel log
    print("login Google: testo scritto (%d caratteri)" % len(text), flush=True)
    return _session().cmd(cmd="type", text=str(text))


def login_key(key):
    if key not in ("Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp", "Space"):
        raise RuntimeError("tasto non ammesso")
    return _session().cmd(cmd="key", key=key)


def login_close():
    with _login_lock:
        s, _login["s"] = _login["s"], None
    if s:
        s.close()


def login_done():
    """Finito l'accesso: passa da YouTube, salva i cookie dell'account e chiude."""
    s = _session()
    s.cmd(cmd="goto", url=YT_HOME + "/", wait=4000)
    r = s.cmd(cmd="cookies")
    if not r.get("ok"):
        raise RuntimeError(r.get("error", "cookie non letti"))
    text, kept, _ = cookies_youtube_only(_netscape(r.get("cookies") or []))
    names = _account_names(text)
    if not _has_account(names):
        raise RuntimeError("su YouTube non risulta nessun account collegato: completa l'accesso e riprova")
    cookies_write(text)
    _save_login(t=time.time(), refresh=time.time(), refresh_msg="accesso fatto")
    login_close()
    print("login Google riuscito: %d cookie dell'account salvati" % kept, flush=True)
    from .youtube import cookies_check
    chk = cookies_check()
    return {"kept": kept, "check": chk}


def _netscape_to_pw(text):
    """Cookie di ~/cookies.txt nel formato di Playwright (per ricaricarli nel profilo)."""
    out = []
    for line in text.splitlines():
        http_only = line.startswith("#HttpOnly_")
        if line.startswith("#") and not http_only:
            continue
        f = line.split("\t")
        if len(f) != 7:
            continue
        dom = f[0][len("#HttpOnly_"):] if http_only else f[0]
        out.append({"name": f[5], "value": f[6], "domain": dom, "path": f[2] or "/",
                    "expires": int(f[4]) if f[4].isdigit() and int(f[4]) > 0 else -1,
                    "httpOnly": http_only, "secure": f[3] == "TRUE"})
    return out


def account_refresh(reason="programmato"):
    """Riapre YouTube con il profilo collegato e ripassa a yt-dlp i cookie aggiornati.
    Prima ricarica nel profilo i valori piu' recenti di ~/cookies.txt (ruotati da
    yt-dlp), cosi' browser e yt-dlp proseguono sulla stessa sessione."""
    st = login_state()
    if not st["logged"] or st["active"]:
        return None
    with _login_lock:
        s = BrowserSession()
    try:
        try:
            with open(COOKIES, encoding="utf-8", errors="replace") as f:
                current = _netscape_to_pw(f.read())
            if current:
                s.cmd(cmd="addcookies", cookies=current)
        except OSError:
            pass
        s.cmd(cmd="goto", url=YT_HOME + "/", wait=5000)
        s.cmd(cmd="goto", url=YT_HOME + "/watch?v=jNQXAC9IVRw", wait=6000)
        r = s.cmd(cmd="cookies")
    finally:
        s.close()
    text, kept, _ = cookies_youtube_only(_netscape((r or {}).get("cookies") or []))
    names = _account_names(text)
    if not _has_account(names):
        msg = "l'account nel browser del server non e' piu' collegato: rifai l'accesso dalla pagina admin"
        _save_login(refresh=time.time(), refresh_msg=msg)
        COOKIE_CHECK.update(t=time.time(), ok=False, msg=msg)
        print("rinnovo cookie (%s): %s" % (reason, msg), flush=True)
        return {"ok": False, "msg": msg}
    cookies_write(text)
    _save_login(refresh=time.time(), refresh_msg="rinnovati %d cookie (%s)" % (kept, reason))
    COOKIE_CHECK.update(t=time.time(), ok=None, msg="cookie rinnovati dal browser del server")
    print("rinnovo cookie dal browser del server (%s): %d cookie" % (reason, kept), flush=True)
    return {"ok": True, "kept": kept}


def refresh_supervisor():
    """Ogni 5 minuti: chiude l'accesso interattivo dimenticato aperto e, con un
    account collegato, rinnova i cookie ogni 6 ore o subito se risultano scaduti."""
    last_try = 0.0
    while True:
        time.sleep(300)
        try:
            s = _login["s"]
            if s and s.alive() and time.time() - s.seen > IDLE_CLOSE_LOGIN:
                login_close()
                print("accesso Google lasciato aperto: chiuso", flush=True)
            st = login_state()
            if not st["logged"] or st["active"]:
                continue
            due = time.time() - (st["last_refresh"] or 0) > REFRESH_EVERY
            dead = COOKIE_CHECK.get("ok") is False and time.time() - last_try > 1800
            if due or dead:
                last_try = time.time()
                account_refresh("cookie scaduti" if dead else "ogni 6 ore")
        except Exception as e:
            print("rinnovo cookie non riuscito:", e, flush=True)

"""yt-dlp per i siti diversi da YouTube (Twitch, Kick, RaiPlay): URL degli stream e
informazioni, con messaggi d'errore comprensibili. Senza cookie di YouTube."""
from .config import UA
from .context import print
from .youtube import StreamError, run_yt


def site_format(max_h=720):
    # video e audio separati se il sito li ha cosi', altrimenti lo stream unico
    return "bv*[height<=%d]+ba/b[height<=%d]/bv*+ba/b" % (max_h, max_h)


def explain(err):
    """(tipo, messaggio) da mostrare per un errore di yt-dlp."""
    e = err or ""
    if "not currently live" in e or "is offline" in e:
        return "none", "il canale non e' in diretta adesso"
    if "DRM" in e:
        return "none", "contenuto protetto (DRM): non si puo' riprodurre"
    if "geo" in e.lower() or "not available from your location" in e or "Likely due to geo-restriction" in e:
        return "none", "contenuto visibile solo dall'Italia"
    if "Impersonate target" in e or "impersonat" in e.lower():
        return "failed", "serve yt-dlp completo (con curl_cffi): installalo dalla pagina admin"
    if "403" in e and "cloudflare" in e.lower():
        return "failed", "il sito blocca le richieste dal server (Cloudflare)"
    last = e.strip().splitlines()[-1] if e.strip() else "yt-dlp non ha dato lo stream"
    return "failed", last.replace("ERROR: ", "")[:200]


def stream_urls(page_url, max_h=720, binary=None):
    p = run_yt("-g", "--no-playlist", "--user-agent", UA, "-f", site_format(max_h), page_url,
               cookies=False, binary=binary, text=True, capture_output=True, timeout=90)
    err = p.stderr or ""
    lines = [x for x in (p.stdout or "").splitlines() if x.startswith("http")]
    # yt-dlp a volte da' comunque un URL (un video "non disponibile nel tuo paese")
    # e lo dice solo con un avviso: meglio dirlo subito che mostrare quel video
    geo = "Likely due to geo-restriction" in err
    if p.returncode != 0 or not lines or geo:
        print(err[-600:], flush=True)
        kind, msg = explain(err if not geo or not lines else "geo-restriction")
        raise StreamError(kind, msg)
    return lines


def info(page_url, binary=None):
    """{"duration", "is_live", "title"} oppure None."""
    p = run_yt("--no-playlist", "--print", "%(duration)s\t%(is_live)s\t%(title)s", page_url,
               cookies=False, binary=binary, text=True, capture_output=True, timeout=60)
    for line in reversed((p.stdout or "").splitlines()):
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        try:
            dur = int(float(parts[0]))
        except ValueError:
            dur = 0
        return {"duration": dur, "is_live": parts[1].strip() == "True",
                "title": "" if parts[2].strip() == "NA" else parts[2].strip()}
    return None

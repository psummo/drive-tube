"""Da cosa c'e' dopo /watch?v= alla sorgente da riprodurre.

  AAAAAAAAAAA         video YouTube (id di 11 caratteri)
  tv.<lista>.<canale> canale di una lista M3U (TV in diretta)
  tw.<nome>           diretta di un canale Twitch     twv.<numero>      video salvato Twitch
  k.<nome>            diretta di un canale Kick       kv.<nome>~<uuid>  video salvato Kick
  rai.<percorso>      video di RaiPlay (percorso in base64url)
  rail.<canale>       diretta di RaiPlay (rai1, rainews24...)

Gli id YouTube non contengono mai il punto, quindi non si confondono."""
import re
from . import live, raiplay
from .streamers import by_key

YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def resolve(watch):
    """(url, src) oppure None. src: kind, watch, title, thumb, live, home e altri
    campi propri della sorgente."""
    w = (watch or "").strip()
    if YT_ID.match(w):
        return "https://www.youtube.com/watch?v=" + w, {
            "kind": "yt", "watch": w, "home": "/youtube",
            "thumb": "https://i.ytimg.com/vi/%s/hqdefault.jpg" % w}
    parts = w.split(".")
    if parts[0] == "tv" and len(parts) == 3:
        lid, key = parts[1], parts[2]
        _, row = live.channel(lid, key)
        if not row:
            return None
        return row["url"], {"kind": "m3u", "watch": w, "title": row["name"], "thumb": row["logo"],
                            "live": not row.get("vod"), "dur": max(0, row.get("dur") or 0) if row.get("vod") else 0,
                            "list": lid, "row": row, "home": "/live?l=" + lid}
    if parts[0] == "rai" and len(parts) == 2:
        p = raiplay.path_of_key(parts[1])
        if not p:
            return None
        return raiplay.video_url(p), {"kind": "rai", "watch": w, "thumb": "", "live": False,
                                      "rai_path": p, "home": "/raiplay"}
    if parts[0] == "rail" and len(parts) == 2 and parts[1] in raiplay.LIVE_IDS:
        name = dict((k, n) for n, k in raiplay.LIVES)[parts[1]]
        return raiplay.live_url(parts[1]), {"kind": "rai", "watch": w, "title": name + " (RaiPlay)",
                                            "thumb": "", "live": True, "home": "/raiplay"}
    plat, vod = by_key(parts[0])
    if plat and len(parts) == 2:
        if not vod:
            ch = plat.clean_name(parts[1])
            if not ch:
                return None
            return plat.live_url(ch), {"kind": "stream", "plat": plat.live_key, "watch": "%s.%s" % (plat.live_key, ch),
                                       "title": "%s (%s)" % (ch, plat.name), "thumb": plat.preview(ch),
                                       "live": True, "channel": ch, "home": "%s?c=%s" % (plat.path, ch)}
        if plat.valid_vod(parts[1]):
            ch = parts[1].split("~")[0] if "~" in parts[1] else ""
            return plat.vod_url(parts[1]), {"kind": "stream", "plat": plat.live_key, "watch": w, "thumb": "",
                                            "live": False, "channel": ch,
                                            "home": "%s?c=%s" % (plat.path, ch) if ch else plat.path}
    return None

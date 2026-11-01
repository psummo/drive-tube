"""Da cosa c'e' dopo /watch?v= alla sorgente da riprodurre.

  AAAAAAAAAAA         video YouTube (id di 11 caratteri)
  tv.<lista>.<canale> canale di una lista M3U (TV in diretta)
  tw.<nome>           diretta di un canale Twitch
  twv.<numero>        video salvato (VOD) di Twitch

Gli id YouTube non contengono mai il punto, quindi non si confondono."""
import re
from . import live, twitch

YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def resolve(watch):
    """(url, src) oppure None. src: kind, watch, title, thumb, live, home e,
    per le liste M3U, la riga del canale."""
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
                            "live": True, "list": lid, "row": row, "home": "/live?l=" + lid}
    if parts[0] == "tw" and len(parts) == 2 and twitch.clean_name(parts[1]):
        name = twitch.clean_name(parts[1])
        return twitch.live_url(name), {"kind": "twitch", "watch": "tw." + name, "title": name + " (Twitch)",
                                       "thumb": twitch.preview(name), "live": True, "channel": name,
                                       "home": "/twitch?c=" + name}
    if parts[0] == "twv" and len(parts) == 2 and parts[1].isdigit():
        return twitch.vod_url(parts[1]), {"kind": "twitch", "watch": w, "thumb": "", "live": False,
                                         "home": "/twitch"}
    return None

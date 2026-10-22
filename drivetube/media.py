"""ffmpeg: comando, code FIFO, lettura dei frame JPEG, demux MPEG-TS dell'H.264 e audio MP3.
Non sa nulla di YouTube: riceve URL gia' pronti."""
import os
import struct
import time
from .config import HEADERS, UA
from .context import cur, print, state
from .settings import C


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

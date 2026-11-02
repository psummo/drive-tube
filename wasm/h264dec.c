// Decoder H.264 e H.265 (HEVC) per il browser: parser + decoder di FFmpeg,
// compilati in WebAssembly. dec_init() prepara H.264, dec_init_codec(1) H.265.
// Due modi d'uso:
//  - stream Annex-B grezzo: JS scrive i byte in in_buf() e chiama feed(n) oppure
//    push(n)/next(); il parser di FFmpeg divide i frame;
//  - pacchetti gia' divisi (player): decode(n, pts) per ogni pacchetto, con il
//    suo tempo in secondi.
// Per ogni frame decodificato viene chiamata la funzione JS on_frame(...), che
// riceve anche il tempo del frame in secondi (-1 se sconosciuto).
#include <libavcodec/avcodec.h>
#include <libavutil/log.h>
#include <libavutil/pixfmt.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT(n) __attribute__((export_name(n)))
__attribute__((import_module("env"), import_name("on_frame")))
void on_frame(const uint8_t *y, const uint8_t *u, const uint8_t *v,
              int ly, int lu, int lv, int w, int h, double pts);

static AVCodecContext *ctx;
static AVCodecParserContext *par;
static AVPacket *pkt;
static AVFrame *frm;
static uint8_t *inbuf;
static int incap;
static uint8_t *rgba;
static int rgbacap;
static int frames_out;
static uint8_t *cur;   // input ancora da consumare per push()/next()
static int left;
static int codec_id = AV_CODEC_ID_H264;
static uint8_t *p8[3];  // piani a 8 bit per i video a 10 bit (HEVC Main10)
static int p8cap[3];

EXPORT("dec_init") int dec_init(void) {
    av_log_set_level(AV_LOG_QUIET);
    if (ctx) avcodec_free_context(&ctx);
    if (par) { av_parser_close(par); par = NULL; }
    av_packet_free(&pkt);
    av_frame_free(&frm);
    left = 0;
    const AVCodec *c = avcodec_find_decoder(codec_id);
    if (!c) return -1;
    ctx = avcodec_alloc_context3(c);
    ctx->thread_count = 1;
    ctx->pkt_timebase = (AVRational){1, 1000000};
    ctx->flags2 |= AV_CODEC_FLAG2_FAST;
    if (avcodec_open2(ctx, c, NULL) < 0) return -2;
    par = av_parser_init(codec_id);
    pkt = av_packet_alloc();
    frm = av_frame_alloc();
    return (par && pkt && frm) ? 0 : -3;
}

// 0 = H.264, 1 = H.265/HEVC
EXPORT("dec_init_codec") int dec_init_codec(int c) {
    codec_id = c == 1 ? AV_CODEC_ID_HEVC : AV_CODEC_ID_H264;
    return dec_init();
}

EXPORT("in_buf") uint8_t *in_buf(int size) {
    if (size + AV_INPUT_BUFFER_PADDING_SIZE > incap) {
        free(inbuf);
        incap = size + AV_INPUT_BUFFER_PADDING_SIZE;
        inbuf = malloc(incap);
    }
    memset(inbuf + size, 0, AV_INPUT_BUFFER_PADDING_SIZE);
    return inbuf;
}

// 10 bit -> 8 bit (il disegno in JS lavora a 8 bit): un piano alla volta
static uint8_t *to8(int i, const uint8_t *src, int ls, int w, int h) {
    int need = w * h;
    if (need > p8cap[i]) { free(p8[i]); p8[i] = malloc(need); p8cap[i] = need; }
    for (int j = 0; j < h; j++) {
        const uint16_t *r = (const uint16_t *)(src + j * ls);
        uint8_t *d = p8[i] + j * w;
        for (int x = 0; x < w; x++) d[x] = r[x] >> 2;
    }
    return p8[i];
}

static void drain(void) {
    while (avcodec_receive_frame(ctx, frm) == 0) {
        frames_out++;
        int64_t ts = frm->pts != AV_NOPTS_VALUE ? frm->pts : frm->best_effort_timestamp;
        double sec = ts == AV_NOPTS_VALUE ? -1.0 : ts / 1000000.0;
        if (frm->format == AV_PIX_FMT_YUV420P10LE) {
            int w = frm->width, h = frm->height, cw = (w + 1) >> 1, ch = (h + 1) >> 1;
            on_frame(to8(0, frm->data[0], frm->linesize[0], w, h), to8(1, frm->data[1], frm->linesize[1], cw, ch),
                     to8(2, frm->data[2], frm->linesize[2], cw, ch), w, cw, cw, w, h, sec);
            av_frame_unref(frm);
            continue;
        }
        on_frame(frm->data[0], frm->data[1], frm->data[2],
                 frm->linesize[0], frm->linesize[1], frm->linesize[2],
                 frm->width, frm->height, sec);
        av_frame_unref(frm);
    }
}

// ritorna il numero di frame usciti durante questa chiamata
EXPORT("feed") int feed(int size) {
    int before = frames_out;
    uint8_t *p = inbuf;
    while (size > 0) {
        uint8_t *out; int outsz;
        int used = av_parser_parse2(par, ctx, &out, &outsz, p, size,
                                    AV_NOPTS_VALUE, AV_NOPTS_VALUE, 0);
        if (used < 0) break;
        p += used; size -= used;
        if (outsz) {
            pkt->data = out; pkt->size = outsz;
            pkt->pts = AV_NOPTS_VALUE;
            avcodec_send_packet(ctx, pkt);  // anche se rifiuta, svuoto i frame pronti
            drain();
        }
    }
    return frames_out - before;
}

// decodifica un frame alla volta: push(n) consegna n byte gia' scritti in
// in_buf(); next() decodifica fino al primo frame (0 se l'input e' finito);
// left() dice quanti byte restano prima di poter fare un altro push.
EXPORT("push") void push(int size) { cur = inbuf; left = size; }
EXPORT("left") int left_bytes(void) { return left; }
EXPORT("next") int next(void) {
    int before = frames_out;
    while (left > 0 && frames_out == before) {
        uint8_t *out; int outsz;
        int used = av_parser_parse2(par, ctx, &out, &outsz, cur, left,
                                    AV_NOPTS_VALUE, AV_NOPTS_VALUE, 0);
        if (used < 0) { left = 0; break; }
        cur += used; left -= used;
        if (outsz) {
            pkt->data = out; pkt->size = outsz;
            avcodec_send_packet(ctx, pkt);
            drain();
        }
    }
    return frames_out - before;
}

// un pacchetto (un frame codificato) gia' scritto in in_buf(), con il suo pts
EXPORT("decode") int decode(int size, double pts) {
    int before = frames_out;
    pkt->data = inbuf; pkt->size = size;
    pkt->pts = pts >= 0 ? (int64_t)(pts * 1000000.0 + 0.5) : AV_NOPTS_VALUE;
    pkt->dts = AV_NOPTS_VALUE;
    avcodec_send_packet(ctx, pkt);
    pkt->pts = AV_NOPTS_VALUE;
    drain();
    return frames_out - before;
}

// fine stream: svuota parser e decoder
EXPORT("flush") int flush(void) {
    int before = frames_out;
    uint8_t *out; int outsz;
    av_parser_parse2(par, ctx, &out, &outsz, NULL, 0, AV_NOPTS_VALUE, AV_NOPTS_VALUE, 0);
    if (outsz) { pkt->data = out; pkt->size = outsz; avcodec_send_packet(ctx, pkt); drain(); }
    avcodec_send_packet(ctx, NULL);
    drain();
    return frames_out - before;
}

// conversione YUV420 -> RGBA (BT.709, range limitato) per il disegno senza WebGL
EXPORT("to_rgba") uint8_t *to_rgba(const uint8_t *y, const uint8_t *u, const uint8_t *v,
                                    int ly, int lu, int lv, int w, int h) {
    int need = w * h * 4;
    if (need > rgbacap) { free(rgba); rgba = malloc(need); rgbacap = need; }
    for (int j = 0; j < h; j++) {
        const uint8_t *yr = y + j * ly, *ur = u + (j >> 1) * lu, *vr = v + (j >> 1) * lv;
        uint8_t *d = rgba + j * w * 4;
        for (int i = 0; i < w; i++) {
            int c = (yr[i] - 16) * 298;
            int du = ur[i >> 1] - 128, dv = vr[i >> 1] - 128;
            int r = (c + 459 * dv + 128) >> 8;
            int g = (c - 55 * du - 136 * dv + 128) >> 8;
            int b = (c + 541 * du + 128) >> 8;
            d[0] = r < 0 ? 0 : r > 255 ? 255 : r;
            d[1] = g < 0 ? 0 : g > 255 ? 255 : g;
            d[2] = b < 0 ? 0 : b > 255 ? 255 : b;
            d[3] = 255;
            d += 4;
        }
    }
    return rgba;
}

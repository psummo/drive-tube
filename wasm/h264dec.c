// Decoder H.264 per il browser: parser + decoder di FFmpeg, compilati in WebAssembly.
// JS scrive i byte dello stream Annex-B in in_buf() e chiama feed(n);
// per ogni frame decodificato viene chiamata la funzione JS on_frame(...).
#include <libavcodec/avcodec.h>
#include <libavutil/log.h>
#include <stdlib.h>
#include <string.h>

#define EXPORT(n) __attribute__((export_name(n)))
__attribute__((import_module("env"), import_name("on_frame")))
void on_frame(const uint8_t *y, const uint8_t *u, const uint8_t *v,
              int ly, int lu, int lv, int w, int h);

static AVCodecContext *ctx;
static AVCodecParserContext *par;
static AVPacket *pkt;
static AVFrame *frm;
static uint8_t *inbuf;
static int incap;
static uint8_t *rgba;
static int rgbacap;
static int frames_out;

EXPORT("dec_init") int dec_init(void) {
    av_log_set_level(AV_LOG_QUIET);
    const AVCodec *c = avcodec_find_decoder(AV_CODEC_ID_H264);
    if (!c) return -1;
    ctx = avcodec_alloc_context3(c);
    ctx->thread_count = 1;
    ctx->flags2 |= AV_CODEC_FLAG2_FAST;
    if (avcodec_open2(ctx, c, NULL) < 0) return -2;
    par = av_parser_init(AV_CODEC_ID_H264);
    pkt = av_packet_alloc();
    frm = av_frame_alloc();
    return (par && pkt && frm) ? 0 : -3;
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

static void drain(void) {
    while (avcodec_receive_frame(ctx, frm) == 0) {
        frames_out++;
        on_frame(frm->data[0], frm->data[1], frm->data[2],
                 frm->linesize[0], frm->linesize[1], frm->linesize[2],
                 frm->width, frm->height);
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
            avcodec_send_packet(ctx, pkt);  // anche se rifiuta, svuoto i frame pronti
            drain();
        }
    }
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

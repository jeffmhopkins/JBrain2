#include <string.h>

#include "dbg_core.h"

#define RUN_MAX 128

static void flush(dbg_shot_t *s)
{
    if (s->n && !s->failed) s->sink(s->ctx, s->out, s->n);
    s->bytes += (uint32_t)s->n;
    s->n = 0;
}

static void emit(dbg_shot_t *s, const uint8_t *b, size_t n)
{
    for (size_t i = 0; i < n; i++) {
        if (s->n == sizeof s->out) flush(s);
        s->out[s->n++] = b[i];
    }
}

static void u16(dbg_shot_t *s, uint16_t v)
{
    uint8_t b[2] = {(uint8_t)v, (uint8_t)(v >> 8)};
    emit(s, b, 2);
}

void dbg_shot_begin(dbg_shot_t *s, uint16_t w, uint16_t h, dbg_sink_fn sink, void *ctx)
{
    memset(s, 0, sizeof *s);
    s->sink = sink;
    s->ctx = ctx;
    s->w = w;
    s->h = h;
    emit(s, (const uint8_t *)"JPS1", 4);
    u16(s, w);
    u16(s, h);
}

/* Two passes over the stripe: the first sizes it so the header can say how long it is,
 * which lets a reader skip or resync without decoding. Cheap next to rendering it. */
static uint32_t runs(const uint16_t *px, size_t n, dbg_shot_t *s)
{
    uint32_t len = 0;
    for (size_t i = 0; i < n;) {
        size_t k = 1;
        while (i + k < n && k < RUN_MAX && px[i + k] == px[i]) k++;
        if (s) {
            uint8_t b[3] = {(uint8_t)(k - 1), (uint8_t)px[i], (uint8_t)(px[i] >> 8)};
            emit(s, b, 3);
        }
        len += 3;
        i += k;
    }
    return len;
}

bool dbg_shot_stripe(dbg_shot_t *s, uint16_t y0, uint16_t rows, const uint16_t *px)
{
    if (s->failed || y0 != s->next_row || !rows || y0 + rows > s->h) {
        s->failed = true;
        return false;
    }
    size_t n = (size_t)s->w * rows;
    uint32_t len = runs(px, n, NULL);
    u16(s, y0);
    u16(s, rows);
    uint8_t b[4] = {(uint8_t)len, (uint8_t)(len >> 8), (uint8_t)(len >> 16), (uint8_t)(len >> 24)};
    emit(s, b, 4);
    runs(px, n, s);
    s->next_row = (uint16_t)(y0 + rows);
    s->stripes++;
    return true;
}

bool dbg_shot_end(dbg_shot_t *s)
{
    if (s->next_row != s->h) s->failed = true;
    u16(s, 0xFFFF);
    flush(s);
    return !s->failed;
}

static uint16_t rd16(const uint8_t *p)
{
    return (uint16_t)(p[0] | p[1] << 8);
}

bool dbg_shot_decode(const uint8_t *buf, size_t n, uint16_t *frame, uint16_t w, uint16_t h)
{
    if (n < 8 || memcmp(buf, "JPS1", 4) || rd16(buf + 4) != w || rd16(buf + 6) != h) return false;
    size_t at = 8;
    uint16_t want = 0;
    for (;;) {
        if (at + 2 > n) return false;
        uint16_t y0 = rd16(buf + at);
        if (y0 == 0xFFFF) return want == h;
        if (at + 8 > n || y0 != want) return false;
        uint16_t rows = rd16(buf + at + 2);
        uint32_t len = buf[at + 4] | buf[at + 5] << 8 | (uint32_t)buf[at + 6] << 16 |
                       (uint32_t)buf[at + 7] << 24;
        at += 8;
        if (at + len > n || len % 3 || y0 + rows > h) return false;
        size_t px = (size_t)y0 * w, end = px + (size_t)rows * w;
        for (uint32_t i = 0; i < len; i += 3) {
            size_t k = (size_t)buf[at + i] + 1;
            if (px + k > end) return false;
            uint16_t c = rd16(buf + at + i + 1);
            for (size_t j = 0; j < k; j++) frame[px++] = c;
        }
        if (px != end) return false;
        at += len;
        want = (uint16_t)(y0 + rows);
    }
}

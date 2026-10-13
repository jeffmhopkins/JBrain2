/* The sim's notices as the box sends them: a JN1 stream built here (META, the texts, a picture as
 * rgb565 run stripes, a sound as PCM records, END) and fed through the panel's own collection path
 * (svc_notice_fetch_*), so every notice board renders from a real file through the overlay's
 * picture decoder. The pictures are the mock boards' own shapes (NotifyPicture's weather,
 * NotifyBanner's Minecraft strip), drawn without the mock's lettering. */
#include "sim_notice.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "jn1.h"
#include "notice_logic.h"
#include "svc_notice_drv.h"

typedef struct {
    uint8_t *b;
    size_t n, cap;
} buf_t;

static void put(buf_t *s, const void *p, size_t n)
{
    if (s->n + n > s->cap) {
        s->cap = (s->n + n) * 2;
        s->b = realloc(s->b, s->cap);
    }
    memcpy(s->b + s->n, p, n);
    s->n += n;
}

static void rec(buf_t *s, uint8_t kind, const void *p, size_t n)
{
    const uint8_t h[3] = {kind, (uint8_t)n, (uint8_t)(n >> 8)};
    put(s, h, 3);
    if (n) put(s, p, n);
}

static uint16_t rgb565(uint32_t c)
{
    return (uint16_t)(((c >> 19) & 0x1F) << 11 | ((c >> 10) & 0x3F) << 5 | ((c >> 3) & 0x1F));
}

static uint32_t mix(uint32_t a, uint32_t b, float t)
{
    uint32_t out = 0;
    for (int sh = 0; sh <= 16; sh += 8) {
        const float x = (float)((a >> sh) & 0xFF) * (1 - t) + (float)((b >> sh) & 0xFF) * t;
        out |= (uint32_t)(x + 0.5f) << sh;
    }
    return out;
}

static bool in_circle(int x, int y, int cx, int cy, int r)
{
    return (x - cx) * (x - cx) + (y - cy) * (y - cy) <= r * r;
}

static bool in_rect(int x, int y, int x0, int y0, int w, int h)
{
    return x >= x0 && x < x0 + w && y >= y0 && y < y0 + h;
}

/* NotifyPicture's weather card: a dusk sky, the sun going down behind hills, a rain cloud. */
static uint32_t weather(int x, int y)
{
    if (in_circle(x, y, 318, 168, 34) || in_circle(x, y, 360, 150, 44) || in_circle(x, y, 406, 172, 30) ||
        in_rect(x, y, 286, 168, 150, 34))
        return 0xE9EEF5;
    for (int k = 0; k < 3; k++) {
        const int x0 = 318 + 34 * k - (y - 220) / 3;
        if (y >= 220 && y < 244 && x >= x0 - 2 && x <= x0 + 2) return 0x9CC3E6;
    }
    const int hill = 300 + (int)(18 * sinf((float)x / 42.0f)) + (x % 140 < 70 ? (x % 70) / 3 : (70 - x % 70) / 3);
    if (y >= 330) return 0x2A2238;
    if (y >= hill) return 0x3A2F4C;
    if (in_circle(x, y, 330, 300, 62)) return 0xFBD38A;
    const float t = (float)y / 480.0f;
    return t < 0.62f ? mix(0x1B2A4A, 0xC46A4A, t / 0.62f) : mix(0xC46A4A, 0xF2B66B, (t - 0.62f) / 0.38f);
}

/* NotifyBanner's Minecraft strip, 480x140 as the box fits it. */
static uint32_t minecraft(int x, int y)
{
    static const struct {
        int x, y, w, h;
        uint32_t c;
    } R[] = {
        {360, 18, 36, 36, 0xF6E27A}, {40, 26, 64, 14, 0xFFFFFF}, {52, 16, 36, 12, 0xFFFFFF},
        {210, 40, 48, 12, 0xFFFFFF}, {120, 44, 16, 48, 0x6E4630}, {100, 20, 56, 32, 0x3F7A2A},
        {420, 56, 14, 36, 0x6E4630}, {404, 36, 46, 26, 0x3F7A2A}, {250, 74, 16, 18, 0x5A5A5A},
        {236, 60, 44, 6, 0x8F8F8F},  {236, 60, 44, 32, 0xB0B0B0}, {24, 116, 12, 8, 0x6E4630},
        {96, 126, 12, 8, 0x6E4630},  {180, 118, 12, 8, 0x6E4630}, {300, 128, 12, 8, 0x6E4630},
        {420, 120, 12, 8, 0x6E4630},
    };
    for (size_t i = 0; i < sizeof R / sizeof *R; i++)
        if (in_rect(x, y, R[i].x, R[i].y, R[i].w, R[i].h)) return R[i].c;
    if (y >= 108) return 0x8A5A3B;
    if (y >= 92) return 0x5C9E3F;
    return y < 40 ? 0x8BBCE6 : 0x6FA8DC;
}

/* The picture as STRIPE records: as many whole rows as fit a record, never more than 40, runs
   greedy within a stripe (backend notice.stripes). */
static uint16_t stripes(buf_t *s, uint32_t (*px)(int, int), int w, int h)
{
    static uint8_t body[JN1_MAX_RECORD];
    uint16_t n = 0;
    int y0 = 0;
    while (y0 < h) {
        size_t k = 4;
        int rows = 0;
        uint16_t color = 0;
        int count = 0;
        while (rows < 40 && y0 + rows < h) {
            const size_t mark = k;
            const uint16_t mark_c = color;
            const int mark_n = count;
            for (int x = 0; x < w; x++) {
                const uint16_t p = rgb565(px(x, y0 + rows));
                if (count && p == color && count < 128) {
                    count++;
                } else {
                    if (count) {
                        body[k++] = (uint8_t)(count - 1);
                        body[k++] = (uint8_t)color;
                        body[k++] = (uint8_t)(color >> 8);
                    }
                    color = p;
                    count = 1;
                }
            }
            if (k + 3 > JN1_MAX_RECORD) {
                k = mark, color = mark_c, count = mark_n;
                break;
            }
            rows++;
        }
        body[k++] = (uint8_t)(count - 1);
        body[k++] = (uint8_t)color;
        body[k++] = (uint8_t)(color >> 8);
        body[0] = (uint8_t)y0, body[1] = (uint8_t)(y0 >> 8), body[2] = (uint8_t)rows, body[3] = (uint8_t)(rows >> 8);
        rec(s, JN1_STRIPE, body, k);
        y0 += rows;
        n++;
    }
    return n;
}

bool sim_notice_push(const sim_notice_t *sn)
{
    buf_t s = {0};
    const bool picture = sn->picture != SIM_PIC_NONE;
    const int w = picture ? 480 : 0, h = sn->picture == SIM_PIC_WEATHER ? 480 : picture ? 140 : 0;
    uint8_t meta[JN1_META_FIXED + JN1_SENDER_BYTES] = {0};
    meta[15] = sn->id;
    meta[16] = (uint8_t)((sn->chime ? JN1_FLAG_CHIME : 0) | (sn->n_choices ? JN1_FLAG_QUESTION : 0) |
                         (picture ? JN1_FLAG_PICTURE : 0) | (sn->audio_ms ? JN1_FLAG_SOUND : 0));
    meta[17] = (uint8_t)sn->hide_s, meta[18] = (uint8_t)(sn->hide_s >> 8);
    meta[19] = (uint8_t)(900 & 0xFF), meta[20] = (uint8_t)(900 >> 8);
    meta[23] = (uint8_t)(sn->picture == SIM_PIC_WEATHER ? JN1_IMG_FULL : picture ? JN1_IMG_BANNER : 0);
    meta[24] = (uint8_t)w, meta[25] = (uint8_t)(w >> 8), meta[26] = (uint8_t)h, meta[27] = (uint8_t)(h >> 8);
    meta[28] = (uint8_t)sn->audio_ms, meta[29] = (uint8_t)(sn->audio_ms >> 8);
    const size_t sl = strlen(sn->from);
    memcpy(meta + JN1_META_FIXED, sn->from, sl);
    put(&s, "JN1", 3);
    rec(&s, JN1_META, meta, JN1_META_FIXED + sl);
    rec(&s, JN1_TITLE, sn->title, strlen(sn->title));
    if (sn->body && sn->body[0]) rec(&s, JN1_BODY, sn->body, strlen(sn->body));
    for (uint8_t i = 0; i < sn->n_choices; i++) rec(&s, JN1_CHOICE, sn->choices[i], strlen(sn->choices[i]));
    if (picture) stripes(&s, sn->picture == SIM_PIC_WEATHER ? weather : minecraft, w, h);
    /* A soft two-note tone, as the box would send a clip: 16 kHz mono s16. */
    const uint32_t samples = (uint32_t)sn->audio_ms * 16u;
    static int16_t pcm[2048];
    for (uint32_t at = 0; at < samples;) {
        uint32_t k = samples - at < 2048 ? samples - at : 2048;
        for (uint32_t i = 0; i < k; i++)
            pcm[i] = (int16_t)(3000 * sinf(2.0f * 3.14159f * ((at + i) < samples / 2 ? 660.0f : 880.0f) *
                                           (float)(at + i) / 16000.0f));
        rec(&s, JN1_PCM, pcm, k * 2);
        at += k;
    }
    const uint32_t count = (uint32_t)s.n;
    const uint8_t end[5] = {0, (uint8_t)count, (uint8_t)(count >> 8), (uint8_t)(count >> 16), (uint8_t)(count >> 24)};
    rec(&s, JN1_END, end, sizeof end);

    nfetch_t *f = svc_notice_fetch_begin(NOTICE_NEXT_PATH);
    size_t at = 0;
    while (f && at < s.n) {
        uint8_t *dst;
        const size_t room = svc_notice_fetch_room(f, &dst);
        if (!room) break;
        const size_t k = room < s.n - at ? room : s.n - at;
        memcpy(dst, s.b + at, k);
        at += k;
        if (!svc_notice_fetch_took(f, k)) break;
    }
    const bool ok = svc_notice_fetch_end(f, true, 200);
    free(s.b);
    if (!ok) fprintf(stderr, "sim: notice %u was not queued\n", sn->id);
    return ok;
}

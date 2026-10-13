#include "jn1.h"

#include <string.h>

static uint16_t u16(const uint8_t *p)
{
    return (uint16_t)(p[0] | p[1] << 8);
}

static uint32_t u32(const uint8_t *p)
{
    return (uint32_t)p[0] | (uint32_t)p[1] << 8 | (uint32_t)p[2] << 16 | (uint32_t)p[3] << 24;
}

bool jn1_utf8_ok(const uint8_t *s, size_t n)
{
    size_t i = 0;
    while (i < n) {
        const uint8_t c = s[i];
        if (c < 0x80) {
            i++;
            continue;
        }
        size_t k;
        uint32_t cp, min;
        if ((c & 0xE0) == 0xC0) k = 1, cp = c & 0x1F, min = 0x80;
        else if ((c & 0xF0) == 0xE0) k = 2, cp = c & 0x0F, min = 0x800;
        else if ((c & 0xF8) == 0xF0) k = 3, cp = c & 0x07, min = 0x10000;
        else return false;
        if (i + k >= n) return false;
        for (size_t j = 1; j <= k; j++) {
            if ((s[i + j] & 0xC0) != 0x80) return false;
            cp = cp << 6 | (s[i + j] & 0x3F);
        }
        if (cp < min || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return false;
        i += k + 1;
    }
    return true;
}

/* Valid UTF-8 into `cap` bytes with its terminator, cut before a character that would not fit
   whole: a title longer than the panel keeps loses its tail, never half a letter. */
static void cut_copy(char *dst, size_t cap, const uint8_t *src, size_t n)
{
    size_t k = n < cap - 1 ? n : cap - 1;
    if (k < n)
        while (k && (src[k] & 0xC0) == 0x80) k--;
    memcpy(dst, src, k);
    dst[k] = '\0';
}

void jn1_uuid_text(const uint8_t id[16], char out[37])
{
    static const char D[] = "0123456789abcdef";
    size_t o = 0;
    for (int i = 0; i < 16; i++) {
        if (i == 4 || i == 6 || i == 8 || i == 10) out[o++] = '-';
        out[o++] = D[id[i] >> 4];
        out[o++] = D[id[i] & 15];
    }
    out[o] = '\0';
}

void jn1_init(jn1_reader_t *r, uint8_t *buf, jn1_notice_t *out)
{
    memset(r, 0, sizeof *r);
    memset(out, 0, sizeof *out);
    r->rec = buf;
    r->n = out;
    r->want = JN1_HEAD; /* the magic first, the same three bytes a header is */
    r->order = -1;
}

bool jn1_ended(const jn1_reader_t *r)
{
    return r->ended;
}

bool jn1_torn(const jn1_reader_t *r)
{
    return !r->ended;
}

static jn1_ev_t refuse(jn1_reader_t *r, const char *why)
{
    r->bad = true;
    r->why = why;
    return JN1_EV_BAD;
}

static int rank_of(uint8_t kind)
{
    return kind >= JN1_META && kind <= JN1_END ? kind - JN1_META : -1;
}

static jn1_ev_t stripe(jn1_reader_t *r, const uint8_t *p, uint32_t size)
{
    jn1_notice_t *n = r->n;
    if (size < 4) return refuse(r, "STRIPE too short");
    const uint16_t y0 = u16(p), rows = u16(p + 2);
    if (y0 != r->want_row || !rows || (uint32_t)y0 + rows > n->img_h || rows > JN1_STRIPE_ROWS_MAX)
        return refuse(r, "stripe out of place");
    uint32_t left = (uint32_t)rows * n->img_w;
    for (uint32_t i = 4; i < size; i += 3) {
        const uint32_t k = (uint32_t)p[i] + 1;
        if (i + 3 > size || k > left) return refuse(r, "run overflows its stripe");
        left -= k;
    }
    if (left) return refuse(r, "stripe short of its rows");
    if (!n->stripes) n->stripe_at = r->at;
    n->stripes++;
    r->want_row = (uint16_t)(y0 + rows);
    return JN1_EV_RECORD;
}

/* What decode() checks once END is read: the flags against what came. */
static jn1_ev_t end(jn1_reader_t *r, const uint8_t *p, uint32_t size)
{
    jn1_notice_t *n = r->n;
    if (size < 5) return refuse(r, "END too short");
    if (p[0] != 0 || u32(p + 1) != r->at) return refuse(r, "END does not match the stream");
    if (!(n->flags & JN1_FLAG_PICTURE) != !n->stripes || (n->stripes && r->want_row != n->img_h))
        return refuse(r, "picture flag and stripes disagree");
    if (!(n->flags & JN1_FLAG_QUESTION) != !n->n_choices)
        return refuse(r, "question flag and choices disagree");
    const int32_t ms = (int32_t)((uint64_t)n->pcm_bytes * 1000u / (JN1_PCM_RATE * 2u));
    if (!(n->flags & JN1_FLAG_SOUND) != !n->pcm_bytes ||
        (n->pcm_bytes && (ms - n->audio_ms > 1 || n->audio_ms - ms > 1)))
        return refuse(r, "sound flag and PCM disagree");
    if (n->img_mode > JN1_IMG_BANNER) return refuse(r, "unknown picture mode");
    r->ended = true;
    n->bytes = r->at + JN1_HEAD + size;
    return JN1_EV_END;
}

static jn1_ev_t record(jn1_reader_t *r)
{
    const uint8_t kind = r->rec[0];
    const uint8_t *p = r->rec + JN1_HEAD;
    const uint32_t size = r->rec_n - JN1_HEAD;
    jn1_notice_t *n = r->n;
    const int rank = rank_of(kind);
    if (rank >= 0) {
        if (rank < r->order || (rank == r->order && kind <= JN1_BODY))
            return refuse(r, "record out of order");
        r->order = (int8_t)rank;
    }
    if (kind == JN1_META) {
        if (size < JN1_META_FIXED) return refuse(r, "META too short");
        if (!jn1_utf8_ok(p + JN1_META_FIXED, size - JN1_META_FIXED))
            return refuse(r, "sender is not UTF-8");
        memcpy(n->id, p, 16);
        n->flags = p[16];
        n->hide_s = u16(p + 17);
        n->ttl_s = u32(p + 19);
        n->img_mode = p[23];
        n->img_w = u16(p + 24);
        n->img_h = u16(p + 26);
        n->audio_ms = u16(p + 28);
        cut_copy(n->sender, sizeof n->sender, p + JN1_META_FIXED, size - JN1_META_FIXED);
        r->meta = true;
        return JN1_EV_RECORD;
    }
    if (!r->meta) return refuse(r, "first record is not META");
    switch (kind) {
    case JN1_TITLE:
    case JN1_BODY:
    case JN1_CHOICE:
        if (!jn1_utf8_ok(p, size)) return refuse(r, "text is not UTF-8");
        if (kind == JN1_TITLE) {
            cut_copy(n->title, sizeof n->title, p, size);
        } else if (kind == JN1_BODY) {
            cut_copy(n->body, sizeof n->body, p, size);
        } else {
            if (n->n_choices == JN1_CHOICES_MAX) return refuse(r, "more than four choices");
            cut_copy(n->choices[n->n_choices], JN1_CHOICE_BYTES, p, size);
            n->n_choices++;
        }
        return JN1_EV_RECORD;
    case JN1_STRIPE: return stripe(r, p, size);
    case JN1_PCM:
        if (size % 2) return refuse(r, "odd PCM record");
        if (!n->pcm_at) n->pcm_at = r->at;
        n->pcm_bytes += size;
        return JN1_EV_RECORD;
    case JN1_END: return end(r, p, size);
    default: return JN1_EV_RECORD; /* a type this panel does not know: kept, never read */
    }
}

/* The unit the caller has just kept gives way to the next. */
static void advance(jn1_reader_t *r)
{
    if (!r->done) return;
    r->done = false;
    r->at += r->rec_n;
    r->rec_n = 0;
    r->want = JN1_HEAD;
}

size_t jn1_room(jn1_reader_t *r, uint8_t **at)
{
    if (r->bad || r->ended) return 0;
    advance(r);
    *at = r->rec + r->rec_n;
    return r->want - r->rec_n;
}

jn1_ev_t jn1_took(jn1_reader_t *r, size_t n)
{
    if (r->bad) return JN1_EV_BAD;
    r->rec_n += (uint32_t)n;
    if (r->rec_n < r->want) return JN1_EV_NONE;
    if (!r->magic) {
        if (memcmp(r->rec, "JN1", 3)) return refuse(r, "not a JN1 stream");
        r->magic = true;
        r->done = true;
        return JN1_EV_RECORD;
    }
    if (r->want == JN1_HEAD) {
        const uint16_t size = u16(r->rec + 1);
        if (size > JN1_MAX_RECORD) return refuse(r, "record too long");
        r->want = JN1_HEAD + size;
        if (size) return JN1_EV_NONE;
    }
    const jn1_ev_t ev = record(r);
    r->done = ev != JN1_EV_BAD;
    return ev;
}

size_t jn1_feed(jn1_reader_t *r, const uint8_t *b, size_t n, jn1_ev_t *ev)
{
    *ev = JN1_EV_NONE;
    if (r->bad) {
        *ev = JN1_EV_BAD;
        return 0;
    }
    if (r->ended) {
        if (n) *ev = refuse(r, "bytes after END");
        return 0;
    }
    size_t used = 0;
    while (used < n && *ev == JN1_EV_NONE) {
        uint8_t *at;
        const size_t room = jn1_room(r, &at);
        const size_t k = n - used < room ? n - used : room;
        memcpy(at, b + used, k);
        used += k;
        *ev = jn1_took(r, k);
    }
    return used;
}

bool jn1_stripe_pixels(const uint8_t *payload, uint32_t len, uint16_t img_w, uint16_t *px,
                       uint32_t cap, uint16_t *y0, uint16_t *rows)
{
    if (len < 4 || (len - 4) % 3) return false;
    *y0 = u16(payload);
    *rows = u16(payload + 2);
    const uint32_t want = (uint32_t)*rows * img_w;
    if (want > cap) return false;
    uint32_t got = 0;
    for (uint32_t i = 4; i < len; i += 3) {
        const uint32_t k = (uint32_t)payload[i] + 1;
        if (k > want - got) return false;
        const uint16_t c = u16(payload + i + 1);
        for (uint32_t j = 0; j < k; j++) px[got + j] = c;
        got += k;
    }
    return got == want;
}

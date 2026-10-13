#include "hist_logic.h"

#include <stdio.h>
#include <string.h>

void hist_init(hist_win_t *w)
{
    memset(w, 0, sizeof *w);
}

/* Bytes `len` of words take in the arena: their terminator too. */
static uint16_t cost(uint16_t len)
{
    return (uint16_t)(len + 1);
}

/* The length a message's words keep: whole, or cut back to a character boundary so one message
   can never fill more than the whole arena. */
static uint16_t keep_len(const char *text)
{
    size_t n = text ? strlen(text) : 0;
    if (n + 1 > HIST_TEXT_BYTES) {
        n = HIST_TEXT_BYTES - 1;
        while (n > 0 && ((unsigned char)text[n] & 0xC0) == 0x80) n--;
    }
    return (uint16_t)n;
}

/* Drops entry `i` (the first or the last) and closes its gap in the arena. */
static void drop(hist_win_t *w, bool first)
{
    if (!w->n) return;
    const uint8_t i = first ? 0 : (uint8_t)(w->n - 1);
    const uint16_t c = cost(w->e[i].len);
    if (first) {
        memmove(w->text, w->text + c, (size_t)(w->used - c));
        for (uint8_t k = 1; k < w->n; k++) w->e[k].off = (uint16_t)(w->e[k].off - c);
        memmove(w->e, w->e + 1, sizeof w->e[0] * (size_t)(w->n - 1));
    }
    w->used = (uint16_t)(w->used - c);
    w->n--;
}

static void fill(hist_ent_t *e, const hist_in_t *in, uint16_t off, uint16_t len)
{
    *e = (hist_ent_t){.local_id = in->local_id,
                      .box_id = in->box_id,
                      .off = off,
                      .len = len,
                      .dur_s = in->dur_s,
                      .dur_ms = in->dur_ms,
                      .mine = in->mine,
                      .voice = in->voice,
                      .played = in->played,
                      .pending = in->pending};
    if (!hist_uuid_parse(in->uuid, e->uuid)) memset(e->uuid, 0, sizeof e->uuid);
}

uint8_t hist_add(hist_win_t *w, bool front, const hist_in_t *in, uint8_t n)
{
    /* What fits of the new ones, nearest the window first: the far end of a page is what goes
       when even the page alone is more than the window holds. */
    uint16_t lens[HIST_MAX];
    uint8_t take = 0;
    uint32_t need = 0;
    for (uint8_t k = 0; k < n && take < HIST_MAX; k++) {
        const uint8_t j = front ? (uint8_t)(n - 1 - k) : k;
        const uint16_t len = keep_len(in[j].text);
        if (need + cost(len) > HIST_TEXT_BYTES) break;
        lens[take++] = len;
        need += cost(len);
    }
    uint8_t trimmed = 0;
    while (w->n && (w->n + take > HIST_MAX || w->used + need > HIST_TEXT_BYTES)) {
        drop(w, !front);
        trimmed++;
    }
    if (front) {
        w->newer += trimmed;
        w->older += (uint32_t)(n - take);
        memmove(w->text + need, w->text, w->used);
        memmove(w->e + take, w->e, sizeof w->e[0] * w->n);
        for (uint8_t k = take; k < take + w->n; k++) w->e[k].off = (uint16_t)(w->e[k].off + need);
        uint16_t off = 0;
        for (uint8_t k = 0; k < take; k++) {
            /* lens[] runs nearest first; the window runs oldest first. */
            const uint8_t j = (uint8_t)(n - take + k);
            const uint16_t len = lens[take - 1 - k];
            memcpy(w->text + off, in[j].text ? in[j].text : "", len);
            w->text[off + len] = '\0';
            fill(&w->e[k], &in[j], off, len);
            off = (uint16_t)(off + cost(len));
        }
    } else {
        w->older += trimmed;
        w->newer += (uint32_t)(n - take);
        uint16_t off = w->used;
        for (uint8_t k = 0; k < take; k++) {
            memcpy(w->text + off, in[k].text ? in[k].text : "", lens[k]);
            w->text[off + lens[k]] = '\0';
            fill(&w->e[w->n + k], &in[k], off, lens[k]);
            off = (uint16_t)(off + cost(lens[k]));
        }
    }
    w->n = (uint8_t)(w->n + take);
    w->used = (uint16_t)(w->used + need);
    return trimmed;
}

const char *hist_text(const hist_win_t *w, uint8_t i)
{
    return i < w->n ? w->text + w->e[i].off : "";
}

void hist_to_msg(const hist_win_t *w, uint8_t i, msg_t *out)
{
    memset(out, 0, sizeof *out);
    if (i >= w->n) return;
    const hist_ent_t *e = &w->e[i];
    out->id = e->local_id;
    out->box_id = e->box_id;
    out->mine = e->mine;
    out->voice = e->voice;
    out->played = e->played;
    out->pending = e->pending;
    out->dur_s = e->dur_s;
    out->dur_ms = e->dur_ms;
    /* A bubble keeps 255 bytes: a longer one (only ever the agent's whole newest answer, copied
       from the live thread) is cut on a character. */
    const char *t = hist_text(w, i);
    size_t n = e->len;
    if (n > sizeof out->text - 1) {
        n = sizeof out->text - 1;
        while (n > 0 && ((unsigned char)t[n] & 0xC0) == 0x80) n--;
    }
    memcpy(out->text, t, n);
    out->text[n] = '\0';
    if (e->mine && hist_uuid_set(e->uuid))
        snprintf(out->status, sizeof out->status, "%s", e->played ? "Played" : "Sent");
}

int hist_find(const hist_win_t *w, uint32_t local_id)
{
    for (uint8_t i = 0; i < w->n; i++)
        if (w->e[i].local_id == local_id) return i;
    return -1;
}

static int hexv(char c)
{
    return c >= '0' && c <= '9' ? c - '0' : c >= 'a' && c <= 'f' ? c - 'a' + 10 : -1;
}

bool hist_uuid_parse(const char *text, uint8_t out[16])
{
    if (!text || strlen(text) != 36) return false;
    uint8_t k = 0;
    for (int i = 0; i < 36;) {
        if (i == 8 || i == 13 || i == 18 || i == 23) {
            if (text[i++] != '-') return false;
            continue;
        }
        const int hi = hexv(text[i]), lo = hexv(text[i + 1]);
        if (hi < 0 || lo < 0) return false;
        out[k++] = (uint8_t)(hi << 4 | lo);
        i += 2;
    }
    return true;
}

void hist_uuid_text(const uint8_t raw[16], char out[37])
{
    static const char D[] = "0123456789abcdef";
    int o = 0;
    for (int k = 0; k < 16; k++) {
        if (k == 4 || k == 6 || k == 8 || k == 10) out[o++] = '-';
        out[o++] = D[raw[k] >> 4];
        out[o++] = D[raw[k] & 15];
    }
    out[o] = '\0';
}

bool hist_uuid_set(const uint8_t raw[16])
{
    for (int k = 0; k < 16; k++)
        if (raw[k]) return true;
    return false;
}

hist_top_t hist_top_row(bool loading, int32_t older, int64_t start_at)
{
    if (loading) return HIST_TOP_LOADING;
    if (older > 0) return HIST_TOP_LOAD;
    if (older == 0 && start_at > 0) return HIST_TOP_START;
    return HIST_TOP_NONE;
}

hist_bottom_t hist_bottom_row(bool loading, int32_t newer)
{
    if (loading) return HIST_BOTTOM_LOADING;
    return newer > 0 ? HIST_BOTTOM_NEWER : HIST_BOTTOM_NONE;
}

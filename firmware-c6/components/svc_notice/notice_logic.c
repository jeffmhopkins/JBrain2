#include "notice_logic.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void notice_pickup_query(const notice_prefs_t *p, char *out, size_t cap)
{
    snprintf(out, cap, "&na=%d&nq=%d&nc=%d&nf=%u&nt=%u&tz=%u", p->accept ? 1 : 0,
             p->quiet ? 1 : 0, p->chime ? 1 : 0, (unsigned)(p->night_from % 1440),
             (unsigned)(p->night_to % 1440), (unsigned)p->tz);
}

uint8_t notice_waiting_count(const char *header)
{
    if (!header || !header[0]) return 0;
    unsigned n = 0;
    for (const char *c = header; *c; c++) {
        if (*c < '0' || *c > '9' || n > 1000) return 0;
        n = n * 10 + (unsigned)(*c - '0');
    }
    return n > 255 ? 255 : (uint8_t)n;
}

static bool id_ok(const char *id)
{
    if (strlen(id) != NOTICE_ID_TEXT - 1) return false;
    for (int i = 0; i < NOTICE_ID_TEXT - 1; i++) {
        const char c = id[i];
        const bool dash = i == 8 || i == 13 || i == 18 || i == 23;
        if (dash ? c != '-' : !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    return true;
}

void notice_body_path(const char *id, char *out, size_t cap)
{
    snprintf(out, cap, "/endpoint/notice/%s/body", id);
}

void notice_state_path(const char *id, char *out, size_t cap)
{
    snprintf(out, cap, "/endpoint/notice/%s/state", id);
}

const char *notice_state_name(notice_state_t s)
{
    static const char *const N[] = {"shown", "answered", "dismissed", "expired"};
    return s <= NOTICE_EXPIRED ? N[s] : "shown";
}

void notice_state_json(notice_state_t s, uint8_t choice, char *out, size_t cap)
{
    if (s == NOTICE_ANSWERED)
        snprintf(out, cap, "{\"state\":\"answered\",\"choice\":%u}", (unsigned)choice);
    else
        snprintf(out, cap, "{\"state\":\"%s\"}", notice_state_name(s));
}

bool notice_posts_add(notice_posts_t *l, const char *id, notice_state_t s, uint8_t choice)
{
    bool kept = true;
    if (l->n == NOTICE_POSTS) {
        notice_posts_pop(l);
        kept = false;
    }
    notice_post_t *p = &l->p[l->n++];
    snprintf(p->id, sizeof p->id, "%s", id);
    p->state = (uint8_t)s;
    p->choice = choice;
    return kept;
}

void notice_posts_pop(notice_posts_t *l)
{
    if (!l->n) return;
    memmove(l->p, l->p + 1, sizeof l->p[0] * (size_t)(l->n - 1));
    l->n--;
}

bool notice_posts_final(const notice_posts_t *l, const char *id)
{
    for (uint8_t i = 0; i < l->n; i++)
        if (l->p[i].state != NOTICE_SHOWN && !strcmp(l->p[i].id, id)) return true;
    return false;
}

void notice_posts_clean(notice_posts_t *l)
{
    uint8_t k = 0;
    const uint8_t n = l->n > NOTICE_POSTS ? NOTICE_POSTS : l->n;
    for (uint8_t i = 0; i < n; i++) {
        notice_post_t p = l->p[i];
        p.id[NOTICE_ID_TEXT - 1] = '\0';
        if (!id_ok(p.id) || p.state > NOTICE_EXPIRED || p.choice > 3) continue;
        l->p[k++] = p;
    }
    l->n = k;
}

bool notice_post_done(int status)
{
    return (status >= 200 && status < 300) || status == 404 || status == 409 || status == 422;
}

uint32_t notice_backoff_ms(uint8_t fails)
{
    static const uint32_t STEP[] = {5000, 15000, 60000};
    if (!fails) return 0;
    return fails <= 3 ? STEP[fails - 1] : 300000;
}

uint32_t notice_refetch_ms(uint8_t tries)
{
    static const uint32_t STEP[] = {2000, 10000, 30000};
    return tries >= 1 && tries <= 3 ? STEP[tries - 1] : 30000;
}

bool notice_hide_due(uint32_t shown_ms, uint16_t hide_s, uint32_t now_ms)
{
    return hide_s && (uint32_t)(now_ms - shown_ms) >= (uint32_t)hide_s * 1000u;
}

bool notice_lapsed(uint32_t arrived_ms, uint32_t ttl_s, uint32_t now_ms)
{
    return (uint64_t)(uint32_t)(now_ms - arrived_ms) >= (uint64_t)ttl_s * 1000u;
}

void notice_file_name(uint32_t seq, const uint8_t id[16], uint32_t deadline, char *out, size_t cap)
{
    char hex[33];
    for (int i = 0; i < 16; i++) snprintf(hex + 2 * i, 3, "%02x", id[i]);
    snprintf(out, cap, "%08lx-%s-%08lx.jn1", (unsigned long)seq, hex, (unsigned long)deadline);
}

uint32_t notice_window_left(uint32_t deadline, bool now_known, int64_t now)
{
    if (!deadline || !now_known || now >= (int64_t)deadline) return 0;
    return (uint32_t)((int64_t)deadline - now);
}

static int nib(char c)
{
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static bool hex32(const char *p, uint32_t *out)
{
    uint32_t v = 0;
    for (int i = 0; i < 8; i++) {
        const int d = nib(p[i]);
        if (d < 0) return false;
        v = v << 4 | (uint32_t)d;
    }
    *out = v;
    return true;
}

bool notice_file_parse(const char *name, uint32_t *seq, uint8_t id[16], uint32_t *deadline)
{
    /* 8 hex, '-', 32 hex, '-', 8 hex, ".jn1" */
    if (strlen(name) != 54 || name[8] != '-' || name[41] != '-' || strcmp(name + 50, ".jn1"))
        return false;
    uint32_t s, d;
    if (!hex32(name, &s) || !hex32(name + 42, &d)) return false;
    for (int i = 0; i < 16; i++) {
        const int hi = nib(name[9 + 2 * i]), lo = nib(name[10 + 2 * i]);
        if (hi < 0 || lo < 0) return false;
        id[i] = (uint8_t)(hi << 4 | lo);
    }
    *seq = s;
    *deadline = d;
    return true;
}

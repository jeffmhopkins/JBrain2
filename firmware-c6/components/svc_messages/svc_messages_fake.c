#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "app_event.h"
#include "hist_logic.h"
#include "svc_debug.h"
#include "svc_messages_fake.h"

/* Four threads, each its own buffer: Jerv's and Dave's are separate conversations with separate
 * agents on the box (owner, 2026-10-12: "separate instances with separate history"), so neither
 * may ever show the other's messages or lose them to the other's traffic. The agents keep eight
 * (four exchanges, 0.0.10's number); the two people's threads keep four — the box's last four
 * messages with each twin (svc_post, 0.0.16), the rest are in the PWA. */
#define AGENT_KEEP 8
#define PERSON_KEEP 4

/* An agent's newest answer, whole (MSG_LONG_TEXT): one per agent thread, 2 KB in all, rather
 * than every bubble at 1 KB (24 KB). Older answers show their bubble's 255 bytes. */
typedef struct {
    uint32_t id;
    char text[MSG_LONG_TEXT];
} long_t;

typedef struct {
    const char *principal;
    uint16_t n, cap;
    uint32_t rev;
    msg_t *m;
    long_t *keep; /* NULL for a person's thread */
} thread_t;

static msg_t s_jerv[AGENT_KEEP], s_dave[AGENT_KEEP], s_one[PERSON_KEEP], s_two[PERSON_KEEP];
static long_t s_jerv_long, s_dave_long;

static thread_t s_t[] = {
    {"jerv", 0, AGENT_KEEP, 0, s_jerv, &s_jerv_long},
    {"dave", 0, AGENT_KEEP, 0, s_dave, &s_dave_long},
    {SVC_MESSAGES_THREAD_1, 0, PERSON_KEEP, 0, s_one, NULL},
    {SVC_MESSAGES_THREAD_2, 0, PERSON_KEEP, 0, s_two, NULL},
};
#define THREADS (sizeof s_t / sizeof *s_t)

static bool is_person(const thread_t *t)
{
    return t->cap == PERSON_KEEP;
}

static thread_t *find(const char *principal)
{
    for (unsigned i = 0; i < THREADS; i++)
        if (principal && !strcmp(s_t[i].principal, principal)) return &s_t[i];
    return NULL;
}

/* ---- Load earlier (0.0.21): one window, for the one thread paged back, only while it is ---- */

static hist_win_t *s_win;
static const thread_t *s_win_of;
/* What the live view's "Load earlier · N more" says, per thread: how many older than the live
   thread's oldest message (`counted`, its local id: a count for another oldest is no count), when
   the conversation began, and which row is loading. */
typedef struct {
    int32_t older;
    int64_t start_at;
    uint32_t counted;
    uint8_t loading; /* 0 none, 1 the top row, 2 the bottom row */
} meta_t;
static meta_t s_meta[THREADS];

static unsigned ix(const thread_t *t)
{
    return (unsigned)(t - s_t);
}

static hist_win_t *win(const thread_t *t)
{
    return t && s_win && s_win_of == t ? s_win : NULL;
}

uint16_t svc_messages_count(const char *principal)
{
    thread_t *t = find(principal);
    const hist_win_t *w = win(t);
    return w ? w->n : t ? t->n : 0;
}

uint32_t svc_messages_rev(const char *principal)
{
    thread_t *t = find(principal);
    return t ? t->rev : 0;
}

bool svc_messages_find(const char *principal, uint32_t id, msg_t *out)
{
    thread_t *t = find(principal);
    for (uint16_t i = 0; t && i < t->n; i++)
        if (t->m[i].id == id) {
            *out = t->m[i];
            return true;
        }
    const hist_win_t *w = win(t);
    const int k = w ? hist_find(w, id) : -1;
    if (k >= 0) hist_to_msg(w, (uint8_t)k, out);
    return k >= 0;
}

bool svc_messages_fake_long(const char *principal, uint32_t id, const char *text)
{
    thread_t *t = find(principal);
    if (!t || !t->keep) return false;
    t->rev++;
    t->keep->id = id;
    const size_t n = strnlen(text, sizeof t->keep->text - 1);
    memcpy(t->keep->text, text, n);
    t->keep->text[n] = 0;
    return true;
}

const char *svc_messages_text(const char *principal, const msg_t *m)
{
    thread_t *t = find(principal);
    const hist_win_t *w = win(t);
    const int k = w ? hist_find(w, m->id) : -1;
    if (k >= 0) return hist_text(w, (uint8_t)k);
    if (t && t->keep && t->keep->id && t->keep->id == m->id && !m->mine) return t->keep->text;
    return m->text;
}

bool svc_messages_get(const char *principal, uint16_t idx, msg_t *out)
{
    thread_t *t = find(principal);
    const hist_win_t *w = win(t);
    if (w) {
        if (idx >= w->n) return false;
        hist_to_msg(w, (uint8_t)idx, out);
        return true;
    }
    if (!t || idx >= t->n) return false;
    *out = t->m[idx];
    return true;
}

uint16_t svc_messages_unplayed(const char *principal)
{
    thread_t *t = find(principal);
    uint16_t n = 0;
    for (uint16_t i = 0; t && i < t->n; i++)
        if (!t->m[i].mine && t->m[i].voice && !t->m[i].played) n++;
    return n;
}

void svc_messages_mark_played(const char *principal, uint32_t id)
{
    thread_t *t = find(principal);
    for (uint16_t i = 0; t && i < t->n; i++)
        if (t->m[i].id == id && !t->m[i].played) {
            t->m[i].played = true;
            t->rev++;
        }
    hist_win_t *w = win(t);
    const int k = w ? hist_find(w, id) : -1;
    if (k >= 0 && !w->e[k].played) {
        w->e[k].played = true;
        t->rev++;
    }
}

uint16_t svc_messages_unplayed_total(void)
{
    uint16_t n = 0;
    for (unsigned i = 0; i < THREADS; i++)
        if (is_person(&s_t[i])) n += svc_messages_unplayed(s_t[i].principal);
    return n;
}

bool svc_messages_first_unplayed(char principal[16])
{
    for (unsigned i = 0; i < THREADS; i++)
        if (is_person(&s_t[i]) && svc_messages_unplayed(s_t[i].principal)) {
            snprintf(principal, 16, "%s", s_t[i].principal);
            return true;
        }
    return false;
}

static void announce(const char *principal)
{
    app_event_t ev = {.type = APP_EV_MSG_ARRIVED};
    strncpy(ev.u.principal, principal, sizeof ev.u.principal - 1);
    app_events_post(&ev);
}

bool svc_messages_clear(const char *principal)
{
    thread_t *t = find(principal);
    if (!t) return false;
    svc_messages_page_end(principal);
    s_meta[ix(t)] = (meta_t){.older = -1};
    t->n = 0;
    t->rev++;
    if (t->keep) t->keep->id = 0;
    announce(principal);
    return true;
}

void svc_messages_fake_clear(void)
{
    free(s_win);
    s_win = NULL;
    s_win_of = NULL;
    for (unsigned i = 0; i < THREADS; i++) {
        s_meta[i] = (meta_t){.older = -1};
        s_t[i].n = 0;
        s_t[i].rev++;
        if (s_t[i].keep) s_t[i].keep->id = 0;
    }
}

/* The live thread moved under a window: a message the window has is updated in place; one it has
   not is new — the window steps aside for it when it reaches the newest, and counts it below
   otherwise. */
static void follow(thread_t *t, const msg_t *m)
{
    hist_win_t *w = win(t);
    if (!w) return;
    const int k = hist_find(w, m->id);
    if (k >= 0) {
        w->e[k].played = m->played;
        w->e[k].pending = m->pending;
        if (m->box_id) w->e[k].box_id = m->box_id;
        return;
    }
    if (w->newer == 0)
        svc_messages_page_end(t->principal);
    else
        w->newer++;
}

static msg_t *slot_for(thread_t *t, uint32_t id)
{
    for (uint16_t i = 0; i < t->n; i++)
        if (t->m[i].id == id) return &t->m[i];
    if (t->n == t->cap) {
        memmove(t->m, t->m + 1, sizeof t->m[0] * (t->cap - 1));
        t->n--;
    }
    return &t->m[t->n++];
}

bool svc_messages_fake_add(const char *principal, const msg_t *m)
{
    thread_t *t = find(principal);
    if (!t) return false;
    *slot_for(t, m->id) = *m;
    t->rev++;
    follow(t, m);
    announce(principal);
    return true;
}

/* A bubble the panel made that the box has not listed: a send on its way, or one that failed. */
static bool local_only(const msg_t *m)
{
    return m->mine && (m->pending || !strcmp(m->status, "Not sent"));
}

bool svc_messages_fake_set(const char *principal, const msg_t *m, uint16_t n)
{
    thread_t *t = find(principal);
    if (!t || t->keep) return false;
    msg_t next[PERSON_KEEP];
    uint16_t k = 0, all = 0;
    for (uint16_t i = 0; i < t->n; i++) all += local_only(&t->m[i]);
    const uint16_t local = all > t->cap ? t->cap : all;
    /* The box's newest that fit beside the panel's own newest, then those. */
    const uint16_t room = (uint16_t)(t->cap - local);
    for (uint16_t i = n > room ? (uint16_t)(n - room) : 0; i < n; i++) next[k++] = m[i];
    for (uint16_t i = 0, seen = 0; i < t->n; i++) {
        if (!local_only(&t->m[i]) || seen++ < all - local) continue;
        next[k++] = t->m[i];
    }
    if (k == t->n && !memcmp(next, t->m, sizeof next[0] * k)) return false;
    memcpy(t->m, next, sizeof next[0] * k);
    t->n = k;
    t->rev++;
    for (uint16_t i = 0; i < k && win(t); i++) follow(t, &t->m[i]);
    announce(principal);
    return true;
}

/* ---- Load earlier: the window ---- */

bool svc_messages_page_begin(const char *principal)
{
    thread_t *t = find(principal);
    if (!t) return false;
    if (win(t)) return true;
    if (s_win) svc_messages_page_end(s_win_of->principal);
    s_win = malloc(sizeof *s_win);
    if (!s_win) return false;
    hist_init(s_win);
    s_win_of = t;
    hist_in_t in[AGENT_KEEP];
    for (uint16_t i = 0; i < t->n; i++)
        in[i] = (hist_in_t){.local_id = t->m[i].id,
                            .box_id = t->m[i].box_id,
                            .mine = t->m[i].mine,
                            .voice = t->m[i].voice,
                            .played = t->m[i].played,
                            .pending = t->m[i].pending,
                            .dur_s = t->m[i].dur_s,
                            .dur_ms = t->m[i].dur_ms,
                            .text = svc_messages_text(principal, &t->m[i])};
    hist_add(s_win, false, in, (uint8_t)t->n);
    s_win->newer = 0;
    const unsigned i = ix(t);
    s_win->older = s_meta[i].older > 0 ? (uint32_t)s_meta[i].older : 0;
    s_win->start_at = s_meta[i].start_at;
    t->rev++;
    return true;
}

void svc_messages_page_end(const char *principal)
{
    thread_t *t = find(principal);
    if (!win(t)) return;
    free(s_win);
    s_win = NULL;
    s_win_of = NULL;
    s_meta[ix(t)].loading = 0;
    t->rev++;
    announce(principal);
}

bool svc_messages_paged(const char *principal)
{
    return win(find(principal)) != NULL;
}

void svc_messages_page_add(const char *principal, bool front, const hist_in_t *in, uint8_t n,
                           uint32_t remaining, int64_t start_at)
{
    thread_t *t = find(principal);
    hist_win_t *w = win(t);
    if (!w) return;
    /* Already there (the live thread's own, or a page asked twice): never twice on the glass. */
    hist_in_t fresh[HIST_PAGE];
    uint8_t k = 0;
    for (uint8_t i = 0; i < n && k < HIST_PAGE; i++) {
        bool dup = false;
        uint8_t raw[16];
        const bool has_uuid = hist_uuid_parse(in[i].uuid, raw);
        for (uint8_t j = 0; j < w->n && !dup; j++)
            dup = (in[i].box_id && in[i].box_id == w->e[j].box_id) ||
                  (has_uuid && !memcmp(raw, w->e[j].uuid, 16));
        if (!dup) fresh[k++] = in[i];
    }
    hist_add(w, front, fresh, k);
    if (front) {
        w->older = remaining;
        if (start_at) w->start_at = start_at;
    } else {
        w->newer = remaining;
    }
    t->rev++;
    announce(principal);
}

void svc_messages_page_uuid_set(const char *principal, uint32_t local_id, const char *uuid)
{
    hist_win_t *w = win(find(principal));
    const int k = w ? hist_find(w, local_id) : -1;
    if (k >= 0 && !hist_uuid_parse(uuid, w->e[k].uuid)) memset(w->e[k].uuid, 0, 16);
}

bool svc_messages_page_uuid(uint32_t local_id, char principal[16], char uuid[37])
{
    if (!s_win) return false;
    const int k = hist_find(s_win, local_id);
    if (k < 0 || !hist_uuid_set(s_win->e[k].uuid)) return false;
    hist_uuid_text(s_win->e[k].uuid, uuid);
    snprintf(principal, 16, "%s", s_win_of->principal);
    return true;
}

bool svc_messages_edge(const char *principal, bool oldest, msg_t *out)
{
    const uint16_t n = svc_messages_count(principal);
    if (!n) return false;
    return svc_messages_get(principal, oldest ? 0 : (uint16_t)(n - 1), out);
}

void svc_messages_hist_counts(const char *principal, uint32_t counted, int32_t older,
                              int64_t start_at)
{
    thread_t *t = find(principal);
    if (!t) return;
    meta_t *m = &s_meta[ix(t)];
    m->counted = counted;
    m->older = older;
    if (start_at) m->start_at = start_at;
    t->rev++;
    announce(principal);
}

void svc_messages_hist_loading(const char *principal, uint8_t side)
{
    thread_t *t = find(principal);
    if (!t || s_meta[ix(t)].loading == side) return;
    s_meta[ix(t)].loading = side;
    t->rev++;
    announce(principal);
}

bool svc_messages_hist_counted(const char *principal)
{
    thread_t *t = find(principal);
    if (!t) return false;
    const meta_t *m = &s_meta[ix(t)];
    return m->older >= 0 && m->counted == (t->n ? t->m[0].id : 0);
}

void svc_messages_hist(const char *principal, hist_view_t *out)
{
    memset(out, 0, sizeof *out);
    thread_t *t = find(principal);
    if (!t) return;
    const meta_t *m = &s_meta[ix(t)];
    const hist_win_t *w = win(t);
    if (w) {
        out->paged = true;
        out->older = w->older;
        out->start_at = w->start_at;
        out->top = (uint8_t)hist_top_row(m->loading == 1, (int32_t)w->older, w->start_at);
        out->newer = w->newer;
        out->bottom = (uint8_t)hist_bottom_row(m->loading == 2, (int32_t)w->newer);
        return;
    }
    const int32_t older = svc_messages_hist_counted(principal) ? m->older : -1;
    out->older = older > 0 ? (uint32_t)older : 0;
    out->start_at = m->start_at;
    out->top = (uint8_t)hist_top_row(m->loading == 1, older, m->start_at);
}

uint32_t svc_messages_window_bytes(void)
{
    return s_win ? (uint32_t)sizeof *s_win : 0;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    for (unsigned i = 0; i < THREADS; i++) {
        dbg_json_obj_begin(w, s_t[i].principal);
        dbg_json_int(w, "count", s_t[i].n);
        dbg_json_int(w, "keeps", s_t[i].cap);
        dbg_json_int(w, "unplayed", svc_messages_unplayed(s_t[i].principal));
        dbg_json_int(w, "last_id", s_t[i].n ? s_t[i].m[s_t[i].n - 1].id : 0);
        dbg_json_int(w, "older", s_meta[i].older);
        if (s_win_of == &s_t[i]) {
            dbg_json_int(w, "paged", s_win->n);
            dbg_json_int(w, "paged_older", s_win->older);
            dbg_json_int(w, "paged_newer", s_win->newer);
            dbg_json_int(w, "paged_text", s_win->used);
        }
        if (s_t[i].keep) {
            dbg_json_int(w, "long_id", s_t[i].keep->id);
            dbg_json_int(w, "long_bytes", s_t[i].keep->id ? (int32_t)strlen(s_t[i].keep->text) : 0);
        }
        dbg_json_obj_end(w);
    }
}

void svc_messages_init(void)
{
    for (unsigned i = 0; i < THREADS; i++) s_meta[i].older = -1;
    svc_debug_register("messages", describe, NULL);
}

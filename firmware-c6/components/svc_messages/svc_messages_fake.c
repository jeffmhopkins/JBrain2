#include <string.h>

#include "app_event.h"
#include "svc_debug.h"
#include "svc_messages_fake.h"

#define THREADS 3
#define PER_THREAD 12

typedef struct {
    const char *principal;
    uint16_t n;
    msg_t m[PER_THREAD];
} thread_t;

static thread_t s_t[THREADS] = {
    {"jerv", 2, {{1, true, true, true, false, 3, "Add milk to the shopping list.", ""},
                 {2, false, false, true, false, 0, "Added milk to your shopping list.", ""}}},
    {"quorra", 2, {{3, true, true, true, false, 4, "Have a good day at school!", "Played"},
                   {4, false, true, false, false, 5, "Daddy, when are you coming home?", ""}}},
    {"tamsin", 2, {{5, false, true, true, false, 3, "I made you a drawing.", ""},
                   {6, true, true, true, false, 2, "I can't wait to see it!", "Played"}}},
};

static thread_t *find(const char *principal)
{
    for (int i = 0; i < THREADS; i++)
        if (principal && !strcmp(s_t[i].principal, principal)) return &s_t[i];
    return NULL;
}

uint16_t svc_messages_count(const char *principal)
{
    thread_t *t = find(principal);
    return t ? t->n : 0;
}

bool svc_messages_get(const char *principal, uint16_t idx, msg_t *out)
{
    thread_t *t = find(principal);
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
        if (t->m[i].id == id) t->m[i].played = true;
}

bool svc_messages_fake_add(const char *principal, const msg_t *m)
{
    thread_t *t = find(principal);
    if (!t) return false;
    if (t->n == PER_THREAD) {
        memmove(t->m, t->m + 1, sizeof t->m[0] * (PER_THREAD - 1));
        t->n--;
    }
    t->m[t->n++] = *m;
    app_event_t ev = {.type = APP_EV_MSG_ARRIVED};
    strncpy(ev.u.principal, principal, sizeof ev.u.principal - 1);
    app_events_post(&ev);
    return true;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    for (int i = 0; i < THREADS; i++) {
        dbg_json_obj_begin(w, s_t[i].principal);
        dbg_json_int(w, "count", s_t[i].n);
        dbg_json_int(w, "unplayed", svc_messages_unplayed(s_t[i].principal));
        dbg_json_int(w, "last_id", s_t[i].n ? s_t[i].m[s_t[i].n - 1].id : 0);
        dbg_json_obj_end(w);
    }
}

void svc_messages_init(void)
{
    svc_debug_register("messages", describe, NULL);
}

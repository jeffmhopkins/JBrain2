#include <stdio.h>
#include <string.h>

#include "app_event.h"
#include "svc_debug.h"
#include "svc_messages_fake.h"

/* Four threads, each its own buffer: Jerv's and Dave's are separate conversations with separate
 * agents on the box (owner, 2026-10-12: "separate instances with separate history"), so neither
 * may ever show the other's messages or lose them to the other's traffic. The agents keep eight
 * (four exchanges, 0.0.10's number); the two people's threads keep four, because until the box
 * client for messages lands they only ever hold "not sent" placeholders — and the bytes that saves
 * are what Dave's eight cost, so 0.0.11's static RAM for messages is 0.0.10's. */
#define AGENT_KEEP 8
#define PERSON_KEEP 4

typedef struct {
    const char *principal;
    uint16_t n, cap;
    msg_t *m;
} thread_t;

static msg_t s_jerv[AGENT_KEEP], s_dave[AGENT_KEEP], s_one[PERSON_KEEP], s_two[PERSON_KEEP];

static thread_t s_t[] = {
    {"jerv", 0, AGENT_KEEP, s_jerv},
    {"dave", 0, AGENT_KEEP, s_dave},
    {SVC_MESSAGES_THREAD_1, 0, PERSON_KEEP, s_one},
    {SVC_MESSAGES_THREAD_2, 0, PERSON_KEEP, s_two},
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

uint16_t svc_messages_count(const char *principal)
{
    thread_t *t = find(principal);
    return t ? t->n : 0;
}

bool svc_messages_find(const char *principal, uint32_t id, msg_t *out)
{
    thread_t *t = find(principal);
    for (uint16_t i = 0; t && i < t->n; i++)
        if (t->m[i].id == id) {
            *out = t->m[i];
            return true;
        }
    return false;
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
    t->n = 0;
    announce(principal);
    return true;
}

void svc_messages_fake_clear(void)
{
    for (unsigned i = 0; i < THREADS; i++) s_t[i].n = 0;
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
    announce(principal);
    return true;
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
        dbg_json_obj_end(w);
    }
}

void svc_messages_init(void)
{
    svc_debug_register("messages", describe, NULL);
}

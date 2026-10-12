/* The siblings list as the panel holds it: the slots (the UI task's alone), and one hand-over
 * from the main task, which fetches it. */
#include <stdio.h>
#include <string.h>

#include "app_event.h"
#include "svc_debug.h"
#include "svc_messages.h"
#include "svc_siblings.h"
#include "svc_siblings_drv.h"

static const char *const THREADS[SIB_THREADS] = {SVC_MESSAGES_THREAD_1, SVC_MESSAGES_THREAD_2};

static sib_slots_t s_slots;
static const char *s_source = "none"; /* "none", "cache", "box" */

/* The hand-over: written by the main task, read by the UI task. A sequence count makes it safe
 * with no lock — odd while the writer is inside, and a reader that sees it change across its
 * copy throws the copy away and takes it on the next pass. Fetches are minutes apart. */
static sib_list_t s_pending;
static uint32_t s_seq, s_applied;
static uint32_t s_box, s_fails, s_bad;
static int s_last_status;

static uint32_t ld(const uint32_t *p)
{
    return __atomic_load_n(p, __ATOMIC_ACQUIRE);
}

static void st(uint32_t *p, uint32_t v)
{
    __atomic_store_n(p, v, __ATOMIC_RELEASE);
}

bool svc_siblings_offer(const char *json, size_t len)
{
    sib_list_t got;
    if (!sib_parse(json, len, &got)) {
        s_bad++;
        s_last_status = 200;
        return false;
    }
    const uint32_t seq = ld(&s_seq);
    st(&s_seq, seq + 1);
    s_pending = got;
    st(&s_seq, seq + 2);
    s_box++;
    s_last_status = 200;
    return true;
}

void svc_siblings_failed(int status)
{
    s_fails++;
    s_last_status = status;
}

static void announce(const char *thread)
{
    app_event_t ev = {.type = APP_EV_MSG_ARRIVED};
    snprintf(ev.u.principal, sizeof ev.u.principal, "%s", thread);
    app_events_post(&ev);
}

void svc_siblings_service(void)
{
    const uint32_t seq = ld(&s_seq);
    if (seq == s_applied || seq & 1u) return;
    const sib_list_t list = s_pending;
    if (ld(&s_seq) != seq) return;
    s_applied = seq;
    s_source = "box";
    bool renamed;
    const uint8_t changed = sib_merge(&s_slots, &list, &renamed);
    for (int k = 0; k < SIB_THREADS; k++) {
        /* That thread's messages were with a different panel (or with nobody). */
        if (changed >> k & 1u) svc_messages_clear(THREADS[k]);
        else if (renamed) announce(THREADS[k]);
    }
    if (changed || renamed) sib_port_save(&s_slots);
}

bool svc_siblings_slot(uint8_t slot, sibling_t *out)
{
    if (slot >= SIB_MAX || !s_slots.used[slot]) return false;
    *out = s_slots.s[slot];
    return true;
}

void svc_siblings_name(uint8_t slot, char *out, size_t cap)
{
    if (slot < SIB_MAX && s_slots.used[slot] && s_slots.s[slot].name[0])
        snprintf(out, cap, "%s", s_slots.s[slot].name);
    else
        snprintf(out, cap, "Panel %u", (unsigned)slot + 1);
}

char svc_siblings_initial(uint8_t slot)
{
    char name[SIB_NAME_BYTES + 1];
    svc_siblings_name(slot, name, sizeof name);
    /* "Panel 2"'s letter would be P on both tiles; the number tells them apart. */
    if (slot < SIB_MAX && !s_slots.used[slot]) return (char)('1' + slot);
    return sib_initial(name);
}

ui_accent_t svc_siblings_accent(uint8_t slot)
{
    static const ui_accent_t PEOPLE[SIB_MAX] = {UI_ACCENT_PERSON_A, UI_ACCENT_PERSON_B,
                                                UI_ACCENT_PERSON_C, UI_ACCENT_PERSON_D};
    if (slot < SIB_MAX && s_slots.used[slot]) return PEOPLE[s_slots.s[slot].accent % SIB_MAX];
    return slot < SIB_MAX ? PEOPLE[slot] : UI_ACCENT_NEUTRAL;
}

const char *svc_siblings_thread(uint8_t slot)
{
    return slot < SIB_THREADS ? THREADS[slot] : NULL;
}

void svc_siblings_note(char *out, size_t len)
{
    snprintf(out, len, "sib=%s,box:%lu,bad:%lu,fails:%lu,last:%d", s_source,
             (unsigned long)s_box, (unsigned long)s_bad, (unsigned long)s_fails, s_last_status);
}

/* Names are fine in debug output (owner, 2026-10-12); ids too — neither is a secret. */
static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_str(w, "source", s_source);
    dbg_json_int(w, "fetched", s_box);
    dbg_json_int(w, "unreadable", s_bad);
    dbg_json_int(w, "failed", s_fails);
    dbg_json_int(w, "last_status", s_last_status);
    dbg_json_arr_begin(w, "slots");
    for (uint8_t k = 0; k < SIB_MAX; k++) {
        dbg_json_obj_begin(w, NULL);
        dbg_json_int(w, "slot", k);
        if (s_slots.used[k]) {
            dbg_json_str(w, "device_id", s_slots.s[k].device_id);
            dbg_json_str(w, "name", s_slots.s[k].name);
            dbg_json_int(w, "accent", s_slots.s[k].accent);
        }
        if (k < SIB_THREADS) dbg_json_str(w, "thread", THREADS[k]);
        dbg_json_obj_end(w);
    }
    dbg_json_arr_end(w);
}

void svc_siblings_init(void)
{
    memset(&s_slots, 0, sizeof s_slots);
    if (sib_port_load(&s_slots)) s_source = "cache";
    svc_debug_register("siblings", describe, NULL);
}

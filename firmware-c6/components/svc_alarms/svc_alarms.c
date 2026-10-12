/* The alarms as the panel holds them: the schedule (the UI task's alone), one hand-over from the
 * main task, which fetches the list, the switch queue it drains, and a published view of the
 * schedule for the readers on other tasks (telemetry, the debug snapshot). */
#include <stdio.h>
#include <string.h>

#include "app_event.h"
#include "svc_alarms.h"
#include "svc_alarms_drv.h"
#include "svc_audio.h"
#include "svc_debug.h"
#include "svc_rtc.h"
#include "svc_settings.h"
#include "time_logic.h"

/* A change is written to flash once the panel has been still this long: a run of switches on the
 * glass is one write, as the settings are (settings_store.c). */
#define SAVE_AFTER_MS 2000

static alarm_sched_t s_s;
static const char *s_source = "none"; /* "none", "cache", "box" */
static bool s_dirty;
static uint32_t s_dirty_ms;
static void (*s_wake)(void);

/* The zone's rule, parsed once per change of zone. The UI task's alone, like the schedule. */
static tz_rule_t s_tz;
static int32_t s_tz_idx = -1;

/* What another task may read, under alarm_port_lock. The schedule and the zone are the UI task's
 * alone: telemetry runs on the main task and the debug snapshot can too, and neither may walk
 * the list while an applied one is being copied in, nor parse the zone beside the UI task. So
 * the UI task publishes a copy — its state, the zone it used and what rings next — at most once
 * a second and whenever something changed, and those readers read only that. */
typedef struct {
    alarm_sched_t s;
    tz_rule_t tz;
    const char *source;
    bool has_next;
    alarm_next_t next;
} alarm_view_t;
static alarm_view_t s_view;
static bool s_view_stale;
static uint32_t s_view_ms;
#define VIEW_EVERY_MS 1000
/* The debug snapshot's own copy: static rather than on whichever task's stack runs it, and one
 * is enough because the relay runs one request at a time. */
static alarm_view_t s_dbg;

/* Shared with the main task, under alarm_port_lock: the switches on their way and the version. */
static alarm_queue_t s_q;
static char s_etag[ALARM_ETAG_BYTES];

/* The hand-over: written by the main task, read by the UI task, guarded by a sequence count as
 * the siblings' is (odd while the writer is inside; a reader that sees it move retries). */
static alarm_list_t s_pending;
static uint32_t s_seq, s_applied;

/* For telemetry and the snapshot. Written by the main task, read anywhere: counters. */
static uint32_t s_fetched, s_unchanged, s_failed, s_bad, s_puts, s_put_fails;
static int s_last_status;
static uint32_t s_fetch_ms; /* uptime of the last answer, 200 or 304 */

static uint32_t ld(const uint32_t *p)
{
    return __atomic_load_n(p, __ATOMIC_ACQUIRE);
}

static void st(uint32_t *p, uint32_t v)
{
    __atomic_store_n(p, v, __ATOMIC_RELEASE);
}

static const tz_rule_t *zone(void)
{
    const int32_t idx = svc_settings_get(SET_TZ);
    if (idx != s_tz_idx) {
        /* Parsed aside and committed with its index, so the rule and the index never disagree. */
        tz_rule_t r;
        if (!tz_parse(tz_posix(idx), &r)) r = (tz_rule_t){0};
        s_tz = r;
        s_tz_idx = idx;
    }
    return &s_tz;
}

static void touched(void)
{
    s_view_stale = true;
    s_dirty = true;
    s_dirty_ms = svc_rtc_uptime_ms();
}

/* UI task only. Every pass of the service once a second or after a change, and at once after a
 * call from an app or the relay, so a snapshot taken in the same request already shows it. */
static void publish(uint32_t now_ms)
{
    alarm_next_t n;
    const bool has = svc_alarms_next(&n);
    alarm_port_lock();
    s_view.s = s_s;
    s_view.tz = s_tz;
    s_view.source = s_source;
    s_view.has_next = has;
    if (has) s_view.next = n;
    alarm_port_unlock();
    s_view_stale = false;
    s_view_ms = now_ms;
}

static void post(app_event_type_t type, uint32_t id)
{
    app_event_t ev = {.type = type, .at_ms = svc_rtc_uptime_ms(), .u.alarm_id = id};
    app_events_post(&ev);
}

/* ---- main task ---- */

void svc_alarms_etag(char *out, size_t cap)
{
    alarm_port_lock();
    snprintf(out, cap, "%s", s_etag);
    alarm_port_unlock();
}

bool svc_alarms_offer(const char *json, size_t len)
{
    alarm_list_t got;
    s_last_status = 200;
    if (!alarm_parse(json, len, &got)) {
        s_bad++;
        return false;
    }
    const uint32_t seq = ld(&s_seq);
    st(&s_seq, seq + 1);
    s_pending = got;
    st(&s_seq, seq + 2);
    s_fetched++;
    s_fetch_ms = svc_rtc_uptime_ms();
    return true;
}

void svc_alarms_unchanged(void)
{
    s_unchanged++;
    s_last_status = 304;
    s_fetch_ms = svc_rtc_uptime_ms();
}

void svc_alarms_failed(int status)
{
    s_failed++;
    s_last_status = status;
}

bool svc_alarms_take_put(alarm_put_t *out)
{
    alarm_port_lock();
    const bool got = alarm_queue_peek(&s_q, out);
    alarm_port_unlock();
    return got;
}

void svc_alarms_put_done(const alarm_put_t *sent, int status)
{
    alarm_port_lock();
    alarm_queue_done(&s_q, sent, status);
    alarm_port_unlock();
    if ((status >= 200 && status < 300) || status == 404) s_puts++;
    else s_put_fails++;
    s_last_status = status;
    /* The queue moved: the cache is saved on the UI task's next still moment. */
    s_dirty = true;
}

/* ---- UI task ---- */

static void sound(bool on)
{
    if (on) svc_audio_ring(s_s.ring.sound);
    else svc_audio_ring_stop();
}

/* A one-off that has rung switches itself off, and the box hears it like a switch on the glass. */
static void switch_off(uint32_t id)
{
    if (!id) return;
    alarm_port_lock();
    alarm_queue_put(&s_q, id, false);
    alarm_port_unlock();
    touched();
    if (s_wake) s_wake();
}

static void apply_pending(void)
{
    const uint32_t seq = ld(&s_seq);
    if (seq == s_applied || seq & 1u) return;
    alarm_list_t l = s_pending;
    if (ld(&s_seq) != seq) return;
    s_applied = seq;
    s_source = "box";
    alarm_port_lock();
    alarm_queue_overlay(&s_q, &l);
    const bool moved = strcmp(s_etag, l.etag) != 0;
    snprintf(s_etag, sizeof s_etag, "%s", l.etag);
    alarm_port_unlock();
    alarm_sched_set_list(&s_s, &l);
    s_view_stale = true;
    if (moved) touched();
}

static void save(void)
{
    alarm_cache_t c;
    memset(&c, 0, sizeof c);
    c.list = s_s.list;
    alarm_port_lock();
    c.queue = s_q;
    snprintf(c.list.etag, sizeof c.list.etag, "%s", s_etag);
    alarm_port_unlock();
    alarm_port_save(&c);
}

void svc_alarms_service(uint32_t now_ms)
{
    apply_pending();
    int64_t now;
    if (svc_rtc_epoch(&now)) {
        uint32_t off;
        if (alarm_sched_cap(&s_s, now, now_ms, &off)) {
            s_view_stale = true;
            sound(false);
            switch_off(off);
            post(APP_EV_ALARM_ENDED, s_s.ring.id);
        }
        if (alarm_sched_tick(&s_s, zone(), now, now_ms)) {
            s_view_stale = true;
            sound(true);
            post(APP_EV_ALARM_DUE, s_s.ring.id);
        }
    }
    if (s_dirty && (uint32_t)(now_ms - s_dirty_ms) >= SAVE_AFTER_MS) {
        s_dirty = false;
        save();
    }
    if (s_view_stale || (uint32_t)(now_ms - s_view_ms) >= VIEW_EVERY_MS) publish(now_ms);
}

/* ---- the app-facing interface ---- */

uint8_t svc_alarms_count(void)
{
    return s_s.list.n;
}

bool svc_alarms_get(uint8_t idx, alarm_t *out)
{
    if (idx >= s_s.list.n) return false;
    *out = s_s.list.a[idx];
    return true;
}

void svc_alarms_set_on(uint32_t id, bool on)
{
    if (!alarm_sched_set_on(&s_s, id, on)) return;
    alarm_port_lock();
    alarm_queue_put(&s_q, id, on);
    alarm_port_unlock();
    touched();
    publish(svc_rtc_uptime_ms());
    if (s_wake) s_wake();
}

bool svc_alarms_ringing(alarm_t *out)
{
    if (!s_s.ringing) return false;
    if (out) *out = s_s.ring;
    return true;
}

uint16_t svc_alarms_snooze(void)
{
    int64_t now = 0;
    svc_rtc_epoch(&now);
    const uint32_t id = s_s.ring.id;
    int64_t until = alarm_sched_snooze(&s_s, now);
    if (until >= 0) {
        sound(false);
        post(APP_EV_ALARM_ENDED, id);
    } else {
        until = now;
    }
    publish(svc_rtc_uptime_ms());
    civil_t c;
    tz_local(zone(), until, &c);
    return (uint16_t)(c.h * 60 + c.m);
}

void svc_alarms_stop(void)
{
    int64_t now = 0;
    svc_rtc_epoch(&now);
    const uint32_t id = s_s.ring.id;
    const bool was = s_s.ringing;
    uint32_t off;
    alarm_sched_stop(&s_s, now, &off);
    switch_off(off);
    if (was) {
        sound(false);
        post(APP_EV_ALARM_ENDED, id);
    }
    publish(svc_rtc_uptime_ms());
}

bool svc_alarms_next(alarm_next_t *out)
{
    int64_t now, at;
    if (!svc_rtc_epoch(&now)) return false;
    if (!alarm_sched_next(&s_s, zone(), now, &out->a, &at, &out->snoozed)) return false;
    civil_t c;
    tz_local(zone(), at, &c);
    out->at_h = c.h;
    out->at_m = c.m;
    out->wait_min = (uint32_t)((at - now + 59) / 60);
    return true;
}

void svc_alarms_sync_line(char *buf, size_t len)
{
    alarm_port_lock();
    const uint8_t waiting = alarm_queue_count(&s_q);
    alarm_port_unlock();
    const char *where = "Set in the PWA (jpanel → Panels → Jeff's). They ring from the panel's "
                        "own clock, even with Wi-Fi off.";
    if (!strcmp(s_source, "none"))
        snprintf(buf, len, "Waiting for the box. Alarms are set in the PWA (jpanel → Panels → "
                           "Jeff's) and ring from the panel's own clock.");
    else if (waiting)
        snprintf(buf, len, "%s Your switch reaches the box when it next answers.", where);
    else
        snprintf(buf, len, "%s", where);
}

bool svc_alarms_test(uint8_t snd)
{
    int64_t now;
    if (!svc_rtc_epoch(&now)) return false;
    alarm_sched_test(&s_s, snd, zone(), now, svc_rtc_uptime_ms());
    publish(svc_rtc_uptime_ms());
    sound(true);
    post(APP_EV_ALARM_DUE, 0);
    return true;
}

/* ---- telemetry and the snapshot ---- */

static const char *const ENDS[] = {"none", "stopped", "snoozed", "timed_out"};

static const char *fetch_word(void)
{
    if (!s_fetched && !s_unchanged && !s_failed) return "never";
    return s_last_status == 304 ? "304" : s_last_status == 200 ? "ok" : "fail";
}

/* Main task: the published view only. */
void svc_alarms_note(char *out, size_t len)
{
    alarm_port_lock();
    const uint8_t q = alarm_queue_count(&s_q);
    const uint8_t n = s_view.s.list.n;
    const uint32_t fired = s_view.s.fired, snz = s_view.s.snoozes, stop = s_view.s.dismissed,
                   cap = s_view.s.timed_out, miss = s_view.s.missed;
    const bool has = s_view.has_next, snoozed = s_view.next.snoozed;
    const uint32_t wait = s_view.next.wait_min;
    const char *src = s_view.source;
    alarm_port_unlock();
    char next[16] = "-";
    if (has) snprintf(next, sizeof next, "%s%lu", snoozed ? "z" : "+", (unsigned long)wait);
    snprintf(out, len, "alarm=n:%u,next:%s,fired:%lu,snz:%lu,stop:%lu,cap:%lu,miss:%lu,q:%u,src:%s,"
                       "fetch:%s/%d",
             (unsigned)n, next, (unsigned long)fired, (unsigned long)snz, (unsigned long)stop,
             (unsigned long)cap, (unsigned long)miss, (unsigned)q, src, fetch_word(), s_last_status);
}

static void when(dbg_json_t *w, const tz_rule_t *z, const char *key, int64_t utc)
{
    if (!utc) {
        dbg_json_str(w, key, "never");
        return;
    }
    civil_t c;
    tz_local(z, utc, &c);
    char s[24];
    snprintf(s, sizeof s, "%04u-%02u-%02u %02u:%02u", c.year, c.mon, c.mday, c.h, c.m);
    dbg_json_str(w, key, s);
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    /* Whichever task runs the relay: the published view, never the live schedule. */
    alarm_port_lock();
    s_dbg = s_view;
    alarm_port_unlock();
    const alarm_sched_t *v = &s_dbg.s;
    const alarm_next_t n = s_dbg.next;
    dbg_json_int(w, "count", v->list.n);
    dbg_json_str(w, "source", s_dbg.source);
    if (s_dbg.has_next) {
        dbg_json_obj_begin(w, "next");
        dbg_json_int(w, "id", (int32_t)n.a.id);
        char hm[8];
        snprintf(hm, sizeof hm, "%02u:%02u", n.at_h % 24u, n.at_m % 60u);
        dbg_json_str(w, "at", hm);
        dbg_json_int(w, "in_min", (int32_t)n.wait_min);
        dbg_json_bool(w, "snoozed", n.snoozed);
        dbg_json_obj_end(w);
    } else {
        dbg_json_str(w, "next", "none");
    }
    dbg_json_int(w, "ringing", v->ringing ? (int32_t)v->ring.id : -1);
    if (v->ringing) {
        dbg_json_int(w, "ring_ms", (int32_t)(svc_rtc_uptime_ms() - v->ring_ms));
        dbg_json_str(w, "ring_sound", alarm_sound_name(v->ring.sound));
    }
    dbg_json_int(w, "ring_cap_min", ALARM_RING_CAP_MIN);
    when(w, &s_dbg.tz, "last_fired", v->last_fired);
    dbg_json_int(w, "last_fired_id", (int32_t)v->last_fired_id);
    when(w, &s_dbg.tz, "last_snoozed", v->last_snoozed);
    when(w, &s_dbg.tz, "last_dismissed", v->last_dismissed);
    dbg_json_str(w, "last_end", ENDS[v->last_end]);
    dbg_json_int(w, "fired", v->fired);
    dbg_json_int(w, "snoozes", v->snoozes);
    dbg_json_int(w, "dismissed", v->dismissed);
    dbg_json_int(w, "timed_out", v->timed_out);
    dbg_json_int(w, "missed", v->missed);
    dbg_json_int(w, "snooze_cancelled", v->snooze_cancelled);
    dbg_json_int(w, "taken_over", v->taken_over);
    dbg_json_obj_begin(w, "fetch");
    dbg_json_str(w, "last", fetch_word());
    dbg_json_int(w, "status", s_last_status);
    dbg_json_int(w, "ok", s_fetched);
    dbg_json_int(w, "unchanged", s_unchanged);
    dbg_json_int(w, "failed", s_failed);
    dbg_json_int(w, "unreadable", s_bad);
    dbg_json_int(w, "age_s", s_fetch_ms ? (int32_t)((svc_rtc_uptime_ms() - s_fetch_ms) / 1000) : -1);
    char etag[ALARM_ETAG_BYTES];
    svc_alarms_etag(etag, sizeof etag);
    dbg_json_str(w, "etag", etag);
    dbg_json_int(w, "puts", s_puts);
    dbg_json_int(w, "put_fails", s_put_fails);
    dbg_json_obj_end(w);
    dbg_json_arr_begin(w, "queue");
    alarm_port_lock();
    const alarm_queue_t q = s_q;
    alarm_port_unlock();
    for (int i = 0; i < ALARM_MAX; i++) {
        if (!q.q[i].used) continue;
        dbg_json_obj_begin(w, NULL);
        dbg_json_int(w, "id", (int32_t)q.q[i].id);
        dbg_json_bool(w, "on", q.q[i].on);
        dbg_json_obj_end(w);
    }
    dbg_json_arr_end(w);
    dbg_json_arr_begin(w, "alarms");
    for (uint8_t i = 0; i < v->list.n; i++) {
        const alarm_t *a = &v->list.a[i];
        dbg_json_obj_begin(w, NULL);
        dbg_json_int(w, "id", (int32_t)a->id);
        dbg_json_int(w, "at", a->h * 60 + a->m);
        dbg_json_int(w, "days", a->days);
        dbg_json_bool(w, "on", a->on);
        dbg_json_str(w, "sound", alarm_sound_name(a->sound));
        dbg_json_str(w, "label", a->label);
        dbg_json_obj_end(w);
    }
    dbg_json_arr_end(w);
}

void svc_alarms_set_wake(void (*wake)(void))
{
    s_wake = wake;
}

void svc_alarms_init(void)
{
    alarm_sched_init(&s_s);
    s_source = "none";
    s_dirty = false;
    s_applied = ld(&s_seq);
    alarm_port_lock();
    memset(&s_q, 0, sizeof s_q);
    s_etag[0] = '\0';
    alarm_port_unlock();
    alarm_cache_t c;
    if (alarm_port_load(&c)) {
        s_source = "cache";
        alarm_port_lock();
        s_q = c.queue;
        snprintf(s_etag, sizeof s_etag, "%s", c.list.etag);
        alarm_port_unlock();
        alarm_sched_set_list(&s_s, &c.list);
    }
    /* Before the UI task runs: what was loaded, with no "next" until its first pass works it out
     * (the zone is the UI task's to parse). */
    alarm_port_lock();
    memset(&s_view, 0, sizeof s_view);
    s_view.s = s_s;
    s_view.source = s_source;
    alarm_port_unlock();
    s_view_stale = true;
    svc_debug_register("alarms", describe, NULL);
}

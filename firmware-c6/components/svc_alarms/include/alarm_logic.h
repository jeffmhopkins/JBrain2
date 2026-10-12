/* The alarms' rules, apart from storing, fetching or sounding them. Pure C, host-tested: the box's
 * JSON, when an alarm next rings (in UTC, through the zone's daylight saving), the ring / snooze /
 * stop / cap machine, and the queue that carries the panel's switches to the box. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_alarms.h"
#include "time_logic.h"

/* ---- the box's list ---- */

#define ALARM_ETAG_BYTES 24

/* The longest answer the box can give, from its own shape (panel_alarms.py, compact JSON): every
 * number at its widest, every label full. A label never holds `"` or `\` (the box refuses them),
 * so nothing in it is escaped — which is what keeps this bound true. The fetch buffer is sized
 * from it, and the host suite builds that worst case and checks it fits and parses. */
#define ALARM_JSON_ENTRY_MAX                                                                       \
    (sizeof("{\"at\":1439,\"days\":127,\"on\":false,\"label\":\"\",\"sound\":\"chime\","            \
            "\"id\":9223372036854775807},") - 1 + ALARM_LABEL_BYTES)
#define ALARM_JSON_MAX                                                                             \
    (sizeof("{\"etag\":\"\",\"alarms\":[],\"max\":8,\"panel_etag\":\"\",\"panel_seen_s\":4294967295}") \
     - 1 + 2 * (ALARM_ETAG_BYTES - 1) + ALARM_MAX * ALARM_JSON_ENTRY_MAX)

typedef struct {
    alarm_t a[ALARM_MAX];
    uint8_t n;
    char etag[ALARM_ETAG_BYTES];
} alarm_list_t;

/* `{"etag":"…","alarms":[{"id":1,"at":405,"days":31,"on":true,"label":"Work","sound":"chime"}]}`
 * Unknown keys are skipped; an entry without an id, or with `at` or `days` out of range, is
 * dropped; at most ALARM_MAX are kept, sorted by time of day. False for anything else — the
 * panel's copy then stays as it is. */
bool alarm_parse(const char *json, size_t len, alarm_list_t *out);
/* Printable ASCII kept, every other code point a '?', at most ALARM_LABEL_BYTES. */
void alarm_clean_label(const char *in, char *out, size_t cap);
uint8_t alarm_sound_from(const char *name); /* unknown: chime */
const char *alarm_sound_name(uint8_t sound);

/* ---- when ---- */

/* The first instant strictly after `after` (UTC seconds) at which `a` rings, or -1 when it is off.
 * Its repeat days are the panel's local days; a one-off (days 0) rings at its next occurrence.
 * A time the clocks skip rings when the gap ends; a time they repeat rings the first time. */
int64_t alarm_next_at(const alarm_t *a, const tz_rule_t *z, int64_t after);

/* ---- the schedule ---- */

/* A due time this far behind the clock still rings: a reboot (an update) at the alarm's minute, or
 * the box stepping the clock forward. Further behind is counted as missed, never rung late. */
#define ALARM_GRACE_S 120
/* The box correcting the panel's clock back by a few seconds just after an alarm rang must not ring
 * it twice; a step back further than this means the clock was wrong, and the schedule starts again
 * from the corrected time rather than staying silent until the wrong one comes round — a missed
 * alarm is worse than a repeated one. */
#define ALARM_STEP_BACK_MAX_S 600
#define ALARM_RING_CAP_MS ((uint32_t)ALARM_RING_CAP_MIN * 60u * 1000u)

typedef enum { ALARM_END_NONE, ALARM_END_STOPPED, ALARM_END_SNOOZED, ALARM_END_TIMED_OUT } alarm_end_t;

typedef struct {
    alarm_list_t list;
    bool checked;
    int64_t last; /* the instant the last check covered up to */
    int64_t high; /* the furthest it has ever covered: a clock stepped back re-rings nothing */
    bool ringing;
    alarm_t ring; /* a copy: an edit from the box does not change what is ringing */
    uint32_t ring_ms; /* uptime the ring started, for the cap */
    bool snoozed;
    alarm_t snooze_of;
    int64_t snooze_until;
    /* For the snapshot: the latest of each, as UTC seconds (0: never), and the counts. */
    int64_t last_fired, last_snoozed, last_dismissed;
    uint32_t last_fired_id;
    alarm_end_t last_end;
    uint32_t fired, snoozes, dismissed, timed_out, missed, snooze_cancelled, taken_over;
} alarm_sched_t;

void alarm_sched_init(alarm_sched_t *s);
/* A list from the box or the cache. A snooze ends when its alarm is deleted, switched off, or
 * moved to another time or other days (a new label or sound is carried into it). True when one
 * ended. */
bool alarm_sched_set_list(alarm_sched_t *s, const alarm_list_t *l);
/* The switch on the glass, locally. False when there is no such alarm. Switching off the
 * snoozed alarm ends its snooze. */
bool alarm_sched_set_on(alarm_sched_t *s, uint32_t id, bool on);
/* Once per pass with the time: true when a ring starts (or another alarm takes over the ring). */
bool alarm_sched_tick(alarm_sched_t *s, const tz_rule_t *z, int64_t now, uint32_t now_ms);
/* While ringing: true once the cap ends it. A one-off that timed out switches itself off as a
 * stop does: `*off_id` names it (0: none). */
bool alarm_sched_cap(alarm_sched_t *s, int64_t now, uint32_t now_ms, uint32_t *off_id);
/* Ends the ring and rings it again ALARM_SNOOZE_MIN from `now`. Returns that instant, or -1 when
 * nothing was ringing. */
int64_t alarm_sched_snooze(alarm_sched_t *s, int64_t now);
/* Ends the ring (and its snooze). A one-off switches itself off: `*off_id` names it. */
void alarm_sched_stop(alarm_sched_t *s, int64_t now, uint32_t *off_id);
/* The soonest of every alarm's next occurrence and the snooze. */
bool alarm_sched_next(const alarm_sched_t *s, const tz_rule_t *z, int64_t now, alarm_t *a,
                      int64_t *at, bool *snooze);
/* Rings now, outside the list (id 0, "Test alarm"). */
void alarm_sched_test(alarm_sched_t *s, uint8_t sound, const tz_rule_t *z, int64_t now,
                      uint32_t now_ms);

/* ---- the switches on their way to the box: one per alarm, the last write wins ---- */

typedef struct {
    uint32_t id;
    bool on, used;
    uint32_t seq;
} alarm_put_t;

typedef struct {
    alarm_put_t q[ALARM_MAX];
    uint32_t seq;
} alarm_queue_t;

void alarm_queue_put(alarm_queue_t *q, uint32_t id, bool on);
/* The oldest waiting write. */
bool alarm_queue_peek(const alarm_queue_t *q, alarm_put_t *out);
/* The box answered `status` to `sent`. A 2xx or a 404 (the alarm is gone) settles it — unless a
 * newer switch of the same alarm replaced it meanwhile, which then still goes. Anything else
 * leaves it for the next pass. */
void alarm_queue_done(alarm_queue_t *q, const alarm_put_t *sent, int status);
uint8_t alarm_queue_count(const alarm_queue_t *q);
/* The box's list has not heard the waiting switches yet: they win over what it says. */
void alarm_queue_overlay(const alarm_queue_t *q, alarm_list_t *l);

/* The accept decision of plan §2, as one pure function. The BOX makes this decision at send
 * time from the panel's last report — the panel never sees a refused notice — so this is the
 * reference both sides are tested against: the firmware's report must carry exactly what it
 * needs, and C3b's Python mirrors it case for case. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define NOTICE_OFFLINE_S 30

/* What the panel reports on every settings poll. The window is the night clock's, in the
 * panel's own timezone. */
typedef struct {
    bool accept;
    bool quiet;
    uint16_t from, to;
} notice_report_t;

typedef enum {
    NOTICE_ACCEPT,             /* 202 {id, state: "queued"} */
    NOTICE_REFUSE_NOT_ACCEPTING, /* 409 {reason: "not_accepting"} */
    NOTICE_REFUSE_QUIET_HOURS, /* 409 {reason: "quiet_hours", until} */
    NOTICE_REFUSE_OFFLINE,     /* 503 {reason: "offline", last_seen_s} */
} notice_verdict_t;

/* `until` is set for a quiet-hours refusal: the minute the window ends. */
notice_verdict_t notice_policy(const notice_report_t *r, uint16_t now_min, uint32_t last_seen_s,
                               uint16_t *until);
int notice_http_status(notice_verdict_t v);
const char *notice_reason(notice_verdict_t v);
/* Chimes unless it is night hours; urgent always chimes, even with the chime switched off. */
bool notice_chime(bool chime_on, bool urgent, bool night_now);

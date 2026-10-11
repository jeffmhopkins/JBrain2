#include "notice_policy.h"

#include "minutes.h"

notice_verdict_t notice_policy(const notice_report_t *r, uint16_t now_min, uint32_t last_seen_s,
                               uint16_t *until)
{
    /* Offline first: a stale report says nothing about the switches now, and the panel could
     * not show it anyway. */
    if (last_seen_s >= NOTICE_OFFLINE_S) return NOTICE_REFUSE_OFFLINE;
    if (!r->accept) return NOTICE_REFUSE_NOT_ACCEPTING;
    if (r->quiet && min_window_contains(now_min, r->from, r->to)) {
        if (until) *until = r->to;
        return NOTICE_REFUSE_QUIET_HOURS;
    }
    return NOTICE_ACCEPT;
}

int notice_http_status(notice_verdict_t v)
{
    switch (v) {
    case NOTICE_ACCEPT: return 202;
    case NOTICE_REFUSE_OFFLINE: return 503;
    default: return 409;
    }
}

const char *notice_reason(notice_verdict_t v)
{
    switch (v) {
    case NOTICE_REFUSE_NOT_ACCEPTING: return "not_accepting";
    case NOTICE_REFUSE_QUIET_HOURS: return "quiet_hours";
    case NOTICE_REFUSE_OFFLINE: return "offline";
    default: return "queued";
    }
}

bool notice_chime(bool chime_on, bool urgent, bool night_now)
{
    if (urgent) return true;
    return chime_on && !night_now;
}

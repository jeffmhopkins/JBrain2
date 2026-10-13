#include <stdio.h>
#include <string.h>

#include "dbg_core.h"

/* Signed distance a - b: right across the uint32 millisecond wrap as long as the two are within
 * ~24 days of each other, which a 60 s schedule always is. */
static int32_t ahead(uint32_t a, uint32_t b)
{
    return (int32_t)(a - b);
}

void dbg_sched_init(dbg_sched_t *s, uint32_t now_ms)
{
    s->due_ms = now_ms;
    s->fails = 0;
}

void dbg_sched_done(dbg_sched_t *s, uint32_t now_ms, dbg_poll_t outcome)
{
    uint32_t gap = DBG_POLL_MS;
    if (outcome == DBG_POLL_FAIL) {
        if (s->fails < 8) s->fails++;
        gap = (uint32_t)DBG_POLL_MS << s->fails;
        if (gap > DBG_POLL_MAX_MS) gap = DBG_POLL_MAX_MS;
    } else {
        s->fails = 0;
        if (outcome == DBG_POLL_CMD) gap = DBG_POLL_AFTER_CMD_MS;
    }
    s->due_ms = now_ms + gap;
}

dbg_next_t dbg_sched_next(const dbg_sched_t *s, uint32_t now_ms, uint32_t check_due_ms, bool online,
                          uint32_t *sleep_ms)
{
    const int32_t to_check = ahead(check_due_ms, now_ms);
    if (to_check <= 0) return DBG_NEXT_CHECK;
    int32_t wait = to_check;
    if (online) {
        const int32_t to_poll = ahead(s->due_ms, now_ms);
        if (to_poll <= 0) return DBG_NEXT_POLL;
        if (to_poll < wait) wait = to_poll;
    }
    *sleep_ms = (uint32_t)wait;
    return DBG_NEXT_SLEEP;
}

void dbg_stats_note(const dbg_stats_t *s, char *out, size_t len)
{
    snprintf(out, len, "dbg=ok:%lu,err:%lu,cmds:%lu,last:%s/%d,shot:%lu/%lu",
             (unsigned long)s->polls_ok, (unsigned long)s->polls_err, (unsigned long)s->cmds,
             s->last_cmd[0] ? s->last_cmd : "-", s->last_status, (unsigned long)s->shot_bytes,
             (unsigned long)s->shot_ms);
}

const char *dbg_answer_phase(const char *cmd)
{
    return cmd && !strcmp(cmd, "screenshot") ? "screenshot" : "answer";
}

bool dbg_req_id_ok(const char *id)
{
    size_t n = 0;
    for (; id[n]; n++) {
        const char c = id[n];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F') || c == '-'))
            return false;
    }
    return n >= 8 && n < sizeof(((dbg_req_t *)0)->id);
}

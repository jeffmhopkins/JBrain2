#include "convo_model.h"

#include <stddef.h>
#include <stdio.h>

bool convo_shows_listen(const msg_t *m, bool tts_on, uint32_t speaking_id)
{
    return !m->mine && !m->voice && !m->pending && !tts_on && m->id != speaking_id;
}

convo_redraw_t convo_redraw(const convo_sig_t *drawn, const convo_sig_t *now)
{
    if (drawn->rev != now->rev || drawn->speaking != now->speaking || drawn->tts != now->tts ||
        drawn->can_speak != now->can_speak || drawn->working != now->working ||
        drawn->meta != now->meta || drawn->fresh != now->fresh)
        return CONVO_REBUILD;
    return drawn->work_seq != now->work_seq ? CONVO_WORK : CONVO_SAME;
}

const char *convo_talk_toast(talk_outcome_t o)
{
    switch (o) {
    case TALK_CANCELLED: return "Cancelled — nothing sent";
    case TALK_TOO_SHORT: return "Hold KEY the whole time you talk";
    default: return NULL;
    }
}

convo_new_t convo_new_decide(bool fresh_pending, uint16_t shown)
{
    return fresh_pending && shown == 0 ? CONVO_NEW_ALREADY : CONVO_NEW_START;
}

const char *convo_subtitle(bool fresh, const char *title)
{
    if (fresh || (title && !title[0])) return "New conversation";
    return title ? title : "Desk panel";
}

bool convo_row_is_current(bool box_current, bool fresh)
{
    return box_current && !fresh;
}

/* Days since 1970-01-01 as a civil date (H. Hinnant's algorithm), and its weekday (0 Monday). */
static void civil(int64_t days, int *mday, int *mon, int *wday)
{
    *wday = (int)(((days % 7) + 7 + 3) % 7);
    days += 719468;
    const int64_t era = (days >= 0 ? days : days - 146096) / 146097;
    const int64_t doe = days - era * 146097;
    const int64_t yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    const int64_t doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    const int64_t mp = (5 * doy + 2) / 153;
    *mday = (int)(doy - (153 * mp + 2) / 5 + 1);
    *mon = (int)(mp < 10 ? mp + 3 : mp - 9);
}

static int64_t floor_div(int64_t a, int64_t b)
{
    return a >= 0 ? a / b : -((-a + b - 1) / b);
}

void convo_when(int64_t at, int64_t now, int32_t offset_s, char *out, size_t cap)
{
    static const char *const DAY[] = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"};
    static const char *const MON[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    const int64_t ago = now > at ? now - at : 0;
    if (ago < 60) {
        snprintf(out, cap, "Just now");
        return;
    }
    if (ago < 3600) {
        snprintf(out, cap, "%d min ago", (int)(ago / 60));
        return;
    }
    const int64_t la = at + offset_s, ln = now + offset_s;
    const int64_t day_a = floor_div(la, 86400), day_n = floor_div(ln, 86400);
    const int secs = (int)(la - day_a * 86400);
    const int h = secs / 3600, m = secs / 60 % 60;
    int mday, mon, wday;
    civil(day_a, &mday, &mon, &wday);
    const int64_t back = day_n - day_a;
    if (back <= 0) snprintf(out, cap, "Today %02d:%02d", h, m);
    else if (back == 1) snprintf(out, cap, "Yesterday %02d:%02d", h, m);
    else if (back < 7) snprintf(out, cap, "%s %02d:%02d", DAY[wday], h, m);
    else snprintf(out, cap, "%d %s", mday, MON[mon - 1]);
}

static bool open_mode(rc_mode_t m)
{
    return m != RC_CLOSED;
}

rc_do_t rc_step(rc_t *r, rc_in_t in, int arg, bool current)
{
    switch (in) {
    case RC_IN_OPEN:
        if (r->mode != RC_CLOSED && r->mode != RC_FAILED) return RC_DO_NOTHING;
        r->mode = RC_LOADING;
        return RC_DO_FETCH;
    case RC_IN_LISTED:
        if (r->mode != RC_LOADING) return RC_DO_NOTHING;
        r->mode = arg > 0 ? RC_LIST : RC_EMPTY;
        return RC_DO_DRAW;
    case RC_IN_FAILED:
        if (r->mode != RC_LOADING) return RC_DO_NOTHING;
        r->mode = RC_FAILED;
        return RC_DO_DRAW;
    case RC_IN_TAP:
        if (r->mode != RC_LIST || arg < 0) return RC_DO_NOTHING;
        if (current) {
            r->mode = RC_CLOSED;
            return RC_DO_CLOSE;
        }
        r->mode = RC_OPENING;
        r->opening = (int8_t)arg;
        return RC_DO_SWITCH;
    case RC_IN_SWITCHED:
        if (r->mode != RC_OPENING) return RC_DO_NOTHING;
        r->mode = RC_CLOSED;
        return RC_DO_CLOSE;
    case RC_IN_SWITCH_FAILED:
        if (r->mode != RC_OPENING) return RC_DO_NOTHING;
        r->mode = RC_LIST;
        return RC_DO_NOT_OPENED;
    case RC_IN_BACK:
        if (!open_mode(r->mode)) return RC_DO_NOTHING;
        r->mode = RC_CLOSED;
        return RC_DO_CLOSE;
    case RC_IN_KEY:
        if (!open_mode(r->mode)) return RC_DO_NOTHING;
        if (r->mode == RC_OPENING) return RC_DO_WAIT;
        r->mode = RC_CLOSED;
        return RC_DO_CLOSE_TALK;
    case RC_IN_NEW:
        if (!open_mode(r->mode)) return RC_DO_NOTHING;
        if (r->mode == RC_OPENING) return RC_DO_WAIT;
        r->mode = RC_CLOSED;
        return RC_DO_CLOSE_NEW;
    }
    return RC_DO_NOTHING;
}

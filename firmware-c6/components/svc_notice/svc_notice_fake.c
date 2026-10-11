#include <string.h>

#include "app_event.h"
#include "svc_debug.h"
#include "svc_notice_fake.h"
#include "svc_settings.h"

static notice_t s_q[NOTICE_QUEUE];
static uint8_t s_n;
static unsigned s_answered, s_dismissed;

uint8_t svc_notice_count(void)
{
    return s_n;
}

bool svc_notice_head(notice_t *out)
{
    if (!s_n) return false;
    *out = s_q[0];
    return true;
}

static void pop(void)
{
    if (!s_n) return;
    memmove(s_q, s_q + 1, sizeof s_q[0] * (size_t)(s_n - 1));
    s_n--;
}

void svc_notice_answer(uint8_t choice)
{
    if (!s_n) return;
    svc_debug_log("notice", "%lu answered %u", (unsigned long)s_q[0].id, choice);
    s_answered++;
    pop();
}

void svc_notice_dismiss(void)
{
    if (!s_n) return;
    svc_debug_log("notice", "%lu dismissed", (unsigned long)s_q[0].id);
    s_dismissed++;
    pop();
}

bool svc_notice_fake_push(const notice_t *n)
{
    if (s_n == NOTICE_QUEUE) return false;
    s_q[s_n++] = *n;
    app_event_t ev = {.type = APP_EV_NOTICE_ARRIVED, .u.notice_id = n->id};
    app_events_post(&ev);
    return true;
}

void svc_notice_report(notice_report_t *out)
{
    out->accept = svc_settings_get(SET_NOTIF_ACCEPT) != 0;
    out->quiet = svc_settings_get(SET_NOTIF_QUIET) != 0;
    out->from = (uint16_t)svc_settings_get(SET_NIGHT_FROM);
    out->to = (uint16_t)svc_settings_get(SET_NIGHT_TO);
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    notice_report_t r;
    svc_notice_report(&r);
    dbg_json_int(w, "queued", s_n);
    dbg_json_int(w, "head_id", s_n ? s_q[0].id : 0);
    dbg_json_int(w, "answered", s_answered);
    dbg_json_int(w, "dismissed", s_dismissed);
    dbg_json_obj_begin(w, "report");
    dbg_json_bool(w, "accept", r.accept);
    dbg_json_bool(w, "quiet", r.quiet);
    dbg_json_int(w, "from", r.from);
    dbg_json_int(w, "to", r.to);
    dbg_json_obj_end(w);
}

void svc_notice_init(void)
{
    svc_debug_register("notice", describe, NULL);
}

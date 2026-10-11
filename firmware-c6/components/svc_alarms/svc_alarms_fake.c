#include "app_event.h"
#include "minutes.h"
#include "svc_alarms_fake.h"
#include "svc_debug.h"
#include "svc_rtc.h"

#define WEEKDAYS 0x1F
#define WEEKEND 0x60
#define EVERY_DAY 0x7F

static alarm_t s_a[ALARM_MAX] = {
    {1, 6, 45, WEEKDAYS, true, "Work"},
    {2, 8, 30, WEEKEND, false, ""},
    {3, 21, 0, EVERY_DAY, true, "Girls to bed"},
};
static uint8_t s_n = 3;
static int s_ringing = -1;

uint8_t svc_alarms_count(void)
{
    return s_n;
}

bool svc_alarms_get(uint8_t idx, alarm_t *out)
{
    if (idx >= s_n) return false;
    *out = s_a[idx];
    return true;
}

void svc_alarms_set_on(uint8_t id, bool on)
{
    for (uint8_t i = 0; i < s_n; i++)
        if (s_a[i].id == id) s_a[i].on = on;
}

bool svc_alarms_ringing(alarm_t *out)
{
    if (s_ringing < 0) return false;
    if (out) *out = s_a[s_ringing];
    return true;
}

bool svc_alarms_fake_ring(uint8_t id)
{
    for (uint8_t i = 0; i < s_n; i++)
        if (s_a[i].id == id) {
            s_ringing = i;
            app_event_t ev = {.type = APP_EV_ALARM_DUE, .u.alarm_id = id};
            app_events_post(&ev);
            return true;
        }
    return false;
}

static void end_ring(void)
{
    /* A one-off switches itself off once it has rung (mock README "Clock"). */
    if (s_ringing >= 0 && s_a[s_ringing].days == 0) s_a[s_ringing].on = false;
    s_ringing = -1;
}

uint16_t svc_alarms_snooze(void)
{
    rtc_time_t t;
    svc_rtc_now(&t);
    s_ringing = -1;
    return min_add((uint16_t)(t.h * 60 + t.m), ALARM_SNOOZE_MIN);
}

void svc_alarms_stop(void)
{
    end_ring();
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_int(w, "ringing", s_ringing >= 0 ? s_a[s_ringing].id : 0);
    dbg_json_arr_begin(w, "alarms");
    for (uint8_t i = 0; i < s_n; i++) {
        dbg_json_obj_begin(w, NULL);
        dbg_json_int(w, "id", s_a[i].id);
        dbg_json_int(w, "at", s_a[i].h * 60 + s_a[i].m);
        dbg_json_int(w, "days", s_a[i].days);
        dbg_json_bool(w, "on", s_a[i].on);
        dbg_json_obj_end(w);
    }
    dbg_json_arr_end(w);
}

void svc_alarms_init(void)
{
    svc_debug_register("alarms", describe, NULL);
}

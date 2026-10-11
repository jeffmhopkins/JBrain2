#include "svc_rtc_fake.h"

#include "app_event.h"
#include "svc_debug.h"
#include "svc_rtc.h"

static uint32_t s_day_ms = (9 * 60 + 41) * 60000u;
static uint8_t s_wday = 2;
static uint32_t s_uptime;

void svc_rtc_now(rtc_time_t *out)
{
    uint32_t s = s_day_ms / 1000;
    out->h = (uint8_t)(s / 3600);
    out->m = (uint8_t)(s / 60 % 60);
    out->s = (uint8_t)(s % 60);
    out->wday = s_wday;
}

uint32_t svc_rtc_uptime_ms(void)
{
    return s_uptime;
}

void svc_rtc_fake_set(uint8_t h, uint8_t m, uint8_t wday)
{
    s_day_ms = (h * 60u + m) * 60000u;
    s_wday = wday % 7;
}

void svc_rtc_fake_advance_ms(uint32_t ms)
{
    uint32_t before = s_day_ms / 60000;
    s_uptime += ms;
    s_day_ms += ms;
    if (s_day_ms >= 86400000u) {
        s_day_ms -= 86400000u;
        s_wday = (s_wday + 1) % 7;
    }
    if (s_day_ms / 60000 != before) app_events_post_type(APP_EV_TICK_MINUTE);
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    rtc_time_t t;
    svc_rtc_now(&t);
    char hm[12];
    hm[0] = (char)('0' + t.h / 10), hm[1] = (char)('0' + t.h % 10), hm[2] = ':';
    hm[3] = (char)('0' + t.m / 10), hm[4] = (char)('0' + t.m % 10), hm[5] = '\0';
    dbg_json_str(w, "time", hm);
    dbg_json_int(w, "wday", t.wday);
    dbg_json_int(w, "uptime_ms", s_uptime);
    dbg_json_str(w, "source", "fake");
}

void svc_rtc_init(void)
{
    svc_debug_register("rtc", describe, NULL);
}

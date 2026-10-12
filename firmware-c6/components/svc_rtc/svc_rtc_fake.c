#include "svc_rtc_fake.h"

#include "app_event.h"
#include "svc_debug.h"
#include "svc_rtc.h"
#include "svc_settings.h"
#include "time_logic.h"

/* Monday 12 October 2026, 09:41: a weekday, so the sample "Work" alarm is next. */
static int64_t s_day = 20738;
static uint32_t s_day_ms = (9 * 60 + 41) * 60000u;
static uint32_t s_uptime;

void svc_rtc_now(rtc_time_t *out)
{
    civil_t c;
    epoch_to_civil(s_day * 86400 + s_day_ms / 1000, &c);
    *out = (rtc_time_t){.valid = true, .h = c.h, .m = c.m, .s = c.s, .wday = c.wday,
                        .mday = c.mday, .mon = c.mon, .year = c.year};
}

uint32_t svc_rtc_uptime_ms(void)
{
    return s_uptime;
}

/* The fake keeps the WALL clock (what the face shows); the instant is that wall time in the zone
 * the panel is set to, so the alarms and the face agree as they do on the device. */
bool svc_rtc_epoch(int64_t *out)
{
    tz_rule_t z;
    const int64_t wall = s_day * 86400 + s_day_ms / 1000;
    *out = tz_parse(tz_posix(svc_settings_get(SET_TZ)), &z) ? tz_wall_to_utc(&z, wall) : wall;
    return true;
}

void svc_rtc_status(rtc_status_t *out)
{
    *out = (rtc_status_t){.source = TIME_SRC_BOX, .synced_s = (int32_t)(s_uptime / 1000), .rtc_ok = true};
}

void svc_rtc_fake_set(uint8_t h, uint8_t m)
{
    s_day_ms = (h * 60u + m) * 60000u;
}

void svc_rtc_fake_set_date(uint16_t year, uint8_t mon, uint8_t mday)
{
    civil_t c = {.year = year, .mon = mon, .mday = mday};
    s_day = civil_to_epoch(&c) / 86400;
}

void svc_rtc_fake_advance_ms(uint32_t ms)
{
    uint32_t before = s_day_ms / 60000;
    s_uptime += ms;
    s_day_ms += ms;
    if (s_day_ms >= 86400000u) {
        s_day_ms -= 86400000u;
        s_day++;
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

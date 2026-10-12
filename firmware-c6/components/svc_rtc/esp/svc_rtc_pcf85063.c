/* The PCF85063A real-time clock (0x51, HARDWARE.md §6), powered from the AXP2101's backed
 * RTCLDO so it keeps time while the panel is off.
 *
 * The chip holds UTC. The system clock (gettimeofday) is the panel's working time: set from the
 * chip at boot, stepped from the box's Date header, and turned into local time by newlib with
 * the POSIX rule of the zone chosen in settings. Only the control bits that would stop the
 * clock or switch it to 12-hour mode are ever written; the oscillator's load capacitance and
 * the offset trim are left as the board ships them. */

#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "app_event.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "svc_debug.h"
#include "svc_power_drv.h"
#include "svc_rtc.h"
#include "svc_rtc_drv.h"
#include "svc_settings.h"
#include "time_logic.h"

static const char *TAG = "rtc";

#define PCF_ADDR 0x51
#define CTL1_STOP 0x20
#define CTL1_12H 0x02

static svc_power_i2c_t s_dev;
static char s_state[20] = "off";
static volatile bool s_rtc_ok;
static volatile time_src_t s_src = TIME_SRC_NONE;
static volatile int64_t s_box_us = -1;  /* esp_timer when the box last set the time */
static int64_t s_rtc_write_us = -1;     /* and when the chip was last written */
static uint8_t s_ctl1;
static uint32_t s_writes, s_steps, s_errs;
static int32_t s_last_step_s;
static volatile int32_t s_tz = -1;
static uint16_t s_shown_min = 0xFFFF;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void apply_tz(int32_t idx)
{
    setenv("TZ", tz_posix(idx), 1);
    tzset();
    s_tz = idx;
}

static bool have_time(time_t *now)
{
    *now = time(NULL);
    return *now >= TIME_EPOCH_MIN;
}

void svc_rtc_now(rtc_time_t *out)
{
    time_t now;
    if (!have_time(&now)) {
        *out = (rtc_time_t){0};
        return;
    }
    struct tm tm;
    localtime_r(&now, &tm);
    *out = (rtc_time_t){.valid = true, .h = (uint8_t)tm.tm_hour, .m = (uint8_t)tm.tm_min,
                        .s = (uint8_t)tm.tm_sec, .wday = (uint8_t)((tm.tm_wday + 6) % 7),
                        .mday = (uint8_t)tm.tm_mday, .mon = (uint8_t)(tm.tm_mon + 1),
                        .year = (uint16_t)(tm.tm_year + 1900)};
}

uint32_t svc_rtc_uptime_ms(void)
{
    return now_ms();
}

bool svc_rtc_epoch(int64_t *out)
{
    time_t now;
    if (!have_time(&now)) return false;
    *out = (int64_t)now;
    return true;
}

void svc_rtc_status(rtc_status_t *out)
{
    const int64_t box = s_box_us;
    *out = (rtc_status_t){
        .source = s_src,
        .synced_s = box < 0 ? -1 : (int32_t)((esp_timer_get_time() - box) / 1000000),
        .rtc_ok = s_rtc_ok,
    };
}

static bool write_chip(int64_t epoch)
{
    if (!s_dev) return false;
    if (s_ctl1 & (CTL1_STOP | CTL1_12H)) {
        const uint8_t fix[2] = {0x00, (uint8_t)(s_ctl1 & ~(CTL1_STOP | CTL1_12H))};
        if (svc_power_i2c_xfer(s_dev, fix, sizeof fix, NULL, 0, 20) != ESP_OK) {
            s_errs++;
            return false;
        }
        s_ctl1 = fix[1];
    }
    civil_t c;
    epoch_to_civil(epoch, &c);
    uint8_t tx[8] = {0x04};
    pcf_encode(&c, tx + 1);
    if (svc_power_i2c_xfer(s_dev, tx, sizeof tx, NULL, 0, 20) != ESP_OK) {
        s_errs++;
        return false;
    }
    s_writes++;
    s_rtc_write_us = esp_timer_get_time();
    s_rtc_ok = true;
    snprintf(s_state, sizeof s_state, "ok");
    return true;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    static const char *const SRC[] = {"none", "rtc", "box"};
    rtc_time_t t;
    svc_rtc_now(&t);
    char hm[24];
    snprintf(hm, sizeof hm, "%04u-%02u-%02u %02u:%02u", t.year, t.mon, t.mday, t.h, t.m);
    dbg_json_str(w, "time", t.valid ? hm : "");
    dbg_json_str(w, "source", SRC[s_src]);
    dbg_json_str(w, "chip", s_state);
    dbg_json_str(w, "tz", svc_rtc_tz_label(s_tz));
    dbg_json_int(w, "uptime_ms", now_ms());
    dbg_json_int(w, "steps", s_steps);
    dbg_json_int(w, "last_step_s", s_last_step_s);
    dbg_json_int(w, "chip_writes", s_writes);
}

void svc_rtc_start(void)
{
    apply_tz(svc_settings_get(SET_TZ));
    svc_debug_register("rtc", describe, NULL);
    esp_err_t e = svc_power_i2c_attach(PCF_ADDR, 100000, &s_dev);
    if (e != ESP_OK) {
        s_dev = NULL;
        snprintf(s_state, sizeof s_state, "absent:%x", (unsigned)e);
        return;
    }
    const uint8_t reg = 0x00;
    uint8_t r[11];
    e = svc_power_i2c_xfer(s_dev, &reg, 1, r, sizeof r, 20);
    if (e != ESP_OK) {
        s_errs++;
        snprintf(s_state, sizeof s_state, "absent:%x", (unsigned)e);
        return;
    }
    s_ctl1 = r[0];
    civil_t c;
    if ((s_ctl1 & (CTL1_STOP | CTL1_12H)) || !pcf_decode(r + 4, &c)) {
        snprintf(s_state, sizeof s_state, "unset");
        ESP_LOGW(TAG, "PCF85063 holds no good time (ctl1 %02x sec %02x) — waiting for the box",
                 s_ctl1, r[4]);
        return;
    }
    const struct timeval tv = {.tv_sec = (time_t)civil_to_epoch(&c)};
    settimeofday(&tv, NULL);
    s_rtc_ok = true;
    s_src = TIME_SRC_RTC;
    snprintf(s_state, sizeof s_state, "ok");
    ESP_LOGI(TAG, "time from the PCF85063: %04u-%02u-%02u %02u:%02u:%02u UTC", c.year, c.mon,
             c.mday, c.h, c.m, c.s);
}

void svc_rtc_box_time(int64_t epoch)
{
    time_t now;
    const bool had = have_time(&now);
    const bool stepped = time_should_step(had, (int64_t)now, epoch);
    if (stepped) {
        const struct timeval tv = {.tv_sec = (time_t)epoch};
        settimeofday(&tv, NULL);
        s_steps++;
        s_last_step_s = had ? (int32_t)(epoch - (int64_t)now) : 0;
        ESP_LOGI(TAG, "clock set from the box (%s %+ld s)", had ? "was off by" : "was unset",
                 (long)s_last_step_s);
    }
    if (epoch < TIME_EPOCH_MIN) return;
    s_box_us = esp_timer_get_time();
    s_src = TIME_SRC_BOX;
    const int64_t since = s_rtc_write_us < 0 ? -1 : (esp_timer_get_time() - s_rtc_write_us) / 1000000;
    if (time_should_write_rtc(s_rtc_ok, stepped, since)) write_chip(epoch);
}

void svc_rtc_poll(void)
{
    const int32_t tz = svc_settings_get(SET_TZ);
    if (tz != s_tz) apply_tz(tz);
    rtc_time_t t;
    svc_rtc_now(&t);
    const uint16_t shown = t.valid ? (uint16_t)(t.h * 60 + t.m) : 0xFFFE;
    if (shown != s_shown_min) {
        s_shown_min = shown;
        app_events_post_type(APP_EV_TICK_MINUTE);
    }
}

void svc_rtc_note(char *out, size_t len)
{
    static const char *const SRC[] = {"none", "rtc", "box"};
    rtc_status_t st;
    svc_rtc_status(&st);
    snprintf(out, len, "time=%s:%ld step=%lu/%ld rtc=%s rtc_w=%lu rtc_err=%lu tz=%ld",
             SRC[st.source], (long)st.synced_s, (unsigned long)s_steps, (long)s_last_step_s,
             s_state, (unsigned long)s_writes, (unsigned long)s_errs, (long)s_tz);
}

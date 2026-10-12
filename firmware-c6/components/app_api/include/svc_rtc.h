/* Wall time. The PCF85063 keeps it through Wi-Fi outages and power loss so the clock is right
 * at boot and alarms ring without the box; the box's clock corrects it whenever it is reached. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef struct {
    bool valid; /* false until the chip or the box has given a time: the face shows --:-- */
    uint8_t h, m, s;
    uint8_t wday; /* 0 = Monday … 6 = Sunday */
    uint8_t mday; /* 1-31 */
    uint8_t mon;  /* 1-12 */
    uint16_t year;
} rtc_time_t;

void svc_rtc_now(rtc_time_t *out);
uint32_t svc_rtc_uptime_ms(void);
/* The instant in UTC seconds, false while the panel has no time. The alarms schedule in UTC so a
 * zone change or a daylight-saving step moves the wall clock, never the instant an alarm names. */
bool svc_rtc_epoch(int64_t *out);

typedef enum { TIME_SRC_NONE, TIME_SRC_RTC, TIME_SRC_BOX } time_src_t;

typedef struct {
    time_src_t source;
    int32_t synced_s; /* seconds since the box last set the time; -1 never this boot */
    bool rtc_ok;      /* the chip answered and held a time */
} rtc_status_t;
void svc_rtc_status(rtc_status_t *out);

/* The zones the panel can be set to: a short table rather than the tz database, which does not
 * fit, each with its POSIX rule so daylight saving is computed on the panel. */
#define TZ_ZONES 12
#define TZ_DEFAULT 5 /* New York */
const char *svc_rtc_tz_label(int32_t idx);

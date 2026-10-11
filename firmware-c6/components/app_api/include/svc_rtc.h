/* Wall time. The PCF85063 keeps it through Wi-Fi outages so alarms ring without the box. */
#pragma once

#include <stdint.h>

typedef struct {
    uint8_t h, m, s;
    uint8_t wday; /* 0 = Monday … 6 = Sunday */
} rtc_time_t;

void svc_rtc_now(rtc_time_t *out);
uint32_t svc_rtc_uptime_ms(void);

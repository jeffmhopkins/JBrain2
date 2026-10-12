/* In-memory wall clock: the sim's and the host suite's. It always holds a time, advanced by the
 * sim's own clock. Only the sim, tests and main include this; apps cannot (check_app_deps). */
#pragma once

#include <stdint.h>

void svc_rtc_init(void);
void svc_rtc_fake_set(uint8_t h, uint8_t m);
void svc_rtc_fake_set_date(uint16_t year, uint8_t mon, uint8_t mday);
void svc_rtc_fake_advance_ms(uint32_t ms);

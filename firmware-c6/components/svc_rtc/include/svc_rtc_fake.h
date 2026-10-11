/* In-memory wall clock: the sim's, the host suite's, and the device's until the PCF85063
 * driver lands in C2. Only the sim, tests and main include this; apps cannot (check_app_deps). */
#pragma once

#include <stdint.h>

void svc_rtc_init(void);
void svc_rtc_fake_set(uint8_t h, uint8_t m, uint8_t wday);
void svc_rtc_fake_advance_ms(uint32_t ms);

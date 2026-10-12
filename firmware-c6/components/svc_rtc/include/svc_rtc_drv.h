/* The device's clock service, beside the app-facing svc_rtc.h. The PCF85063 at 0x51 is reached
 * only through svc_power's bus API, and only from the main task (start and box_time), so the
 * chip has one owner. The UI task reads the time from the system clock, never the chip. */
#pragma once

#include <stddef.h>
#include <stdint.h>

/* Main task, after svc_power_start: applies the zone from settings and sets the system clock
 * from the chip, so the face is right before Wi-Fi is. Any failure leaves the panel without a
 * time until the box gives one; it never stops the boot. */
void svc_rtc_start(void);

/* Main task: the box's clock, from the Date header of a reply. Steps the system clock if it is
 * off by TIME_STEP_S or more, and rewrites the chip when it was wrong, unset or not written for
 * a day. */
void svc_rtc_box_time(int64_t epoch);

/* UI task, every pass: applies a zone changed on the glass, and raises TICK_MINUTE when the
 * minute shown changes (a step from the box included). */
void svc_rtc_poll(void);

/* `time=box:12 rtc=ok tz=5 rtc_w=1` — telemetry-safe. */
void svc_rtc_note(char *out, size_t len);

/* The CST9220 touch controller (driven as a CST9217, HARDWARE.md §3). This service polls it on
 * its own task over the I2C bus svc_power owns, and hands samples to the UI task through a ring;
 * it never calls LVGL or the shell. What a touch MEANS (wake, tap, swipe) is the shell's. */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "tp_logic.h"

/* After the rollback gate and svc_power_start. Pulses TP_RST (GPIO11), checks the chip answers
 * at 0x5A, then polls it every 20 ms. False, with telemetry saying `touch=failed:<step>`, leaves
 * the panel running without touch. */
bool svc_touch_start(void);

/* UI task, from LVGL's pointer read: the next press, move or release, oldest first. */
bool svc_touch_next(tp_sample_t *out);

/* `touch=… taps=… tp_last=… tp_err=…` — telemetry-safe tokens. */
void svc_touch_note(char *out, size_t len);

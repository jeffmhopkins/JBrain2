/* The auto-rotate service's device side, beside the app-facing svc_orient.h. The same file runs
 * on the device and in the sim (svc_orient.c); only the port differs: esp/orient_port_qmi8658.c
 * reads the chip over svc_power's bus, orient_port_fake.c is a tilt the sim and the host set.
 *
 * NO TASK OF ITS OWN. The UI task calls svc_orient_service every pass; it reads the chip at most
 * every ORIENT_PERIOD_MS (8 Hz), only while the screen is lit and the switch is on, and powers the
 * accelerometer down otherwise. One six-byte read is ~0.3 ms on the bus. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "dbg_json.h"

/* After svc_power_start (the bus): the chip's id, a reset and its configuration. False leaves the
 * panel upright for the boot, with `orient=failed:<step>` in telemetry and Settings' switch dead. */
bool svc_orient_start(void);
/* UI task, every pass. */
void svc_orient_service(uint32_t now_ms);
/* UI task: called with each new rotation as it is asked of the display, so touch follows it. */
void svc_orient_on_change(void (*fn)(uint8_t quarters));
/* `orient=<state>,rot:<deg>,chg:N,rd:N,err:N,axes:N` */
void svc_orient_note(char *out, size_t len);

/* ---- the port ---- */
/* False with the step that failed ("bus", "i2c", "id", "cfg"). */
bool orient_port_start(const char **step);
/* The accelerometer on (screen lit, switch on) or powered down. */
bool orient_port_power(bool on);
/* X, Y, Z in counts at ±2 g (ORIENT_1G a g). */
bool orient_port_read(int16_t a[3]);
void orient_port_describe(dbg_json_t *w);

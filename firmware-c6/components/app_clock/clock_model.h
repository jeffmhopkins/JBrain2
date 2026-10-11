/* The Clock's logic, apart from its widgets: 24-hour text, the next alarm, the night face and
 * snooze. Pure C, host-tested. Everything is 24-hour, on the glass and in the PWA. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_alarms.h"

void clock_fmt_hm(uint8_t h, uint8_t m, char out[6]);

/* Minutes until each enabled alarm next rings from (now_min, wday), honouring repeat days;
 * a one-off rings at its next occurrence. False when none is on. Exactly now is not "next". */
bool clock_next_alarm(const alarm_t *a, uint8_t n, uint16_t now_min, uint8_t wday, uint8_t *idx,
                      uint32_t *wait_min);
/* "Next 06:45 - in 9 h 4 min" */
void clock_fmt_next(const alarm_t *a, uint32_t wait_min, char *buf, size_t len);
/* "Weekdays - Work", "Sat, Sun", "Every day", "Once" */
void clock_fmt_days(uint8_t days, const char *label, char *buf, size_t len);

/* Minute of day a snooze rings again, wrapping past midnight. */
uint16_t clock_snooze(uint8_t h, uint8_t m, uint8_t minutes);

typedef enum { CLOCK_NIGHT_RED, CLOCK_NIGHT_AMBER, CLOCK_NIGHT_GREY } clock_night_color_t;

typedef struct {
    uint32_t color;
    uint8_t opa; /* 0-255 */
    bool night;
} clock_face_t;

/* Deep red by default: least disruptive to dark-adapted eyes, and on an AMOLED dim digits on
 * black light almost nothing else in the room. */
clock_face_t clock_face(bool night_on, uint16_t from, uint16_t to, uint8_t color, uint16_t now_min,
                        bool force_night);

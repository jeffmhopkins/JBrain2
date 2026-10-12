/* The Clock's logic, apart from its widgets: 24-hour text, the next alarm, the night face and
 * snooze. Pure C, host-tested. Everything is 24-hour, on the glass and in the PWA. */
#pragma once

/* A finger held still this long on the face opens the alarms list; a tap does nothing (owner,
 * 2026-10-12: "that needs to be a long press, maybe 2 seconds or more"). */
#define CLOCK_HOLD_MS 2000

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "svc_alarms.h"
#include "svc_rtc.h"

void clock_fmt_hm(uint8_t h, uint8_t m, char out[6]);
/* The face's digits, or "--:--" while the panel has no time from the chip or the box. */
void clock_fmt_face(const rtc_time_t *t, char out[6]);
/* "Mon 12 Oct": en-GB's short weekday, day and month, as the mock's toLocaleDateString. Empty
 * while there is no time. */
void clock_fmt_daydate(const rtc_time_t *t, char *buf, size_t len);

/* Minutes until each enabled alarm next rings from (now_min, wday), honouring repeat days;
 * a one-off rings at its next occurrence. False when none is on. Exactly now is not "next". */
bool clock_next_alarm(const alarm_t *a, uint8_t n, uint16_t now_min, uint8_t wday, uint8_t *idx,
                      uint32_t *wait_min);
/* "Next 06:45 · in 9 h 4 min" */
void clock_fmt_next(const alarm_t *a, uint32_t wait_min, char *buf, size_t len);
/* "Weekdays · Work", "Sat, Sun", "Every day", "Once" */
void clock_fmt_days(uint8_t days, const char *label, char *buf, size_t len);

/* Minute of day a snooze rings again, wrapping past midnight. */
uint16_t clock_snooze(uint8_t h, uint8_t m, uint8_t minutes);

typedef enum { CLOCK_NIGHT_RED, CLOCK_NIGHT_AMBER, CLOCK_NIGHT_GREY } clock_night_color_t;

typedef struct {
    uint32_t color;
    uint8_t opa;      /* the digits, 0-255 */
    uint8_t date_opa; /* the day and date: fainter, and dimmer still at night */
    bool night;
} clock_face_t;

/* Deep red by default: least disruptive to dark-adapted eyes, and on an AMOLED dim digits on
 * black light almost nothing else in the room. */
clock_face_t clock_face(bool night_on, uint16_t from, uint16_t to, uint8_t color, uint16_t now_min,
                        bool force_night);

/* "21:30 – 06:30" */
void clock_fmt_window(uint16_t from, uint16_t to, char *buf, size_t len);

/* The face is never covered (owner, 2026-10-11): a notice or a person's message waits as a
 * corner badge, and a notice opens only once its badge is tapped. `released_id` is the notice
 * the owner tapped open; the next one waits again, as the mock's noteOpen does. */
bool clock_holds_notices(bool face_up, uint8_t notices, uint32_t head_id, uint32_t released_id);
/* What the badge counts: queued notices and unheard messages from people. 0 hides it. */
uint16_t clock_badge_count(uint8_t notices, uint16_t unplayed);
/* Full strength for an urgent notice, 80% otherwise. */
uint8_t clock_badge_opa(bool urgent);

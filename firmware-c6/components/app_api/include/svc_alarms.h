/* Alarms. Created and edited in the PWA (jpanel → Panels → Jeff's); the panel only switches them
 * on and off. The service owns the schedule: a copy in NVS from the box's `GET /endpoint/alarms`,
 * fired from the panel's own clock in UTC with the zone's daylight saving, so an alarm rings with
 * the Clock closed and the box unreachable. It raises ALARM_DUE (the shell opens the Clock's
 * ringing view) and ALARM_ENDED (snoozed, stopped, or the ten-minute cap). */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ALARM_MAX 8
#define ALARM_LABEL_BYTES 24
#define ALARM_SNOOZE_MIN 9
/* A ring nobody answers stops itself after this, and the snapshot says it timed out. */
#define ALARM_RING_CAP_MIN 10

/* Synthesised on the panel from the tone path: no audio files. */
typedef enum { ALARM_SOUND_CHIME, ALARM_SOUND_BEEPS, ALARM_SOUND_RISE, ALARM_SOUNDS } alarm_sound_t;

typedef struct {
    uint32_t id; /* the box's; 0 is the debug relay's test alarm */
    uint8_t h, m; /* in the panel's zone */
    uint8_t days; /* bit 0 = Monday … bit 6 = Sunday; 0 = once, then it switches itself off */
    bool on;
    uint8_t sound;
    char label[ALARM_LABEL_BYTES + 1]; /* printable ASCII, as the box allows */
} alarm_t;

uint8_t svc_alarms_count(void);
/* In time-of-day order. */
bool svc_alarms_get(uint8_t idx, alarm_t *out);
/* The switch on the glass: takes effect at once, and is queued for the box (last write wins,
 * retried until the box has it, kept through a reboot). */
void svc_alarms_set_on(uint32_t id, bool on);
bool svc_alarms_ringing(alarm_t *out);
/* Returns the minute of day it rings again. */
uint16_t svc_alarms_snooze(void);
void svc_alarms_stop(void);

/* What rings next: a snooze, or an alarm's next occurrence. False when nothing is on or the
 * panel has no time. `at_h:at_m` is the local time it rings; `wait_min` rounds up. */
typedef struct {
    alarm_t a;
    bool snoozed;
    uint8_t at_h, at_m;
    uint32_t wait_min;
} alarm_next_t;
bool svc_alarms_next(alarm_next_t *out);

/* Where the panel's copy came from, for the alarms page's last line. */
void svc_alarms_sync_line(char *buf, size_t len);

/* The debug relay's `inject event=alarm_test`: rings now, as an alarm due this minute would. */
bool svc_alarms_test(uint8_t sound);

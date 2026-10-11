/* Alarms. The service owns the schedule (NVS copy, RTC wake) so an alarm rings with the Clock
 * closed and Wi-Fi down; it raises ALARM_DUE and the shell opens Clock in its ringing view.
 * Alarms are created and edited in the PWA; the panel only switches them on and off. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#define ALARM_MAX 8
#define ALARM_SNOOZE_MIN 9

typedef struct {
    uint8_t id;
    uint8_t h, m;
    uint8_t days; /* bit 0 = Monday … bit 6 = Sunday; 0 = once, then it switches itself off */
    bool on;
    char label[24];
} alarm_t;

uint8_t svc_alarms_count(void);
bool svc_alarms_get(uint8_t idx, alarm_t *out);
void svc_alarms_set_on(uint8_t id, bool on);
bool svc_alarms_ringing(alarm_t *out);
/* Returns the minute of day it rings again. */
uint16_t svc_alarms_snooze(void);
void svc_alarms_stop(void);

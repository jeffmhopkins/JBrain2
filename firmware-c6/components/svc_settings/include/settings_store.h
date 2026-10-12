/* The settings' values and what is owed to flash, apart from the flash itself. Pure C, shared by
 * the device's NVS service and the in-memory one, and host-tested.
 *
 * A brightness slider moves through a dozen values a second; writing each would spend NVS
 * pages for nothing. So a change only marks the key, and the keys are written once the panel
 * has been left alone for SETTINGS_QUIET_MS — and only those whose value differs from what
 * flash already holds. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "svc_settings.h"

#define SETTINGS_QUIET_MS 2000

typedef struct {
    int32_t val[SET__COUNT];
    int32_t saved[SET__COUNT]; /* what flash holds; a default counts as held until changed */
    uint32_t changed_ms;
} settings_store_t;

void settings_store_defaults(settings_store_t *s);
/* A value read back from flash: clamped like any write, so a stored value from an older image
 * with a wider range cannot put the panel outside today's limits. */
void settings_store_loaded(settings_store_t *s, setting_t key, int32_t raw);
/* True when the value changed. */
bool settings_store_set(settings_store_t *s, setting_t key, int32_t value, uint32_t now_ms);
/* Bit per key that should be written now: none while the last change is younger than the
 * quiet period, and never one flash already holds. */
uint32_t settings_store_due(const settings_store_t *s, uint32_t now_ms);
void settings_store_mark_saved(settings_store_t *s, setting_t key);

#pragma once

#include <stdint.h>

#include "esp_err.h"

/* Everything that makes one unit different from another. The firmware image itself is
   generic and carries none of it: the box writes this into NVS at flash time, from an
   `nvs_partition_gen.py` blob, so the published artifact holds no credential and an OTA
   (which rewrites only an app slot) cannot disturb it. */
typedef struct {
    char *api;   /* base URL, e.g. "https://jbrain.local/api" — no trailing slash */
    char *token; /* device credential, sent as a bearer token */
    char *ca;    /* PEM of the box's Caddy root; NUL-terminated, as mbedTLS wants it */
    char *ssid;
    char *pass;
    char *name; /* which twin's endpoint this is — logs and, later, the pet it binds to */
} cfg_t;

/* Reads the `jbrain` NVS namespace. Returns ESP_ERR_NVS_NOT_FOUND when the unit has never
   been provisioned, which is a normal state on a freshly flashed board and not a fault. */
esp_err_t cfg_load(cfg_t *out);
void cfg_free(cfg_t *c);

/* THE TOUCH CALIBRATION, kept apart from the provisioning above.
 *
 * Its own namespace because the two have opposite lifecycles: provisioning is written by the
 * box at flash time and never by the firmware, while this is measured on the panel, by the
 * owner, and written from here. A re-flash regenerates the whole NVS partition and takes this
 * with it — which is why the panel also reports its calibration in telemetry, so a wipe costs
 * a re-run of the routine rather than a fact nobody has any more.
 *
 * `cfg_calibration_load` returns the number of bytes read, 0 when there is none. */
int cfg_calibration_load(uint8_t *buf, int cap);
esp_err_t cfg_calibration_save(const uint8_t *buf, int len);

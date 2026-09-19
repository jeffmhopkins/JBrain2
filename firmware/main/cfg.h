#pragma once

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

#pragma once

#include <stdbool.h>

#include "esp_err.h"

/* Everything that makes one unit different from another. The image is generic and carries none
   of it: the box writes this into the `jbrain` NVS namespace at flash time — the same keys it
   writes for the S3 panels — so the artifact holds no credential and an OTA cannot disturb it. */
typedef struct {
    char *api;   /* base URL, e.g. "https://jbrain.local/api" — no trailing slash */
    char *token; /* device credential, sent as a bearer token */
    char *ca;    /* PEM of the box's Caddy root; absent means "trust the public bundle" */
    char *ssid;
    char *pass;
    char *name;
    char *role; /* read so a later image has it; the bring-up image does nothing with it */
} cfg_t;

/* ESP_ERR_NVS_NOT_FOUND when the unit has never been provisioned — normal on a fresh board. */
esp_err_t cfg_load(cfg_t *out);
void cfg_free(cfg_t *c);

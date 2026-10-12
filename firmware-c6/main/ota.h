#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "cfg.h"
#include "esp_err.h"

#define OTA_VERSION_MAX 48

typedef struct {
    char version[OTA_VERSION_MAX];
    char url[256];
    /* The box's clock from the reply's Date header (Unix seconds), 0 when it sent none. This is
       how the panel learns the time: the box serves no NTP, and this request is already made
       every minute (README "What 0.0.8 adds"). */
    int64_t date;
} ota_manifest_t;

/* What an install reports as it goes, for the Firmware card. Both called on the main task. */
typedef struct {
    void (*progress)(uint8_t pct);
    void (*restarting)(void);
} ota_hooks_t;

/* GET {api}/endpoint/firmware with the device key. Reaching it is also the health signal the
   rollback gate turns on: the thing that proves this panel is recoverable is exactly the thing
   that recovers it. */
esp_err_t ota_fetch_manifest(const cfg_t *cfg, ota_manifest_t *out);

/* POST {api}/endpoint/telemetry. ESP_OK only on the box's 204 — a 422 or 401 is a report that
   did not land, whatever the transport managed. */
esp_err_t ota_report(const cfg_t *cfg, const char *body);

/* The version this image was built as, from the app descriptor (i.e. version.txt). */
const char *ota_running_version(void);

/* Downloads `url` into the inactive slot and reboots into it; does not return on success.
   Refuses, before writing anything, an image built for another board — see ota.c. */
esp_err_t ota_apply(const cfg_t *cfg, const char *url, const ota_hooks_t *hooks);

/* True when `version` was offered, downloaded far enough to read its descriptor, and refused as
   another board's image. The caller skips it until the box offers something else, so a box
   serving only the S3 image costs this panel nothing per check. */
bool ota_refused(const char *version);

/* Why the last install failed and how many have failed since boot ("" and 0 when none has).
   A panel that cannot install keeps reporting its old version, which from the box reads
   exactly like a panel nobody offered an update to. */
void ota_apply_faults(const char **err, int *tries);

/* The rollback gate, and the reason this firmware exists. A freshly OTA'd image boots
   PENDING_VERIFY and the bootloader reverts it on the next reset unless it marks itself good.
   The bar is "the box is still reachable for the next update", not "the app works".
   `reachable` false on a pending image reverts and does not return. True when this boot is the
   first of a freshly installed image, which has just marked itself good. */
bool ota_confirm_health(bool reachable);

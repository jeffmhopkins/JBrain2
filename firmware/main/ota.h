#pragma once

#include <stdbool.h>

#include "cfg.h"
#include "esp_err.h"

#define OTA_VERSION_MAX 48

typedef struct {
    char version[OTA_VERSION_MAX];
    char url[256];
} ota_manifest_t;

/* GET {api}/endpoint/firmware. Reaching this endpoint is also the health signal the rollback
   gate turns on — see ota_confirm_health(). */
esp_err_t ota_fetch_manifest(const cfg_t *cfg, ota_manifest_t *out);

/* The version this image was built as, from the app descriptor. */
const char *ota_running_version(void);

/* Downloads and installs `url` into the inactive slot, then reboots into it. Does not return
   on success. The new image is on probation until it, in turn, reaches the manifest. */
esp_err_t ota_apply(const cfg_t *cfg, const char *url);

/* Tell the box what this panel looks like from the inside.

   The channel that neither lies nor resets what it measures. Register reads over QSPI return
   zeros that read like a diagnosis, and opening the USB console restarts the chip before the
   fault can be seen — so with a cable this was hard, and on a plain USB charger it was
   impossible. `body` is the JSON, built by the caller. Best-effort: a panel that cannot report
   is not a panel that should stop working. */
esp_err_t ota_report(const cfg_t *cfg, const char *body);

/* The rollback gate, and the reason this firmware exists.
 *
 * A freshly OTA'd image boots in PENDING_VERIFY and is reverted by the bootloader on the next
 * reset unless it affirmatively marks itself good. The bar for "good" here is deliberately
 * NOT "the app is working" but "the box is still reachable for the next update" — because the
 * only unrecoverable state is one an OTA cannot reach, and a unit in a child's bedroom has no
 * cable. An image that renders nothing but can still be updated is a bad afternoon; an image
 * that looks perfect and cannot be updated is a trip with a screwdriver.
 *
 * `reachable` false on a pending image triggers the revert and does not return. */
void ota_confirm_health(bool reachable);

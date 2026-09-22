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

/* The panel knobs the box is holding for this unit.

   Microphone gain, speaker volume and display brightness were compile-time constants, so
   changing any of them cost a build, a CI run, a deploy and an OTA — which is why the volume
   took two rounds to settle and the other two were never tuned at all. Fetched at boot and on
   every cycle; the five-second hold reboots, so it is also how a change is applied at once. */
typedef struct {
    int volume;      /* 0..100 as esp_codec_dev takes it; the box clamps the ceiling */
    int mic_gain_db; /* 0..42; the ES8311's PGA quantises to 6 dB steps */
    int brightness;  /* 0..255, written to the panel's 0x51 */
    int debug_overlay; /* draw the microphone meter; 0 unless the owner switched it on */
} ota_settings_t;

esp_err_t ota_fetch_settings(const cfg_t *cfg, ota_settings_t *out);

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

/* IS THIS THE FIRST BOOT OF A NEWLY INSTALLED IMAGE?
 *
 * Which is the question behind the second boot after an update — the only thing that has ever
 * reliably cured the black screen short of pulling the plug. A panel that updates comes up
 * dark; rebooted again, same image, nothing reinstalled, it comes up lit. §10.4by's
 * mid-transfer theory was tested in 0.2.65 and falsified in 0.2.76, so this is a workaround
 * and is labelled one. The remaining suspect is the CO5300's reset line, which the vendor BSP
 * leaves at `GPIO_NUM_NC` and which most likely hangs off the TCA9554 expander.
 *
 * NO FLAG, AND THAT IS THE FIX FOR 0.2.77. The first attempt carried the answer across the
 * reboot in an `RTC_NOINIT` word — and RTC memory DOES NOT SURVIVE AN OTA. It survives
 * `esp_restart()` of the same image, which is why the gesture reboot's telemetry shows a live
 * crash phase and a full PMU ring; but a new image is a new link, its RTC variables land at
 * different addresses, and the incoming firmware reads the outgoing one's bytes. Measured
 * directly: the post-update boot reports `crash_phase: -1` and an empty ring where a gesture
 * reboot reports 9 and eight samples. The magic rejected the garbage, so nothing broke — the
 * restart simply never happened.
 *
 * `ESP_OTA_IMG_PENDING_VERIFY` is the same question asked of the system rather than of memory
 * we do not control. It is true on exactly the first boot of a newly installed image and on no
 * other, it needs no storage, and it cannot be confused by a layout change.
 *
 * MUST BE READ BEFORE `ota_confirm_health`, which clears the state when it marks the image
 * good. */
bool ota_boot_is_new_image(void);

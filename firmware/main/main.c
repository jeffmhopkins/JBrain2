/* The room endpoint's first firmware. Its entire job is to make every firmware after it
 * arrive over Wi-Fi.
 *
 * Both units are going to the twins, so there is no permanent bench unit and no cable in the
 * room. That makes the recovery path the product: this image carries the rollback gate and
 * defers everything it does not strictly need — no display, no touch, no audio, and
 * deliberately no PSRAM, because a PSRAM misconfiguration is exactly the class of fault that
 * ends in a boot loop and a screwdriver. Those arrive over the air, on a unit that has already
 * proved it can take an update.
 */

#include <stdbool.h>

#include "cfg.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net.h"
#include "audio.h"
#include "display.h"
#include "esp_psram.h"
#include "nvs_flash.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "ota.h"
#include "pmu.h"

static const char *TAG = "endpoint";

#define WIFI_TIMEOUT_MS 30000
/* The probation window. A pending image gets several minutes and several tries to reach the
   box before it is judged, because the box restarting during an update round is ordinary and
   must not cost a good image. */
#define HEALTH_ATTEMPTS 5
#define HEALTH_GAP_MS 20000
/* How often a settled image looks for a newer one. Slow on purpose: a pushed update is not
   urgent, and an endpoint hammering the box is a worse failure than a late rollout. */
#define CHECK_PERIOD_MS (15 * 60 * 1000)

/* Tries the manifest repeatedly, and reports both whether the box was reachable at all and
   what it offered. Reachability is the rollback criterion; the manifest is the payload. */

/* What this panel looks like from the inside, as JSON.

   THE HISTORY IS THE POINT, and it is why this is sent from here rather than logged. The
   samples in `pmu.c` survive a soft reset, so the five-second hold is a shutter: the owner
   sees a dark screen, holds, and the panel comes back and posts the two minutes that preceded
   the fault. Reading that off the console was never going to work — opening the port restarts
   the chip — and once the panel moves to a plain USB charger there is no console at all.

   Built on the stack and deliberately bounded: a panel with something to say must not be able
   to spend the heap saying it. */
/* Ask the box what this panel should sound and look like, and apply it.

   Every one of these was a compile-time constant until now, which is why the volume took two
   OTA cycles to settle on 70 and the microphone gain and the brightness were never tuned at
   all — the loop was too slow to bother with. Fetched here rather than in the render task so
   a slow or unreachable box can never stall a frame.

   Defaults are the firmware's own, so a box that has never had them set, or one running older
   code that does not serve the route, leaves the panel exactly as it shipped. */
static void apply_settings(const cfg_t *cfg)
{
    ota_settings_t st = {.volume = -1, .mic_gain_db = -1, .brightness = -1};
    if (ota_fetch_settings(cfg, &st) != ESP_OK) return;
    if (st.volume >= 0 || st.mic_gain_db >= 0) audio_set_levels(st.volume, st.mic_gain_db);
    if (st.brightness >= 0) display_set_brightness(st.brightness);
}

static void report(const cfg_t *cfg)
{
    static const char *REASONS[] = {"unknown", "power", "ext",  "sw",   "panic",  "int_wdt",
                                    "task_wdt", "wdt",  "sleep", "brownout", "sdio"};
    const int r = (int)esp_reset_reason();
    const char *reason = (r >= 0 && r < (int)(sizeof(REASONS) / sizeof(REASONS[0])))
                             ? REASONS[r]
                             : "other";

    char hist[8][PMU_SAMPLE_CHARS];
    const int n = pmu_history_hex(hist, 8);

    char body[768];
    int w = snprintf(body, sizeof(body),
                     "{\"version\":\"%s\",\"uptime_ms\":%llu,\"reset_reason\":\"%s\","
                     "\"free_heap\":%u,\"free_psram\":%u,\"mic_peak\":%d,"
                     "\"pmu_history\":[",
                     ota_running_version(),
                     (unsigned long long)(esp_timer_get_time() / 1000), reason,
                     (unsigned)esp_get_free_heap_size(),
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                     display_mic_peak());
    for (int i = 0; i < n && w > 0 && w < (int)sizeof(body) - 32; i++) {
        w += snprintf(body + w, sizeof(body) - (size_t)w, "%s\"%s\"", i ? "," : "", hist[i]);
    }
    if (w > 0 && w < (int)sizeof(body) - 4) snprintf(body + w, sizeof(body) - (size_t)w, "]}");
    ota_report(cfg, body);
}

static bool reach_box(const cfg_t *cfg, ota_manifest_t *manifest)
{
    for (int i = 1; i <= HEALTH_ATTEMPTS; i++) {
        if (ota_fetch_manifest(cfg, manifest) == ESP_OK) return true;
        ESP_LOGW(TAG, "box unreachable (%d/%d)", i, HEALTH_ATTEMPTS);
        if (i < HEALTH_ATTEMPTS) vTaskDelay(pdMS_TO_TICKS(HEALTH_GAP_MS));
    }
    return false;
}

void app_main(void)
{
    ESP_LOGI(TAG, "jbrain room endpoint, version %s", ota_running_version());

    /* Reported explicitly because the rollback gate cannot catch this one. PSRAM is
       configured to DEGRADE rather than abort when it is not found, so a wrong mode gives a
       panel that boots, reaches the box and marks itself good — while being 8 MB short of
       what the display needs. "It came up" is not the reading; this line is. */
    if (esp_psram_is_initialized()) {
        ESP_LOGI(TAG, "psram: %u KB", (unsigned)(esp_psram_get_size() / 1024));
    } else {
        ESP_LOGE(TAG, "psram: ABSENT — the display cannot be driven from internal RAM");
    }

    /* Before the network on purpose: it is the slow, visible thing, and a unit that shows
       something while it joins Wi-Fi is one whose owner can tell "working" from "dead". It
       cannot fail fatally — see display.c on why an abort here would be the worst outcome
       available rather than a safe one. */
    if (!display_start()) {
        ESP_LOGE(TAG, "display did not come up — continuing, the box is still reachable");
    } else {
        display_run_face();
    }

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        /* Erasing NVS erases the provisioning with it, so this is the one path that ends with
           a unit needing the cable again. It should only ever fire on a corrupt partition. */
        ESP_LOGW(TAG, "NVS unusable — erasing, which also clears this unit's provisioning");
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    cfg_t cfg;
    if (cfg_load(&cfg) != ESP_OK) {
        /* An unprovisioned board is not broken and must not roll back — it has simply never
           been told which network or which box. It waits for the flasher to write NVS. */
        ESP_LOGE(TAG, "no provisioning in NVS — flash this unit from the box (Ops > Endpoints)");
        return;
    }

    if (net_connect(&cfg, WIFI_TIMEOUT_MS) != ESP_OK) {
        /* No Wi-Fi means no possible update, which is the one thing this image exists to
           prevent, so a pending image gives up its probation here rather than persisting as
           an unreachable unit. */
        ESP_LOGE(TAG, "no network");
        ota_confirm_health(false);
        cfg_free(&cfg);
        return;
    }

    ota_manifest_t manifest;
    bool reachable = reach_box(&cfg, &manifest);
    ota_confirm_health(reachable);
    if (!reachable) {
        ESP_LOGE(TAG, "on Wi-Fi but the box did not answer; retrying on the next cycle");
    }

    /* Before the loop and before anything else touches the ring: this call carries whatever
       survived the last restart, and one more sample would dilute it. */
    if (reachable) {
        apply_settings(&cfg);
        report(&cfg);
    }

    while (true) {
        if (reachable) {
            const char *running = ota_running_version();
            if (strcmp(manifest.version, running) != 0) {
                ESP_LOGI(TAG, "update offered: %s -> %s", running, manifest.version);
                ota_apply(&cfg, manifest.url); /* reboots on success */
            } else {
                ESP_LOGI(TAG, "up to date at %s", running);
            }
        }
        vTaskDelay(pdMS_TO_TICKS(CHECK_PERIOD_MS));
        reachable = ota_fetch_manifest(&cfg, &manifest) == ESP_OK;
        if (reachable) {
            apply_settings(&cfg);
            report(&cfg);
        }
    }
}

/* Jeff's jpanel. Its first job is still to make every firmware after it arrive over Wi-Fi:
 * join, reach the box, mark itself good, report, and install what the box offers.
 *
 * 0.0.4 is the first image that drives the screen: the AXP2101 (svc_power), the buttons
 * (svc_buttons), the CO5300 under LVGL (svc_display) and the shell with every app over fake
 * services (ui_task.c). 0.0.6 adds the CST9220 (svc_touch). ALL OF IT STARTS AFTER THE ROLLBACK GATE, and the boot after a panic or a
 * watchdog leaves the display off (disp_guard.c), so no fault in the new code can stop this
 * loop reaching the box and taking the image that fixes it (README "What 0.0.4 adds").
 */

#include <inttypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "cfg.h"
#include "dbg_core.h"
#include "disp_guard.h"
#include "esp_chip_info.h"
#include "esp_core_dump.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net.h"
#include "nvs_flash.h"
#include "ota.h"
#include "svc_buttons.h"
#include "svc_display_drv.h"
#include "svc_power_drv.h"
#include "svc_touch.h"
#include "ui_task.h"

static const char *TAG = "jpanel-c6";

#define WIFI_TIMEOUT_MS 30000
/* The probation window: a pending image gets five tries over ~100 s to reach the box, so the
   box restarting mid-update does not cost a good image. Same as the S3 panels. */
#define HEALTH_ATTEMPTS 5
#define HEALTH_GAP_MS 20000
/* How often to ask for a newer image. A minute rather than the S3's fifteen because this panel
   is on the owner's desk and its next dozen images are bring-up steps — an update should land
   while he is still looking at it. One handshake a minute is nothing to the box. */
#define CHECK_PERIOD_MS (60 * 1000)
#define REPORT_PERIOD_MS (15 * 60 * 1000)
/* A failed install retries no faster than this, so a broken image URL is not a download a
   minute. */
#define OTA_RETRY_BACKOFF_MS (15 * 60 * 1000)
/* How long the box may be silent with the link up before the radio is not believed. The S3
   panels sat half-open — associated, holding an IP, no route — for eight hours with no
   disconnect event (firmware/README.md); the box is the link test that matters. Ten checks. */
#define LINK_SUSPECT_MS (10 * 60 * 1000)

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* Last time any request reached the box; `s_box_ever` separates "never" from "at 0 ms". */
static uint32_t s_box_ms;
static bool s_box_ever;
static unsigned s_relinks;

static bool fetch_manifest(const cfg_t *cfg, ota_manifest_t *m)
{
    if (ota_fetch_manifest(cfg, m) != ESP_OK) return false;
    s_box_ms = now_ms();
    s_box_ever = true;
    return true;
}

static bool reach_box(const cfg_t *cfg, ota_manifest_t *m)
{
    for (int i = 1; i <= HEALTH_ATTEMPTS; i++) {
        if (fetch_manifest(cfg, m)) return true;
        ESP_LOGW(TAG, "box unreachable (%d/%d)", i, HEALTH_ATTEMPTS);
        if (i < HEALTH_ATTEMPTS) vTaskDelay(pdMS_TO_TICKS(HEALTH_GAP_MS));
    }
    return false;
}

/* The heartbeat, in the S3 panels' telemetry shape so the box stores it without a schema
   change: every field here is one `TelemetryIn` already declares. `note` says which board this
   is, because nothing else in the body does. */
static bool report(const cfg_t *cfg)
{
    /* All sixteen, with the number: on the S3 a short table printed "other" for a crash loop. */
    static const char *REASONS[] = {"unknown", "power", "ext",   "sw",    "panic",  "int_wdt",
                                    "task_wdt", "wdt",  "sleep", "brownout", "sdio", "usb",
                                    "jtag",    "efuse", "pwr_glitch", "cpu_lockup"};
    const int r = (int)esp_reset_reason();
    char reason[24];
    if (r >= 0 && r < (int)(sizeof(REASONS) / sizeof(REASONS[0]))) {
        snprintf(reason, sizeof(reason), "%s(%d)", REASONS[r], r);
    } else {
        snprintf(reason, sizeof(reason), "unmapped(%d)", r);
    }

    esp_chip_info_t chip;
    esp_chip_info(&chip);
    size_t dump_addr = 0, dump_size = 0;
    const bool dump = esp_core_dump_image_get(&dump_addr, &dump_size) == ESP_OK;

    int wifi_reason = 0, wifi_drops = 0;
    net_link_faults(&wifi_reason, &wifi_drops);
    const char *ota_err = "";
    int ota_tries = 0;
    ota_apply_faults(&ota_err, &ota_tries);
    const long quiet_s = s_box_ever ? (long)((now_ms() - s_box_ms) / 1000) : -1;

    /* Bounded on its own, so however long a service's tokens grow the body still fits and the
       report still goes: a truncated note is a smaller loss than no heartbeat. Static, as is
       the body, because this task then runs a TLS handshake on the same 8 KB stack. */
    static char note[720], part[256];
    size_t used = 0;
    void (*const parts[])(char *, size_t) = {svc_display_note, ui_task_note, svc_power_note,
                                             svc_buttons_note, svc_touch_note};
    note[0] = '\0';
    for (size_t i = 0; i < sizeof parts / sizeof *parts && used + 1 < sizeof note; i++) {
        parts[i](part, sizeof part);
        const int k = snprintf(note + used, sizeof note - used, " %s", part);
        if (k > 0) used += (size_t)k < sizeof note - used ? (size_t)k : sizeof note - used - 1;
    }

    static char body[1280];
    const int n = snprintf(
        body, sizeof(body),
        "{\"version\":\"%s\",\"uptime_ms\":%" PRIu32 ",\"reset_reason\":\"%s\","
        "\"free_heap\":%u,\"int_free\":%u,\"int_min\":%u,\"int_big\":%u,\"int_largest\":%u,"
        "\"wifi_reason\":%d,\"wifi_drops\":%d,\"ota_err\":\"%s\",\"ota_tries\":%d,"
        "\"relinks\":%u,\"box_quiet_s\":%ld,"
        "\"note\":\"board=jpanel-c6 image=display chip=esp32c6 rev=%u.%u dump=%d "
        "main_stack=%u%s\"}",
        ota_running_version(), now_ms(), reason,
        (unsigned)esp_get_free_heap_size(),
        (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
        (unsigned)heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
        (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA),
        wifi_reason, wifi_drops, ota_err, ota_tries, s_relinks, quiet_s,
        (unsigned)(chip.revision / 100), (unsigned)(chip.revision % 100), dump ? 1 : 0,
        (unsigned)uxTaskGetStackHighWaterMark(NULL), note);
    if (n < 0 || n >= (int)sizeof(body)) {
        ESP_LOGE(TAG, "telemetry body overflowed — not sent");
        return false;
    }
    ESP_LOGI(TAG, "reset %s, uptime %" PRIu32 " ms, internal free %u", reason, now_ms(),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    return ota_report(cfg, body) == ESP_OK;
}

/* True when an install was attempted, which on return means it failed. */
static bool maybe_install(const cfg_t *cfg, const ota_manifest_t *m, uint32_t *last_try_ms)
{
    const char *running = ota_running_version();
    if (strcmp(m->version, running) == 0) return false;
    if (ota_refused(m->version)) return false;
    const uint32_t now = now_ms();
    /* 0 is "never tried"; unsigned subtraction stays right across the 49-day rollover. */
    if (*last_try_ms != 0 && (uint32_t)(now - *last_try_ms) < OTA_RETRY_BACKOFF_MS) return false;
    ESP_LOGI(TAG, "update offered: %s -> %s", running, m->version);
    *last_try_ms = now == 0 ? 1 : now;
    ota_apply(cfg, m->url); /* reboots on success */
    return true;
}

/* disp_guard.c is pure and host-tested with its own copy of these numbers. */
_Static_assert((int)DG_RST_PANIC == (int)ESP_RST_PANIC &&
                   (int)DG_RST_INT_WDT == (int)ESP_RST_INT_WDT &&
                   (int)DG_RST_TASK_WDT == (int)ESP_RST_TASK_WDT &&
                   (int)DG_RST_WDT == (int)ESP_RST_WDT &&
                   (int)DG_RST_BROWNOUT == (int)ESP_RST_BROWNOUT &&
                   (int)DG_RST_PWR_GLITCH == (int)ESP_RST_PWR_GLITCH &&
                   (int)DG_RST_CPU_LOCKUP == (int)ESP_RST_CPU_LOCKUP &&
                   (int)DG_RST_SW == (int)ESP_RST_SW && (int)DG_RST_POWERON == (int)ESP_RST_POWERON,
               "disp_guard.h's reset numbers have drifted from esp_reset_reason_t");

/* After the rollback gate, never before it: this image has already proved it can reach the box
   and take the next one, whatever the new code below does. */
static void start_panel(void)
{
    dbg_sys_init();
    const int reset = (int)esp_reset_reason();
    const bool may_start = dg_may_start(reset);
    if (!may_start) {
        ESP_LOGW(TAG, "last boot ended in a fault (reset %d, stage %lu) — no display this boot",
                 reset, (unsigned long)dbg_sys_crumb_at_boot());
        svc_display_mark_skipped();
    }
    /* Read-only on a fault boot: the PMIC keeps its registers across an ESP reset, so a bad
       write is the one fault a rollback would not undo. */
    const esp_err_t pmu = svc_power_start(may_start, svc_buttons_pek);
    if (pmu != ESP_OK) ESP_LOGE(TAG, "PMIC: %s", esp_err_to_name(pmu));
    svc_buttons_start(ui_task_button, xTaskGetCurrentTaskHandle());
    if (!may_start) return;
    ui_task_start(xTaskGetCurrentTaskHandle());
    /* Its failure is reported and survivable: the panel keeps BOOT, KEY and PWR without it. */
    svc_touch_start();
}

void app_main(void)
{
    ESP_LOGI(TAG, "jbrain jpanel (ESP32-C6), version %s", ota_running_version());

    esp_err_t err = nvs_flash_init();
    if (err == ESP_ERR_NVS_NO_FREE_PAGES || err == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        /* Erasing NVS erases the provisioning with it — the one path that needs the cable
           again. It should only ever fire on a corrupt partition. */
        ESP_LOGW(TAG, "NVS unusable — erasing, which also clears this unit's provisioning");
        ESP_ERROR_CHECK(nvs_flash_erase());
        err = nvs_flash_init();
    }
    ESP_ERROR_CHECK(err);

    cfg_t cfg;
    if (cfg_load(&cfg) != ESP_OK) {
        /* Unprovisioned is not broken, and a factory image must not roll back over it. On a
           pending OTA image it is: that image cannot reach the box, so give the slot back. */
        ESP_LOGE(TAG, "no provisioning in NVS — flash this unit from the box (Endpoints)");
        ota_confirm_health(false);
        return;
    }

    /* A failed join is a retry, not an exit: returning here would end the update loop, and a
       panel that booted while the router was down would need hands. */
    bool joined = net_connect(&cfg, WIFI_TIMEOUT_MS) == ESP_OK;
    if (!joined) {
        ESP_LOGE(TAG, "no network — will retry every %d s", CHECK_PERIOD_MS / 1000);
        ota_confirm_health(false);
    }

    ota_manifest_t manifest = {0};
    bool reachable = false;
    if (joined) {
        reachable = reach_box(&cfg, &manifest);
        ota_confirm_health(reachable);
        if (!reachable) ESP_LOGE(TAG, "on Wi-Fi but the box did not answer");
    }
    start_panel();

    uint32_t last_report_ms = 0;
    bool reported = false;
    uint32_t ota_last_try_ms = 0;
    while (true) {
        if (reachable) {
            if (!reported || (uint32_t)(now_ms() - last_report_ms) >= REPORT_PERIOD_MS) {
                if (report(&cfg)) {
                    reported = true;
                    last_report_ms = now_ms();
                }
            }
            /* A failed install is reported on the next pass rather than in fifteen minutes:
               it is the one fault that otherwise looks like "no update was offered". */
            if (maybe_install(&cfg, &manifest, &ota_last_try_ms)) reported = false;
        }

        /* A PWR press, or the UI settling, wakes the loop early so the box hears it in seconds. */
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(CHECK_PERIOD_MS));
        /* Both asked, both cleared: `|`, not `||`. */
        if (svc_buttons_take_dirty() | ui_task_take_dirty()) reported = false;

        if (joined && s_box_ever && (uint32_t)(now_ms() - s_box_ms) > LINK_SUSPECT_MS) {
            /* Never-reached is excluded: a wrong key or URL is not fixed by a re-join, and
               thrashing the radio at it only adds a second fault. */
            ESP_LOGW(TAG, "box silent for %" PRIu32 " s with the link up — re-joining",
                     (uint32_t)((now_ms() - s_box_ms) / 1000));
            s_relinks++;
            joined = false;
        }
        if (!joined) {
            joined = net_retry(WIFI_TIMEOUT_MS) == ESP_OK;
            if (joined) ESP_LOGI(TAG, "network recovered");
        }
        reachable = joined && fetch_manifest(&cfg, &manifest);
    }
}

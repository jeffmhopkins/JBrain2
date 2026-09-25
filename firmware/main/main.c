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

#include "cadence.h"
#include "cfg.h"
#include "esp_log.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "net.h"
#include "audio.h"
#include "display.h"
#include "imu.h"
#include "jpanel.h"
#include "esp_psram.h"
#include "nvs_flash.h"
#include "esp_heap_caps.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "mem.h"
#include "ota.h"
#include "vocab.h"
#include "speech.h"
#include "talk.h"
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
/* How often a panel with no network tries again. Much shorter than the update cycle: a
   settled panel asking sooner is noise, but an unreachable one is the failure this whole
   design exists to prevent, and every minute of it is a minute nobody can fix remotely. */
#define OFFLINE_RETRY_MS (60 * 1000)
/* THE ONE POLL, and everything a panel can learn from the box rides it.
   `apply_settings` used to run ONLY after the manifest fetch above, so a brightness the owner
   moved in the PWA took up to fifteen minutes to arrive while a message took thirty seconds —
   a quarter of an hour of a slider that looks broken, on the one surface the owner has
   (CLAUDE.md #10). The interval it inherited was chosen for UPDATES ("a pushed update is not
   urgent"), which was never an argument about settings.

   THREE SECONDS, AND IT NOW ANSWERS THE UPDATE QUESTION TOO. `GET /endpoint/settings` carries
   `fw_version` — what this box would serve — so ONE round trip says both "here are your knobs"
   and "there is new firmware". That matters because every pass is a fresh TLS handshake
   (`ota_fetch_settings` inits and cleans up its own client) and both panels report
   `int_largest` — the largest free INTERNAL DMA block — at 31 KB, where internal fragmentation
   is the fault `report()` says "has explained the fault twice". Two requests answering one
   question each would cost twice the handshakes of one answering both.

   WHAT IS NOT ON THIS CADENCE, deliberately:
     - the 3.25 MB image, which is fetched only when the version actually CHANGES, never per
       poll (see `maybe_install`);
     - a FAILED install, which backs off to `OTA_RETRY_BACKOFF_MS` — the noticing is fast, the
       retrying is not;
     - the telemetry post, which stays on `CHECK_PERIOD_MS`. It is an upsert so there is no
       table to grow, but twenty writes a minute per panel buys nothing, and
       `ROOM_ENDPOINT_PLAN.md` §10.4bh reads a report at 6-7 s of uptime as PROOF OF A BOOT —
       a diagnostic that only works while the interval is long.

   If `int_largest` sags under this rate, this is the number to raise; it is reported every
   cycle, so the evidence arrives without anyone instrumenting anything. */
#define POLL_PERIOD_MS (3 * 1000)
/* HOW LONG A FAILED INSTALL WAITS, and it is the old cycle on purpose: a version that genuinely
   changed is installed within a poll, while an install that failed retries no faster than it
   ever did. Bounds the WITHIN-SESSION rate, which is the one the fast poll created. A crash
   during an install still re-attempts on the next boot exactly as it always has — that path
   predates this change and fixing it needs the attempt written to NVS, which is its own
   decision, not a rider on a cadence change. */
#define OTA_RETRY_BACKOFF_MS CHECK_PERIOD_MS

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
/* Answers whether the box was actually reachable, which the ten-second cadence needs and the
   fifteen-minute one never did: `joined` is only re-read after the sleep below, so a router that
   goes down mid-period leaves this being called on a dead network. Each of those costs
   `HTTP_TIMEOUT_MS` (15 s) of a BLOCKED main task against a 10 s slice, which would stretch the
   period to ~37 minutes and delay the manifest fetch and the telemetry post on exactly the panel
   that most needs them. One failure ends the asking for the rest of the period. */
static bool apply_settings(const cfg_t *cfg, char *served, size_t served_cap)
{
    ota_settings_t st = {.volume = -1, .mic_gain_db = -1, .brightness = -1, .form = -1, .dim_percent = -1};
    if (ota_fetch_settings(cfg, &st) != ESP_OK) return false;
    /* The version this box would serve, for the caller's install check. Optional, because the
       two callers that only want the knobs applied should not have to declare a buffer. */
    if (served != NULL && served_cap > 0) snprintf(served, served_cap, "%s", st.fw_version);
    if (st.volume >= 0 || st.mic_gain_db >= 0) audio_set_levels(st.volume, st.mic_gain_db);
    if (st.brightness >= 0) display_set_brightness(st.brightness);
    display_set_debug_overlay(st.debug_overlay != 0);
    audio_set_agc(st.mic_agc != 0);
    if (st.dim_percent >= 0) display_set_dim_percent(st.dim_percent);
    if (st.form >= 0) display_set_form(st.form);
    /* RENAMING THE PET IS RENAMING THE WAKE WORD, so the model has to be told and the label
       above his head has to be redrawn. `vocab_set_name` answers false when the phrase is
       unchanged — which is every fetch but the one after the owner actually edits it — so the
       command list is not rebuilt twenty times a minute for nothing. */
    if (vocab_set_name(st.pet_name)) {
        speech_reload_vocabulary();
        display_refresh_name();
    }
    return true;
}

/* THE INSTALL, off the fast poll instead of the fifteen-minute cycle. `served` is what the box
   said it would serve, from the same fetch that just applied the knobs, so noticing an update
   costs no extra round trip at all.
 *
 * `last_try_ms` is the caller's, so the backoff survives the whole loop rather than each pass.
 * Recorded BEFORE the attempt because `ota_apply` reboots on success and never returns: an
 * attempt that is not written down first is an attempt that never happened. */
static void maybe_install(const cfg_t *cfg, const char *served, uint32_t *last_try_ms)
{
    /* Empty means the box has no image in its checkout — nothing to compare against, which is
       not the same as being out of date. The settings route answers "" rather than 503 here
       precisely so the knobs still arrive on a box mid-deploy. */
    if (served == NULL || served[0] == '\0') return;
    if (strcmp(served, ota_running_version()) == 0) return;

    const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
    if (!cadence_retry_due(now, *last_try_ms, OTA_RETRY_BACKOFF_MS)) return;

    /* The URL comes from the manifest, which is the route that owns it; the poll only told us a
       version. One extra fetch on the rare pass where an update is actually waiting. */
    ota_manifest_t m;
    if (ota_fetch_manifest(cfg, &m) != ESP_OK) return;
    if (strcmp(m.version, ota_running_version()) == 0) return; /* raced a deploy; nothing to do */

    ESP_LOGI(TAG, "update offered by poll: %s -> %s", ota_running_version(), m.version);
    *last_try_ms = (now == 0) ? 1 : now; /* 0 is reserved for "never tried" */
    ota_apply(cfg, m.url);               /* reboots on success */
}

static void report(const cfg_t *cfg)
{
    /* ALL SIXTEEN, AND THE NUMBER TOO. This table stopped at `sdio` (10) while ESP-IDF's
       enum runs to 15, so every reason above it printed "other" — and "other" is what the
       panel reported for a crash loop that rebooted it roughly every ninety seconds. The one
       field whose entire job is to name the cause was falling off the end of its own lookup
       and saying nothing.
       The five that were missing are not exotic, and one of them is the prime suspect:
       ESP_RST_USB is a reset by the USB peripheral, which is what attaching a serial console
       to this panel does — the very hazard the telemetry route exists to work around. A
       diagnosis channel that cannot distinguish "the firmware crashed" from "someone plugged
       in a cable" is worse than none, because it invites the wrong fix.
       The raw number ships beside the name so that an enum which grows again says so, rather
       than silently rejoining the "other" bucket this cost a day to find. */
    static const char *REASONS[] = {"unknown", "power",    "ext",       "sw",
                                    "panic",   "int_wdt",  "task_wdt",  "wdt",
                                    "sleep",   "brownout", "sdio",      "usb",
                                    "jtag",    "efuse",    "pwr_glitch", "cpu_lockup"};
    const int r = (int)esp_reset_reason();
    char reason_buf[24];
    if (r >= 0 && r < (int)(sizeof(REASONS) / sizeof(REASONS[0]))) {
        snprintf(reason_buf, sizeof(reason_buf), "%s(%d)", REASONS[r], r);
    } else {
        snprintf(reason_buf, sizeof(reason_buf), "unmapped(%d)", r);
    }
    const char *reason = reason_buf;

    /* SAID ON THE CONSOLE AS WELL AS SENT. Telemetry reaches a human through the box's API
       log, which is the right channel and was the one that finally caught the crash loop —
       but it needs the box, the network and the device key all working. The console needs a
       cable. Neither is reliable enough to be the only place the two numbers that identify a
       restart are written down. */
    ESP_LOGW(TAG, "restart: reason %s, crash phase %d, uptime %llu ms", reason,
             display_crash_phase(), (unsigned long long)(esp_timer_get_time() / 1000));

    /* Raw counts, not a derived orientation: which axis points where on this board is
       exactly what is unknown, and a number this firmware has already interpreted cannot
       answer that. Zeros mean the part did not answer, which is its own reading. */
    int16_t ax = 0, ay = 0, az = 0;
    const bool have_imu = imu_read(&ax, &ay, &az);

    /* Where the last tap landed and what the firmware made of it. The touch controller's
       orientation has never been measured; this is how it gets measured. */
    int tap_x = -1, tap_y = -1, tap_zone = 0;
    display_last_tap(&tap_x, &tap_y, &tap_zone);

    char hist[8][PMU_SAMPLE_CHARS];
    const int n = pmu_history_hex(hist, 8);

    /* TWO ANSWERS THAT ONLY EXISTED ON A CABLE. Both of these are ESP_LOG lines as well, and
       an ESP_LOG only reaches a serial console — which this panel no longer has, because the
       owner moved it onto a plain charger, which was always the plan. The ALC reading is the
       answer to a question the owner actually asked, and the blit counts are how a screen
       that has stopped drawing says so; neither is worth having if it can only be read by
       someone holding a USB cable. */
    int blit_ok = 0, blit_fail = 0;
    display_blit_counts(&blit_ok, &blit_fail);

    /* AND A THIRD ANSWER THAT ONLY EXISTED ON A CABLE. The recogniser counts the phrases the
       model refused and names them, and until now it said so only to a serial console. That
       is the one reading that could have answered "the kids say the word and nothing
       happens" — the owner's report on `burp` — so it is worth exactly as much as the two
       above and was reaching exactly as far: nowhere. */
    int vocab_ok = 0, vocab_bad = 0;
    speech_vocab(&vocab_ok, &vocab_bad);

    /* THE REST OF WHAT ONLY A CABLE COULD SEE. Each of these was computed, formatted into an
       ESP_LOG and dropped, on a panel whose console has not existed since it went in a
       bedroom. Ordered by what they would have caught:
         `int_largest` — the largest free INTERNAL DMA block, which is what `free_heap` cannot
           tell you: 60 KB free and fragmented and 60 KB free and contiguous read the same,
           and the difference is every blit failing. This number has explained the fault twice.
         `ota_err`     — an update that will never install, currently silent.
         `levels`      — a volume or gain the codec REFUSED, currently indistinguishable from
           one it accepted.
         `wifi`        — the disconnect reason, the difference between out of range, wrong
           password, and the router dropping it.
         `blit_*`      — the totals, because the pair already reported is reset on recovery.
         `restart_why` — which of the three callers of `esp_restart` it was. */
    int blit_fail_total = 0, blit_recov = 0, meter_fail = 0;
    display_blit_totals(&blit_fail_total, &blit_recov, &meter_fail);
    int wifi_reason = 0, wifi_drops = 0;
    net_link_faults(&wifi_reason, &wifi_drops);
    const char *ota_err = "";
    int ota_tries = 0;
    ota_apply_faults(&ota_err, &ota_tries);

    /* 2048, up from 1536: the heard ring is twelve variable-length entries now and the loop
       below stops on room rather than on a count, so a small body silently costs the OLDEST
       decodes — which is the right end to lose, but only after the buffer has actually been
       sized for the job rather than left at what three entries needed. */
    char body[2048];
    int w = snprintf(body, sizeof(body),
                     "{\"version\":\"%s\",\"uptime_ms\":%llu,\"reset_reason\":\"%s\","
                     "\"free_heap\":%u,\"free_psram\":%u,\"mic_peak\":%d,"
                     "\"accel\":[%d,%d,%d],\"stack_free\":%d,\"crash_phase\":%d,"
                     "\"alc\":\"%s\",\"blit_ok\":%d,\"blit_fail\":%d,\"boot_btn\":%d,"
                     "\"vocab_ok\":%d,\"vocab_bad\":%d,"
                     "\"int_largest\":%u,\"levels\":\"%s\","
                     "\"blit_fail_total\":%d,\"blit_recov\":%d,\"meter_fail\":%d,"
                     "\"wifi_reason\":%d,\"wifi_drops\":%d,"
                     "\"ota_err\":\"%s\",\"ota_tries\":%d,\"restart_why\":\"%s\","
                     "\"tap\":[%d,%d,%d],\"panel_reset\":%s,\"screen\":\"%s\","
                     "\"pmu_history\":[",
                     ota_running_version(),
                     (unsigned long long)(esp_timer_get_time() / 1000), reason,
                     (unsigned)esp_get_free_heap_size(),
                     (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
                     display_mic_peak(), have_imu ? ax : 0, have_imu ? ay : 0,
                     have_imu ? az : 0, display_stack_free(), display_crash_phase(),
                     audio_alc_state(), blit_ok, blit_fail, display_boot_presses(),
                     vocab_ok, vocab_bad,
                     (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL |
                                                               MALLOC_CAP_DMA),
                     audio_levels_state(), blit_fail_total, blit_recov, meter_fail,
                     wifi_reason, wifi_drops, ota_err, ota_tries, display_restart_reason(),
                     tap_x, tap_y, tap_zone,
                     display_panel_reset() ? "true" : "false", display_screen());
    for (int i = 0; i < n && w > 0 && w < (int)sizeof(body) - 32; i++) {
        w += snprintf(body + w, sizeof(body) - (size_t)w, "%s\"%s\"", i ? "," : "", hist[i]);
    }
    if (w > 0 && w < (int)sizeof(body) - 4) w += snprintf(body + w, sizeof(body) - (size_t)w, "]");
    /* The NAMES, not just the count. A number says the panel is deaf to something; a name
       says to what, and only the name can be acted on. */
    if (w > 0 && w < (int)sizeof(body) - 24) {
        w += snprintf(body + w, sizeof(body) - (size_t)w, ",\"vocab_refused\":[");
        for (int i = 0; i < 6 && w > 0 && w < (int)sizeof(body) - 32; i++) {
            const char *bad = speech_vocab_refused(i);
            if (bad == NULL) break;
            w += snprintf(body + w, sizeof(body) - (size_t)w, "%s\"%s\"", i ? "," : "", bad);
        }
        if (w > 0 && w < (int)sizeof(body) - 4) w += snprintf(body + w, sizeof(body) - (size_t)w, "]");
    }
    /* WHAT IT ACTUALLY HEARD, and how sure it was. `speech.c` computes this on every decode
       and its own comment says the confidence floor cannot be chosen until a correct decode's
       score is known on this hardware — a measurement that has been waiting on a console. */
    if (w > 0 && w < (int)sizeof(body) - 32) {
        w += snprintf(body + w, sizeof(body) - (size_t)w, ",\"heard\":[");
        /* TWELVE, and the loop stops on room rather than on a count it assumes will fit — the
           entries are variable-length now (a raw decode is as long as whatever was said) and a
           body that ran out mid-token is the unterminated-JSON failure the comment below is
           about. A fourth field carries how many times the same decode repeated. */
        for (int i = 0; i < 12 && w > 0 && w < (int)sizeof(body) - 64; i++) {
            const char *phrase = NULL;
            int prob = 0;
            int count = 0;
            bool fired = false;
            if (!speech_heard(i, &phrase, &prob, &fired, &count)) break;
            w += snprintf(body + w, sizeof(body) - (size_t)w, "%s[\"%s\",%d,%d,%d]",
                          i ? "," : "", phrase, prob, fired ? 1 : 0, count);
        }
        if (w > 0 && w < (int)sizeof(body) - 4) w += snprintf(body + w, sizeof(body) - (size_t)w, "]");
    }
    /* THE BRACE CLOSES UNCONDITIONALLY, which it did not when this was written. Every
       optional block above is guarded on having room left, and the `}` used to live INSIDE
       the last of them — so a body that ran out of room mid-way was sent as unterminated
       JSON, the box answered 422, and (before the fix in `ota_report`) that 422 counted as a
       successful report and cleared the crash ring. Each block above already reserves more
       room than it can write, so no token is ever cut in half; what was missing was only the
       terminator, and a terminator that depends on room being left is not a terminator. */
    if (w < 0 || w > (int)sizeof(body) - 2) w = (int)sizeof(body) - 2;
    body[w] = '}';
    body[w + 1] = '\0';
    /* Cleared only once it has left the box. Clearing at boot is what made every report say
       `pmu_history: []` while the ring had in fact survived. */
    if (ota_report(cfg, body) == ESP_OK && n > 0) pmu_history_clear();
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
    mem_log("boot");
    if (!display_start()) {
        ESP_LOGE(TAG, "display did not come up — continuing, the box is still reachable");
    } else {
        display_run_face();
    }
    mem_log("display");

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

    /* NO WI-FI IS NOT A REASON TO STOP BEING A ROBOT, and until 0.2.32 it was. This call
       used to sit AFTER the network block's early `return`, so a panel that missed its
       network at boot never started the accelerometer — it drew, it beeped, it answered
       taps, and it would not lean or flip, because `imu_read` was failing silently forever.
       Two symptoms, one cause, and the cause was an ordering nobody chose deliberately.
       The IMU is on I2C and owes the radio nothing, so it goes first. */
    imu_start();
    mem_log("imu");

    /* A FAILED JOIN IS A RETRY, NOT AN EXIT. This used to `return` out of `app_main`, which
       ended the OTA loop with it: a panel that booted while the router was down stayed
       unreachable until someone power-cycled it, on a device whose whole premise is that
       nobody has to touch it. Thirty seconds of bad timing is not a reason to need hands. */
    bool joined = net_connect(&cfg, WIFI_TIMEOUT_MS) == ESP_OK;
    mem_log("wifi");
    if (!joined) {
        /* Still gives up probation: no Wi-Fi means no possible update, which is the one
           thing a pending image must not persist through. On a settled image this is a
           no-op, so the loop below keeps its chance to recover. */
        ESP_LOGE(TAG, "no network — will retry every %d s", OFFLINE_RETRY_MS / 1000);
        ota_confirm_health(false);
    }

    /* BEFORE `ota_confirm_health`, which clears the state when it marks the image good — see
       `ota_boot_is_new_image`. A local, not a flag in memory that survives a reboot: RTC does
       not survive an OTA, which is what 0.2.77 learned the hard way. */
    const bool fresh_image = ota_boot_is_new_image();

    ota_manifest_t manifest;
    bool reachable = false;
    if (joined) {
        reachable = reach_box(&cfg, &manifest);
        ota_confirm_health(reachable);
        if (!reachable) {
            ESP_LOGE(TAG, "on Wi-Fi but the box did not answer; retrying on the next cycle");
        }
    }

    if (reachable) {
        apply_settings(&cfg, NULL, 0);
        report(&cfg);
        /* THE SECOND BOOT AFTER AN UPDATE — `ota.h` explains what it works around and why it
           is a workaround. Placed HERE and nowhere earlier: `ota_confirm_health` above has
           just marked this image good, and restarting while it is still PENDING_VERIFY would
           make the bootloader roll back to the firmware we came from. After `report` as well,
           so the boot that went dark still gets its telemetry out before we restart it — the
           evidence is worth more than the two seconds. */
        if (fresh_image) {
            ESP_LOGW(TAG, "post-update boot — restarting once more (display workaround)");
            display_request_restart();
            vTaskDelay(pdMS_TO_TICKS(1500));
            esp_restart();
        }
    }

    bool ears_tried = false;
    /* Shared by both install paths — the manifest one below and the fast poll's `maybe_install`
       — so a failed attempt is one attempt however it was noticed. Zero is "never tried". */
    uint32_t ota_last_try_ms = 0;
    while (true) {
        if (reachable) {
            const char *running = ota_running_version();
            const uint32_t now = (uint32_t)(esp_timer_get_time() / 1000);
            if (strcmp(manifest.version, running) != 0) {
                /* THROUGH THE SAME BACKOFF AS THE POLL, so the two paths cannot between them
                   retry a failing install faster than one of them would alone. */
                if (cadence_retry_due(now, ota_last_try_ms, OTA_RETRY_BACKOFF_MS)) {
                    ESP_LOGI(TAG, "update offered: %s -> %s", running, manifest.version);
                    ota_last_try_ms = (now == 0) ? 1 : now;
                    ota_apply(&cfg, manifest.url); /* reboots on success */
                } else {
                    ESP_LOGW(TAG, "install of %s failed recently — waiting out the backoff",
                             manifest.version);
                }
            } else {
                ESP_LOGI(TAG, "up to date at %s", running);
            }
        }
        /* THE RECOGNISER STARTS LAST, AND THAT ORDERING IS THE FIX FOR 0.2.37.
           It used to start inside the render task, ~1.5 s into boot — which put it ahead of
           `net_connect` at ~3.0 s, so ESP-SR took the internal RAM and the radio got
           ESP_ERR_NO_MEM. Reaching this line means the radio has had its chance, the
           manifest has been fetched over TLS (so mbedTLS has taken and released its
           handshake buffers), and any pending update has already rebooted us. Whatever is
           left is genuinely spare, and `speech_start` still refuses it if it is too little.
           Listening is the LAST thing this panel earns, because being updatable is the
           first. */
        if (!ears_tried) {
            ears_tried = true;
            mem_log("pre-speech");
            if (!speech_start()) ESP_LOGW(TAG, "no recogniser — the panel listens to nobody");
            /* Alongside the recogniser, and after the first successful box contact for the
               same reason: this needs the API base, the token and the CA out of `cfg`, and
               starting it before the network is up would only mean a task blocked on a
               socket that cannot open yet. */
            if (!talk_start(&cfg)) ESP_LOGW(TAG, "no conversation — holds will not upload");
            /* Voice post, for the same reasons in the same place: it needs `cfg`, and its
               own ~30 s poll is a socket that cannot open before the radio is up. A panel
               that starts without it is still a pet — it simply cannot carry messages, which
               is worth a line in the log rather than a refusal to boot.

               A display never starts it at all. The box's roster is `device_role = 'jpet'`,
               so a display asking for messages gets nothing it could ever be sent — this is
               not a feature being withheld but a poll with no possible answer, and skipping
               it saves a task, 6 KB of stack and a request every thirty seconds forever. */
            if (!cfg_is_jpet(&cfg)) {
                ESP_LOGI(TAG, "display — no voice post");
            } else if (!jpanel_start(&cfg)) {
                ESP_LOGW(TAG, "no voice post — messages will not arrive");
            }
            mem_log("post-speech");
        }
        /* Offline panels come back faster than settled ones check for updates: a router
           reboot should cost a minute, not a quarter of an hour. */
        const uint32_t period = joined ? CHECK_PERIOD_MS : OFFLINE_RETRY_MS;
        /* SLICED, SO SETTINGS ARE NOT A PASSENGER ON THE UPDATE CYCLE. The sleep is the same
           length in total — `cadence_slice_ms` never overshoots, so the manifest fetch below
           still lands on its own schedule rather than drifting later every round — but a knob
           the owner moves is picked up within a slice instead of at the end of the period.

           THE FETCH STAYS ON THIS TASK, and that is the whole reason this is a slice loop
           rather than a flag set by the voice-post task. `apply_settings` walks into the
           codec's I2C registers through `audio_set_levels`, and on 2026-09-21 doing that from
           anywhere but here panicked the panel while the render task sat inside
           `esp_codec_dev_read` (see `audio.h`). Same task, same call, only more often.

           Only while joined: offline there is nobody to ask, and the retry below is what
           matters. */
        bool box_answering = joined;
        for (uint32_t left = period; left > 0;) {
            /* Zero once the box stops answering, which sleeps out the remainder in one go —
               see `apply_settings`. That puts an offline panel back on exactly the single-sleep
               behaviour it had before settings got their own cadence. */
            const uint32_t slice = cadence_slice_ms(left, box_answering ? POLL_PERIOD_MS : 0);
            vTaskDelay(pdMS_TO_TICKS(slice));
            left -= slice;
            /* Not on the last slice: the manifest branch below applies settings anyway, and
               asking twice in the same instant is a handshake for nothing. */
            if (box_answering && left > 0) {
                char served[OTA_VERSION_MAX] = "";
                box_answering = apply_settings(&cfg, served, sizeof(served));
                /* Same pass, no extra fetch unless something is actually waiting. Reboots on a
                   successful install, so anything after this line runs only when there was
                   nothing to do or the attempt failed. */
                if (box_answering) maybe_install(&cfg, served, &ota_last_try_ms);
            }
        }
        if (!joined) {
            joined = net_retry(WIFI_TIMEOUT_MS) == ESP_OK;
            if (joined) ESP_LOGI(TAG, "network recovered");
        }
        reachable = joined && ota_fetch_manifest(&cfg, &manifest) == ESP_OK;
        if (reachable) {
            apply_settings(&cfg, NULL, 0);
            report(&cfg);
        }
    }
}

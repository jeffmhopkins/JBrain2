#include "ota.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "display.h"
#include "link.h"
#include "reach.h"

static const char *TAG = "ota";

/* The repo's millisecond idiom, local because `reach.c` takes the clock as a parameter: it is on
   the host suite, where `esp_timer` does not exist. */
static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

bool ota_boot_is_new_image(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(running, &state) != ESP_OK) return false;
    return state == ESP_OTA_IMG_PENDING_VERIFY;
}


#define MANIFEST_MAX 1024
#define HTTP_TIMEOUT_MS 15000
/* How long to let the renderer park before restarting anyway. One frame plus its own 150 ms
   drain is about 200 ms, so a second is generous — and the fallback is not optional: the
   image is already installed and marked bootable at this point, so a render task that has
   died must not be able to strand the panel on the old one. Rebooting late beats not
   rebooting. */
#define PARK_TIMEOUT_MS 1000

/* Who this panel is willing to believe.
 *
 * Exactly one of two answers, and which one is decided at flash time by where the box told
 * the panel to find it. A LAN name (https://jbrain.local) is served by Caddy's INTERNAL CA,
 * which no public bundle contains — so the box hands over its own root and that root is the
 * only thing trusted. A public hostname has an ordinary certificate, so the bundle is the
 * only thing that can validate it.
 *
 * Never both: pinning one root is a stronger guarantee than "any of ~150 CAs", and it is
 * available for free on the LAN path, which is the one a panel in a bedroom should be using.
 */
static void trust(esp_http_client_config_t *hc, const cfg_t *cfg)
{
    if (cfg->ca != NULL && cfg->ca[0] != '\0') {
        hc->cert_pem = cfg->ca;
        return;
    }
    hc->crt_bundle_attach = esp_crt_bundle_attach;
}

static char *bearer(const cfg_t *cfg)
{
    size_t n = strlen(cfg->token) + 8;
    char *v = malloc(n);
    if (v != NULL) snprintf(v, n, "Bearer %s", cfg->token);
    return v;
}

/* esp_https_ota opens its own client, so the credential is attached through this hook rather
   than through the config struct. */
static esp_err_t attach_auth(esp_http_client_handle_t client)
{
    void *value = NULL;
    if (esp_http_client_get_user_data(client, &value) != ESP_OK || value == NULL) return ESP_OK;
    return esp_http_client_set_header(client, "Authorization", (const char *)value);
}

/* WHY THE SETTINGS FETCH LAST FAILED, AND HOW MANY TIMES IT HAS.
 *
 * MEASURED 2026-09-27: Lydian's panel reported `levels` EMPTY at 941 seconds of uptime while
 * Elora's, on older firmware, reported `70/30`. `levels` is what the codec last ACCEPTED, so an
 * empty one means `audio_set_levels` had never run — which means `apply_settings` had never
 * succeeded in a quarter of an hour, on a panel that was otherwise online and carrying
 * messages. The owner saw the same thing from the other end: *"the volume doesn't actually
 * change the volume on the panel."*
 *
 * NOTHING ON THE BOX COULD SEE IT. A fetch that dies on the panel never reaches the box, so its
 * access log shows only the requests that WORKED and the failure is invisible from the one
 * surface the owner has (CLAUDE.md #10). Every settings knob rides this fetch — volume, the
 * appearance, the report-now counter, and the waiting count — so one silent failure mode stalls
 * all of them at once, which is exactly what it did.
 *
 * THE BOOKKEEPING MOVED TO `reach.c` IN 0.3.33 and the reason is worth keeping here, because the
 * version of it that lived in this file was reasonable and did not work. It cleared the reason on
 * success, "so what is reported is the CURRENT state rather than the worst thing that ever
 * happened" — but a report only ever goes out at the end of a cycle, immediately after a fetch
 * that succeeded, so the string was empty in every report that has ever been sent. On 2026-09-29,
 * with the fetch failing for an hour, the panel reported `set_fails: 3, set_err: ""`. The count
 * survived; the name did not. `reach.c` keeps the name and reports its AGE instead. */
static void note_settings_fail(const char *why)
{
    reach_fail(REACH_SETTINGS, why, now_ms());
}

esp_err_t ota_fetch_settings(const cfg_t *cfg, ota_settings_t *out)
{
    (void)cfg;
    /* OVER THE ONE SOCKET (`link.c`), which is the whole fix for 0.3.38: this poll runs every
       three seconds on the main task, and as its own HTTPS session it was the handshake most
       often in flight when a talk turn or a jpanel poll opened another. */
    char body[MANIFEST_MAX];
    const link_req_t req = {.method = "GET",
                            .path = "/endpoint/settings",
                            .timeout_ms = HTTP_TIMEOUT_MS,
                            .buf = (uint8_t *)body,
                            .cap = sizeof(body) - 1};
    const link_res_t res = link_request(&req);
    esp_err_t err = ESP_OK;
    if (res.status < 0) {
        /* THE ONE THAT MATTERS MOST. Everything below this line means the box answered; this
           means the panel could not get a connection up at all — and `res.err` now says why
           (esp-tls code, errno, upgrade status) instead of the bare "connect" of 0.3.38. */
        note_settings_fail(res.err);
        return ESP_FAIL;
    }
    if (res.status != 200) {
        static char code[16];
        snprintf(code, sizeof(code), "http-%d", res.status);
        note_settings_fail(code);
        return ESP_FAIL;
    }
    const int len = (int)res.got;
    if (len <= 0 || res.cut) {
        note_settings_fail(res.cut ? "cut" : "empty-body");
        return ESP_FAIL;
    }
    body[len] = '\0';

    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        note_settings_fail("bad-json");
        return ESP_FAIL;
    }
    /* Each field independently: a box running older code that omits one should still deliver
       the others rather than leaving the panel on every default. */
    const cJSON *v = cJSON_GetObjectItemCaseSensitive(root, "volume");
    const cJSON *g = cJSON_GetObjectItemCaseSensitive(root, "mic_gain_db");
    const cJSON *b = cJSON_GetObjectItemCaseSensitive(root, "brightness");
    const cJSON *d = cJSON_GetObjectItemCaseSensitive(root, "debug_overlay");
    if (cJSON_IsNumber(v)) out->volume = v->valueint;
    if (cJSON_IsNumber(g)) out->mic_gain_db = g->valueint;
    if (cJSON_IsNumber(b)) out->brightness = b->valueint;
    /* A box that predates the column sends no field at all, and the absent case has to mean
       OFF rather than "leave it as it was" — otherwise a panel that once had the overlay on
       keeps it forever and the switch only works in one direction. */
    out->debug_overlay = cJSON_IsTrue(d) ? 1 : 0;
    const cJSON *agc = cJSON_GetObjectItemCaseSensitive(root, "mic_agc");
    out->mic_agc = cJSON_IsTrue(agc) ? 1 : 0;
    const cJSON *dp = cJSON_GetObjectItemCaseSensitive(root, "dim_percent");
    if (cJSON_IsNumber(dp)) out->dim_percent = dp->valueint;
    /* Both absent on a box that predates them, and absent has to leave the firmware's own
       answer standing — unlike `debug_overlay` above, where absent means OFF because that
       switch has to work in both directions. Here the fallbacks are a name the panel already
       answers to and a body it is already wearing, and overriding either from a silent box
       would be a change nobody asked for. */
    const cJSON *fv = cJSON_GetObjectItemCaseSensitive(root, "fw_version");
    if (cJSON_IsString(fv) && fv->valuestring != NULL) {
        snprintf(out->fw_version, sizeof(out->fw_version), "%s", fv->valuestring);
    }
    const cJSON *pn = cJSON_GetObjectItemCaseSensitive(root, "pet_name");
    if (cJSON_IsString(pn) && pn->valuestring != NULL && pn->valuestring[0] != '\0') {
        snprintf(out->pet_name, sizeof(out->pet_name), "%s", pn->valuestring);
    }
    /* Unlike the name above, an EMPTY value here is taken: "" is the box saying it could not
       pronounce this name, and that has to be able to undo a previous answer it could. */
    const cJSON *pp = cJSON_GetObjectItemCaseSensitive(root, "pet_name_phonemes");
    if (cJSON_IsString(pp) && pp->valuestring != NULL) {
        snprintf(out->pet_name_phonemes, sizeof(out->pet_name_phonemes), "%s", pp->valuestring);
    }
    /* Absent means 0 means "nothing asked", which is exactly right for a box that predates
       the column: a panel must not post because an older box said nothing. */
    const cJSON *ts = cJSON_GetObjectItemCaseSensitive(root, "telemetry_seq");
    if (cJSON_IsNumber(ts)) out->telemetry_seq = ts->valueint;
    /* Left at the caller's -1 when the box does not send it — see `ota.h`: absent must not read
       as "nothing is waiting". */
    const cJSON *wt = cJSON_GetObjectItemCaseSensitive(root, "waiting");
    if (cJSON_IsNumber(wt)) out->waiting = wt->valueint;
    const cJSON *fm = cJSON_GetObjectItemCaseSensitive(root, "form");
    if (cJSON_IsString(fm) && fm->valuestring != NULL) {
        out->form = strcmp(fm->valuestring, "robot") == 0 ? 1 : 0;
    }
    reach_ok(REACH_SETTINGS, now_ms());
    cJSON_Delete(root);
    return err;
}

esp_err_t ota_report(const cfg_t *cfg, const char *body)
{
    (void)cfg;
    const link_req_t req = {.method = "POST",
                            .path = "/endpoint/telemetry",
                            .content_type = "application/json",
                            .body = (const uint8_t *)body,
                            .body_len = strlen(body),
                            .timeout_ms = HTTP_TIMEOUT_MS};
    const link_res_t res = link_request(&req);
    if (res.status < 0) {
        ESP_LOGW(TAG, "telemetry unreachable: %s", res.err);
        return ESP_FAIL;
    }
    /* A REJECTED REPORT IS A FAILED REPORT, and this used to call it success. A transport that
       managed to receive ANY status — a 422 from a malformed body, a 401 from a rotated key, a
       500 — is not a report that landed. The caller is `report()`, and what it does on success
       is `pmu_history_clear()`: the crash ring, which exists precisely because a panel in a
       bedroom cannot be asked what happened, must not be wiped on the strength of a report
       nobody accepted. */
    if (res.status != 204) {
        ESP_LOGW(TAG, "telemetry returned HTTP %d", res.status);
        return ESP_FAIL;
    }
    return ESP_OK;
}

esp_err_t ota_fetch_manifest(const cfg_t *cfg, ota_manifest_t *out)
{
    (void)cfg;
    char body[MANIFEST_MAX];
    const link_req_t req = {.method = "GET",
                            .path = "/endpoint/firmware",
                            .timeout_ms = HTTP_TIMEOUT_MS,
                            .buf = (uint8_t *)body,
                            .cap = sizeof(body) - 1};
    const link_res_t res = link_request(&req);
    if (res.status < 0) {
        ESP_LOGW(TAG, "manifest unreachable: %s", res.err);
        return ESP_FAIL;
    }
    if (res.status != 200) {
        ESP_LOGW(TAG, "manifest returned HTTP %d", res.status);
        return ESP_FAIL;
    }
    if (res.got == 0 || res.cut) {
        ESP_LOGW(TAG, "manifest body empty");
        return ESP_FAIL;
    }
    body[res.got] = '\0';

    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        ESP_LOGW(TAG, "manifest is not JSON");
        return ESP_FAIL;
    }
    const cJSON *version = cJSON_GetObjectItemCaseSensitive(root, "version");
    const cJSON *bin = cJSON_GetObjectItemCaseSensitive(root, "url");
    if (!cJSON_IsString(version) || !cJSON_IsString(bin)) {
        ESP_LOGW(TAG, "manifest is missing version or url");
        cJSON_Delete(root);
        return ESP_FAIL;
    }
    strlcpy(out->version, version->valuestring, sizeof(out->version));
    strlcpy(out->url, bin->valuestring, sizeof(out->url));
    cJSON_Delete(root);
    return ESP_OK;
}

const char *ota_running_version(void)
{
    return esp_app_get_description()->version;
}

/* What the last failed install said, and how many have failed since boot. Empty and zero
   when none has. */
static char s_apply_err[28];
static int s_apply_tries;

void ota_apply_faults(const char **err, int *tries)
{
    if (err != NULL) *err = s_apply_err;
    if (tries != NULL) *tries = s_apply_tries;
}

esp_err_t ota_apply(const cfg_t *cfg, const char *url)
{
    ESP_LOGI(TAG, "installing %s", url);
    char *auth = bearer(cfg);
    if (auth == NULL) return ESP_ERR_NO_MEM;

    esp_http_client_config_t hc = {
        .url = url,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .keep_alive_enable = true,
        .user_data = auth,
    };
    trust(&hc, cfg);
    esp_https_ota_config_t oc = {
        .http_config = &hc,
        .http_client_init_cb = attach_auth,
    };
    /* ONE TLS SESSION AT A TIME, the OTA included. The image comes down its own HTTPS session
       (`esp_https_ota` writes app slots and wants its own client), so the socket is closed first
       and its ~40 KB returned, and every other request is held off until this is over. */
    link_suspend();
    esp_err_t err = esp_https_ota(&oc);
    free(auth);
    if (err != ESP_OK) link_resume();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "install failed: %s — staying on the current image", esp_err_to_name(err));
        /* REMEMBERED, because nobody reads the return. `main.c` calls this and discards the
           result — reasonably, since there is nothing it can do — so a panel that CANNOT
           install (a flash write that fails, an image the validator rejects, TLS that will
           not complete on the binary URL, a truncated download) retries every fifteen
           minutes forever while reporting the OLD version. From the box that is
           indistinguishable from "no update was ever offered", which is the one failure this
           entire firmware exists to prevent, and it was the only one with no symptom. */
        snprintf(s_apply_err, sizeof(s_apply_err), "%s", esp_err_to_name(err));
        s_apply_tries++;
        return err;
    }
    /* THROUGH THE RENDERER, NOT FROM HERE, and the black screen after every update is the
       reason. `esp_restart()` on this task cuts a QSPI pixel transfer in half and the CO5300
       keeps the half it got — it is still waiting for the rest of a memory-write when the
       chip comes back, so the next boot's init bytes are swallowed as pixel data and the
       panel never lights. Measured 2026-09-22: the rails were up the whole time (the PMU ring
       through the dark period is byte-identical to a working panel's), the firmware was alive
       and beeping, and `blit_ok` climbed with `blit_fail` at zero — frames going out to a
       controller that was not listening. The reboot GESTURE recovered it every time, and the
       only thing it does differently is leave from inside the render loop with nothing in
       flight. See `display_request_restart`. */
    ESP_LOGI(TAG, "installed; parking the renderer, then rebooting into the new slot");
    display_request_restart();
    vTaskDelay(pdMS_TO_TICKS(PARK_TIMEOUT_MS));
    ESP_LOGW(TAG, "renderer did not park in %d ms — restarting from here", PARK_TIMEOUT_MS);
    esp_restart();
    return ESP_OK;
}

void ota_confirm_health(bool reachable)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(running, &state) != ESP_OK) return;
    if (state != ESP_OTA_IMG_PENDING_VERIFY) return;

    if (reachable) {
        ESP_LOGI(TAG, "reached the box — marking this image good");
        esp_ota_mark_app_valid_cancel_rollback();
        return;
    }

    /* Nothing to fall back to means this is the first flash, where reverting would only
       replace a reachable-nothing image with no image at all. Say so and stay put; the
       cable is already attached at this point in a unit's life. */
    ESP_LOGE(TAG, "could not reach the box — reverting to the previous image");
    esp_err_t err = esp_ota_mark_app_invalid_rollback_and_reboot();
    ESP_LOGE(TAG, "rollback unavailable (%s) — no previous image to return to",
             esp_err_to_name(err));
}

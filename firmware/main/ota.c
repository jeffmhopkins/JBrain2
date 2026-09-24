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
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "display.h"

static const char *TAG = "ota";

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

esp_err_t ota_fetch_settings(const cfg_t *cfg, ota_settings_t *out)
{
    char url[256];
    snprintf(url, sizeof(url), "%s/endpoint/settings", cfg->api);

    char *auth = bearer(cfg);
    if (auth == NULL) return ESP_ERR_NO_MEM;

    esp_http_client_config_t hc = {.url = url, .timeout_ms = HTTP_TIMEOUT_MS};
    trust(&hc, cfg);
    esp_http_client_handle_t client = esp_http_client_init(&hc);
    if (client == NULL) {
        free(auth);
        return ESP_FAIL;
    }
    esp_http_client_set_header(client, "Authorization", auth);

    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) goto done;
    esp_http_client_fetch_headers(client);
    if (esp_http_client_get_status_code(client) != 200) {
        err = ESP_FAIL;
        goto done;
    }

    char body[MANIFEST_MAX];
    const int len = esp_http_client_read_response(client, body, sizeof(body) - 1);
    if (len <= 0) {
        err = ESP_FAIL;
        goto done;
    }
    body[len] = '\0';

    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        err = ESP_FAIL;
        goto done;
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
    const cJSON *pn = cJSON_GetObjectItemCaseSensitive(root, "pet_name");
    if (cJSON_IsString(pn) && pn->valuestring != NULL && pn->valuestring[0] != '\0') {
        snprintf(out->pet_name, sizeof(out->pet_name), "%s", pn->valuestring);
    }
    const cJSON *fm = cJSON_GetObjectItemCaseSensitive(root, "form");
    if (cJSON_IsString(fm) && fm->valuestring != NULL) {
        out->form = strcmp(fm->valuestring, "robot") == 0 ? 1 : 0;
    }
    cJSON_Delete(root);

done:
    esp_http_client_cleanup(client);
    free(auth);
    return err;
}

esp_err_t ota_report(const cfg_t *cfg, const char *body)
{
    char url[256];
    snprintf(url, sizeof(url), "%s/endpoint/telemetry", cfg->api);

    char *auth = bearer(cfg);
    if (auth == NULL) return ESP_ERR_NO_MEM;

    esp_http_client_config_t hc = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .timeout_ms = HTTP_TIMEOUT_MS,
    };
    trust(&hc, cfg);
    esp_http_client_handle_t client = esp_http_client_init(&hc);
    if (client == NULL) {
        free(auth);
        return ESP_FAIL;
    }
    esp_http_client_set_header(client, "Authorization", auth);
    esp_http_client_set_header(client, "Content-Type", "application/json");
    esp_http_client_set_post_field(client, body, (int)strlen(body));

    esp_err_t err = esp_http_client_perform(client);
    if (err == ESP_OK) {
        const int status = esp_http_client_get_status_code(client);
        /* A REJECTED REPORT IS A FAILED REPORT, and this used to call it success.
           `esp_http_client_perform` returns ESP_OK for any status it managed to receive —
           a 422 from a malformed body, a 401 from a rotated key, a 500 — so returning `err`
           told the caller the telemetry had landed when the box had thrown it away. That
           caller is `report()`, and what it does on success is `pmu_history_clear()`: the
           crash ring, which exists precisely because a panel in a bedroom cannot be asked
           what happened, was being wiped on the strength of a report nobody accepted.
           Logged quietly still — a panel that cannot reach the box has a louder problem and
           the manifest poll already reports it — but no longer called OK. */
        if (status != 204) {
            ESP_LOGW(TAG, "telemetry returned HTTP %d", status);
            err = ESP_FAIL;
        }
    } else {
        ESP_LOGW(TAG, "telemetry unreachable: %s", esp_err_to_name(err));
    }
    esp_http_client_cleanup(client);
    free(auth);
    return err;
}

esp_err_t ota_fetch_manifest(const cfg_t *cfg, ota_manifest_t *out)
{
    char url[256];
    snprintf(url, sizeof(url), "%s/endpoint/firmware", cfg->api);

    char *auth = bearer(cfg);
    if (auth == NULL) return ESP_ERR_NO_MEM;

    esp_http_client_config_t hc = {
        .url = url,
        .timeout_ms = HTTP_TIMEOUT_MS,
    };
    trust(&hc, cfg);
    esp_http_client_handle_t client = esp_http_client_init(&hc);
    if (client == NULL) {
        free(auth);
        return ESP_FAIL;
    }
    esp_http_client_set_header(client, "Authorization", auth);

    esp_err_t err = esp_http_client_open(client, 0);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "manifest unreachable: %s", esp_err_to_name(err));
        goto done;
    }
    esp_http_client_fetch_headers(client);

    int status = esp_http_client_get_status_code(client);
    if (status != 200) {
        ESP_LOGW(TAG, "manifest returned HTTP %d", status);
        err = ESP_FAIL;
        goto done;
    }

    char body[MANIFEST_MAX];
    int len = esp_http_client_read_response(client, body, sizeof(body) - 1);
    if (len <= 0) {
        ESP_LOGW(TAG, "manifest body empty");
        err = ESP_FAIL;
        goto done;
    }
    body[len] = '\0';

    cJSON *root = cJSON_Parse(body);
    if (root == NULL) {
        ESP_LOGW(TAG, "manifest is not JSON");
        err = ESP_FAIL;
        goto done;
    }
    const cJSON *version = cJSON_GetObjectItemCaseSensitive(root, "version");
    const cJSON *bin = cJSON_GetObjectItemCaseSensitive(root, "url");
    if (!cJSON_IsString(version) || !cJSON_IsString(bin)) {
        ESP_LOGW(TAG, "manifest is missing version or url");
        cJSON_Delete(root);
        err = ESP_FAIL;
        goto done;
    }
    strlcpy(out->version, version->valuestring, sizeof(out->version));
    strlcpy(out->url, bin->valuestring, sizeof(out->url));
    cJSON_Delete(root);
    err = ESP_OK;

done:
    esp_http_client_close(client);
    esp_http_client_cleanup(client);
    free(auth);
    return err;
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
    esp_err_t err = esp_https_ota(&oc);
    free(auth);
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

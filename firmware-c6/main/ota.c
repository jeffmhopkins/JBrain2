#include "ota.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "box_http.h"
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_http_client.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "time_logic.h"

static const char *TAG = "ota";

#define MANIFEST_MAX 1024
#define HTTP_TIMEOUT_MS 15000

/* The download's half of box_http.c's trust rule: the box's own root, or the public bundle. */
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

esp_err_t ota_fetch_manifest(const cfg_t *cfg, ota_manifest_t *out)
{
    (void)cfg;
    char body[MANIFEST_MAX];
    size_t got = 0;
    box_reply_t reply = {{0}};
    const int status = box_http_call("/endpoint/firmware?board=" OTA_BOARD_ID, NULL, NULL, 0, body,
                                     sizeof(body) - 1, &got, &reply, HTTP_TIMEOUT_MS);
    out->date = 0;
    if (!http_date_parse(reply.date, &out->date)) out->date = 0;
    if (status != 200) {
        ESP_LOGW(TAG, "manifest: HTTP %d", status);
        return ESP_FAIL;
    }
    if (got == 0) {
        ESP_LOGW(TAG, "manifest body empty");
        return ESP_FAIL;
    }
    body[got] = '\0';

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

esp_err_t ota_report(const cfg_t *cfg, const char *body)
{
    (void)cfg;
    const int status = box_http_call("/endpoint/telemetry", "application/json", body, strlen(body),
                                     NULL, 0, NULL, NULL, HTTP_TIMEOUT_MS);
    if (status != 204) {
        ESP_LOGW(TAG, "telemetry: HTTP %d", status);
        return ESP_FAIL;
    }
    return ESP_OK;
}

const char *ota_running_version(void)
{
    return esp_app_get_description()->version;
}

static char s_apply_err[32];
static int s_apply_tries;
static char s_refused[OTA_VERSION_MAX];

void ota_apply_faults(const char **err, int *tries)
{
    if (err != NULL) *err = s_apply_err;
    if (tries != NULL) *tries = s_apply_tries;
}

bool ota_refused(const char *version)
{
    return s_refused[0] != '\0' && strcmp(version, s_refused) == 0;
}

/* esp_https_ota opens its own client, so the credential goes in through this hook. */
static esp_err_t attach_auth(esp_http_client_handle_t client)
{
    void *value = NULL;
    if (esp_http_client_get_user_data(client, &value) != ESP_OK || value == NULL) return ESP_OK;
    return esp_http_client_set_header(client, "Authorization", (const char *)value);
}

static esp_err_t failed(esp_err_t err, const char *why)
{
    snprintf(s_apply_err, sizeof(s_apply_err), "%s", why);
    s_apply_tries++;
    ESP_LOGE(TAG, "install failed: %s — staying on the current image", why);
    return err;
}

esp_err_t ota_apply(const cfg_t *cfg, const char *url, const ota_hooks_t *hooks)
{
    ESP_LOGI(TAG, "installing %s", url);
    /* The download opens its own session; the kept one goes first, so there is never two. */
    box_http_close();
    char *auth = bearer(cfg);
    if (auth == NULL) return failed(ESP_ERR_NO_MEM, "no-mem");

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

    esp_https_ota_handle_t h = NULL;
    esp_err_t err = esp_https_ota_begin(&oc, &h);
    if (err != ESP_OK) {
        free(auth);
        return failed(err, esp_err_to_name(err));
    }

    /* NOT THE S3 PANELS' IMAGE, checked before a byte is written. A box that predates `?board=`
       ignores it and offers `jbrain-endpoint`; the chip-id check in `esp_https_ota_perform` would
       refuse that too, but only after `esp_ota_begin` has started erasing the inactive slot.
       The descriptor is in the first few KB, so this costs one header read and no flash wear. */
    esp_app_desc_t incoming;
    err = esp_https_ota_get_img_desc(h, &incoming);
    if (err != ESP_OK) {
        esp_https_ota_abort(h);
        free(auth);
        return failed(err, esp_err_to_name(err));
    }
    const char *mine = esp_app_get_description()->project_name;
    if (strncmp(incoming.project_name, mine, sizeof(incoming.project_name)) != 0) {
        ESP_LOGW(TAG, "offered '%s' %s, which is not this board's image ('%s') — refusing",
                 incoming.project_name, incoming.version, mine);
        strlcpy(s_refused, incoming.version, sizeof(s_refused));
        esp_https_ota_abort(h);
        free(auth);
        return failed(ESP_ERR_INVALID_VERSION, "wrong-board");
    }

    const int total = esp_https_ota_get_image_size(h);
    int shown = -1;
    while ((err = esp_https_ota_perform(h)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
        const int read = esp_https_ota_get_image_len_read(h);
        const int pct = total > 0 && read > 0 ? (int)((int64_t)read * 100 / total) : 0;
        if (pct != shown && hooks && hooks->progress) hooks->progress((uint8_t)(pct > 100 ? 100 : pct));
        shown = pct;
    }
    if (err == ESP_OK && !esp_https_ota_is_complete_data_received(h)) err = ESP_ERR_INVALID_SIZE;
    if (err != ESP_OK) {
        esp_https_ota_abort(h);
        free(auth);
        return failed(err, esp_err_to_name(err));
    }
    err = esp_https_ota_finish(h);
    free(auth);
    if (err != ESP_OK) return failed(err, esp_err_to_name(err));

    /* A flush is synchronous and a strip is ~2 ms on the wire, so a reset lands between strips at
       worst; the next boot resets the panel through ALDO3 before drawing. The pause lets the
       Firmware card say "Restarting" first. */
    ESP_LOGI(TAG, "installed %s; rebooting into it", incoming.version);
    if (hooks && hooks->restarting) hooks->restarting();
    vTaskDelay(pdMS_TO_TICKS(500));
    esp_restart();
    return ESP_OK;
}

bool ota_confirm_health(bool reachable)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(running, &state) != ESP_OK) return false;
    if (state != ESP_OTA_IMG_PENDING_VERIFY) return false;

    if (reachable) {
        ESP_LOGI(TAG, "reached the box — marking this image good");
        return esp_ota_mark_app_valid_cancel_rollback() == ESP_OK;
    }
    ESP_LOGE(TAG, "could not reach the box — reverting to the previous image");
    const esp_err_t err = esp_ota_mark_app_invalid_rollback_and_reboot();
    /* Only reached with nothing to fall back to, i.e. the first flash, where the cable is
       already attached. */
    ESP_LOGE(TAG, "rollback unavailable (%s)", esp_err_to_name(err));
    return false;
}

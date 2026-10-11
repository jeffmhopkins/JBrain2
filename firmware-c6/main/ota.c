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
#include "esp_system.h"

static const char *TAG = "ota";

#define MANIFEST_MAX 1024
/* The box serves one manifest per board, chosen by the panel asking; absent means the S3
   panels. The image URL it hands back already carries the same id. */
#define BOARD_ID "c6-amoled-216"
#define HTTP_TIMEOUT_MS 15000

/* Exactly one of two trust answers, decided at flash time. A LAN name is served by Caddy's
   internal CA, which no public bundle contains, so the box's own root is the only thing
   trusted; a public hostname has an ordinary certificate and only the bundle can validate it.
   Never both: pinning one root is the stronger guarantee and is free on the LAN path. */
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

/* One request, one TLS session, closed before returning. The S3 panels moved to a shared
   WebSocket (firmware/main/link.c) because talk turns and polls overlapped; this image makes
   one request at a time on one task, so there is nothing to share and the plain HTTPS twin of
   each route is the simpler thing to trust. Returns the HTTP status, or -1 when no response
   arrived at all. */
static int box_call(const cfg_t *cfg, const char *path, const char *body, char *out, size_t cap,
                    size_t *got)
{
    if (got != NULL) *got = 0;
    char url[320];
    if (snprintf(url, sizeof(url), "%s%s", cfg->api, path) >= (int)sizeof(url)) return -1;
    char *auth = bearer(cfg);
    if (auth == NULL) return -1;

    esp_http_client_config_t hc = {
        .url = url,
        .timeout_ms = HTTP_TIMEOUT_MS,
        .method = body != NULL ? HTTP_METHOD_POST : HTTP_METHOD_GET,
    };
    trust(&hc, cfg);
    esp_http_client_handle_t c = esp_http_client_init(&hc);
    if (c == NULL) {
        free(auth);
        return -1;
    }
    esp_http_client_set_header(c, "Authorization", auth);
    if (body != NULL) esp_http_client_set_header(c, "Content-Type", "application/json");

    int status = -1;
    const int len = body != NULL ? (int)strlen(body) : 0;
    esp_err_t err = esp_http_client_open(c, len);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "%s: %s", path, esp_err_to_name(err));
        goto done;
    }
    if (len > 0 && esp_http_client_write(c, body, len) != len) {
        ESP_LOGW(TAG, "%s: short write", path);
        goto done;
    }
    if (esp_http_client_fetch_headers(c) < 0) {
        ESP_LOGW(TAG, "%s: no response headers", path);
        goto done;
    }
    status = esp_http_client_get_status_code(c);
    if (out != NULL && cap > 0) {
        const int n = esp_http_client_read_response(c, out, (int)cap);
        if (n > 0 && got != NULL) *got = (size_t)n;
    }

done:
    esp_http_client_close(c);
    esp_http_client_cleanup(c);
    free(auth);
    return status;
}

esp_err_t ota_fetch_manifest(const cfg_t *cfg, ota_manifest_t *out)
{
    char body[MANIFEST_MAX];
    size_t got = 0;
    const int status =
        box_call(cfg, "/endpoint/firmware?board=" BOARD_ID, NULL, body, sizeof(body) - 1, &got);
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
    const int status = box_call(cfg, "/endpoint/telemetry", body, NULL, 0, NULL);
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

esp_err_t ota_apply(const cfg_t *cfg, const char *url)
{
    ESP_LOGI(TAG, "installing %s", url);
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

    while ((err = esp_https_ota_perform(h)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
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

    /* Nothing is drawing on this image, so unlike the S3 there is no transfer to park first. */
    ESP_LOGI(TAG, "installed %s; rebooting into it", incoming.version);
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
    ESP_LOGE(TAG, "could not reach the box — reverting to the previous image");
    const esp_err_t err = esp_ota_mark_app_invalid_rollback_and_reboot();
    /* Only reached with nothing to fall back to, i.e. the first flash, where the cable is
       already attached. */
    ESP_LOGE(TAG, "rollback unavailable (%s)", esp_err_to_name(err));
}

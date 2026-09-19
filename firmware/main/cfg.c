#include "cfg.h"

#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "nvs_flash.h"

static const char *TAG = "cfg";
static const char *NS = "jbrain";

/* NVS hands back a length first, then the bytes. Wrapped because getting this wrong is how
   a config string ends up unterminated, and an unterminated PEM fails inside mbedTLS with an
   error that says nothing about where it came from. */
static esp_err_t dup_str(nvs_handle_t h, const char *key, char **out, bool required)
{
    size_t len = 0;
    esp_err_t err = nvs_get_str(h, key, NULL, &len);
    if (err != ESP_OK) {
        if (!required && err == ESP_ERR_NVS_NOT_FOUND) {
            *out = NULL;
            return ESP_OK;
        }
        ESP_LOGE(TAG, "missing key '%s': %s", key, esp_err_to_name(err));
        return err;
    }
    char *buf = calloc(1, len + 1);
    if (buf == NULL) return ESP_ERR_NO_MEM;
    err = nvs_get_str(h, key, buf, &len);
    if (err != ESP_OK) {
        free(buf);
        return err;
    }
    buf[len] = '\0';
    *out = buf;
    return ESP_OK;
}

esp_err_t cfg_load(cfg_t *out)
{
    memset(out, 0, sizeof(*out));
    nvs_handle_t h;
    esp_err_t err = nvs_open(NS, NVS_READONLY, &h);
    if (err != ESP_OK) {
        ESP_LOGW(TAG, "namespace '%s' not present — unit is unprovisioned", NS);
        return ESP_ERR_NVS_NOT_FOUND;
    }

    const struct {
        const char *key;
        char **dst;
        bool required;
    } fields[] = {
        {"api", &out->api, true},   {"token", &out->token, true}, {"ca", &out->ca, true},
        {"ssid", &out->ssid, true}, {"pass", &out->pass, true},   {"name", &out->name, false},
    };
    for (size_t i = 0; i < sizeof(fields) / sizeof(fields[0]); i++) {
        err = dup_str(h, fields[i].key, fields[i].dst, fields[i].required);
        if (err != ESP_OK) {
            nvs_close(h);
            cfg_free(out);
            return err;
        }
    }
    nvs_close(h);
    ESP_LOGI(TAG, "provisioned as '%s' against %s", out->name ? out->name : "(unnamed)", out->api);
    return ESP_OK;
}

void cfg_free(cfg_t *c)
{
    free(c->api);
    free(c->token);
    free(c->ca);
    free(c->ssid);
    free(c->pass);
    free(c->name);
    memset(c, 0, sizeof(*c));
}

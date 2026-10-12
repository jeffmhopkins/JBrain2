/* The panel's settings in NVS, namespace `jpanel`, one i32 per key named as svc_settings_name.
 *
 * Read on the main task before the UI starts and written only from the UI task (every setter
 * is on it: the glass and the debug commands both run there), so one task owns the namespace.
 * A failed read or write is counted and reported, never retried in a loop and never fatal: the
 * worst a broken flash page can do is bring a setting back at its default after a restart. */

#include <stdio.h>
#include <string.h>

#include "dbg_json.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "nvs.h"
#include "settings_store.h"
#include "svc_debug.h"
#include "svc_settings.h"
#include "svc_settings_drv.h"

static const char *TAG = "settings";
static const char *NS = "jpanel";

static settings_store_t s_store;
static char s_load[24] = "off";
static uint8_t s_loaded;
static bool s_bri_restored;
static uint32_t s_writes, s_write_errs;
static esp_err_t s_last_err = ESP_OK;
static const settings_kv_t *s_prov;
static unsigned s_prov_n;

int32_t svc_settings_get(setting_t key)
{
    return key < SET__COUNT ? s_store.val[key] : 0;
}

void svc_settings_set(setting_t key, int32_t value)
{
    /* The timestamp is all the debounce needs; uptime is monotonic where wall time is not. */
    settings_store_set(&s_store, key, value, (uint32_t)(esp_timer_get_time() / 1000));
}

void svc_settings_each_provisioning(settings_kv_fn fn, void *ctx)
{
    for (unsigned i = 0; i < s_prov_n; i++)
        if (s_prov[i].value) fn(s_prov[i].key, s_prov[i].value, ctx);
}

void svc_settings_provisioning(const settings_kv_t *kv, unsigned n)
{
    s_prov = kv;
    s_prov_n = n;
    for (unsigned i = 0; i < n; i++)
        if (kv[i].value && dbg_json_key_is_secret(kv[i].key)) dbg_secret_add(kv[i].value);
}

static void prov_kv(const char *key, const char *value, void *ctx)
{
    dbg_json_str((dbg_json_t *)ctx, key, value);
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    for (int i = 0; i < SET__COUNT; i++)
        dbg_json_int(w, svc_settings_name((setting_t)i), s_store.val[i]);
    dbg_json_str(w, "load", s_load);
    dbg_json_int(w, "writes", s_writes);
    dbg_json_int(w, "write_errors", s_write_errs);
    dbg_json_obj_begin(w, "provisioning");
    svc_settings_each_provisioning(prov_kv, w);
    dbg_json_obj_end(w);
}

void svc_settings_init(void)
{
    settings_store_defaults(&s_store);
    svc_debug_register("settings", describe, NULL);
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READONLY, &h);
    if (e == ESP_ERR_NVS_NOT_FOUND) {
        /* Nothing saved yet: every image before 0.0.8, and a panel fresh from a USB flash. */
        snprintf(s_load, sizeof s_load, "fresh");
        return;
    }
    if (e != ESP_OK) {
        snprintf(s_load, sizeof s_load, "defaults:%x", (unsigned)e);
        ESP_LOGW(TAG, "settings unreadable (%s) — defaults this boot", esp_err_to_name(e));
        return;
    }
    for (int k = 0; k < SET__COUNT; k++) {
        int32_t v;
        e = nvs_get_i32(h, svc_settings_name((setting_t)k), &v);
        if (e == ESP_OK) {
            settings_store_loaded(&s_store, (setting_t)k, v);
            s_loaded++;
            if (k == SET_BRIGHTNESS) s_bri_restored = true;
        } else if (e != ESP_ERR_NVS_NOT_FOUND) {
            s_last_err = e;
        }
    }
    nvs_close(h);
    if (s_last_err != ESP_OK)
        snprintf(s_load, sizeof s_load, "partial:%x", (unsigned)s_last_err);
    else
        snprintf(s_load, sizeof s_load, "ok");
    ESP_LOGI(TAG, "%u settings loaded (%s), brightness %ld", s_loaded, s_load,
             (long)s_store.val[SET_BRIGHTNESS]);
}

void svc_settings_service(uint32_t now_ms)
{
    const uint32_t due = settings_store_due(&s_store, now_ms);
    if (!due) return;
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READWRITE, &h);
    if (e != ESP_OK) {
        /* Mark them held anyway: a namespace that will not open is retried at the next change,
           not on every pass of the UI loop. */
        s_write_errs++;
        s_last_err = e;
        for (int k = 0; k < SET__COUNT; k++)
            if (due >> k & 1) settings_store_mark_saved(&s_store, (setting_t)k);
        return;
    }
    for (int k = 0; k < SET__COUNT; k++) {
        if (!(due >> k & 1)) continue;
        e = nvs_set_i32(h, svc_settings_name((setting_t)k), s_store.val[k]);
        if (e == ESP_OK)
            s_writes++;
        else {
            s_write_errs++;
            s_last_err = e;
        }
        settings_store_mark_saved(&s_store, (setting_t)k);
    }
    e = nvs_commit(h);
    if (e != ESP_OK) {
        s_write_errs++;
        s_last_err = e;
    }
    nvs_close(h);
}

void svc_settings_note(char *out, size_t len)
{
    snprintf(out, len, "set=%s:%u bri=%ld/%s set_w=%lu set_err=%lu/%x", s_load, s_loaded,
             (long)s_store.val[SET_BRIGHTNESS], s_bri_restored ? "nvs" : "dflt",
             (unsigned long)s_writes, (unsigned long)s_write_errs, (unsigned)s_last_err);
}

/* The alarms cache: one NVS blob beside the settings, so after a reboot the panel rings the alarms
 * the box last sent before it has answered again — or if it never does — and the switches the box
 * had not heard yet still go. A blob of another shape (an older layout) reads as no cache. */
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "nvs.h"
#include "svc_alarms_drv.h"

static const char *TAG = "alarms";
static const char *NS = "jpanel";
static const char *KEY = "alarms";
#define LAYOUT 1

typedef struct {
    uint8_t layout;
    alarm_cache_t c;
} blob_t;

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

void alarm_port_lock(void)
{
    portENTER_CRITICAL(&s_mux);
}

void alarm_port_unlock(void)
{
    portEXIT_CRITICAL(&s_mux);
}

/* Static, not on the UI task's stack: ~430 bytes the stack has no other use for. */
static blob_t s_blob;

bool alarm_port_load(alarm_cache_t *out)
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) return false;
    size_t len = sizeof s_blob;
    const esp_err_t e = nvs_get_blob(h, KEY, &s_blob, &len);
    nvs_close(h);
    if (e != ESP_OK || len != sizeof s_blob || s_blob.layout != LAYOUT) return false;
    alarm_cache_t *c = &s_blob.c;
    /* Whatever flash holds, the counts are in range and the strings end inside their arrays. */
    if (c->list.n > ALARM_MAX) c->list.n = 0;
    c->list.etag[ALARM_ETAG_BYTES - 1] = '\0';
    for (int i = 0; i < ALARM_MAX; i++) c->list.a[i].label[ALARM_LABEL_BYTES] = '\0';
    *out = *c;
    return true;
}

void alarm_port_save(const alarm_cache_t *in)
{
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READWRITE, &h);
    if (e == ESP_OK) {
        memset(&s_blob, 0, sizeof s_blob);
        s_blob.layout = LAYOUT;
        s_blob.c = *in;
        e = nvs_set_blob(h, KEY, &s_blob, sizeof s_blob);
        if (e == ESP_OK) e = nvs_commit(h);
        nvs_close(h);
    }
    if (e != ESP_OK) ESP_LOGW(TAG, "cache not saved: %s", esp_err_to_name(e));
}

/* The voice post service's port on the device. The lock: the main task hands requests, answers
 * and the pickup's tag to the UI task through a few small copies, so a critical section is
 * enough. The stamps: one small NVS blob beside the siblings' cache, so a "played" the box has
 * not confirmed survives a reboot; a blob of another shape reads as none. */
#include <string.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "nvs.h"
#include "svc_post_drv.h"

static const char *TAG = "post";
static const char *NS = "jpanel";
static const char *KEY = "post_stamps";
#define LAYOUT 1

typedef struct {
    uint8_t layout;
    post_stamps_t stamps;
} blob_t;

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

void post_port_lock(void)
{
    portENTER_CRITICAL(&s_mux);
}

void post_port_unlock(void)
{
    portEXIT_CRITICAL(&s_mux);
}

bool post_port_load(post_stamps_t *out)
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) return false;
    blob_t b;
    size_t len = sizeof b;
    const esp_err_t e = nvs_get_blob(h, KEY, &b, &len);
    nvs_close(h);
    if (e != ESP_OK || len != sizeof b || b.layout != LAYOUT) return false;
    *out = b.stamps;
    return true;
}

void post_port_save(const post_stamps_t *in)
{
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READWRITE, &h);
    if (e == ESP_OK) {
        blob_t b;
        memset(&b, 0, sizeof b);
        b.layout = LAYOUT;
        b.stamps = *in;
        e = nvs_set_blob(h, KEY, &b, sizeof b);
        if (e == ESP_OK) e = nvs_commit(h);
        nvs_close(h);
    }
    if (e != ESP_OK) ESP_LOGW(TAG, "stamps not saved: %s", esp_err_to_name(e));
}

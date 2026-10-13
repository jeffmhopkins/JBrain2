/* Touch calibration's port on the device (0.0.18): one small NVS blob beside the panel's other
 * keeps (namespace `jpanel`, key `tp_cal`), and the touch task's mapping. A blob of another shape
 * — a later layout, a torn write — reads as none, which is the 0.0.7 mapping: never worse than
 * before calibration existed. */
#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "svc_touch.h"
#include "tp_cal_port.h"

static const char *TAG = "touch";
static const char *NS = "jpanel";
static const char *KEY = "tp_cal";
#define LAYOUT 1

typedef struct {
    uint8_t layout;
    uint8_t pad[3];
    tp_cal_t cal;
    uint32_t date;
} blob_t;

bool tp_cal_port_load(tp_cal_t *cal, uint32_t *date)
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) return false;
    blob_t b;
    size_t len = sizeof b;
    const esp_err_t e = nvs_get_blob(h, KEY, &b, &len);
    nvs_close(h);
    if (e != ESP_OK || len != sizeof b || b.layout != LAYOUT) return false;
    *cal = b.cal;
    *date = b.date;
    return true;
}

bool tp_cal_port_save(const tp_cal_t *cal, uint32_t date)
{
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READWRITE, &h);
    if (e == ESP_OK) {
        if (cal) {
            blob_t b = {.layout = LAYOUT, .cal = *cal, .date = date};
            e = nvs_set_blob(h, KEY, &b, sizeof b);
        } else {
            e = nvs_erase_key(h, KEY);
            if (e == ESP_ERR_NVS_NOT_FOUND) e = ESP_OK;
        }
        if (e == ESP_OK) e = nvs_commit(h);
        nvs_close(h);
    }
    if (e != ESP_OK) ESP_LOGW(TAG, "calibration not kept: %s", esp_err_to_name(e));
    return e == ESP_OK;
}

void tp_cal_port_apply(const tp_cal_t *cal, bool on)
{
    svc_touch_set_cal(cal, on);
}

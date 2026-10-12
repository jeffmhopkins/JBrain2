/* The siblings cache: one small NVS blob beside the settings, so after a reboot the threads show
 * the names the box last sent before it has answered again. A blob of another shape (an older
 * layout) reads as no cache, never as garbage names. */
#include <string.h>

#include "esp_log.h"
#include "nvs.h"
#include "svc_siblings_drv.h"

static const char *TAG = "siblings";
static const char *NS = "jpanel";
static const char *KEY = "siblings";
#define LAYOUT 1

typedef struct {
    uint8_t layout;
    sib_slots_t slots;
} blob_t;

bool sib_port_load(sib_slots_t *out)
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) return false;
    blob_t b;
    size_t len = sizeof b;
    const esp_err_t e = nvs_get_blob(h, KEY, &b, &len);
    nvs_close(h);
    if (e != ESP_OK || len != sizeof b || b.layout != LAYOUT) return false;
    for (int k = 0; k < SIB_MAX; k++) {
        /* Whatever flash holds, the strings end inside their arrays. */
        b.slots.s[k].device_id[SIB_ID_LEN - 1] = '\0';
        b.slots.s[k].name[SIB_NAME_BYTES] = '\0';
    }
    *out = b.slots;
    return true;
}

void sib_port_save(const sib_slots_t *in)
{
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READWRITE, &h);
    if (e == ESP_OK) {
        blob_t b;
        memset(&b, 0, sizeof b);
        b.layout = LAYOUT;
        b.slots = *in;
        e = nvs_set_blob(h, KEY, &b, sizeof b);
        if (e == ESP_OK) e = nvs_commit(h);
        nvs_close(h);
    }
    if (e != ESP_OK) ESP_LOGW(TAG, "cache not saved: %s", esp_err_to_name(e));
}

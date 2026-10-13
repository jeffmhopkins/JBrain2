/* The notice service's port on the device. The files: littlefs on the `storage` partition
 * (partitions.csv; never mounted before 0.0.19, so its first mount formats it), at /notice. The
 * lock: the main and UI tasks hand each other a few small copies, so a critical section is
 * enough. The owed POSTs: one small NVS blob beside the voice post's stamps. */
#include <string.h>

#include "esp_heap_caps.h"
#include "esp_littlefs.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "notice_port.h"
#include "nvs.h"

static const char *TAG = "notice";
static const char *BASE = "/notice";
static const char *LABEL = "storage";
static const char *NS = "jpanel";
static const char *KEY = "notice_posts";
#define LAYOUT 1

typedef struct {
    uint8_t layout;
    notice_posts_t posts;
} blob_t;

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;

void notice_port_lock(void)
{
    portENTER_CRITICAL(&s_mux);
}

void notice_port_unlock(void)
{
    portEXIT_CRITICAL(&s_mux);
}

bool notice_port_mount(uint32_t *heap_cost)
{
    const size_t before = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    const esp_vfs_littlefs_conf_t conf = {
        .base_path = BASE,
        .partition_label = LABEL,
        /* The partition has never held a filesystem: formatting writes two metadata blocks, not
           the 3.75 MB (lfs_format erases only what it writes). */
        .format_if_mount_failed = true,
    };
    const esp_err_t e = esp_vfs_littlefs_register(&conf);
    const size_t after = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    *heap_cost = before > after ? (uint32_t)(before - after) : 0;
    if (e != ESP_OK) {
        ESP_LOGE(TAG, "storage did not mount: %s", esp_err_to_name(e));
        return false;
    }
    ESP_LOGI(TAG, "storage mounted at %s (%lu B of internal heap)", BASE, (unsigned long)*heap_cost);
    return true;
}

const char *notice_port_base(void)
{
    return BASE;
}

bool notice_port_fs_info(uint32_t *total, uint32_t *used)
{
    size_t t = 0, u = 0;
    if (esp_littlefs_info(LABEL, &t, &u) != ESP_OK) return false;
    *total = (uint32_t)t;
    *used = (uint32_t)u;
    return true;
}

bool notice_port_load(notice_posts_t *out)
{
    nvs_handle_t h;
    if (nvs_open(NS, NVS_READONLY, &h) != ESP_OK) return false;
    blob_t b;
    size_t len = sizeof b;
    const esp_err_t e = nvs_get_blob(h, KEY, &b, &len);
    nvs_close(h);
    if (e != ESP_OK || len != sizeof b || b.layout != LAYOUT) return false;
    *out = b.posts;
    return true;
}

void notice_port_save(const notice_posts_t *in)
{
    nvs_handle_t h;
    esp_err_t e = nvs_open(NS, NVS_READWRITE, &h);
    if (e == ESP_OK) {
        blob_t b;
        memset(&b, 0, sizeof b);
        b.layout = LAYOUT;
        b.posts = *in;
        e = nvs_set_blob(h, KEY, &b, sizeof b);
        if (e == ESP_OK) e = nvs_commit(h);
        nvs_close(h);
    }
    if (e != ESP_OK) ESP_LOGW(TAG, "owed states not saved: %s", esp_err_to_name(e));
}

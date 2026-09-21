#include "mem.h"

#include "esp_heap_caps.h"
#include "esp_log.h"

static const char *TAG = "mem";

unsigned mem_free_internal(void)
{
    return (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
}

void mem_log(const char *stage)
{
    ESP_LOGI(TAG, "%-14s internal %6u free / %6u largest | psram %7u free / %7u largest",
             stage ? stage : "?", (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
             (unsigned)heap_caps_get_free_size(MALLOC_CAP_SPIRAM),
             (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM));
}

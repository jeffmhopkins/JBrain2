/* The "system" describer: what the S3 panels report in telemetry, available on demand. On the
 * host it says so and nothing else. */
#include <string.h>

#include "dbg_core.h"

#ifdef ESP_PLATFORM
#include "esp_attr.h"
#include "esp_core_dump.h"
#include "esp_heap_caps.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#define WATCHED_MAX 8

/* Survives the soft reset a panic performs, so the boot after a crash can say where the UI
 * task was (the S3's breadcrumbs, firmware/README.md "The panic"). */
static RTC_NOINIT_ATTR uint32_t s_crumb;
static RTC_NOINIT_ATTR uint32_t s_crumb_magic;
static uint32_t s_crumb_at_boot;
static TaskHandle_t s_tasks[WATCHED_MAX];
static unsigned s_ntasks;

void dbg_sys_watch_task(void *handle)
{
    if (s_ntasks < WATCHED_MAX) s_tasks[s_ntasks++] = handle;
}

void dbg_sys_crumb(uint32_t stage)
{
    s_crumb = stage;
    s_crumb_magic = 0xC6B0071Eu;
}

static const char *reset_name(esp_reset_reason_t r)
{
    switch (r) {
    case ESP_RST_POWERON: return "poweron";
    case ESP_RST_SW: return "sw";
    case ESP_RST_PANIC: return "panic";
    case ESP_RST_INT_WDT: return "int_wdt";
    case ESP_RST_TASK_WDT: return "task_wdt";
    case ESP_RST_WDT: return "wdt";
    case ESP_RST_BROWNOUT: return "brownout";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    default: return "other";
    }
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_str(w, "platform", "esp32c6");
    dbg_json_int(w, "uptime_ms", esp_timer_get_time() / 1000);
    dbg_json_str(w, "reset_reason", reset_name(esp_reset_reason()));
    dbg_json_int(w, "crumb_at_boot", s_crumb_at_boot);
#if CONFIG_ESP_COREDUMP_ENABLE_TO_FLASH
    dbg_json_bool(w, "coredump", esp_core_dump_image_check() == ESP_OK);
#endif
    dbg_json_int(w, "int_free", heap_caps_get_free_size(MALLOC_CAP_INTERNAL));
    dbg_json_int(w, "int_min", heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL));
    dbg_json_int(w, "int_largest", heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL));
    dbg_json_int(w, "log_evicted", dbg_log_evicted());
    dbg_json_arr_begin(w, "tasks");
    for (unsigned i = 0; i < s_ntasks; i++) {
        dbg_json_obj_begin(w, NULL);
        dbg_json_str(w, "name", pcTaskGetName(s_tasks[i]));
        dbg_json_int(w, "stack_free", uxTaskGetStackHighWaterMark(s_tasks[i]));
        dbg_json_obj_end(w);
    }
    dbg_json_arr_end(w);
}

void dbg_sys_init(void)
{
    s_crumb_at_boot = s_crumb_magic == 0xC6B0071Eu ? s_crumb : 0;
    svc_debug_register("system", describe, NULL);
}

#else

void dbg_sys_watch_task(void *handle)
{
    (void)handle;
}

void dbg_sys_crumb(uint32_t stage)
{
    (void)stage;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_str(w, "platform", "host");
    dbg_json_int(w, "log_evicted", dbg_log_evicted());
}

void dbg_sys_init(void)
{
    svc_debug_register("system", describe, NULL);
}

#endif

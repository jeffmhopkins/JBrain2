/* The "system" describer: what the S3 panels report in telemetry, available on demand. On the
 * host it says so and nothing else. */
#include <stdio.h>
#include <string.h>

#include "dbg_core.h"

bool dbg_low_feed(dbg_low_t *l, uint32_t min_now, const char *doing, bool handshake, bool ring)
{
    if (l->min && min_now >= l->min) return false;
    l->min = min_now;
    snprintf(l->at, sizeof l->at, "%s%s%s", doing && doing[0] ? doing : "boot",
             handshake ? "+handshake" : "", ring ? "+ring" : "");
    return true;
}

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

/* The low-water mark's attribution: read and written by the main and UI tasks under s_low_mux.
   `s_doing` is always a string literal, so the pointer alone crosses. */
static portMUX_TYPE s_low_mux = portMUX_INITIALIZER_UNLOCKED;
static dbg_low_t s_low;
static const char *s_doing = "boot";
static bool s_handshake;
static bool (*s_ring_held)(void);

void dbg_sys_low_probe(bool (*ring_held)(void))
{
    s_ring_held = ring_held;
}

void dbg_sys_low_check(void)
{
    const uint32_t min = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
    const bool ring = s_ring_held && s_ring_held();
    portENTER_CRITICAL(&s_low_mux);
    dbg_low_feed(&s_low, min, s_doing, s_handshake, ring);
    portEXIT_CRITICAL(&s_low_mux);
}

void dbg_sys_doing(const char *what)
{
    /* A low reached during the last piece of work is that work's. */
    dbg_sys_low_check();
    portENTER_CRITICAL(&s_low_mux);
    s_doing = what;
    portEXIT_CRITICAL(&s_low_mux);
}

void dbg_sys_handshake(bool on)
{
    dbg_sys_low_check();
    portENTER_CRITICAL(&s_low_mux);
    s_handshake = on;
    portEXIT_CRITICAL(&s_low_mux);
}

static dbg_low_t low_now(void)
{
    dbg_sys_low_check();
    portENTER_CRITICAL(&s_low_mux);
    const dbg_low_t l = s_low;
    portEXIT_CRITICAL(&s_low_mux);
    return l;
}

void dbg_sys_low_note(char *out, size_t len)
{
    const dbg_low_t l = low_now();
    snprintf(out, len, "low=%lu@%s", (unsigned long)l.min, l.at);
}

void dbg_sys_watch_task(void *handle)
{
    if (s_ntasks < WATCHED_MAX) s_tasks[s_ntasks++] = handle;
}

void dbg_sys_crumb(uint32_t stage)
{
    s_crumb = stage;
    s_crumb_magic = 0xC6B0071Eu;
}

uint32_t dbg_sys_crumb_at_boot(void)
{
    return s_crumb_at_boot;
}

uint32_t dbg_sys_crumb_now(void)
{
    return s_crumb_magic == 0xC6B0071Eu ? s_crumb : 0;
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
    const dbg_low_t low = low_now();
    dbg_json_int(w, "int_min", low.min);
    dbg_json_str(w, "int_min_at", low.at);
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
    /* Zeroed so the next boot reports this one's stage, never an older boot's. */
    dbg_sys_crumb(0);
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

uint32_t dbg_sys_crumb_at_boot(void)
{
    return 0;
}

uint32_t dbg_sys_crumb_now(void)
{
    return 0;
}

void dbg_sys_low_probe(bool (*ring_held)(void))
{
    (void)ring_held;
}

void dbg_sys_doing(const char *what)
{
    (void)what;
}

void dbg_sys_handshake(bool on)
{
    (void)on;
}

void dbg_sys_low_check(void)
{
}

void dbg_sys_low_note(char *out, size_t len)
{
    snprintf(out, len, "low=0@host");
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

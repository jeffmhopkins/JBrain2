/* The UI task: the only task that touches LVGL, the shell, an app or the panel io handle.
 * Services reach it through the shell's queue; brightness through svc_display's record.
 *
 * Priority 1, the update loop's own, so even a UI task that spun forever would get only its
 * round-robin share and the loop that takes the next image would keep running. */

#include "ui_task.h"

#include <stdio.h>

#include "apps.h"
#include "dbg_core.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lvgl.h"
#include "shell.h"
#include "svc_alarms_fake.h"
#include "svc_audio_fake.h"
#include "svc_box_fake.h"
#include "svc_display.h"
#include "svc_display_drv.h"
#include "svc_messages_fake.h"
#include "svc_net_fake.h"
#include "svc_notice_fake.h"
#include "svc_rtc_fake.h"
#include "svc_settings.h"
#include "svc_settings_fake.h"

static const char *TAG = "ui";

/* 0.0.4 reports the high-water mark so the next image can size this from a measurement. */
#define UI_STACK 12288
/* A moderate default: the init table lights the panel at full, and this is where it settles. */
#define BRIGHTNESS_DEFAULT 50
#define HEAP_AFTER_MS 5000

/* Breadcrumb stages (dbg_sys_crumb). svc_display writes 101-109 during the panel's init. */
enum { ST_LVGL = 1, ST_SHELL, ST_PUMP = 10, ST_RENDER, ST_BRIGHTNESS, ST_IDLE, ST_STOPPED };

static TaskHandle_t s_task, s_notify;
static volatile bool s_running, s_dirty;
static volatile uint32_t s_beat_ms;
static const char *s_fail = "";
static uint32_t s_heap_free, s_heap_min;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* The first report goes out while the panel is still starting; this asks for one that shows
   how it came up, rather than leaving that to the next fifteen-minute heartbeat. */
static void poke(void)
{
    s_dirty = true;
    if (s_notify) xTaskNotifyGive(s_notify);
}

static void stop(const char *why)
{
    s_fail = why;
    s_running = false;
    dbg_sys_crumb(ST_STOPPED);
    ESP_LOGE(TAG, "UI stopped (%s); the panel keeps updating headless", why);
    poke();
    for (;;) vTaskDelay(portMAX_DELAY);
}

static void ui_loop(void *arg)
{
    (void)arg;
    dbg_sys_crumb(ST_LVGL);
    lv_init();
    lv_tick_set_cb(now_ms);
    const char *step = NULL;
    if (!svc_display_start(&step)) stop(step);

    dbg_sys_crumb(ST_SHELL);
    svc_settings_init();
    svc_settings_set(SET_BRIGHTNESS, BRIGHTNESS_DEFAULT);
    svc_display_set_brightness(BRIGHTNESS_DEFAULT);
    svc_rtc_init();
    svc_net_init();
    svc_box_init();
    svc_messages_init();
    svc_audio_init();
    svc_alarms_init();
    svc_notice_init();
    char err[96];
    if (!apps_register(NULL) || !shell_start(err, sizeof err)) stop("shell");
    s_running = true;
    ESP_LOGI(TAG, "shell up");

    const uint32_t started = now_ms();
    uint32_t last = started;
    for (;;) {
        const uint32_t now = now_ms();
        /* The fake clock is the shell's uptime and the apps' wall clock until the PCF85063
           driver lands; it advances by real time. */
        svc_rtc_fake_advance_ms(now - last);
        last = now;
        dbg_sys_crumb(ST_PUMP);
        shell_pump();
        dbg_sys_crumb(ST_RENDER);
        uint32_t wait = lv_timer_handler();
        dbg_sys_crumb(ST_BRIGHTNESS);
        svc_display_service();
        if (!svc_display_ok()) stop("flush");
        s_beat_ms = now;
        if (!s_heap_free && (uint32_t)(now - started) >= HEAP_AFTER_MS) {
            s_heap_free = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
            s_heap_min = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL);
            ESP_LOGI(TAG, "internal heap with the UI up: %lu free, %lu low-water",
                     (unsigned long)s_heap_free, (unsigned long)s_heap_min);
            poke();
        }
        dbg_sys_crumb(ST_IDLE);
        /* At least a tick, so the loop can never hold the CPU; at most 50 ms, so a button
           press waits no longer than that for the shell. */
        if (wait < 10) wait = 10;
        if (wait > 50) wait = 50;
        vTaskDelay(pdMS_TO_TICKS(wait));
    }
}

void ui_task_start(TaskHandle_t notify)
{
    s_notify = notify;
    if (xTaskCreate(ui_loop, "ui", UI_STACK, NULL, 1, &s_task) != pdPASS) {
        s_fail = "task";
        return;
    }
    dbg_sys_watch_task(s_task);
}

void ui_task_button(btn_event_t ev)
{
    static const sh_button_t MAP[] = {SH_BTN_KEY_DOWN, SH_BTN_KEY_UP, SH_BTN_BOOT, SH_BTN_PWR};
    if (s_running) shell_button(MAP[ev]);
}

void ui_task_note(char *out, size_t len)
{
    const long age = s_beat_ms ? (long)((now_ms() - s_beat_ms) / 1000) : -1;
    snprintf(out, len, "ui=%s stage=%lu crumb=%lu ui_heap=%lu/%lu ui_stack=%lu ui_age=%ld",
             s_running ? "up" : s_fail[0] ? s_fail : "off", (unsigned long)dbg_sys_crumb_now(),
             (unsigned long)dbg_sys_crumb_at_boot(), (unsigned long)s_heap_free,
             (unsigned long)s_heap_min,
             (unsigned long)(s_task ? uxTaskGetStackHighWaterMark(s_task) : 0), age);
}

bool ui_task_take_dirty(void)
{
    const bool d = s_dirty;
    s_dirty = false;
    return d;
}

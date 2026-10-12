/* The UI task: the only task that touches LVGL, the shell, an app or the panel io handle.
 * Services reach it through the shell's queue; brightness through svc_display's record.
 *
 * Priority 1, the update loop's own, so even a UI task that spun forever would get only its
 * round-robin share and the loop that takes the next image would keep running. */

#include "ui_task.h"

#include <stdio.h>

#include "apps.h"
#include "dbg_core.h"
#include "dbg_link.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lvgl.h"
#include "shell.h"
#include "svc_alarms_drv.h"
#include "svc_audio_drv.h"
#include "svc_convo_drv.h"
#include "svc_display.h"
#include "svc_display_drv.h"
#include "svc_messages_fake.h"
#include "svc_notice_fake.h"
#include "svc_rtc_drv.h"
#include "svc_settings.h"
#include "svc_settings_drv.h"
#include "svc_siblings_drv.h"
#include "svc_touch.h"

static const char *TAG = "ui";

/* 0.0.4 reports the high-water mark so the next image can size this from a measurement. */
#define UI_STACK 12288
#define HEAP_AFTER_MS 5000

/* Breadcrumb stages (dbg_sys_crumb). svc_display writes 101-109 during the panel's init. */
enum { ST_LVGL = 1, ST_SHELL, ST_PUMP = 10, ST_RENDER, ST_BRIGHTNESS, ST_IDLE, ST_STOPPED, ST_DEBUG };

static TaskHandle_t s_task, s_notify;
static volatile bool s_running, s_dirty, s_stopped;
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
    s_stopped = true;
    dbg_sys_crumb(ST_STOPPED);
    ESP_LOGE(TAG, "UI stopped (%s); the panel keeps updating headless", why);
    poke();
    for (;;) vTaskDelay(portMAX_DELAY);
}

/* LVGL's pointer read, on this task: the touch service's queue, one sample at a time. */
static bool touch_feed(int16_t *x, int16_t *y, bool *pressed)
{
    tp_sample_t s;
    if (!svc_touch_next(&s)) return false;
    *x = s.x;
    *y = s.y;
    *pressed = s.ev != TP_EV_UP;
    return true;
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
    /* The saved brightness (svc_settings, loaded before this task started): the init table
       lights the panel at full, and this is where it settles. */
    svc_display_set_brightness((uint8_t)svc_settings_get(SET_BRIGHTNESS));
    svc_messages_init();
    svc_messages_fake_clear();
    /* Before the apps: a thread is drawn with the name the box last sent (the cache). */
    svc_siblings_init();
    svc_convo_init();
    svc_notice_init();
    char err[96];
    shell_set_pointer(touch_feed);
    if (!apps_register(NULL) || !shell_start(err, sizeof err)) stop("shell");
    s_running = true;
    ESP_LOGI(TAG, "shell up");

    const uint32_t started = now_ms();
    for (;;) {
        const uint32_t now = now_ms();
        svc_rtc_poll();
        svc_settings_service(now);
        svc_audio_service();
        svc_siblings_service();
        svc_convo_service();
        /* Before the shell: an alarm due now is raised and taken over in this same pass. */
        svc_alarms_service(now);
        dbg_sys_crumb(ST_PUMP);
        shell_pump();
        dbg_sys_crumb(ST_RENDER);
        uint32_t wait = lv_timer_handler();
        dbg_sys_crumb(ST_BRIGHTNESS);
        svc_display_service();
        if (!svc_display_ok()) stop("flush");
        /* Between frames, after the brightness write: a screenshot re-renders the screen and
           must not find a transfer of the last one in flight. */
        dbg_sys_crumb(ST_DEBUG);
        dbg_link_ui_service();
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
    static const sh_button_t MAP[] = {SH_BTN_KEY_DOWN, SH_BTN_KEY_UP, SH_BTN_BOOT, SH_BTN_PWR,
                                      SH_BTN_BOOT_LONG};
    if (s_running) shell_button(MAP[ev]);
}

bool ui_task_owns_lvgl(void)
{
    return s_task && !s_stopped;
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

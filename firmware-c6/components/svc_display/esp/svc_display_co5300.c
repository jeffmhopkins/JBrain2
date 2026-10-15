/* The CO5300 AMOLED over QSPI, drawn by LVGL in 480×20 strips from the two halves of one
 * internal DMA buffer.
 *
 * Portions (the panel init table `INIT_CMDS`, the bus and IO setup, the ALDO3 reset order and
 * the even-alignment rounding) are ported from Waveshare's board support package
 * `waveshare/esp32_c6_touch_amoled_2_16` v1.0.0, esp32_c6_touch_amoled_2_16.c,
 * Copyright 2026 Waveshare Team, licensed under the Apache License, Version 2.0
 * (http://www.apache.org/licenses/LICENSE-2.0). Changes: the table is unchanged; the reset is
 * driven through svc_power; strips are queued from alternate halves and brightness is applied
 * between frames.
 *
 * ONE STRIP ON THE WIRE WHILE LVGL DRAWS THE NEXT (0.0.25). A strip is queued and the flush
 * returns; the next flush waits for it only when it needs that half back, which is after LVGL
 * has drawn a whole strip: a strip's ~1 ms on the wire is hidden under its drawing. A frame's
 * LAST strip is waited for before the flush returns, so no pixel transfer is ever in flight when
 * the UI task returns from lv_timer_handler. That is the window brightness is written in: on the
 * S3's CO5300 a correctly framed 0x51 sent while a frame was still going out hung the renderer
 * (firmware/main/display.c, QSPI_CMD), so here the two are still ordered by construction.
 */

#include <stdio.h>
#include <string.h>

#include "dbg_core.h"
#include "disp_logic.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_sh8601.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "lvgl.h"
#include "svc_display.h"
#include "svc_display_drv.h"
#include "svc_power_drv.h"

static const char *TAG = "display";

#define W DISP_N
#define H DISP_N
/* 38.4 KB, the README's budget, as two halves. Even rows, as the CO5300 needs every window. */
#define BUF_BYTES (W * DISP_BUF_ROWS * 2)
#define HALF_BYTES (DISP_STRIP_PX * 2)
_Static_assert(2 * HALF_BYTES == BUF_BYTES && !(DISP_STRIP_ROWS & 1),
               "two even-height halves are exactly the strip buffer");
#define PIN_CS 15
#define PIN_SCLK 0
#define PIN_D0 1
#define PIN_D1 2
#define PIN_D2 3
#define PIN_D3 4
/* A strip is ~1 ms on the wire at 40 MHz. This is the stuck-bus verdict, long enough that a
   flash erase holding the SPI interrupt off during an OTA is never mistaken for one. */
#define FLUSH_TIMEOUT_MS 1000
/* The boot frame goes out at the table's full brightness and the default lands this long after:
   a visible step down is the owner's proof, from across the room, that runtime 0x51 writes
   reach this glass. */
#define BRIGHTNESS_HOLD_MS 2000
#define QSPI_WRITE_CMD(reg) ((0x02u << 24) | ((uint32_t)(reg) << 8))

/* The vendor table, byte for byte (HARDWARE.md §2). Page-0x20 registers 0x19/0x1C are opaque
   panel tuning; MADCTL's 0x30 sets bits the CO5300 datasheet calls don't-care. */
static const sh8601_lcd_init_cmd_t INIT_CMDS[] = {
    {0x11, (uint8_t[]){0x00}, 0, 600},
    {0xFE, (uint8_t[]){0x20}, 1, 0},
    {0x19, (uint8_t[]){0x10}, 1, 0},
    {0x1C, (uint8_t[]){0xA0}, 1, 0},
    {0xFE, (uint8_t[]){0x00}, 1, 0},
    {0xC4, (uint8_t[]){0x80}, 1, 0},
    {0x3A, (uint8_t[]){0x55}, 1, 0},
    {0x35, (uint8_t[]){0x00}, 1, 0},
    {0x36, (uint8_t[]){0x30}, 1, 0},
    {0x53, (uint8_t[]){0x20}, 1, 0},
    {0x51, (uint8_t[]){0xFF}, 1, 0},
    {0x63, (uint8_t[]){0xFF}, 1, 0},
    {0x2A, (uint8_t[]){0x00, 0x00, 0x01, 0xDF}, 4, 0},
    {0x2B, (uint8_t[]){0x00, 0x00, 0x01, 0xDF}, 4, 0},
    {0x29, (uint8_t[]){0x00}, 0, 100},
};

/* Init stages, written as the crash breadcrumb (dbg_sys_crumb) so a panic names the step. */
enum {
    ST_ALDO3 = 101,
    ST_SPI,
    ST_IO,
    ST_PANEL,
    ST_RESET,
    ST_INIT,
    ST_ON,
    ST_BUF,
    ST_LVGL,
};

static esp_lcd_panel_io_handle_t s_io;
static esp_lcd_panel_handle_t s_panel;
static SemaphoreHandle_t s_done;
static uint16_t *s_buf;
static char s_state[28] = "off";
static const char *s_lcdrst = "-";
static bool s_ok;

static lv_display_t *s_disp;
static uint32_t s_frames, s_flush_errs, s_last_frame_ms;
static uint64_t s_frame_us;
static uint32_t s_frame_px;
static int64_t s_render_t0;
static uint32_t s_first_frame_ms;
static disp_stat_t s_stat[4]; /* by rotation */

/* Recorded by any task, applied by the UI task. */
static volatile uint8_t s_req_pct = 50;
static volatile bool s_dark, s_pending = true;
static uint8_t s_sent = 0xFF; /* the table's 0x51 */
static uint32_t s_writes;
static esp_err_t s_bri_err = ESP_OK;
static bool s_applied;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* Runs in the SPI ISR. The bus allocates it without ESP_INTR_FLAG_IRAM, so it is held off while
 * an OTA write has the flash cache disabled, and may live in flash.
 *
 * THE YIELD IS OURS TO ASK FOR (0.0.25). esp_lcd's post-transaction hook throws this function's
 * return away, and the SPI ISR yields only for tasks its own queues woke. So the UI task woken
 * here stayed ready behind the idle task, asleep in WFI, until the next interrupt that switches
 * tasks — at worst the 100 Hz tick. Every wait for a DMA cost ~3.4 ms instead of its transfer's
 * 0.2-2 ms: measured on 0.0.23, 65 ms a frame upright (12 waits) and 414 ms on its side (120).
 * portYIELD_FROM_ISR only marks the switch for the ISR's exit, so it is safe from inside the
 * bus's handler, and it switches only when the UI task outranks what was running. */
static bool color_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *e, void *ctx)
{
    (void)io, (void)e, (void)ctx;
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_done, &woken);
    portYIELD_FROM_ISR(woken);
    return woken == pdTRUE;
}

static svc_display_strip_fn s_tap;
static void *s_tap_ctx;

/* Auto-rotate (0.0.18, disp_logic.h): asked for by svc_orient from the UI task, applied by the
   same task between frames. On its side the turned strip goes out of the buffer's second half,
   which LVGL gives up while the panel is turned, so a turn asks the heap for nothing. */
static volatile uint8_t s_rot_req;
static uint8_t s_rot;
static void (*s_rotated)(uint8_t quarters);

/* The one strip queued and not yet waited for, and the frame's time spent waiting. */
static bool s_in_flight;
static uint64_t s_wait_us;

/* Dark, with 0x51 = 0 on the glass: LVGL's invalidation is switched off, so nothing is drawn
   that nobody can see (0.0.25). */
static bool s_frozen;

/* The strip on the wire is done with its half: color_done has fired for it. */
static esp_err_t settle(void)
{
    if (!s_in_flight) return ESP_OK;
    s_in_flight = false;
    const int64_t t0 = esp_timer_get_time();
    const bool got = xSemaphoreTake(s_done, pdMS_TO_TICKS(FLUSH_TIMEOUT_MS)) == pdTRUE;
    s_wait_us += (uint64_t)(esp_timer_get_time() - t0);
    return got ? ESP_OK : ESP_ERR_TIMEOUT;
}

/* One window and its pixels, queued: CASET, RASET and RAMWR are polled (five short
   transactions), the pixels are the bus's queued DMA and color_done marks their end. */
static esp_err_t queue(const disp_win_t *g, const uint16_t *px)
{
    const esp_err_t e =
        esp_lcd_panel_draw_bitmap(s_panel, g->x1, g->y1, g->x2 + 1, g->y2 + 1, px);
    s_in_flight = e == ESP_OK;
    return e;
}

/* Upright and 180° the strip goes out of LVGL's own half, swapped (and reversed) while the other
 * half's strip may still be on the wire. On its side the turned strip is gathered whole into the
 * second half (0.0.25) once the strip before has left it, and goes out as ONE window and one
 * transfer: 0.0.22 streamed it as 8 pieces, each a DMA waited for. */
static esp_err_t send_turned(const lv_area_t *a, uint16_t *px)
{
    const disp_win_t l = {a->x1, a->y1, a->x2, a->y2};
    const uint32_t n = lv_area_get_size(a);
    if (s_rot & 1) {
        const esp_err_t e = settle();
        if (e != ESP_OK) return e;
        uint16_t *turned = s_buf + DISP_STRIP_PX;
        const disp_win_t g = disp_rot_gather(s_rot, W, l, px, turned);
        return queue(&g, turned);
    }
    lv_draw_sw_rgb565_swap(px, n);
    if (s_rot == 2) disp_reverse(px, n);
    const disp_win_t g = disp_rot_area(s_rot, W, l);
    const esp_err_t e = settle();
    return e == ESP_OK ? queue(&g, px) : e;
}

/* lv_display_flush_ready is called at once: upright LVGL draws next into the other half, whose
   strip the next flush waits for before queueing; on its side it draws into the first half
   again, which the strip on the wire was gathered out of. */
static void flush(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
    const bool last = lv_display_flush_is_last(d);
    if (s_ok) {
        if (s_tap) s_tap(s_tap_ctx, a->x1, a->y1, a->x2, a->y2, (const uint16_t *)px);
        const int64_t t0 = esp_timer_get_time();
        esp_err_t e = send_turned(a, (uint16_t *)px);
        if (e == ESP_OK && last) e = settle();
        s_frame_us += (uint64_t)(esp_timer_get_time() - t0);
        s_frame_px += lv_area_get_size(a);
        if (e != ESP_OK) {
            s_flush_errs++;
            s_ok = false;
            snprintf(s_state, sizeof s_state, "failed:flush");
            ESP_LOGE(TAG, "flush %s — the panel is left alone for this boot", esp_err_to_name(e));
        }
    }
    if (last) {
        s_frames++;
        s_last_frame_ms = (uint32_t)(s_frame_us / 1000);
        disp_stat_add(&s_stat[s_rot & 3], (uint32_t)s_frame_us, (uint32_t)s_wait_us,
                      (uint32_t)(esp_timer_get_time() - s_render_t0), s_frame_px >= W * H);
        s_frame_us = 0;
        s_wait_us = 0;
        s_frame_px = 0;
        if (!s_first_frame_ms) s_first_frame_ms = now_ms() | 1;
    }
    lv_display_flush_ready(d);
}

static void rounder(lv_event_t *e)
{
    lv_area_t *a = lv_event_get_param(e);
    disp_area_even(&a->x1, &a->y1, &a->x2, &a->y2, W, H);
}

static void render_start(lv_event_t *e)
{
    (void)e;
    s_render_t0 = esp_timer_get_time();
}

static bool fail(const char **failed_step, const char *step, esp_err_t e)
{
    snprintf(s_state, sizeof s_state, "failed:%s", step);
    ESP_LOGE(TAG, "%s: %s — running headless", step, esp_err_to_name(e));
    *failed_step = step;
    return false;
}

static uint32_t st_full_max(const disp_stat_t *s)
{
    return s->full_max_ms;
}

/* One number per rotation, "0°/90°/180°/270°". */
static const char *rot4(char *out, size_t len, uint32_t (*get)(const disp_stat_t *))
{
    snprintf(out, len, "%lu/%lu/%lu/%lu", (unsigned long)get(&s_stat[0]),
             (unsigned long)get(&s_stat[1]), (unsigned long)get(&s_stat[2]),
             (unsigned long)get(&s_stat[3]));
    return out;
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    dbg_json_str(w, "state", s_state);
    dbg_json_int(w, "brightness", s_req_pct);
    dbg_json_bool(w, "dark", s_dark);
    dbg_json_bool(w, "applied", s_applied);
    dbg_json_int(w, "sent", s_sent);
    dbg_json_int(w, "frames", s_frames);
    dbg_json_int(w, "flush_errors", s_flush_errs);
    dbg_json_int(w, "rotation", s_rot * 90);
    char v[128];
    disp_stat_fmt(v, sizeof v, s_stat);
    dbg_json_str(w, "rot_ms", v);
    dbg_json_str(w, "driver", "co5300");
}

bool svc_display_start(const char **failed_step)
{
    svc_debug_register("display", describe, NULL);
    snprintf(s_state, sizeof s_state, "starting");
    s_done = xSemaphoreCreateBinary();
    if (!s_done) return fail(failed_step, "sem", ESP_ERR_NO_MEM);

    /* The panel's RESET is ALDO3's pull-up. Without the PMIC the line is still high from
       power-up, so the software reset below is the fallback, not a failure. */
    dbg_sys_crumb(ST_ALDO3);
    const esp_err_t rst = svc_power_lcd_reset();
    s_lcdrst = rst == ESP_OK ? "aldo3" : "sw-only";

    dbg_sys_crumb(ST_SPI);
    const spi_bus_config_t bus =
        SH8601_PANEL_BUS_QSPI_CONFIG(PIN_SCLK, PIN_D0, PIN_D1, PIN_D2, PIN_D3, HALF_BYTES);
    esp_err_t e = spi_bus_initialize(SPI2_HOST, &bus, SPI_DMA_CH_AUTO);
    if (e != ESP_OK) return fail(failed_step, "spi", e);

    dbg_sys_crumb(ST_IO);
    const esp_lcd_panel_io_spi_config_t io = SH8601_PANEL_IO_QSPI_CONFIG(PIN_CS, color_done, NULL);
    e = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)SPI2_HOST, &io, &s_io);
    if (e != ESP_OK) return fail(failed_step, "io", e);

    dbg_sys_crumb(ST_PANEL);
    const sh8601_vendor_config_t vendor = {
        .init_cmds = INIT_CMDS,
        .init_cmds_size = sizeof INIT_CMDS / sizeof *INIT_CMDS,
        .flags.use_qspi_interface = 1,
    };
    const esp_lcd_panel_dev_config_t dev = {
        .reset_gpio_num = -1,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = 16,
        .vendor_config = (void *)&vendor,
    };
    e = esp_lcd_new_panel_sh8601(s_io, &dev, &s_panel);
    if (e != ESP_OK) return fail(failed_step, "panel", e);

    dbg_sys_crumb(ST_RESET);
    if ((e = esp_lcd_panel_reset(s_panel)) != ESP_OK) return fail(failed_step, "reset", e);
    dbg_sys_crumb(ST_INIT);
    if ((e = esp_lcd_panel_init(s_panel)) != ESP_OK) return fail(failed_step, "init", e);
    dbg_sys_crumb(ST_ON);
    if ((e = esp_lcd_panel_disp_on_off(s_panel, true)) != ESP_OK)
        return fail(failed_step, "on", e);

    dbg_sys_crumb(ST_BUF);
    s_buf = heap_caps_aligned_alloc(4, BUF_BYTES, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    if (!s_buf) return fail(failed_step, "buf", ESP_ERR_NO_MEM);

    dbg_sys_crumb(ST_LVGL);
    lv_display_t *d = lv_display_create(W, H);
    if (!d) return fail(failed_step, "lvgl", ESP_ERR_NO_MEM);
    s_disp = d;
    lv_display_set_color_format(d, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(d, s_buf, s_buf + DISP_STRIP_PX, HALF_BYTES,
                           LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(d, flush);
    lv_display_add_event_cb(d, rounder, LV_EVENT_INVALIDATE_AREA, NULL);
    lv_display_add_event_cb(d, render_start, LV_EVENT_RENDER_START, NULL);

    s_ok = true;
    snprintf(s_state, sizeof s_state, "ok");
    ESP_LOGI(TAG, "CO5300 up (reset %s), %d-row strips", s_lcdrst, DISP_STRIP_ROWS);
    return true;
}

/* Between frames: the new turn and a whole screen drawn in it. Onto its side LVGL gives up the
   second half, which the turned strips are gathered into; off it, it draws in both again. No
   strip is in flight here, and both halves are the same even height. */
static void apply_rotation(void)
{
    const uint8_t want = s_rot_req;
    if (want == s_rot) return;
    if ((want & 1) != (s_rot & 1))
        lv_display_set_buffers(s_disp, s_buf, (want & 1) ? NULL : s_buf + DISP_STRIP_PX,
                               HALF_BYTES, LV_DISPLAY_RENDER_MODE_PARTIAL);
    s_rot = want;
    lv_obj_invalidate(lv_screen_active());
    /* Touch turns with the glass, not with the request: until this pass taps stay mapped to what
       is showing. */
    if (s_rotated) s_rotated(want);
    ESP_LOGI(TAG, "turned to %u°", (unsigned)want * 90);
}

void svc_display_set_rotation(uint8_t quarters)
{
    s_rot_req = quarters & 3;
}

uint8_t svc_display_rotation(void)
{
    return s_rot;
}

void svc_display_on_rotated(void (*fn)(uint8_t quarters))
{
    s_rotated = fn;
}

/* A dark screen draws nothing (0.0.25). On 0.0.23 the clock face, rebuilt each minute, sent the
 * whole glass ~1,300 times over a dark night (414 ms each on its side). With invalidation off
 * LVGL keeps no dirty area and its refresh timer stays paused, so a dark frame costs nothing.
 * Waking draws the whole screen as it now is while the glass is still at 0, and only then is the
 * brightness written: the light never comes up on the picture from before the dark. */
static void freeze(bool on)
{
    if (on == s_frozen) return;
    s_frozen = on;
    lv_display_enable_invalidation(s_disp, !on);
    if (on) return;
    lv_obj_invalidate(lv_screen_active());
    lv_refr_now(s_disp);
}

void svc_display_service(void)
{
    if (s_ok) apply_rotation();
    if (s_ok && s_frozen && !s_dark) freeze(false);
    if (!s_ok || !s_pending || !s_first_frame_ms) return;
    if ((uint32_t)(now_ms() - s_first_frame_ms) < BRIGHTNESS_HOLD_MS) return;
    s_pending = false;
    const uint8_t level = disp_level(s_req_pct, s_dark);
    s_bri_err = esp_lcd_panel_io_tx_param(s_io, QSPI_WRITE_CMD(0x51), &level, 1);
    s_writes++;
    if (s_bri_err == ESP_OK) s_sent = level;
    /* "Applied" means the owner sent it between frames and the bus took it. Whether the glass
       changed is not readable back over this bus at 40 MHz; the owner's eye is the check. */
    s_applied = s_bri_err == ESP_OK;
    ESP_LOGI(TAG, "brightness %u%%%s -> 0x51=%u: %s", s_req_pct, s_dark ? " (dark)" : "", level,
             esp_err_to_name(s_bri_err));
    /* Only once the bus took the 0: a write that failed may have left the glass lit. */
    if (s_dark && s_applied) freeze(true);
}

bool svc_display_capture(svc_display_strip_fn strip, void *ctx)
{
    lv_display_t *d = lv_display_get_default();
    if (!s_ok || !d) return false;
    s_tap = strip;
    s_tap_ctx = ctx;
    /* A dark panel still answers a screenshot: drawn for the capture alone, at 0. */
    if (s_frozen) lv_display_enable_invalidation(d, true);
    lv_obj_invalidate(lv_screen_active());
    lv_refr_now(d);
    if (s_frozen) lv_display_enable_invalidation(d, false);
    s_tap = NULL;
    return s_ok;
}

bool svc_display_ok(void)
{
    return s_ok;
}

void svc_display_mark_skipped(void)
{
    snprintf(s_state, sizeof s_state, "skipped-after-panic");
}

void svc_display_note(char *out, size_t len)
{
    const char *bri = s_pending ? "pend" : s_bri_err == ESP_OK ? "sent" : "err";
    /* `full_ms` is each rotation's mean whole-glass flush, 0°/90°/180°/270°, and `full_max` its
       worst: 0 for a rotation not shown this boot. */
    char full[48], worst[48];
    snprintf(out, len,
             "display=%s lcdrst=%s frames=%lu flush_ms=%lu flush_err=%lu bri=%u/%u/%s bri_n=%lu "
             "dark=%d rot=%u full_ms=%s full_max=%s",
             s_state, s_lcdrst, (unsigned long)s_frames, (unsigned long)s_last_frame_ms,
             (unsigned long)s_flush_errs, s_req_pct, s_sent, bri, (unsigned long)s_writes,
             s_dark ? 1 : 0, (unsigned)s_rot * 90, rot4(full, sizeof full, disp_stat_full_avg_ms),
             rot4(worst, sizeof worst, st_full_max));
}

/* ---- svc_display.h: any task records, the UI task applies ---- */

void svc_display_set_brightness(uint8_t pct)
{
    if (pct > 100) pct = 100;
    if (pct == s_req_pct) return;
    s_req_pct = pct;
    s_pending = true;
}

uint8_t svc_display_brightness(void)
{
    return s_req_pct;
}

void svc_display_set_dark(bool dark)
{
    if (dark == s_dark) return;
    s_dark = dark;
    s_pending = true;
}

bool svc_display_dark(void)
{
    return s_dark;
}

bool svc_display_applied(void)
{
    return s_applied;
}

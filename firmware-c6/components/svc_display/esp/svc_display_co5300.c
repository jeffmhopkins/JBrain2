/* The CO5300 AMOLED over QSPI, drawn by LVGL in 480×40 strips from one internal DMA buffer.
 *
 * Portions (the panel init table `INIT_CMDS`, the bus and IO setup, the ALDO3 reset order and
 * the even-alignment rounding) are ported from Waveshare's board support package
 * `waveshare/esp32_c6_touch_amoled_2_16` v1.0.0, esp32_c6_touch_amoled_2_16.c,
 * Copyright 2026 Waveshare Team, licensed under the Apache License, Version 2.0
 * (http://www.apache.org/licenses/LICENSE-2.0). Changes: the table is unchanged; the reset is
 * driven through svc_power; flushes are synchronous and brightness is applied between frames.
 *
 * WHY THE FLUSH WAITS. With one buffer LVGL cannot draw the next strip until this one is sent
 * anyway, so waiting costs nothing — and it means no pixel transfer is ever in flight when the
 * UI task returns from lv_timer_handler. That is the window brightness is written in: on the
 * S3's CO5300 a correctly framed 0x51 sent while a frame was still going out hung the renderer
 * (firmware/main/display.c, QSPI_CMD), so here the two are ordered by construction.
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

#define W 480
#define H 480
/* 38.4 KB: the sim's strip and the README's budget. Even, as the CO5300 needs every window. */
#define STRIP_ROWS 40
#define BUF_BYTES (W * STRIP_ROWS * 2)
#define PIN_CS 15
#define PIN_SCLK 0
#define PIN_D0 1
#define PIN_D1 2
#define PIN_D2 3
#define PIN_D3 4
/* A strip is ~2 ms on the wire at 40 MHz. This is the stuck-bus verdict, long enough that a
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

static uint32_t s_frames, s_flush_errs, s_last_frame_ms;
static uint64_t s_frame_us;
static uint32_t s_first_frame_ms;

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
   an OTA write has the flash cache disabled, and may live in flash. */
static bool color_done(esp_lcd_panel_io_handle_t io, esp_lcd_panel_io_event_data_t *e, void *ctx)
{
    (void)io, (void)e, (void)ctx;
    BaseType_t woken = pdFALSE;
    xSemaphoreGiveFromISR(s_done, &woken);
    return woken == pdTRUE;
}

static svc_display_strip_fn s_tap;
static void *s_tap_ctx;

/* Auto-rotate (0.0.18, disp_logic.h): asked for by svc_orient from the UI task, applied by the
   same task between frames. A quarter turn needs the piece buffer, which exists only while the
   panel is on its side: upright or upside down it is never allocated. */
#define PIECE_BYTES (DISP_ROT_PIECE_PX * 2)
static volatile uint8_t s_rot_req;
static uint8_t s_rot;
static uint16_t *s_piece;
static uint32_t s_rot_changes, s_rot_fail;
static void (*s_rotated)(uint8_t quarters);

/* One window to the glass, waited for: the buffer is LVGL's strip or the one piece buffer, and
   neither may be touched again until the DMA has finished with it. */
static esp_err_t send(const disp_win_t *g, const uint16_t *px)
{
    esp_err_t e = esp_lcd_panel_draw_bitmap(s_panel, g->x1, g->y1, g->x2 + 1, g->y2 + 1, px);
    if (e == ESP_OK && xSemaphoreTake(s_done, pdMS_TO_TICKS(FLUSH_TIMEOUT_MS)) != pdTRUE)
        e = ESP_ERR_TIMEOUT;
    return e;
}

static esp_err_t send_turned(const lv_area_t *a, uint16_t *px)
{
    const disp_win_t l = {a->x1, a->y1, a->x2, a->y2};
    if (s_rot == 0) return send(&l, px);
    if (s_rot == 2) {
        disp_reverse(px, lv_area_get_size(a));
        const disp_win_t g = disp_rot_area(2, W, l);
        return send(&g, px);
    }
    const int32_t w = a->x2 - a->x1 + 1, k = disp_rot_piece_rows(a->y2 - a->y1 + 1);
    esp_err_t e = ESP_OK;
    for (int32_t r0 = 0; r0 < w && e == ESP_OK; r0 += k) {
        const disp_win_t g = disp_rot_piece(s_rot, W, l, px, r0, k, s_piece);
        e = send(&g, s_piece);
    }
    return e;
}

static void flush(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
    if (s_ok) {
        if (s_tap) s_tap(s_tap_ctx, a->x1, a->y1, a->x2, a->y2, (const uint16_t *)px);
        const int64_t t0 = esp_timer_get_time();
        lv_draw_sw_rgb565_swap(px, lv_area_get_size(a));
        const esp_err_t e = send_turned(a, (uint16_t *)px);
        s_frame_us += (uint64_t)(esp_timer_get_time() - t0);
        if (e != ESP_OK) {
            s_flush_errs++;
            s_ok = false;
            snprintf(s_state, sizeof s_state, "failed:flush");
            ESP_LOGE(TAG, "flush %s — the panel is left alone for this boot", esp_err_to_name(e));
        }
    }
    if (lv_display_flush_is_last(d)) {
        s_frames++;
        s_last_frame_ms = (uint32_t)(s_frame_us / 1000);
        s_frame_us = 0;
        if (!s_first_frame_ms) s_first_frame_ms = now_ms() | 1;
    }
    lv_display_flush_ready(d);
}

static void rounder(lv_event_t *e)
{
    lv_area_t *a = lv_event_get_param(e);
    disp_area_even(&a->x1, &a->y1, &a->x2, &a->y2, W, H);
}

static bool fail(const char **failed_step, const char *step, esp_err_t e)
{
    snprintf(s_state, sizeof s_state, "failed:%s", step);
    ESP_LOGE(TAG, "%s: %s — running headless", step, esp_err_to_name(e));
    *failed_step = step;
    return false;
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
    dbg_json_int(w, "rot_changes", s_rot_changes);
    dbg_json_int(w, "rot_no_memory", s_rot_fail);
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
        SH8601_PANEL_BUS_QSPI_CONFIG(PIN_SCLK, PIN_D0, PIN_D1, PIN_D2, PIN_D3, BUF_BYTES);
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
    lv_display_set_color_format(d, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(d, s_buf, NULL, BUF_BYTES, LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(d, flush);
    lv_display_add_event_cb(d, rounder, LV_EVENT_INVALIDATE_AREA, NULL);

    s_ok = true;
    snprintf(s_state, sizeof s_state, "ok");
    ESP_LOGI(TAG, "CO5300 up (reset %s), %d-row strips", s_lcdrst, STRIP_ROWS);
    return true;
}

/* Between frames: the new turn and a whole screen drawn in it. With no memory for a quarter
   turn's piece buffer the glass stays as it was and the next pass tries again. */
static void apply_rotation(void)
{
    const uint8_t want = s_rot_req;
    if (want == s_rot) return;
    if ((want & 1) && !s_piece) {
        s_piece = heap_caps_malloc(PIECE_BYTES, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
        if (!s_piece) {
            s_rot_fail++;
            return;
        }
    }
    s_rot = want;
    if (!(want & 1)) {
        heap_caps_free(s_piece);
        s_piece = NULL;
    }
    s_rot_changes++;
    lv_obj_invalidate(lv_screen_active());
    /* Touch turns with the glass, not with the request: a turn refused for memory leaves taps
       mapped to what is showing. */
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

void svc_display_service(void)
{
    if (s_ok) apply_rotation();
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
}

bool svc_display_capture(svc_display_strip_fn strip, void *ctx)
{
    lv_display_t *d = lv_display_get_default();
    if (!s_ok || !d) return false;
    s_tap = strip;
    s_tap_ctx = ctx;
    lv_obj_invalidate(lv_screen_active());
    lv_refr_now(d);
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
    snprintf(out, len,
             "display=%s lcdrst=%s frames=%lu flush_ms=%lu flush_err=%lu bri=%u/%u/%s bri_n=%lu "
             "dark=%d rot=%u",
             s_state, s_lcdrst, (unsigned long)s_frames, (unsigned long)s_last_frame_ms,
             (unsigned long)s_flush_errs, s_req_pct, s_sent, bri, (unsigned long)s_writes,
             s_dark ? 1 : 0, (unsigned)s_rot * 90);
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

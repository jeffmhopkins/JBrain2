/* The CST9220, polled.
 *
 * POLLED, NOT INTERRUPT-DRIVEN. TP_INT is GPIO5, a C6 strapping pin (MTDI), and whether the chip
 * holds it low for a whole touch or pulses it once per report is unmeasured. A poll needs no ISR
 * and cannot miss a pulse; Waveshare's Arduino reader for this board polls the same register the
 * same way. GPIO5 is only an input here — never driven, never pulled — and its level is counted
 * against what the reports say, so a later image can choose the interrupt from a measurement.
 *
 * ONE BUS OWNER. Every transfer goes through svc_power_i2c_xfer, under the lock that keeps the
 * PMIC's read-modify-writes whole; this file never sees the bus handle. A transfer is one
 * write-then-read with a repeated start (the Arduino reader's form), bounded by a 20 ms timeout,
 * and a failed one is not retried on the spot: the next poll is the retry. Five failures in a
 * row pulse TP_RST, which also frees the bus if the chip is the one holding SDA low; three
 * pulses with no good read between them back off to a poll every five seconds, so a dead chip
 * costs the PMIC one bounded wait per five seconds and nothing else. */

#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "svc_power_drv.h"
#include "svc_touch.h"

static const char *TAG = "touch";

#define PIN_RST 11
#define PIN_INT 5
#define TP_ADDR 0x5A
/* Every vendor source runs this chip at 400 kHz on this bus. */
#define TP_HZ 400000
#define XFER_TIMEOUT_MS 20
#define POLL_MS 20
#define LOST_POLL_MS 5000
#define FAILS_BEFORE_RESET 5
#define RESETS_BEFORE_LOST 3
#define PROBE_TRIES 5
#define TAPS_KEPT 4

static svc_power_i2c_t s_dev;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static tp_ring_t s_ring;
/* The mapping (0.0.18): written by the UI task under s_mux, copied by the touch task each poll. */
static tp_xf_t s_xf = {.cal = {TP_Q16, 0, 0, 0, TP_Q16, 0}};
static char s_state[20] = "off";

static struct {
    uint16_t a, b;
    int16_t x, y;
} s_taps[TAPS_KEPT];
static uint32_t s_tap_n, s_errs, s_noack, s_noack_down, s_resets;
static uint32_t s_int_lo_down, s_int_lo_up, s_rd_us_max, s_lat_last, s_lat_max;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* +1 tick: at 100 Hz a 10 ms delay could otherwise end on the very next tick boundary. */
static void sleep_ms(uint32_t ms)
{
    vTaskDelay(pdMS_TO_TICKS(ms) + 1);
}

/* The driver's sequence (low 10 ms, then 50 ms to boot), each step doubled for margin. */
static void pulse_reset(void)
{
    gpio_set_level(PIN_RST, 0);
    sleep_ms(20);
    gpio_set_level(PIN_RST, 1);
    sleep_ms(100);
}

static esp_err_t read_report(uint8_t d[TP_REPORT_LEN])
{
    static const uint8_t REG[2] = {0xD0, 0x00};
    const int64_t t0 = esp_timer_get_time();
    const esp_err_t e = svc_power_i2c_xfer(s_dev, REG, sizeof REG, d, TP_REPORT_LEN,
                                           XFER_TIMEOUT_MS);
    const uint32_t us = (uint32_t)(esp_timer_get_time() - t0);
    if (us > s_rd_us_max) s_rd_us_max = us;
    return e;
}

static void set_state(const char *s)
{
    portENTER_CRITICAL(&s_mux);
    snprintf(s_state, sizeof s_state, "%s", s);
    portEXIT_CRITICAL(&s_mux);
}

static void touch_task(void *arg)
{
    (void)arg;
    tp_track_t trk = {0};
    tp_xf_t xf;
    trk.xf = &xf;
    unsigned fails = 0, resets_unanswered = 0;
    for (;;) {
        portENTER_CRITICAL(&s_mux);
        xf = s_xf;
        portEXIT_CRITICAL(&s_mux);
        uint8_t d[TP_REPORT_LEN];
        tp_report_t r = {TP_NOACK, 0, 0};
        const tp_report_t *rp = NULL;
        if (read_report(d) == ESP_OK) {
            r = tp_decode(d);
            rp = &r;
            fails = 0;
            if (resets_unanswered >= RESETS_BEFORE_LOST) {
                ESP_LOGW(TAG, "CST9220 answering again");
                set_state("ok");
            }
            resets_unanswered = 0;
            if (r.kind == TP_NOACK) {
                s_noack++;
                if (trk.down) s_noack_down++;
            }
            if (gpio_get_level(PIN_INT) == 0) {
                if (r.kind == TP_DOWN) s_int_lo_down++;
                else s_int_lo_up++;
            }
        } else {
            s_errs++;
            fails++;
        }

        const tp_ev_t ev = tp_track(&trk, rp);
        if (ev != TP_EV_NONE) {
            const tp_sample_t s = {trk.x, trk.y, ev, now_ms()};
            portENTER_CRITICAL(&s_mux);
            tp_ring_push(&s_ring, &s);
            if (ev == TP_EV_DOWN) {
                s_taps[s_tap_n % TAPS_KEPT].a = r.raw_a;
                s_taps[s_tap_n % TAPS_KEPT].b = r.raw_b;
                s_taps[s_tap_n % TAPS_KEPT].x = trk.x;
                s_taps[s_tap_n % TAPS_KEPT].y = trk.y;
                s_tap_n++;
            }
            portEXIT_CRITICAL(&s_mux);
        }

        if (fails >= FAILS_BEFORE_RESET) {
            fails = 0;
            s_resets++;
            if (++resets_unanswered == RESETS_BEFORE_LOST) {
                ESP_LOGE(TAG, "CST9220 not answering after %d resets — polling every %d s",
                         RESETS_BEFORE_LOST, LOST_POLL_MS / 1000);
                set_state("failed:lost");
            }
            pulse_reset();
        }
        const bool lost = resets_unanswered >= RESETS_BEFORE_LOST;
        vTaskDelay(pdMS_TO_TICKS(lost ? LOST_POLL_MS : POLL_MS));
    }
}

static bool fail(const char *step)
{
    char s[sizeof s_state];
    snprintf(s, sizeof s, "failed:%s", step);
    set_state(s);
    ESP_LOGE(TAG, "touch %s — the panel runs without it", s);
    return false;
}

bool svc_touch_start(void)
{
    /* Low before it becomes an output, so the reset pulse is the first thing the pin does. */
    gpio_set_level(PIN_RST, 0);
    const gpio_config_t rst = {.pin_bit_mask = 1ULL << PIN_RST, .mode = GPIO_MODE_OUTPUT};
    if (gpio_config(&rst) != ESP_OK) return fail("rst");
    const gpio_config_t irq = {.pin_bit_mask = 1ULL << PIN_INT, .mode = GPIO_MODE_INPUT};
    if (gpio_config(&irq) != ESP_OK) return fail("int");
    pulse_reset();
    if (svc_power_i2c_attach(TP_ADDR, TP_HZ, &s_dev) != ESP_OK) return fail("bus");
    uint8_t d[TP_REPORT_LEN];
    esp_err_t e = ESP_FAIL;
    for (int i = 0; i < PROBE_TRIES && e != ESP_OK; i++) {
        if (i) sleep_ms(50);
        e = read_report(d);
    }
    if (e != ESP_OK) {
        s_errs++;
        return fail("i2c");
    }
    /* Priority 1, the update loop's own: nothing new may be able to starve it. */
    if (xTaskCreate(touch_task, "touch", 3072, NULL, 1, NULL) != pdPASS) return fail("task");
    set_state("ok");
    ESP_LOGI(TAG, "CST9220 answering at 0x%02x", TP_ADDR);
    return true;
}

void svc_touch_set_cal(const tp_cal_t *cal, bool on)
{
    portENTER_CRITICAL(&s_mux);
    s_xf.cal = *cal;
    s_xf.cal_on = on;
    portEXIT_CRITICAL(&s_mux);
}

void svc_touch_set_rotation(uint8_t quarters)
{
    portENTER_CRITICAL(&s_mux);
    s_xf.rot = quarters & 3;
    portEXIT_CRITICAL(&s_mux);
}

bool svc_touch_next(tp_sample_t *out)
{
    portENTER_CRITICAL(&s_mux);
    const bool got = tp_ring_pop(&s_ring, out);
    portEXIT_CRITICAL(&s_mux);
    if (got && out->ev == TP_EV_DOWN) {
        s_lat_last = now_ms() - out->at_ms;
        if (s_lat_last > s_lat_max) s_lat_max = s_lat_last;
    }
    return got;
}

void svc_touch_note(char *out, size_t len)
{
    char state[sizeof s_state], last[80] = "-";
    __typeof__(s_taps) taps;
    portENTER_CRITICAL(&s_mux);
    memcpy(state, s_state, sizeof state);
    memcpy(taps, s_taps, sizeof taps);
    const uint32_t n = s_tap_n, dropped = s_ring.dropped;
    const bool cal = s_xf.cal_on;
    const unsigned rot = s_xf.rot * 90u;
    portEXIT_CRITICAL(&s_mux);
    size_t used = 0;
    for (uint32_t i = n > TAPS_KEPT ? n - TAPS_KEPT : 0; i < n; i++) {
        const int k = snprintf(last + used, sizeof last - used, "%s%u,%u>%d,%d", used ? ";" : "",
                               taps[i % TAPS_KEPT].a, taps[i % TAPS_KEPT].b, taps[i % TAPS_KEPT].x,
                               taps[i % TAPS_KEPT].y);
        if (k > 0 && (size_t)k < sizeof last - used) used += (size_t)k;
    }
    snprintf(out, len,
             "touch=%s taps=%lu tp_last=%s tp_err=%lu tp_rst=%lu tp_noack=%lu/%lu tp_int=%lu/%lu "
             "tp_lat=%lu/%lu tp_rd=%lu tp_drop=%lu cal=%s tp_rot=%u",
             state, (unsigned long)n, last, (unsigned long)s_errs, (unsigned long)s_resets,
             (unsigned long)s_noack_down, (unsigned long)s_noack, (unsigned long)s_int_lo_down,
             (unsigned long)s_int_lo_up, (unsigned long)s_lat_last, (unsigned long)s_lat_max,
             (unsigned long)s_rd_us_max, (unsigned long)dropped, cal ? "custom" : "none", rot);
}

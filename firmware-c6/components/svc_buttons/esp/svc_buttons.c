/* BOOT and KEY are active-low with the board's 10 k pull-ups (measured by the 0.0.3 probe).
 * PWR reaches the chip two ways and 0.0.4 is where the working one is chosen by measurement:
 * GPIO18 through the BSS138 inverter — which did not move in 0.0.3 with no pull set, so its
 * internal pull-up is on now — and the AXP2101's latched press, polled by svc_power. Both feed
 * one merge (btn_logic.c), and telemetry says which has ever fired. */

#include "svc_buttons.h"

#include <stdio.h>
#include <string.h>

#include "btn_logic.h"
#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"

static const char *TAG = "buttons";

#define PIN_BOOT 9
#define PIN_KEY 10
#define PIN_PWR 18
#define POLL_MS 10
/* A held or repeated PWR must not become a report a second: the box sees at most one per this
   window, and the edge log keeps every press for the one that goes out. */
#define NOTIFY_GAP_MS 3000

static btn_sink_fn s_sink;
static TaskHandle_t s_notify;
static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static pwr_merge_t s_merge;
static int s_g18_idle = -1;
static volatile bool s_pwr_pending, s_dirty;
static char s_edges[120];

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

/* Oldest first out: the presses that matter are the ones just made. */
static void note_edge(const char *what)
{
    char e[24];
    snprintf(e, sizeof e, "%s@%lu ", what, (unsigned long)(now_ms() / 100));
    ESP_LOGI(TAG, "%s", e);
    portENTER_CRITICAL(&s_mux);
    const size_t have = strlen(s_edges), add = strlen(e);
    if (have + add >= sizeof s_edges) {
        const size_t drop = have + add - sizeof s_edges + 1;
        memmove(s_edges, s_edges + drop, have - drop + 1);
    }
    strncat(s_edges, e, sizeof s_edges - strlen(s_edges) - 1);
    portEXIT_CRITICAL(&s_mux);
}

static void pwr(pwr_src_t src)
{
    portENTER_CRITICAL(&s_mux);
    const bool fresh = pwr_merge_accept(&s_merge, src, now_ms());
    portEXIT_CRITICAL(&s_mux);
    s_pwr_pending = true;
    if (fresh && s_sink) s_sink(BTN_PWR);
}

void svc_buttons_pek(void)
{
    note_edge("pek");
    pwr(PWR_SRC_PEK);
}

static void buttons_task(void *arg)
{
    (void)arg;
    btn_db_t boot = {0}, key = {0}, g18 = {0};
    btn_hold_t boot_hold = {0};
    uint32_t last_notify = 0;
    for (;;) {
        int d = btn_db_step(&boot, gpio_get_level(PIN_BOOT) == 0);
        if (d) note_edge(d > 0 ? "boot=dn" : "boot=up");
        const btn_hold_ev_t h = btn_hold_step(&boot_hold, d, now_ms());
        if (h == BTN_HOLD_LONG) note_edge("boot=long");
        if (h != BTN_HOLD_NONE && s_sink) s_sink(h == BTN_HOLD_LONG ? BTN_BOOT_LONG : BTN_BOOT);

        d = btn_db_step(&key, gpio_get_level(PIN_KEY) == 0);
        if (d) note_edge(d > 0 ? "key=dn" : "key=up");
        if (d && s_sink) s_sink(d > 0 ? BTN_KEY_DOWN : BTN_KEY_UP);

        /* Pressed is "not the level it idled at", whichever way the inverter turns out to be
           wired: polarity is one of the things this measures. */
        d = btn_db_step(&g18, gpio_get_level(PIN_PWR) != s_g18_idle);
        if (d) note_edge(d > 0 ? "g18=dn" : "g18=up");
        if (d > 0) pwr(PWR_SRC_GPIO18);

        if (s_pwr_pending && (uint32_t)(now_ms() - last_notify) >= NOTIFY_GAP_MS) {
            s_pwr_pending = false;
            last_notify = now_ms();
            s_dirty = true;
            if (s_notify) xTaskNotifyGive(s_notify);
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

void svc_buttons_start(btn_sink_fn sink, TaskHandle_t notify)
{
    s_sink = sink;
    s_notify = notify;
    const gpio_config_t keys = {.pin_bit_mask = (1ULL << PIN_BOOT) | (1ULL << PIN_KEY),
                                .mode = GPIO_MODE_INPUT};
    gpio_config(&keys);
    const gpio_config_t pwr_pin = {.pin_bit_mask = 1ULL << PIN_PWR,
                                   .mode = GPIO_MODE_INPUT,
                                   .pull_up_en = GPIO_PULLUP_ENABLE};
    gpio_config(&pwr_pin);
    vTaskDelay(pdMS_TO_TICKS(20)); /* the pull-up charging the line before it is believed */
    s_g18_idle = gpio_get_level(PIN_PWR);
    ESP_LOGI(TAG, "GPIO18 idles at %d with its pull-up", s_g18_idle);
    /* Priority 1, the update loop's own: nothing new may be able to starve it. */
    xTaskCreate(buttons_task, "buttons", 3072, NULL, 1, NULL);
}

void svc_buttons_note(char *out, size_t len)
{
    char edges[sizeof s_edges];
    portENTER_CRITICAL(&s_mux);
    const uint8_t seen = s_merge.seen;
    memcpy(edges, s_edges, sizeof edges);
    portEXIT_CRITICAL(&s_mux);
    snprintf(out, len, "pwr=%s g18idle=%d edges=%s", pwr_seen_name(seen), s_g18_idle, edges);
}

bool svc_buttons_take_dirty(void)
{
    const bool d = s_dirty;
    s_dirty = false;
    return d;
}

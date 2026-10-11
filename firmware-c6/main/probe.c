/* The 0.0.3 probe: settles on the real board what no document could (HARDWARE.md, "Still to
 * check"): which chips answer on the bus, how the AXP2101 left its rails, and which GPIO each
 * top button is. Read-only by design — it configures pins as plain inputs and only reads
 * registers, so nothing it does can leave the panel dark or unbootable. The drivers that
 * WRITE to these chips come after its answers, not before.
 */

#include "probe.h"

#include <stdio.h>
#include <string.h>

#include "driver/gpio.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/semphr.h"

static const char *TAG = "probe";

#define PIN_SDA 8
#define PIN_SCL 7
#define AXP2101_ADDR 0x34
#define I2C_TIMEOUT_MS 50
#define POLL_MS 20
/* A held or bouncing button must not turn into a report a second: the box sees at most one
   per this window, and the edge log keeps every press for the one that goes out. */
#define NOTIFY_GAP_MS 1500

/* Status, chip id, then every rail's enable and voltage register (AXP2101 datasheet §8):
   enough to read what powers the display, the codecs and the amplifier without asking it. */
static const uint8_t AXP_REGS[] = {0x00, 0x01, 0x03, 0x80, 0x82, 0x90, 0x91, 0x92,
                                   0x93, 0x94, 0x95, 0x96, 0x97, 0x99};
static const int BUTTONS[] = {9, 10, 18};
#define N_BUTTONS (sizeof(BUTTONS) / sizeof(BUTTONS[0]))

static char s_hw[200];
static char s_edges[160];
static int s_level[N_BUTTONS];
static SemaphoreHandle_t s_lock;
static volatile bool s_hw_ready;
static volatile bool s_dirty;
static TaskHandle_t s_notify;

static uint32_t now_ms(void)
{
    return (uint32_t)(esp_timer_get_time() / 1000);
}

static void read_bus(void)
{
    i2c_master_bus_config_t bc = {
        .i2c_port = -1,
        .sda_io_num = PIN_SDA,
        .scl_io_num = PIN_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus;
    if (i2c_new_master_bus(&bc, &bus) != ESP_OK) {
        snprintf(s_hw, sizeof(s_hw), "i2c=busfail");
        return;
    }

    int n = snprintf(s_hw, sizeof(s_hw), "i2c=");
    bool axp = false;
    for (uint16_t a = 0x08; a <= 0x77 && n < (int)sizeof(s_hw) - 4; a++) {
        if (i2c_master_probe(bus, a, I2C_TIMEOUT_MS) == ESP_OK) {
            n += snprintf(s_hw + n, sizeof(s_hw) - n, "%02x,", a);
            if (a == AXP2101_ADDR) axp = true;
        }
    }
    if (n > 0 && s_hw[n - 1] == ',') s_hw[--n] = '\0';

    if (axp) {
        i2c_device_config_t dc = {.dev_addr_length = I2C_ADDR_BIT_LEN_7,
                                  .device_address = AXP2101_ADDR,
                                  .scl_speed_hz = 100000};
        i2c_master_dev_handle_t dev;
        if (i2c_master_bus_add_device(bus, &dc, &dev) == ESP_OK) {
            n += snprintf(s_hw + n, sizeof(s_hw) - n, " axp=");
            for (size_t i = 0; i < sizeof(AXP_REGS) && n < (int)sizeof(s_hw) - 7; i++) {
                uint8_t v = 0;
                const esp_err_t e =
                    i2c_master_transmit_receive(dev, &AXP_REGS[i], 1, &v, 1, I2C_TIMEOUT_MS);
                n += e == ESP_OK
                         ? snprintf(s_hw + n, sizeof(s_hw) - n, "%02x:%02x,", AXP_REGS[i], v)
                         : snprintf(s_hw + n, sizeof(s_hw) - n, "%02x:--,", AXP_REGS[i]);
            }
            if (s_hw[n - 1] == ',') s_hw[--n] = '\0';
            i2c_master_bus_rm_device(dev);
        }
    }
    /* Released, not kept: the C2 power service takes this bus and must be its only owner. */
    i2c_del_master_bus(bus);
    ESP_LOGI(TAG, "%s", s_hw);
}

static void note_edge(int gpio, int level)
{
    char e[24];
    snprintf(e, sizeof(e), "g%d=%d@%lu ", gpio, level, (unsigned long)(now_ms() / 100));
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const size_t have = strlen(s_edges), add = strlen(e);
    if (have + add >= sizeof(s_edges)) {
        /* Oldest first out: the presses that matter are the ones just made. */
        const size_t drop = have + add - sizeof(s_edges) + 1;
        memmove(s_edges, s_edges + drop, have - drop + 1);
    }
    strncat(s_edges, e, sizeof(s_edges) - strlen(s_edges) - 1);
    xSemaphoreGive(s_lock);
}

static void probe_task(void *arg)
{
    (void)arg;
    read_bus();
    s_hw_ready = true;

    /* No pull is set: BOOT and KEY have 10 k pull-ups on the board, and GPIO18's idle level
       through the PWR inverter is one of the things being measured, not assumed. */
    for (size_t i = 0; i < N_BUTTONS; i++) {
        gpio_config_t io = {.pin_bit_mask = 1ULL << BUTTONS[i], .mode = GPIO_MODE_INPUT};
        gpio_config(&io);
        s_level[i] = gpio_get_level(BUTTONS[i]);
    }
    ESP_LOGI(TAG, "idle levels g9=%d g10=%d g18=%d", s_level[0], s_level[1], s_level[2]);

    uint32_t last_notify = 0;
    bool pending = false;
    while (true) {
        for (size_t i = 0; i < N_BUTTONS; i++) {
            const int lv = gpio_get_level(BUTTONS[i]);
            if (lv != s_level[i]) {
                s_level[i] = lv;
                note_edge(BUTTONS[i], lv);
                ESP_LOGI(TAG, "gpio%d -> %d", BUTTONS[i], lv);
                pending = true;
            }
        }
        if (pending && (uint32_t)(now_ms() - last_notify) >= NOTIFY_GAP_MS) {
            pending = false;
            last_notify = now_ms();
            s_dirty = true;
            if (s_notify) xTaskNotifyGive(s_notify);
        }
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

void probe_start(TaskHandle_t notify)
{
    s_notify = notify;
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) return;
    xTaskCreate(probe_task, "probe", 3072, NULL, 2, NULL);
}

void probe_note(char *out, size_t len)
{
    if (!s_lock || !s_hw_ready) {
        snprintf(out, len, "probe=pending");
        return;
    }
    xSemaphoreTake(s_lock, portMAX_DELAY);
    snprintf(out, len, "%s lvl=%d,%d,%d edges=%s", s_hw, s_level[0], s_level[1], s_level[2],
             s_edges);
    xSemaphoreGive(s_lock);
}

bool probe_take_dirty(void)
{
    const bool d = s_dirty;
    s_dirty = false;
    return d;
}

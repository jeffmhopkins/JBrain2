/* The AXP2101 PMIC, and the I2C bus it sits on.
 *
 * Register meanings are from Waveshare's board BSP (esp32_c6_touch_amoled_2_16.c, Apache-2.0,
 * pmu init) and XPowersLib's AXP2101 driver (MIT): read, not copied — only the register numbers
 * and bit positions are used, and axp_logic.c decodes them under the host suite.
 *
 * WHAT IS LEFT ALONE, AND WHY. The 0.0.3 probe read every rail as the chip powers up (HARDWARE.md
 * "Measured on the board"): DCDC1 3.3 V, ALDO1-3 3.3 V, ALDO4 1.8 V, every LDO enabled. Those
 * are already what the BSP programs, and a wrong voltage write is the one mistake that survives
 * an OTA rollback (the PMIC keeps its registers across an ESP reset), so none is rewritten. The
 * VBUS input limit and the charge voltage stay at the chip's defaults too; they are reported
 * instead. ALDO3 is switched only as the panel's reset pulse.
 */

#include <stdio.h>
#include <string.h>

#include "axp_logic.h"
#include "driver/i2c_master.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "svc_debug.h"
#include "svc_power.h"
#include "svc_power_drv.h"

static const char *TAG = "power";

#define PIN_SDA 8
#define PIN_SCL 7
#define AXP_ADDR 0x34
#define AXP_CHIP_ID 0x4A
#define I2C_TIMEOUT_MS 50

#define R_STATUS1 0x00
#define R_STATUS2 0x01
#define R_CHIP_ID 0x03
#define R_VBUS_LIMIT 0x16
#define R_ADC_EN 0x30
#define R_VBAT_H 0x34
#define R_VBAT_L 0x35
#define R_VBUS_H 0x38
#define R_VBUS_L 0x39
#define R_VSYS_H 0x3A
#define R_VSYS_L 0x3B
#define R_IRQ_EN2 0x41
#define R_IRQ_ST2 0x49
#define R_PRECHG 0x61
#define R_ICC 0x62
#define R_ITERM 0x63
#define R_CV 0x64
#define R_BAT_DET 0x68
#define R_LDO_EN 0x90
#define R_GAUGE 0xA4

#define ALDO3_BIT 0x04
/* Battery, VBUS and VSYS voltage channels (bits 0, 2, 3). */
#define ADC_CHANNELS 0x0D
/* The BSP's pre-charge (50 mA) and termination (50 mA) codes, with its masks. The constant
   current is code 9, 300 mA, below the BSP's 500 mA: the cell this panel will carry is not
   chosen yet, and too slow a charge costs time where too fast a one costs the cell. */
#define PRECHG_50MA 2
#define ICC_300MA 9
#define ITERM_50MA 2

#define POLL_MS 50
#define READ_EVERY_POLLS 40

static i2c_master_bus_handle_t s_bus;
static i2c_master_dev_handle_t s_dev;
static SemaphoreHandle_t s_lock;
static svc_power_pek_fn s_on_pek;
/* False on the boot after a fault: nothing here writes the PMIC then. */
static bool s_writable;
/* "absent", "readonly", "ok" or "failed:<reg>" — the one-word answer to "did the PMIC take it". */
static char s_state[16] = "absent";

static portMUX_TYPE s_mux = portMUX_INITIALIZER_UNLOCKED;
static power_info_t s_info;
static uint16_t s_vsys_mv;
static uint32_t s_pek_presses, s_i2c_errs;
static uint8_t s_reported[4][2] = {{R_VBUS_LIMIT}, {R_ADC_EN}, {R_ICC}, {R_CV}};

static esp_err_t rd(uint8_t reg, uint8_t *v)
{
    const esp_err_t e = i2c_master_transmit_receive(s_dev, &reg, 1, v, 1, I2C_TIMEOUT_MS);
    if (e != ESP_OK) s_i2c_errs++;
    return e;
}

static esp_err_t wr(uint8_t reg, uint8_t v)
{
    const uint8_t b[2] = {reg, v};
    const esp_err_t e = i2c_master_transmit(s_dev, b, sizeof b, I2C_TIMEOUT_MS);
    if (e != ESP_OK) s_i2c_errs++;
    return e;
}

/* Read back, because a write the chip ignored is indistinguishable from one it took until
   something downstream misbehaves. */
static esp_err_t update(uint8_t reg, uint8_t mask, uint8_t val)
{
    uint8_t cur, back;
    esp_err_t e = rd(reg, &cur);
    if (e != ESP_OK) return e;
    const uint8_t want = axp_field(cur, mask, val);
    if (want == cur) return ESP_OK;
    if ((e = wr(reg, want)) != ESP_OK) return e;
    if ((e = rd(reg, &back)) != ESP_OK) return e;
    return (back & mask) == (want & mask) ? ESP_OK : ESP_ERR_INVALID_RESPONSE;
}

static void configure(void)
{
    static const struct {
        uint8_t reg, mask, val;
    } STEPS[] = {
        /* Latch the power key's edges and presses; the IRQ pin they would drive is unrouted. */
        {R_IRQ_EN2, AXP_PEK_ALL, AXP_PEK_ALL},
        {R_ADC_EN, ADC_CHANNELS, ADC_CHANNELS},
        {R_BAT_DET, 0x01, 0x01},
        {R_PRECHG, 0x03, PRECHG_50MA},
        {R_ICC, 0x1F, ICC_300MA},
        {R_ITERM, 0x0F, ITERM_50MA},
    };
    xSemaphoreTake(s_lock, portMAX_DELAY);
    bool ok = true;
    for (size_t i = 0; i < sizeof STEPS / sizeof *STEPS && ok; i++) {
        if (update(STEPS[i].reg, STEPS[i].mask, STEPS[i].val) != ESP_OK) {
            snprintf(s_state, sizeof s_state, "failed:%02x", STEPS[i].reg);
            ok = false;
        }
    }
    /* Presses latched before this boot are not this boot's: clear by writing their 1s back. */
    uint8_t st;
    if (ok && rd(R_IRQ_ST2, &st) == ESP_OK && (st & AXP_PEK_ALL)) wr(R_IRQ_ST2, st & AXP_PEK_ALL);
    xSemaphoreGive(s_lock);
    if (ok) snprintf(s_state, sizeof s_state, "ok");
}

static void read_supply(void)
{
    uint8_t s1, s2, bh = 0, bl = 0, vh = 0, vl = 0, sh = 0, sl = 0, pct = 0;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const bool ok = rd(R_STATUS1, &s1) == ESP_OK && rd(R_STATUS2, &s2) == ESP_OK &&
                    rd(R_VBAT_H, &bh) == ESP_OK && rd(R_VBAT_L, &bl) == ESP_OK &&
                    rd(R_VBUS_H, &vh) == ESP_OK && rd(R_VBUS_L, &vl) == ESP_OK &&
                    rd(R_VSYS_H, &sh) == ESP_OK && rd(R_VSYS_L, &sl) == ESP_OK &&
                    rd(R_GAUGE, &pct) == ESP_OK;
    xSemaphoreGive(s_lock);
    if (!ok) return;
    const axp_status_t st = axp_status_decode(s1, s2);
    /* With no cell fitted the battery channel reads noise; with no VBUS, so does that one. */
    power_info_t info = {
        .fitted = st.battery,
        .usb = st.vbus_good,
        .charging = st.charging,
        .pct = st.battery ? (pct > 100 ? 100 : pct) : 0,
        .mv = st.battery ? axp_adc13(bh, bl) : 0,
        .usb_mv = st.vbus_good ? axp_adc14(vh, vl) : 0,
    };
    portENTER_CRITICAL(&s_mux);
    s_info = info;
    s_vsys_mv = axp_adc14(sh, sl);
    portEXIT_CRITICAL(&s_mux);
}

static void poll_key(void)
{
    uint8_t st;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const bool ok = rd(R_IRQ_ST2, &st) == ESP_OK;
    if (ok && (st & AXP_PEK_ALL)) wr(R_IRQ_ST2, st & AXP_PEK_ALL);
    xSemaphoreGive(s_lock);
    if (!ok || !axp_pek_pressed(st)) return;
    s_pek_presses++;
    ESP_LOGI(TAG, "PWR press latched by the PMIC (irq2 %02x)", st);
    if (s_on_pek) s_on_pek();
}

static void power_task(void *arg)
{
    (void)arg;
    for (unsigned n = 0;; n++) {
        if (s_writable) poll_key();
        if (n % READ_EVERY_POLLS == 0) read_supply();
        vTaskDelay(pdMS_TO_TICKS(POLL_MS));
    }
}

static void describe(void *ctx, dbg_json_t *w)
{
    (void)ctx;
    power_info_t p;
    svc_power_info(&p);
    dbg_json_str(w, "pmu", s_state);
    dbg_json_bool(w, "battery", p.fitted);
    dbg_json_int(w, "pct", p.pct);
    dbg_json_int(w, "mv", p.mv);
    dbg_json_bool(w, "usb", p.usb);
    dbg_json_int(w, "usb_mv", p.usb_mv);
    dbg_json_bool(w, "charging", p.charging);
    dbg_json_int(w, "pek_presses", s_pek_presses);
    dbg_json_int(w, "i2c_errors", s_i2c_errs);
    dbg_json_str(w, "driver", "axp2101");
}

esp_err_t svc_power_start(bool want_configure, svc_power_pek_fn on_pek)
{
    s_on_pek = on_pek;
    s_lock = xSemaphoreCreateMutex();
    if (!s_lock) return ESP_ERR_NO_MEM;
    const i2c_master_bus_config_t bc = {
        .i2c_port = -1,
        .sda_io_num = PIN_SDA,
        .scl_io_num = PIN_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    esp_err_t e = i2c_new_master_bus(&bc, &s_bus);
    if (e != ESP_OK) return e;
    const i2c_device_config_t dc = {.dev_addr_length = I2C_ADDR_BIT_LEN_7,
                                    .device_address = AXP_ADDR,
                                    .scl_speed_hz = 100000};
    if ((e = i2c_master_bus_add_device(s_bus, &dc, &s_dev)) != ESP_OK) return e;
    uint8_t id = 0;
    if ((e = rd(R_CHIP_ID, &id)) != ESP_OK || id != AXP_CHIP_ID) {
        ESP_LOGE(TAG, "AXP2101 not answering (id %02x, %s)", id, esp_err_to_name(e));
        snprintf(s_state, sizeof s_state, "absent");
        return e != ESP_OK ? e : ESP_ERR_NOT_FOUND;
    }
    snprintf(s_state, sizeof s_state, "readonly");
    s_writable = want_configure;
    if (s_writable) configure();
    xSemaphoreTake(s_lock, portMAX_DELAY);
    for (size_t i = 0; i < 4; i++) rd(s_reported[i][0], &s_reported[i][1]);
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "AXP2101 %s", s_state);
    read_supply();
    svc_debug_register("power", describe, NULL);
    /* Priority 1, the update loop's own: nothing new may be able to starve it. */
    return xTaskCreate(power_task, "power", 3072, NULL, 1, NULL) == pdPASS ? ESP_OK
                                                                         : ESP_ERR_NO_MEM;
}

esp_err_t svc_power_lcd_reset(void)
{
    if (!s_writable) return ESP_ERR_INVALID_STATE;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    esp_err_t e = update(R_LDO_EN, ALDO3_BIT, 0);
    xSemaphoreGive(s_lock);
    if (e != ESP_OK) return e;
    vTaskDelay(pdMS_TO_TICKS(100));
    /* On again whatever happens: a panel left in reset is dark until the next power cycle. */
    for (int i = 0; i < 3; i++) {
        xSemaphoreTake(s_lock, portMAX_DELAY);
        e = update(R_LDO_EN, ALDO3_BIT, ALDO3_BIT);
        xSemaphoreGive(s_lock);
        if (e == ESP_OK) break;
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    vTaskDelay(pdMS_TO_TICKS(100));
    return e;
}

esp_err_t svc_power_i2c_attach(uint8_t addr, uint32_t scl_hz, svc_power_i2c_t *out)
{
    if (!s_bus || !s_lock) return ESP_ERR_INVALID_STATE;
    const i2c_device_config_t dc = {.dev_addr_length = I2C_ADDR_BIT_LEN_7,
                                    .device_address = addr,
                                    .scl_speed_hz = scl_hz};
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const esp_err_t e = i2c_master_bus_add_device(s_bus, &dc, out);
    xSemaphoreGive(s_lock);
    return e;
}

esp_err_t svc_power_i2c_xfer(svc_power_i2c_t dev, const uint8_t *tx, size_t tx_len, uint8_t *rx,
                             size_t rx_len, int timeout_ms)
{
    if (timeout_ms > I2C_TIMEOUT_MS) timeout_ms = I2C_TIMEOUT_MS;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    const esp_err_t e = rx_len ? i2c_master_transmit_receive(dev, tx, tx_len, rx, rx_len, timeout_ms)
                               : i2c_master_transmit(dev, tx, tx_len, timeout_ms);
    xSemaphoreGive(s_lock);
    return e;
}

void svc_power_info(power_info_t *out)
{
    portENTER_CRITICAL(&s_mux);
    *out = s_info;
    portEXIT_CRITICAL(&s_mux);
}

void svc_power_note(char *out, size_t len)
{
    power_info_t p;
    uint16_t vsys;
    portENTER_CRITICAL(&s_mux);
    p = s_info;
    vsys = s_vsys_mv;
    portEXIT_CRITICAL(&s_mux);
    snprintf(out, len,
             "pmu=%s bat=%u vbus=%u vsys=%u chg=%d pek=%lu i2c_err=%lu "
             "axp=%02x:%02x,%02x:%02x,%02x:%02x,%02x:%02x",
             s_state, p.mv, p.usb_mv, vsys, p.charging ? 1 : 0, (unsigned long)s_pek_presses,
             (unsigned long)s_i2c_errs, s_reported[0][0], s_reported[0][1], s_reported[1][0],
             s_reported[1][1], s_reported[2][0], s_reported[2][1], s_reported[3][0],
             s_reported[3][1]);
}

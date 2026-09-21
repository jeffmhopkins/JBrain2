/* One owner for the board's only I2C bus.
 *
 * This exists because of a bug that had not happened yet. The display's revision probe
 * created a bus and deleted it; touch created its own; the codec would have created a third.
 * Deleting after probing is what kept that working, and it worked by accident — the next
 * driver to need the bus while another held it would have failed with a laconic
 * ESP_ERR_INVALID_STATE, on a device whose only symptom is that one peripheral silently
 * does nothing.
 */

#include "i2c_bus.h"

#include <stdio.h>

#include "esp_log.h"

static const char *TAG = "i2c";

#define I2C_PORT I2C_NUM_0
#define I2C_SDA GPIO_NUM_15
#define I2C_SCL GPIO_NUM_14

static i2c_master_bus_handle_t s_bus;

i2c_master_bus_handle_t i2c_bus_get(void)
{
    if (s_bus != NULL) return s_bus;
    const i2c_master_bus_config_t cfg = {
        .i2c_port = I2C_PORT,
        .sda_io_num = I2C_SDA,
        .scl_io_num = I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    const esp_err_t err = i2c_new_master_bus(&cfg, &s_bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "bus: %s", esp_err_to_name(err));
        s_bus = NULL;
    }
    return s_bus;
}

void i2c_bus_scan(void)
{
    i2c_master_bus_handle_t bus = i2c_bus_get();
    if (bus == NULL) return;
    char found[96];
    int n = 0;
    /* 0x08..0x77 is the addressable range; the reserved ends answer for nobody. */
    for (uint8_t addr = 0x08; addr <= 0x77; addr++) {
        if (i2c_master_probe(bus, addr, 30) != ESP_OK) continue;
        if (n < (int)sizeof(found) - 6) {
            n += snprintf(found + n, sizeof(found) - (size_t)n, " 0x%02x", addr);
        }
    }
    ESP_LOGI(TAG, "i2c devices:%s", n ? found : " none");
}

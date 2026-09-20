/* Touch, as one whole-screen button.
 *
 * NOT a touch driver, deliberately. The design round settled this by measurement: a 20 mm
 * child touch target is 69% x 57% of this panel, so two of them do not fit in either axis
 * and the whole screen is the only target there is (mocks/room-endpoint/README.md). A
 * coordinate is therefore something this surface has no use for — and reading the finger
 * count is one register, where a coordinate would be a component dependency and a rotation
 * convention to get wrong.
 *
 * Long-press is not a gesture at this age either: 4-5 year olds produce ordinary taps
 * lasting up to 4.2 seconds. So this reports the EDGE, never the hold — a finger resting on
 * the screen must not fire again, or the robot changes colour eleven times while he looks
 * at it.
 *
 * The controller is CST820 on the V2 board (CST816-compatible) at 0x15, on the same I2C bus
 * as the PMU, the RTC and the IMU.
 */

#include "touch.h"

#include "driver/i2c_master.h"
#include "esp_log.h"

static const char *TAG = "touch";

#define I2C_PORT I2C_NUM_0
#define I2C_SDA GPIO_NUM_15
#define I2C_SCL GPIO_NUM_14
#define CST_ADDR 0x15
#define REG_FINGERS 0x02

static i2c_master_dev_handle_t s_dev;
static bool s_down;

bool touch_start(void)
{
    const i2c_master_bus_config_t bus_cfg = {
        .i2c_port = I2C_PORT,
        .sda_io_num = I2C_SDA,
        .scl_io_num = I2C_SCL,
        .clk_source = I2C_CLK_SRC_DEFAULT,
        .glitch_ignore_cnt = 7,
        .flags.enable_internal_pullup = true,
    };
    i2c_master_bus_handle_t bus = NULL;
    esp_err_t err = i2c_new_master_bus(&bus_cfg, &bus);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "i2c bus: %s", esp_err_to_name(err));
        return false;
    }
    const i2c_device_config_t dev_cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = CST_ADDR,
        .scl_speed_hz = 400000,
    };
    err = i2c_master_bus_add_device(bus, &dev_cfg, &s_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "add device: %s", esp_err_to_name(err));
        return false;
    }
    ESP_LOGI(TAG, "whole-screen target ready");
    return true;
}

bool touch_tapped(void)
{
    if (s_dev == NULL) return false;
    const uint8_t reg = REG_FINGERS;
    uint8_t fingers = 0;
    if (i2c_master_transmit_receive(s_dev, &reg, 1, &fingers, 1, 50) != ESP_OK) {
        /* A read that fails is not a tap. Saying so out loud every poll would bury the log,
           so this is silent by design — `touch_start` already reported whether it opened. */
        return false;
    }
    const bool down = fingers > 0;
    const bool edge = down && !s_down;
    s_down = down;
    return edge;
}

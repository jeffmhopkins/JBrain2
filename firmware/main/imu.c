#include "imu.h"

#include "esp_log.h"
#include "i2c_bus.h"

static const char *TAG = "imu";

#define IMU_ADDR 0x6b
#define REG_WHO_AM_I 0x00
#define REG_CTRL1 0x02
#define REG_CTRL2 0x03
#define REG_CTRL7 0x08
#define REG_AX_L 0x35

#define WHO_AM_I_QMI8658 0x05

/* CTRL1 bit 6 is address auto-increment, which is what makes the six-byte burst read below
   legal rather than six copies of the same register. */
#define CTRL1_ADDR_AI 0x40
/* +/-4 g and ~59 Hz. The range matters (1 g must sit well inside it, with headroom for the
   knock of being put down) and the rate does not: this is sampled about once a second. */
#define CTRL2_4G_59HZ 0x15
/* Accelerometer on, gyroscope off — see the header. */
#define CTRL7_ACCEL_ONLY 0x01

static i2c_master_dev_handle_t s_dev;

static bool write_reg(uint8_t reg, uint8_t val)
{
    const uint8_t buf[2] = {reg, val};
    return i2c_master_transmit(s_dev, buf, sizeof(buf), 50) == ESP_OK;
}

bool imu_start(void)
{
    i2c_master_bus_handle_t bus = i2c_bus_get();
    if (bus == NULL) return false;
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = IMU_ADDR,
        .scl_speed_hz = 400000,
    };
    if (i2c_master_bus_add_device(bus, &cfg, &s_dev) != ESP_OK) {
        s_dev = NULL;
        return false;
    }

    /* WHO_AM_I first. Something acknowledging at 0x6b is not the same as it being the part
       this code knows how to configure, and writing control registers to whatever else might
       be there is how a working bus stops working. */
    const uint8_t reg = REG_WHO_AM_I;
    uint8_t who = 0;
    if (i2c_master_transmit_receive(s_dev, &reg, 1, &who, 1, 50) != ESP_OK) {
        ESP_LOGW(TAG, "no answer at 0x%02x", IMU_ADDR);
        s_dev = NULL;
        return false;
    }
    if (who != WHO_AM_I_QMI8658) {
        ESP_LOGW(TAG, "who_am_i 0x%02x, expected 0x%02x — not configuring it", who,
                 WHO_AM_I_QMI8658);
        s_dev = NULL;
        return false;
    }

    if (!write_reg(REG_CTRL1, CTRL1_ADDR_AI) || !write_reg(REG_CTRL2, CTRL2_4G_59HZ) ||
        !write_reg(REG_CTRL7, CTRL7_ACCEL_ONLY)) {
        ESP_LOGW(TAG, "configuration failed");
        s_dev = NULL;
        return false;
    }
    ESP_LOGI(TAG, "qmi8658 ready (accel only, +/-4g)");
    return true;
}

bool imu_read(int16_t *ax, int16_t *ay, int16_t *az)
{
    if (s_dev == NULL) return false;
    const uint8_t reg = REG_AX_L;
    uint8_t b[6];
    if (i2c_master_transmit_receive(s_dev, &reg, 1, b, sizeof(b), 50) != ESP_OK) return false;
    /* Little-endian pairs, low byte first. */
    *ax = (int16_t)((uint16_t)b[1] << 8 | b[0]);
    *ay = (int16_t)((uint16_t)b[3] << 8 | b[2]);
    *az = (int16_t)((uint16_t)b[5] << 8 | b[4]);
    return true;
}

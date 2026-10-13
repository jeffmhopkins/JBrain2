/* The QMI8658 accelerometer, for auto-rotate (0.0.18). HARDWARE.md §6: at 0x6B on the shared bus,
 * INT1/INT2 on GPIO16/17 — which are UART0's pins, so the interrupts stay unconfigured and the
 * chip is polled.
 *
 * ONE BUS OWNER. Every transfer goes through svc_power_i2c_xfer, the locked transaction the touch
 * controller and the codecs use; this file never sees the bus handle.
 *
 * Registers are the QMI8658 datasheet's, as Waveshare's demo for this board drives them
 * (02_I2C_QMI8658/qmi8658.cpp, read only): WHO_AM_I 0x00 reads 0x05; CTRL1 0x02 bit 6 is the
 * register auto-increment, bit 5 big-endian output, left clear (little-endian, as every demo
 * reads it); CTRL2 0x03 is the accelerometer's range (bits 6:4) and output rate (3:0); CTRL7 0x08
 * bit 0 enables the accelerometer, bit 1 the gyroscope; AX_L 0x35 starts X, Y, Z, two bytes each,
 * low byte first; RESET 0x60 takes 0xB0. The demos run it at ±8 g and 1 kHz with the gyroscope
 * on; telling which edge is down needs ±2 g (the finest step) at 62.5 Hz and no gyroscope. */
#include <stdio.h>

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "orient_logic.h"
#include "svc_orient_drv.h"
#include "svc_power_drv.h"

static const char *TAG = "orient";

#define QMI_ADDR 0x6B
#define QMI_HZ 400000
#define XFER_MS 20
#define REG_WHO 0x00
#define REG_REV 0x01
#define REG_CTRL1 0x02
#define REG_CTRL2 0x03
#define REG_CTRL7 0x08
#define REG_AX_L 0x35
#define REG_RESET 0x60
#define WHO_QMI8658 0x05
#define CTRL1_AUTO_INC 0x40
#define CTRL2_2G_62HZ 0x07 /* aFS 000 = ±2 g, aODR 0111 = 62.5 Hz */
#define CTRL7_ACCEL 0x01

static svc_power_i2c_t s_dev;
static uint8_t s_who, s_rev;
static bool s_on;

static bool rd(uint8_t reg, uint8_t *out, size_t n)
{
    return svc_power_i2c_xfer(s_dev, &reg, 1, out, n, XFER_MS) == ESP_OK;
}

static bool wr(uint8_t reg, uint8_t v)
{
    const uint8_t b[2] = {reg, v};
    return svc_power_i2c_xfer(s_dev, b, sizeof b, NULL, 0, XFER_MS) == ESP_OK;
}

/* Written, then read back: a chip that took a different value is not the one assumed. */
static bool set(uint8_t reg, uint8_t v)
{
    uint8_t got = 0;
    return wr(reg, v) && rd(reg, &got, 1) && got == v;
}

bool orient_port_start(const char **step)
{
    if (svc_power_i2c_attach(QMI_ADDR, QMI_HZ, &s_dev) != ESP_OK) {
        *step = "bus";
        return false;
    }
    bool answered = false;
    for (int i = 0; i < 3 && !answered; i++) {
        if (i) vTaskDelay(pdMS_TO_TICKS(20));
        answered = rd(REG_WHO, &s_who, 1);
    }
    if (!answered) {
        *step = "i2c";
        return false;
    }
    if (s_who != WHO_QMI8658) {
        ESP_LOGE(TAG, "0x%02x answers WHO_AM_I 0x%02x, not a QMI8658", QMI_ADDR, s_who);
        *step = "id";
        return false;
    }
    rd(REG_REV, &s_rev, 1);
    /* A reset first, so a warm reboot finds the chip as a cold one does. */
    wr(REG_RESET, 0xB0);
    vTaskDelay(pdMS_TO_TICKS(20));
    if (!set(REG_CTRL1, CTRL1_AUTO_INC) || !set(REG_CTRL2, CTRL2_2G_62HZ) || !set(REG_CTRL7, 0)) {
        *step = "cfg";
        return false;
    }
    ESP_LOGI(TAG, "QMI8658 answering at 0x%02x (rev 0x%02x), accelerometer ±2 g", QMI_ADDR, s_rev);
    return true;
}

bool orient_port_power(bool on)
{
    if (!wr(REG_CTRL7, on ? CTRL7_ACCEL : 0)) return false;
    s_on = on;
    return true;
}

bool orient_port_read(int16_t a[3])
{
    uint8_t b[6];
    if (!s_on || !rd(REG_AX_L, b, sizeof b)) return false;
    for (int i = 0; i < 3; i++) a[i] = (int16_t)(b[2 * i] | (b[2 * i + 1] << 8));
    return true;
}

void orient_port_describe(dbg_json_t *w)
{
    char id[8];
    snprintf(id, sizeof id, "%02x/%02x", s_who, s_rev);
    dbg_json_str(w, "chip", "qmi8658");
    dbg_json_str(w, "id", id);
}

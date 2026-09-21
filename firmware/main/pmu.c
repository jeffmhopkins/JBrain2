#include "pmu.h"

#include <stdio.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "i2c_bus.h"

static const char *TAG = "pmu";

#define PMU_ADDR 0x34

/* AXP2101: two status bytes, the chip id, and the three enable registers that decide which
   rails are actually up. If a rail feeding the AMOLED is being cut, it is cut here. */
static const uint8_t REGS[] = {0x00, 0x01, 0x03, 0x80, 0x90, 0x91};
#define NREGS (sizeof(REGS) / sizeof(REGS[0]))

#define RING 12
#define RING_MAGIC 0xA2101FEEu

/* NOINIT, not DATA: the bootloader must not zero it, or a restart erases exactly the evidence
   this exists to carry. The magic distinguishes "survived a restart" from "powered up with
   whatever was in the SRAM". */
static RTC_NOINIT_ATTR uint32_t s_magic;
static RTC_NOINIT_ATTR uint32_t s_count;
static RTC_NOINIT_ATTR uint8_t s_ring[RING][NREGS];

static i2c_master_dev_handle_t s_dev;

bool pmu_start(void)
{
    i2c_master_bus_handle_t bus = i2c_bus_get();
    if (bus == NULL) return false;
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = PMU_ADDR,
        .scl_speed_hz = 400000,
    };
    if (i2c_master_bus_add_device(bus, &cfg, &s_dev) != ESP_OK) {
        ESP_LOGE(TAG, "no device at 0x%02x", PMU_ADDR);
        s_dev = NULL;
        return false;
    }
    return true;
}

static bool read_regs(uint8_t *out)
{
    if (s_dev == NULL) return false;
    for (unsigned i = 0; i < NREGS; i++) {
        if (i2c_master_transmit_receive(s_dev, &REGS[i], 1, &out[i], 1, 50) != ESP_OK) {
            return false;
        }
    }
    return true;
}

int pmu_history_hex(char (*out)[PMU_SAMPLE_CHARS], int max)
{
    if (s_magic != RING_MAGIC) return 0;
    const uint32_t have = s_count < RING ? s_count : RING;
    int n = 0;
    for (uint32_t i = 0; i < have && n < max; i++) {
        const uint32_t idx = (s_count - have + i) % RING;
        const uint8_t *r = s_ring[idx];
        snprintf(out[n], PMU_SAMPLE_CHARS, "%02x %02x %02x %02x %02x %02x", r[0], r[1], r[2],
                 r[3], r[4], r[5]);
        n++;
    }
    return n;
}

void pmu_report_history(void)
{
    if (s_magic != RING_MAGIC) {
        /* A cold start. Nothing survived, and saying so is the difference between "the PMU
           was fine" and "we were not looking". */
        ESP_LOGI(TAG, "no history (cold boot)");
    } else {
        const uint32_t have = s_count < RING ? s_count : RING;
        ESP_LOGI(TAG, "history: %u sample(s) from before the restart, oldest first",
                 (unsigned)have);
        for (uint32_t i = 0; i < have; i++) {
            const uint32_t idx = (s_count - have + i) % RING;
            const uint8_t *r = s_ring[idx];
            ESP_LOGI(TAG,
                     "  st0=0x%02x st1=0x%02x id=0x%02x dcdc_en=0x%02x ldo_en0=0x%02x "
                     "ldo_en1=0x%02x",
                     r[0], r[1], r[2], r[3], r[4], r[5]);
        }
    }
    s_magic = RING_MAGIC;
    s_count = 0;
    memset(s_ring, 0, sizeof(s_ring));
}

void pmu_sample(void)
{
    uint8_t buf[NREGS];
    if (!read_regs(buf)) {
        ESP_LOGW(TAG, "sample failed");
        return;
    }
    memcpy(s_ring[s_count % RING], buf, NREGS);
    s_count++;
}

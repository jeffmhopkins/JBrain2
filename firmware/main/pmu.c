#include "pmu.h"

#include <stdio.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "i2c_bus.h"

static const char *TAG = "pmu";

#define PMU_ADDR 0x34
#define EXP_ADDR 0x20

/* AXP2101: two status bytes, the chip id, and the three enable registers that decide which
   rails are actually up. If a rail feeding the AMOLED is being cut, it is cut here. */
static const uint8_t AXP_REGS[] = {0x00, 0x01, 0x03, 0x80, 0x90, 0x91};
/* TCA9554: input port, output port, configuration. Together they say what each pin is driven
   to and which are outputs at all — and the BSP brings the panel's reset and enable lines out
   here, which is why a chip nothing has ever written to can still be holding the screen off. */
static const uint8_t EXP_REGS[] = {0x00, 0x01, 0x03};

#define NAXP (sizeof(AXP_REGS) / sizeof(AXP_REGS[0]))
#define NEXP (sizeof(EXP_REGS) / sizeof(EXP_REGS[0]))
#define NREGS (NAXP + NEXP)

#define RING 12
#define RING_MAGIC 0xA2101FEEu

/* NOINIT, not DATA: the bootloader must not zero it, or a restart erases exactly the evidence
   this exists to carry. The magic distinguishes "survived a restart" from "powered up with
   whatever was in the SRAM" — see the header on why writing it is `pmu_report_history`'s job
   and why nothing else may be the only writer. */
static RTC_NOINIT_ATTR uint32_t s_magic;
static RTC_NOINIT_ATTR uint32_t s_count;
static RTC_NOINIT_ATTR uint8_t s_ring[RING][NREGS];

/* Ordinary RAM, zeroed by every boot: what the ring held when this boot began. Telemetry reads
   this rather than the ring, so the render task can start sampling immediately without racing
   the reader or diluting the history. */
static uint8_t s_snap[RING][NREGS];
static int s_snap_n;

static i2c_master_dev_handle_t s_pmu;
static i2c_master_dev_handle_t s_exp;

static i2c_master_dev_handle_t add_dev(i2c_master_bus_handle_t bus, uint8_t addr)
{
    const i2c_device_config_t cfg = {
        .dev_addr_length = I2C_ADDR_BIT_LEN_7,
        .device_address = addr,
        .scl_speed_hz = 400000,
    };
    i2c_master_dev_handle_t dev = NULL;
    if (i2c_master_bus_add_device(bus, &cfg, &dev) != ESP_OK) return NULL;
    return dev;
}

bool pmu_start(void)
{
    i2c_master_bus_handle_t bus = i2c_bus_get();
    if (bus == NULL) return false;

    s_exp = add_dev(bus, EXP_ADDR);
    if (s_exp == NULL) ESP_LOGW(TAG, "no expander at 0x%02x — its bytes will read ff", EXP_ADDR);

    s_pmu = add_dev(bus, PMU_ADDR);
    if (s_pmu == NULL) {
        ESP_LOGE(TAG, "no device at 0x%02x", PMU_ADDR);
        return false;
    }
    return true;
}

/* Missing device or failed read leaves 0xff rather than failing the whole sample: the two
   chips answer independently, and losing the AXP2101's rails because an expander register
   timed out would throw away the half that has been readable all along. */
static void read_one(i2c_master_dev_handle_t dev, const uint8_t *regs, unsigned n, uint8_t *out)
{
    for (unsigned i = 0; i < n; i++) {
        out[i] = 0xff;
        if (dev == NULL) continue;
        uint8_t v = 0;
        if (i2c_master_transmit_receive(dev, &regs[i], 1, &v, 1, 50) == ESP_OK) out[i] = v;
    }
}

int pmu_history_hex(char (*out)[PMU_SAMPLE_CHARS], int max)
{
    int n = 0;
    for (int i = 0; i < s_snap_n && n < max; i++) {
        const uint8_t *r = s_snap[i];
        snprintf(out[n], PMU_SAMPLE_CHARS, "%02x %02x %02x %02x %02x %02x %02x %02x %02x", r[0],
                 r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]);
        n++;
    }
    return n;
}

void pmu_report_history(void)
{
    s_snap_n = 0;
    if (s_magic == RING_MAGIC) {
        const uint32_t have = s_count < RING ? s_count : RING;
        for (uint32_t i = 0; i < have; i++) {
            memcpy(s_snap[i], s_ring[(s_count - have + i) % RING], NREGS);
        }
        s_snap_n = (int)have;
        ESP_LOGI(TAG, "history: %d sample(s) from before the restart, oldest first", s_snap_n);
        for (int i = 0; i < s_snap_n; i++) {
            const uint8_t *r = s_snap[i];
            ESP_LOGI(TAG,
                     "  st0=0x%02x st1=0x%02x id=0x%02x dcdc_en=0x%02x ldo_en0=0x%02x "
                     "ldo_en1=0x%02x | exp_in=0x%02x exp_out=0x%02x exp_cfg=0x%02x",
                     r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8]);
        }
    } else {
        /* A cold start, or a boot after the arming bug. Nothing survived, and saying so is the
           difference between "the PMU was fine" and "we were not looking". */
        ESP_LOGI(TAG, "no history (cold boot)");
    }

    /* ARM IT, ALWAYS, AND ONLY HERE. The copy above is already taken, so this cannot destroy
       anything; skipping it on a cold boot is what made the ring permanently unreadable. */
    s_magic = RING_MAGIC;
    s_count = 0;
}

void pmu_history_clear(void)
{
    s_snap_n = 0;
}

void pmu_sample(void)
{
    uint8_t buf[NREGS];
    read_one(s_pmu, AXP_REGS, NAXP, buf);
    read_one(s_exp, EXP_REGS, NEXP, buf + NAXP);
    memcpy(s_ring[s_count % RING], buf, NREGS);
    s_count++;
}

#include "pmu.h"

#include <stdio.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
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

static bool write_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t val)
{
    const uint8_t buf[2] = {reg, val};
    return dev != NULL && i2c_master_transmit(dev, buf, 2, 50) == ESP_OK;
}

static bool read_reg(i2c_master_dev_handle_t dev, uint8_t reg, uint8_t *val)
{
    return dev != NULL && i2c_master_transmit_receive(dev, &reg, 1, val, 1, 50) == ESP_OK;
}

/* THE HARDWARE RESET THIS FIRMWARE HAS NEVER ISSUED, AND THE BLACK SCREEN IT EXPLAINS.
 *
 * `display.c` brings the panel up with `.reset_gpio_num = GPIO_NUM_NC` and a comment saying
 * the line is not brought out, so the CO5300 has only ever had a SOFTWARE reset — a command,
 * down the same QSPI bus as everything else. That works from a cold boot, where the
 * controller resets itself when the rails come up, and it cannot work at all in the one case
 * that matters: `esp_restart()` leaves the CO5300 powered and holding its state, so a
 * controller stopped mid memory-write is still waiting for pixels. It swallows the next
 * boot's init sequence as picture data, including the software reset, and the panel never
 * lights. Nothing sent over that bus can reach it, which is why only TIME or luck has
 * recovered it — and why the reboot gesture, which the owner has had to perform after every
 * single update, works on the second try and not the first.
 *
 * The line does exist. `pmu.c` has said so since it was written ("the BSP brings the panel's
 * reset and enable lines out here"), and Waveshare's own V2 sample code says which pins:
 * every display example for this board drives expander pins 0, 1 and 2 low together, waits
 * 20 ms, and releases them BEFORE touching the touch controller or the display. This is that
 * sequence. The pins were not guessed — guessing them is how you cut a rail or hold the touch
 * controller down, which is why this waited for the vendor's code rather than a probe.
 *
 * Measured on the box 2026-09-22, from the PMU ring: the expander's config register reads
 * 0xff, so all eight pins are INPUTS and nothing is driven. External pull-ups hold the three
 * reset lines deasserted, which is exactly why a cold boot works and why no reboot has ever
 * been able to assert them. */
#define PANEL_RESET_PINS 0x07 /* expander P0, P1, P2 — Waveshare's V2 samples, verbatim */
#define RESET_LOW_MS     20   /* the vendor's own pulse width */
#define RESET_SETTLE_MS  120  /* before the init sequence goes out; boot is not time-critical */

bool pmu_reset_panel(void)
{
    if (s_exp == NULL) return false;

    uint8_t out = 0xff, cfg = 0xff;
    if (!read_reg(s_exp, 0x01, &out) || !read_reg(s_exp, 0x03, &cfg)) {
        ESP_LOGW(TAG, "expander would not answer — panel reset skipped");
        return false;
    }

    /* READ-MODIFY-WRITE, and only these three bits. The other five are not ours: two of them
       read low on this board and the vendor drives a sixth for the SD card in one sample. A
       blanket write here is how a diagnostic becomes an outage. */
    const bool ok =
    /* Deassert BEFORE switching to outputs, so becoming an output cannot glitch the lines
       low — the pin drives whatever the output register already held. */
    write_reg(s_exp, 0x01, (uint8_t)(out | PANEL_RESET_PINS)) &&
    write_reg(s_exp, 0x03, (uint8_t)(cfg & (uint8_t)~PANEL_RESET_PINS)) &&
    write_reg(s_exp, 0x01, (uint8_t)(out & (uint8_t)~PANEL_RESET_PINS));
    if (!ok) {
        ESP_LOGW(TAG, "expander write failed — panel reset incomplete");
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(RESET_LOW_MS));
    if (!write_reg(s_exp, 0x01, (uint8_t)(out | PANEL_RESET_PINS))) {
        ESP_LOGE(TAG, "the panel reset went low and would not come back up");
        return false;
    }
    vTaskDelay(pdMS_TO_TICKS(RESET_SETTLE_MS));
    ESP_LOGI(TAG, "panel hardware reset: expander pins 0-2 pulsed low %d ms", RESET_LOW_MS);
    return true;
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

/* The AXP2101 driver's device-side controls, beside the app-facing svc_power.h. This service is
 * the ONE owner of the I2C bus (SDA 8, SCL 7): every other chip on it is reached through here
 * when its driver lands, so no two tasks ever race a read-modify-write of a PMIC register. */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "esp_err.h"

typedef void (*svc_power_pek_fn)(void);

/* Opens the bus and checks the chip id. `configure` false is the boot after a fault: the PMIC is
 * only read, never written, exactly as the 0.0.3 probe did. With `configure`, the battery ADC,
 * battery detection, conservative charge limits and the power key's IRQ latches are set, each
 * write read back; `on_pek` is then called (from this service's task) for every PWR press the
 * PMIC latched. Nothing that is on is turned off. */
esp_err_t svc_power_start(bool configure, svc_power_pek_fn on_pek);

/* The panel's reset: its RESET pin is pulled up to ALDO3 (HARDWARE.md §1), so ALDO3 off for
 * 100 ms, then on, then 100 ms to settle. Blocks ~200 ms. */
esp_err_t svc_power_lcd_reset(void);

/* The speaker amplifier's enable: the NS4150B's CTRL is pulled up to ALDO2 (HARDWARE.md §1), on
 * from power-up. svc_audio switches it on for a reply and off after; nothing else touches it.
 * ESP_ERR_INVALID_STATE on a fault boot, when the PMIC is never written. */
esp_err_t svc_power_amp(bool on);

/* The bus's other chips borrow it through these two calls, never through a handle of their own:
 * the lock that keeps a PMIC read-modify-write whole lives here, and a borrower that held the
 * bus handle could forget to take it. A transfer holds the lock for one transaction, bounded by
 * its timeout (capped at 50 ms), so a chip that stops answering costs the PMIC's poll that much
 * and no more; its errors are the borrower's to count, never this service's `i2c_err`. */
typedef struct i2c_master_dev_t *svc_power_i2c_t;

/* After svc_power_start succeeded; ESP_ERR_INVALID_STATE before. */
esp_err_t svc_power_i2c_attach(uint8_t addr, uint32_t scl_hz, svc_power_i2c_t *out);

/* Writes `tx`, then, when `rx_len` is non-zero, reads `rx` after a repeated start. */
esp_err_t svc_power_i2c_xfer(svc_power_i2c_t dev, const uint8_t *tx, size_t tx_len, uint8_t *rx,
                             size_t rx_len, int timeout_ms);

/* `pmu=… bat=… vbus=… chg=… pek=… axp=reg:val,…` — telemetry-safe tokens. */
void svc_power_note(char *out, size_t len);

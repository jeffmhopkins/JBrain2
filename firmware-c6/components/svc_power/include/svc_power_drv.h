/* The AXP2101 driver's device-side controls, beside the app-facing svc_power.h. This service is
 * the ONE owner of the I2C bus (SDA 8, SCL 7): every other chip on it is reached through here
 * when its driver lands, so no two tasks ever race a read-modify-write of a PMIC register. */
#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

typedef void (*svc_power_pek_fn)(void);

/* Opens the bus and checks the chip id. `configure` false is the boot after a fault: the PMIC is
 * only read, never written, exactly as the 0.0.3 probe did. With `configure`, the battery ADC,
 * battery detection, conservative charge limits and the power key's IRQ latches are set, each
 * write read back; `on_pek` is then called (from this service's task) for every PWR press the
 * PMIC latched. Nothing that is on is turned off. */
esp_err_t svc_power_start(bool configure, svc_power_pek_fn on_pek);

/* The panel's reset: its RESET pin is pulled up to ALDO3 (HARDWARE.md §1), so ALDO3 off for
 * 100 ms, then on, then 100 ms to settle. Blocks ~200 ms. The only rail this service switches. */
esp_err_t svc_power_lcd_reset(void);

/* `pmu=… bat=… vbus=… chg=… pek=… axp=reg:val,…` — telemetry-safe tokens. */
void svc_power_note(char *out, size_t len);

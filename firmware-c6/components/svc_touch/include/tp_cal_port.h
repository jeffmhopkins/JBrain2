/* Touch calibration's port (0.0.18): the device keeps the fit in NVS (namespace `jpanel`, key
 * `tp_cal`) and hands it to the touch task (esp/tp_cal_port_esp.c); the sim and the host keep it
 * in memory (tp_cal_port_fake.c). The service itself is svc_calib.c, the same everywhere. */
#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "tp_logic.h"

/* False when nothing (or a blob of another shape) is kept. */
bool tp_cal_port_load(tp_cal_t *cal, uint32_t *date);
/* NULL erases it. */
bool tp_cal_port_save(const tp_cal_t *cal, uint32_t date);
/* What the touch task maps through from now on; `on` false is the measured mapping alone. */
void tp_cal_port_apply(const tp_cal_t *cal, bool on);

/* The sim's and the host's: what the touch task was last given, and a first boot. */
void tp_cal_port_fake_applied(tp_cal_t *cal, bool *on);
void tp_cal_port_fake_wipe(void);
void tp_cal_port_fake_refuse_save(bool refuse);

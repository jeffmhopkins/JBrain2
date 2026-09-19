#pragma once

#include <stdbool.h>

#include "cfg.h"
#include "esp_err.h"

/* Brings up the station interface and blocks until it has an IP or `timeout_ms` passes.
   The ESP32-S3 has no 5 GHz radio, so a 5 GHz-only or hard band-steering network simply
   will not appear — the failure is logged as "not found" rather than as a wrong password. */
esp_err_t net_connect(const cfg_t *cfg, int timeout_ms);

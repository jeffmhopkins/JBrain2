#pragma once

#include "cfg.h"
#include "esp_err.h"

/* Brings up the station interface and blocks until it has an IP or `timeout_ms` passes. The
   C6's Wi-Fi 6 radio is 2.4 GHz only, so a 5 GHz-only network logs as "not found" (reason 201)
   rather than as a wrong password. */
esp_err_t net_connect(const cfg_t *cfg, int timeout_ms);

/* Try again on a radio that is already up. `net_connect` does one-time init that returns
   ESP_ERR_INVALID_STATE a second time, so calling it again is not a retry. */
esp_err_t net_retry(int timeout_ms);

/* The last `wifi_err_reason_t` and how many times the link has dropped since boot. Monotonic:
   201 (out of range), 15 (wrong password) and 8 (kicked) are three different repairs, and a
   panel that reconnected before its next report would otherwise look like it never dropped. */
void net_link_faults(int *last_reason, int *drops);

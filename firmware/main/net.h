#pragma once

#include <stdbool.h>

#include "cfg.h"
#include "esp_err.h"

/* Brings up the station interface and blocks until it has an IP or `timeout_ms` passes.
   The ESP32-S3 has no 5 GHz radio, so a 5 GHz-only or hard band-steering network simply
   will not appear — the failure is logged as "not found" rather than as a wrong password. */
esp_err_t net_connect(const cfg_t *cfg, int timeout_ms);

/* Try again on a radio that is already up.

   `net_connect` performs one-time initialisation — `esp_netif_init`, the default event loop,
   `esp_wifi_init`, `esp_wifi_start` — every one of which is wrapped in `ESP_ERROR_CHECK` and
   returns `ESP_ERR_INVALID_STATE` the second time. Calling it twice therefore does not fail,
   it ABORTS, which on a panel in a bedroom is a reboot loop. This is the retry path. */
esp_err_t net_retry(int timeout_ms);

/* The last Wi-Fi disconnect reason (`wifi_err_reason_t`) and how many times the link has
   dropped since boot. 0/0 means it has never dropped.
 *
 * A panel that loses the network and comes back before its next report currently looks
 * perfectly healthy, and the reason code — the difference between "out of range", "wrong
 * password" and "the router kicked it", which are three different fixes — was going nowhere
 * but a serial console. Monotonic, because the retry counter beside them is reset on every
 * successful connect and therefore cannot answer this. */
void net_link_faults(int *last_reason, int *drops);

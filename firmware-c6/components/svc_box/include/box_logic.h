/* The Update now button's rule, shared by the device's box client and the in-memory one so the
 * sim draws exactly what the glass will. Pure C, host-tested. */
#pragma once

#include <stdbool.h>

#include "svc_box.h"
#include "svc_net.h"

/* Offline there is no box to ask, and a check or download already running is the answer. */
bool box_may_update(net_state_t net, fw_phase_t phase);
bool box_busy(fw_phase_t phase);

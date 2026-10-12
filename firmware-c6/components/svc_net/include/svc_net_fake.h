/* In-memory Wi-Fi: the sim's and the host suite's (the device's is esp/svc_net_esp.c). Connect
 * completes when the sim says so, which is what lets "Connecting" be rendered at all. */
#pragma once

#include "svc_net.h"

void svc_net_init(void);
void svc_net_fake_set(net_state_t state);

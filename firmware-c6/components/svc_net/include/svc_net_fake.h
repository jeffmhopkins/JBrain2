/* In-memory Wi-Fi until main/net.c is wrapped as this service (C2). Connect completes when the
 * sim says so, which is what lets "Connecting" be rendered at all. */
#pragma once

#include "svc_net.h"

void svc_net_init(void);
void svc_net_fake_set(net_state_t state);

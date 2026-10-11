/* In-memory supply state until the AXP2101 driver (C2). The sim switches between the mock's
 * four power cases. */
#pragma once

#include "svc_power.h"

void svc_power_init(void);
void svc_power_fake_set(const power_info_t *info);
